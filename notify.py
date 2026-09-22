"""
Pushover push notifications for highly-relevant NEW jobs.

Called by scrape_jobs.save_jobs_output() with each run's new_jobs. It is a
no-op unless BOTH PUSHOVER_TOKEN and PUSHOVER_USER env vars are set (so local
runs and forks without Pushover are unaffected). It dedupes against
notified.json so the same role is never pushed twice — across sources or runs.

"Highly relevant" = a posting that either touches a priority topic configured
in config.json or meets the configured deterministic score in
scoring_profile.json. Personalization is required: this module never substitutes
the example profile or an unrelated built-in taxonomy.

Set up (GitHub → Settings → Secrets and variables → Actions):
  PUSHOVER_TOKEN   your Pushover application/API token
  PUSHOVER_USER    your Pushover user key
Optional: NOTIFY_MIN_FIT overrides config.json → notify.min_fit.
"""

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(SCRIPT_DIR, "output")
NOTIFIED_PATH = os.path.join(OUTPUT_DIR, "notified.json")
ALL_JOBS_PATH = os.path.join(OUTPUT_DIR, "all_jobs.json")
SCORES_PATH = os.path.join(OUTPUT_DIR, "scores.json")
# Fork-owner personalization. Example files are documentation only and must
# never be used for live scoring or notification decisions.
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.json")
SCORING_PROFILE_PATH = os.path.join(SCRIPT_DIR, "scoring_profile.json")
PUSHOVER_URL = "https://api.pushover.net/1/messages.json"
# Derive dashboard URL from GITHUB_REPOSITORY (owner/repo) so forks get their own URL.
_gh_repo = os.environ.get("GITHUB_REPOSITORY", "")
DASHBOARD_URL = (
    f"https://{_gh_repo.split('/')[0]}.github.io/{_gh_repo.split('/')[-1]}/triage.html"
    if "/" in _gh_repo
    else "triage.html"
)
MAX_PUSHES_PER_RUN = 8     # cap individual pings; the rest get one summary
NOTIFIED_KEEP = 600        # remember this many recent jobs to avoid repeats

DEFAULT_SCORING_SETTINGS = {
    "title_multiplier": 3,
    "body_multiplier": 1,
    "score_multiplier": 1.6,
    "generic_cap": 35,
    "standout_threshold": 60,
}

_SCORING_PROFILE: dict | None = None
_CONFIG: dict | None = None


def _min_fit() -> int:
    try:
        configured = _load_config().get("notify", {}).get("min_fit", 85)
        return int(os.environ.get("NOTIFY_MIN_FIT", configured))
    except (TypeError, ValueError):
        return 85


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _load_config() -> dict:
    global _CONFIG
    if _CONFIG is not None:
        return _CONFIG
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            loaded = json.load(f)
        if not isinstance(loaded, dict) or not loaded.get("profile"):
            raise ValueError("missing required profile section")
        _CONFIG = loaded
    except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError) as e:
        print(
            "  ⚠️  CONFIGURATION REQUIRED: config.json is missing or invalid; "
            f"personalized notifications are disabled ({e})."
        )
        _CONFIG = {}
    return _CONFIG


def _repair_json_regex_escapes(text: str) -> str:
    r"""LLMs often emit regex JSON with single backslashes, e.g. "\bword\b"
    or "\d+". JSON either rejects those escapes or turns "\b" into a backspace.
    This keeps already-valid JSON escapes intact while doubling regex escapes."""
    out = []
    in_string = False
    i = 0
    regex_escapes = set("AbBdDsSwWZzGQE.?+*^$()[]{}|-")
    while i < len(text):
        ch = text[i]
        if ch == '"':
            # Count preceding backslashes to decide whether this quote is escaped.
            bs = 0
            j = i - 1
            while j >= 0 and text[j] == "\\":
                bs += 1
                j -= 1
            if bs % 2 == 0:
                in_string = not in_string
            out.append(ch)
            i += 1
            continue
        if in_string and ch == "\\" and i + 1 < len(text):
            nxt = text[i + 1]
            if nxt in {'"', "\\", "/", "u", "n", "r", "t"}:
                out.append(ch)
                out.append(nxt)
            elif nxt in regex_escapes:
                out.append("\\\\")
                out.append(nxt)
            else:
                out.append("\\\\")
                out.append(nxt)
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _compile_patterns(items: list) -> list:
    out = []
    for item in items or []:
        pattern = item.get("pattern") if isinstance(item, dict) else item
        if not pattern:
            continue
        try:
            out.append(re.compile(str(pattern), re.I))
        except re.error as e:
            print(f"  Warning: scoring_profile.json ignored invalid regex {pattern!r}: {e}")
    return out


def _compile_weighted_patterns(items: list, value_key: str) -> list:
    out = []
    for item in items or []:
        if isinstance(item, dict):
            pattern = item.get("pattern")
            value = item.get(value_key)
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            pattern, value = item[0], item[1]
        else:
            continue
        if not pattern:
            continue
        try:
            value = float(value)
            out.append((re.compile(str(pattern), re.I), value))
        except (TypeError, ValueError, re.error) as e:
            print(f"  Warning: scoring_profile.json ignored invalid scoring rule {pattern!r}: {e}")
    return out


def _scoring_profile() -> dict:
    global _SCORING_PROFILE
    if _SCORING_PROFILE is not None:
        return _SCORING_PROFILE

    profile = {
        "configured": False,
        "fit_terms": [],
        "signature_terms": [],
        "poor_fit_terms": [],
        "settings": dict(DEFAULT_SCORING_SETTINGS),
    }
    try:
        with open(SCORING_PROFILE_PATH, encoding="utf-8") as f:
            raw_text = f.read()
    except (FileNotFoundError, OSError) as e:
        print(
            "  ⚠️  CONFIGURATION REQUIRED: scoring_profile.json is missing; "
            f"deterministic scoring and fit notifications are disabled ({e})."
        )
        _SCORING_PROFILE = profile
        return profile
    try:
        raw = json.loads(_repair_json_regex_escapes(raw_text))
    except json.JSONDecodeError as e:
        print(
            "  ⚠️  CONFIGURATION REQUIRED: scoring_profile.json is invalid; "
            f"deterministic scoring and fit notifications are disabled ({e})."
        )
        _SCORING_PROFILE = profile
        return profile

    # A custom profile replaces the built-in example terms. Missing sections are
    # treated as empty so example-domain scoring never leaks into another field.
    profile["fit_terms"] = _compile_weighted_patterns(raw.get("fit_terms", []), "weight")
    profile["signature_terms"] = _compile_patterns(raw.get("signature_terms", []))
    profile["poor_fit_terms"] = _compile_weighted_patterns(raw.get("poor_fit_terms", []), "penalty")

    if not profile["fit_terms"] or not profile["signature_terms"]:
        print(
            "  ⚠️  CONFIGURATION REQUIRED: scoring_profile.json needs non-empty "
            "fit_terms and signature_terms; deterministic scoring and fit "
            "notifications are disabled."
        )
        _SCORING_PROFILE = profile
        return profile

    settings = raw.get("settings", {})
    if isinstance(settings, dict):
        for key in DEFAULT_SCORING_SETTINGS:
            try:
                if key in settings:
                    profile["settings"][key] = float(settings[key])
            except (TypeError, ValueError):
                print(f"  Warning: scoring_profile.json ignored invalid setting {key!r}")

    profile["configured"] = True
    _SCORING_PROFILE = profile
    print(
        "scoring_profile.json loaded: "
        f"{len(profile['fit_terms'])} fit, "
        f"{len(profile['signature_terms'])} signature, "
        f"{len(profile['poor_fit_terms'])} penalty rule(s)"
    )
    return profile


def _weekly_digest_days() -> int:
    try:
        raw = os.environ.get("WEEKLY_DIGEST_DAYS") or _load_config().get(
            "notify", {}).get("weekly_digest", {}).get("days", 7)
        return max(1, int(raw))
    except ValueError:
        return 7


def _weekly_digest_enabled(force: bool = False) -> bool:
    if force:
        return True
    if _truthy(os.environ.get("WEEKLY_DIGEST_PUSHOVER")):
        return True
    return bool(_load_config().get("notify", {}).get("weekly_digest", {}).get("enabled"))


def _stars(text: str) -> list:
    configured = _load_config().get("priority_topics", {}).get("terms", [])
    stars = []
    for item in configured:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        name, pattern = item[0], item[1]
        try:
            if re.search(str(pattern), text, re.I):
                stars.append(str(name))
        except re.error as e:
            print(f"  ⚠️  config.json ignored invalid priority-topic regex {pattern!r}: {e}")
    return stars


def _fit(title: str, body: str) -> int:
    profile = _scoring_profile()
    if not profile["configured"]:
        return 0
    settings = profile["settings"]
    text = f"{title} {body}"
    has_signature = any(rx.search(text) for rx in profile["signature_terms"])
    score = 0
    for rx, w in profile["fit_terms"]:
        if rx.search(title):
            score += w * settings["title_multiplier"]
        elif rx.search(body):
            score += w * settings["body_multiplier"]
    for rx, penalty in profile["poor_fit_terms"]:
        if rx.search(text):
            score -= penalty
    if not has_signature:
        score = min(score, settings["generic_cap"])
    return max(0, min(100, round(score * settings["score_multiplier"])))


def relevance(job: dict) -> tuple[bool, list, int]:
    title = job.get("title", "") or ""
    body = f"{job.get('company', '')} {job.get('description', '')}"
    stars = _stars(f"{title} {body}")
    fit = _fit(title, body)
    configured = bool(_load_config()) and _scoring_profile()["configured"]
    return (configured and (bool(stars) or fit >= _min_fit())), stars, fit


def _personalization_ready() -> bool:
    """Load required owner files so missing setup is always visible."""
    return bool(_load_config()) and bool(_scoring_profile()["configured"])


def _identity(job: dict) -> str:
    co = re.sub(r'[^a-z0-9]', '', (job.get("company", "") or "").lower())
    ti = re.sub(r'[^a-z0-9]', '', (job.get("title", "") or "").lower())
    return f"{co}|{ti}"


def _load_notified() -> dict:
    try:
        with open(NOTIFIED_PATH) as f:
            data = json.load(f)
            data.setdefault("ids", [])
            return data
    except (FileNotFoundError, json.JSONDecodeError):
        return {"ids": []}


def _save_notified(data: dict):
    data["ids"] = data["ids"][-NOTIFIED_KEEP:]
    with open(NOTIFIED_PATH, "w") as f:
        json.dump(data, f)


def _load_scores() -> dict:
    try:
        with open(SCORES_PATH, encoding="utf-8") as f:
            return json.load(f).get("scores", {})
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _parse_first_seen(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _salary_numbers(salary: str) -> list[float]:
    core = re.sub(r"\([^)]*\)", "", salary or "")
    values: list[float] = []
    for raw, suffix in re.findall(r"(?:[$A-Z]*\$)?\s*([0-9][0-9,]*(?:\.\d+)?)\s*([kK]?)", core):
        try:
            val = float(raw.replace(",", ""))
        except ValueError:
            continue
        if suffix.lower() == "k":
            val *= 1000
        values.append(val)
    return values


def salary_annual_range(salary: str) -> tuple[float, float] | None:
    values = _salary_numbers(salary)
    if not values:
        return None
    text = salary.lower()
    lo, hi = min(values), max(values)
    if re.search(r"\b(hour|hourly|hr)\b|/hr", text):
        factor = 2080
    elif re.search(r"\b(month|monthly|mo)\b|/mo", text):
        factor = 12
    elif re.search(r"\b(week|weekly|wk)\b|/wk", text):
        factor = 52
    elif re.search(r"\b(day|daily)\b|/day", text):
        factor = 260
    else:
        factor = 1
        if hi < 1000:
            # Some sources label hourly wages as "/yr"; avoid bucketing $34 as
            # an annual salary.
            factor = 2080
    return lo * factor, hi * factor


def salary_band(job: dict) -> str:
    annual = salary_annual_range(job.get("salary", ""))
    if not annual:
        return "No listed salary"
    hi = annual[1]
    if hi >= 200000:
        return "$200k+"
    if hi >= 150000:
        return "$150k-$199k"
    if hi >= 100000:
        return "$100k-$149k"
    if hi >= 75000:
        return "$75k-$99k"
    return "<$75k"


def _score_job(job: dict, scores: dict) -> tuple[int, str]:
    verdict = scores.get(job.get("url", ""), {})
    score = verdict.get("score")
    if isinstance(score, int) and verdict.get("verdict") != "error":
        return max(0, min(100, score)), "agent"
    _, _, fit = relevance(job)
    return fit, "fit"


def _recent_jobs(days: int) -> list[dict]:
    try:
        with open(ALL_JOBS_PATH, encoding="utf-8") as f:
            jobs = list(json.load(f).get("jobs", []))
    except (FileNotFoundError, json.JSONDecodeError):
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    recent = []
    for job in jobs:
        seen = _parse_first_seen(job.get("first_seen", ""))
        if seen and seen >= cutoff:
            recent.append(job)
    return recent


def _count_by(items: list[dict], key_func) -> list[tuple[str, int]]:
    counts: dict[str, int] = {}
    for item in items:
        key = key_func(item) or "Unknown"
        counts[key] = counts.get(key, 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0].lower()))


def _join_counts(counts: list[tuple[str, int]], limit: int = 5) -> str:
    shown = [f"{name} {count}" for name, count in counts[:limit]]
    extra = sum(count for _, count in counts[limit:])
    if extra:
        shown.append(f"other {extra}")
    return "; ".join(shown) if shown else "none"


def _norm_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def _standout_lines(jobs: list[dict], scores: dict, limit: int = 3) -> tuple[str, list[str]]:
    ranked = []
    seen_roles: set[tuple[str, str]] = set()
    for job in jobs:
        role_key = (_norm_key(job.get("company", "")), _norm_key(job.get("title", "")))
        if role_key in seen_roles:
            continue
        seen_roles.add(role_key)
        score, source = _score_job(job, scores)
        annual = salary_annual_range(job.get("salary", ""))
        salary_hi = annual[1] if annual else -1
        ranked.append((score, salary_hi, job.get("first_seen", ""), source, job))
    ranked.sort(key=lambda row: (row[0], row[1], row[2]), reverse=True)

    lines = []
    for score, _, _, source, job in ranked[:limit]:
        where = job.get("location") or "location n/a"
        salary = f" - {job['salary']}" if job.get("salary") else ""
        lines.append(
            f"- {score}/100 {source}: {job.get('title', 'Untitled')} @ "
            f"{job.get('company', 'Unknown')} ({where}){salary}"
        )
    profile = _scoring_profile()
    if not profile["configured"]:
        return "Scoring unavailable (personalization required):", []
    threshold = profile["settings"]["standout_threshold"]
    heading = "Standouts:" if ranked and ranked[0][0] >= threshold else "Closest matches (no high-confidence standouts):"
    return heading, lines


def build_weekly_digest(days: int | None = None) -> tuple[str, str, str]:
    days = days or _weekly_digest_days()
    jobs = _recent_jobs(days)
    scores = _load_scores()
    org_count = len({(j.get("company") or "").strip().lower() for j in jobs if j.get("company")})

    title = f"Weekly job digest: {len(jobs)} new role(s)"
    if not jobs:
        return title, f"No new roles first seen in the last {days} day(s).", DASHBOARD_URL

    salary_counts = _count_by(jobs, salary_band)
    org_counts = _count_by(jobs, lambda j: j.get("company", "").strip())
    standout_heading, standout_lines = _standout_lines(jobs, scores, limit=3)
    lines = [
        f"Last {days}d: {len(jobs)} roles across {org_count} organization(s).",
        f"By salary: {_join_counts(salary_counts, limit=6)}.",
        f"Top orgs: {_join_counts(org_counts, limit=5)}.",
        standout_heading,
    ]
    lines.extend(standout_lines)
    msg = "\n".join(lines)
    if len(msg) > 1000:
        short_heading, short_lines = _standout_lines(jobs, scores, limit=2)
        msg = "\n".join(lines[:3] + [short_heading] + short_lines)
    if len(msg) > 1000:
        msg = msg[:997] + "..."
    return title, msg, os.environ.get("DASHBOARD_URL") or DASHBOARD_URL


def send_pushover(token: str, user: str, *, title: str, message: str,
                  url: str = "", url_title: str = "", priority: int = 0) -> bool:
    body = {"token": token, "user": user, "title": title[:250],
            "message": message[:1024], "priority": priority}
    if url:
        body["url"] = url
        body["url_title"] = url_title or "View posting"
    data = urllib.parse.urlencode(body).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(PUSHOVER_URL, data=data), timeout=15) as r:
            return 200 <= r.status < 300
    except urllib.error.HTTPError as e:
        # Pushover returns a JSON body with the specific error (bad token/user…).
        try:
            detail = e.read().decode("utf-8", "ignore")
        except Exception:
            detail = ""
        print(f"  ⚠️  Pushover HTTP {e.code}: {detail[:300]}")
        return False
    except Exception as e:
        print(f"  ⚠️  Pushover send failed: {e}")
        return False


def notify_new_jobs(new_jobs: list, source_label: str = ""):
    """Push the highly-relevant, not-yet-notified entries of new_jobs."""
    if not _personalization_ready():
        return
    token = os.environ.get("PUSHOVER_TOKEN")
    user = os.environ.get("PUSHOVER_USER")
    if not token or not user:
        return  # notifications disabled — no creds

    notified = _load_notified()
    seen = set(notified["ids"])
    picks = []
    for job in new_jobs:
        ident = _identity(job)
        if ident in seen:
            continue
        relevant, stars, fit = relevance(job)
        if not relevant:
            continue
        seen.add(ident)
        notified["ids"].append(ident)
        picks.append((job, stars, fit))

    if not picks:
        _save_notified(notified)
        return

    # Starred first, then highest fit.
    picks.sort(key=lambda p: (-len(p[1]), -p[2]))
    sent = 0
    for job, stars, fit in picks[:MAX_PUSHES_PER_RUN]:
        tag = ("★ " + ", ".join(stars)) if stars else f"fit {fit}/100"
        msg = f"{job.get('company', '?')} — {job.get('location', '')}\n{tag}"
        if job.get("salary"):
            msg += f" · {job['salary']}"
        send_pushover(
            token, user,
            title=f"🎯 {job.get('title', 'New role')}",
            message=msg,
            url=job.get("url", ""), url_title="Open posting",
            priority=1 if stars else 0,   # priority topics ping with high priority
        )
        sent += 1

    extra = len(picks) - sent
    if extra > 0:
        send_pushover(token, user, title="🎯 More relevant roles",
                      message=f"+{extra} more relevant new role(s) — open the dashboard.")
    print(f"  📲 Pushover: notified {sent} relevant role(s)"
          + (f" (+{extra} summarized)" if extra else ""))
    _save_notified(notified)


def send_weekly_digest(*, days: int | None = None, force: bool = False,
                       dry_run: bool = False) -> bool:
    """Send the opt-in weekly digest. No LLM call is required; standouts use
    scores.json when present and the deterministic fit scorer otherwise."""
    if not _weekly_digest_enabled(force=force) and not dry_run:
        print("Weekly digest disabled; set WEEKLY_DIGEST_PUSHOVER=true to opt in.")
        return True

    title, message, url = build_weekly_digest(days=days)
    if dry_run:
        print(title)
        print(message)
        print(f"URL: {url}")
        return True

    token = os.environ.get("PUSHOVER_TOKEN")
    user = os.environ.get("PUSHOVER_USER")
    if not token or not user:
        print("Weekly digest skipped: PUSHOVER_TOKEN/PUSHOVER_USER are missing.")
        return False

    ok = send_pushover(
        token,
        user,
        title=title,
        message=message,
        url=url,
        url_title="Open dashboard",
        priority=0,
    )
    print("Weekly digest sent." if ok else "Weekly digest send failed.")
    return ok


def send_test() -> bool:
    """Send a single test push to verify the Pushover setup end-to-end.
    Returns True on success. Prints a clear diagnosis on failure."""
    token = os.environ.get("PUSHOVER_TOKEN")
    user = os.environ.get("PUSHOVER_USER")
    print(f"PUSHOVER_TOKEN: {'(set)' if token else '(MISSING)'}")
    print(f"PUSHOVER_USER:  {'(set)' if user else '(MISSING)'}")
    if not token or not user:
        print("\n❌ Both PUSHOVER_TOKEN and PUSHOVER_USER must be set.\n"
              "   • Locally:  PUSHOVER_TOKEN=… PUSHOVER_USER=… python notify.py --test\n"
              "   • On GitHub: add them as Actions secrets, then run the "
              "'Test Pushover Notification' workflow.")
        return False
    ok = send_pushover(
        token, user,
        title="🧪 Job_Scraper — test notification",
        message=("Pushover is wired up correctly. You'll get pings like this for "
                 "highly-relevant new roles matching configured priority topics "
                 "or the personalized fit-score threshold."),
        url=DASHBOARD_URL,
        url_title="Open dashboard",
        priority=0,
    )
    print("\n✅ Test notification sent — check your phone." if ok
          else "\n❌ Send failed (see the error above — usually a wrong token or user key).")
    return ok


if __name__ == "__main__":
    # `python notify.py` or `python notify.py --test` -> send a test push.
    # `python notify.py --weekly-digest` -> send the opt-in weekly brief.
    import argparse
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # let emoji print on Windows too
    except Exception:
        pass

    parser = argparse.ArgumentParser(description="Pushover notifications for Job_Scraper.")
    parser.add_argument("--test", action="store_true", help="send a test push")
    parser.add_argument("--weekly-digest", action="store_true", help="send the weekly digest")
    parser.add_argument("--days", type=int, default=None,
                        help="lookback window for --weekly-digest (default: env or 7)")
    parser.add_argument("--force", action="store_true",
                        help="bypass the weekly opt-in flag for manual dispatch")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the weekly digest without sending Pushover")
    args = parser.parse_args()

    if args.weekly_digest:
        raise SystemExit(0 if send_weekly_digest(
            days=args.days, force=args.force, dry_run=args.dry_run) else 1)
    raise SystemExit(0 if send_test() else 1)
