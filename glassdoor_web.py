"""Glassdoor job search through the public web pages.

Why this exists: python-jobspy's Glassdoor scraper stopped working in September
2026. It resolves "Toronto, ON" to a location id through an endpoint Glassdoor
removed (now a 404/400), and fetches its API token from a page that no longer
exists, so every query fails before it starts ("location not parsed", then 403).

Glassdoor's public search pages still render normally, and they embed the result
list as structured data (Next.js flight chunks), so this module reads that
instead: no API token, no location-suggest call.

Known limits, handled explicitly rather than hidden:
  * Only the first page (about 30 results) is served over plain HTTP; later pages
    need the token-protected API. A search that reports more results than it
    returned is retried with a narrower recency window, and anything still
    truncated is counted in ``stats["truncated"]`` so a coverage gap is visible.
  * Listings carry a snippet, not the full description.
  * Location is a city name only ("Mississauga"); Glassdoor gives no province.

The module is dependency-light and takes the repo's filters as arguments so it
can be tested without importing the (large) scraper.
"""

from __future__ import annotations

import json
import math
import random
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Callable
from urllib.parse import urljoin

BASE = "https://www.glassdoor.ca"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
)
HEADERS = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "accept-language": "en-CA,en;q=0.9",
    "user-agent": USER_AGENT,
}

# Glassdoor's own location ids. A city search also returns nearby metro listings.
# "N" = country. Ids for other cities can be added here; unknown cities fall back
# to the Canada-wide search, which the caller's location filter then narrows.
LOCATIONS: dict[str, tuple[str, int, str]] = {
    "toronto": ("C", 2281069, "toronto-on"),
    "mississauga": ("C", 2280741, "mississauga-on"),
    "canada": ("N", 3, "canada"),
}
# Glassdoor's server-side "posted within N days" filter accepts only these values.
AGE_STEPS = (1, 3, 7, 14, 30)
PAGE_SIZE = 30
REQUEST_TIMEOUT = 20  # seconds per request; the TLS client otherwise waits 30s and a stalled IP would hang a run
PAY_INTERVALS = {"ANNUAL": "yearly", "MONTHLY": "monthly", "WEEKLY": "weekly", "DAILY": "daily", "HOURLY": "hourly"}


class GlassdoorBlocked(RuntimeError):
    """The site answered with a challenge / error instead of search results."""


def age_days_for(hours_old: int) -> int:
    """Smallest supported recency filter covering ``hours_old``.

    Glassdoor ages are whole days, so a 24h window is widened to 3 days: a job
    posted late yesterday must not fall between two runs. Duplicates are cheap
    (the caller dedupes); a missed job is not.
    """
    days = max(1, math.ceil(hours_old / 24))
    if days <= 1:
        days = 3
    return next((s for s in AGE_STEPS if s >= days), AGE_STEPS[-1])


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def build_url(term: str, loc_type: str, loc_id: int, loc_slug: str, from_age: int | None) -> str:
    """Glassdoor's canonical search URL: the KO/IL offsets index into the slug."""
    kw = _slug(term)
    prefix = f"{loc_slug}-"
    ko_start = len(prefix)
    path = (
        f"/Job/{prefix}{kw}-jobs-SRCH_IL.0,{len(loc_slug)}"
        f"_I{'C' if loc_type == 'C' else 'N'}{loc_id}_KO{ko_start},{ko_start + len(kw)}.htm"
    )
    return BASE + path + (f"?fromAge={from_age}" if from_age else "")


def location_key(geo: dict) -> str:
    """Map a config geography ({"location": "Mississauga, ON", ...}) to LOCATIONS."""
    city = (geo.get("location") or "").split(",")[0].strip().lower()
    return city if city in LOCATIONS else "canada"


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #

def _extract_object(text: str, marker: str) -> dict | None:
    """Return the JSON object that follows ``marker`` (brace-matched, string-aware)."""
    i = text.find(marker)
    if i < 0:
        return None
    j = text.find("{", i)
    if j < 0:
        return None
    depth, in_str, esc = 0, False, False
    for k in range(j, len(text)):
        c = text[k]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[j:k + 1])
                except ValueError:
                    return None
    return None


def parse_results(html: str) -> tuple[list[dict], int | None]:
    """Extract (listings, total_count) from a search page. Raises if there is no data."""
    chunks = re.findall(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)</script>', html, re.S)
    parts = []
    for c in chunks:
        try:
            parts.append(json.loads('"' + c + '"'))
        except ValueError:
            continue
    block = _extract_object("".join(parts), '"jobListings":')
    if block is None:
        block = _extract_object(html, '"jobListings":')
    if block is None:
        raise GlassdoorBlocked("no embedded search results in page")
    listings = block.get("jobListings") or []
    return [x for x in listings if isinstance(x, dict) and "jobview" in x], block.get("totalJobsCount")


def to_job(item: dict, *, today: datetime, format_salary: Callable) -> dict | None:
    """Normalize one embedded listing into this repo's job schema."""
    view = item.get("jobview") or {}
    header = view.get("header") or {}
    job = view.get("job") or {}
    listing_id = job.get("listingId")
    title = header.get("jobTitleText") or job.get("jobTitleText") or ""
    if not listing_id or not title or header.get("expired"):
        return None

    employer = header.get("employer") or {}
    company = header.get("employerNameFromSearch") or employer.get("name") or "Unknown"

    city = (header.get("locationName") or "").strip()
    location = "Canada" if city.lower() in ("", "canada") else f"{city}, Canada"

    age = header.get("ageInDays")
    posted = (today - timedelta(days=age)).strftime("%Y-%m-%d") if isinstance(age, int) else ""

    # Only an employer-stated range is a posting fact; Glassdoor's own estimates are not.
    salary = ""
    pay = header.get("payPeriodAdjustedPay") or {}
    if header.get("salarySource") == "EMPLOYER_PROVIDED" and pay.get("p10") and pay.get("p90"):
        interval = PAY_INTERVALS.get(header.get("payPeriod") or "", "")
        salary = format_salary(pay["p10"], pay["p90"], interval)

    snippet = " ".join(str(x) for x in (job.get("descriptionFragmentsText") or []))
    return {
        "company": company,
        "title": title,
        "location": location,
        "url": f"{BASE}/job-listing/j?jl={listing_id}",
        "date_posted": posted,
        "description": re.sub(r"<[^>]+>", " ", snippet)[:600].strip(),
        "salary": salary,
        "salary_source": "employer" if salary else "",
        "salary_currency": header.get("payCurrency") or "",
        "ats": "Glassdoor",
    }


# --------------------------------------------------------------------------- #
# Fetching
# --------------------------------------------------------------------------- #

def make_session():
    """A session with a browser-like TLS fingerprint (python-jobspy is a repo dependency)."""
    from jobspy.util import create_session

    session = create_session(has_retry=False)
    session.headers.update(HEADERS)
    return session


def warm_up(session) -> None:
    """Visit the home page once so later requests carry normal browser cookies."""
    try:
        session.get(BASE + "/")
    except Exception:  # noqa: BLE001 - best effort only
        pass


def fetch(session, url: str, *, hops: int = 5, attempts: int = 3,
          sleep: Callable[[float], None] | None = None) -> str:
    """GET following redirects; raise GlassdoorBlocked for anything but a real page.

    Cloudflare rejects a fraction of requests with 403/429 at random, and the same
    URL succeeds moments later, so those two are retried with a growing pause.
    """
    sleep = sleep or time.sleep          # looked up at call time so tests can replace it
    last = "no response"
    for attempt in range(1, attempts + 1):
        target = url
        retryable = False
        for _ in range(hops):
            try:
                resp = session.get(target, timeout_seconds=REQUEST_TIMEOUT)
            except Exception as exc:  # noqa: BLE001 - timeouts / connection resets from the TLS client
                last, retryable = f"{type(exc).__name__}: {str(exc)[:80]}", True
                break
            code = resp.status_code
            if code in (301, 302, 303, 307, 308):
                target = urljoin(target, resp.headers.get("location") or resp.headers.get("Location") or "")
                continue
            if code == 200:
                return resp.text
            last = f"HTTP {code}" + (" (rate limited or challenged)" if code in (403, 429) else "")
            retryable = code in (403, 429)
            break
        else:
            raise GlassdoorBlocked("too many redirects")
        if not retryable:
            raise GlassdoorBlocked(last)
        if attempt < attempts:
            sleep(6 * attempt + random.uniform(0, 3))
    raise GlassdoorBlocked(f"{last} after {attempts} attempts")


def scrape(
    terms: list[str],
    geos: list[dict],
    hours_old: int,
    *,
    keep_title: Callable[[str], bool],
    keep_location: Callable[[str], bool],
    format_salary: Callable,
    session=None,
    delay: float = 2.0,
    log: Callable[[str], None] = print,
    now: datetime | None = None,
    verbose: bool = False,
) -> tuple[list[dict], dict]:
    """Search every (term, place) pair and return (jobs, stats).

    ``stats`` always says how many queries ran, how many failed, and how many
    hit the ~30-result page limit, so silence never means "nothing there".
    """
    now = now or datetime.now(timezone.utc)
    if session is None:
        session = make_session()
        warm_up(session)
    from_age = age_days_for(hours_old)
    places = list(dict.fromkeys(location_key(g) for g in geos)) or ["canada"]
    if "canada" not in places:
        places.append("canada")  # remote and unlisted cities live only in the national search

    stats = {"queries": 0, "ok": 0, "failed": 0, "raw": 0, "truncated": 0, "from_age": from_age,
             "errors": []}
    jobs: dict[str, dict] = {}
    first = True

    for term in terms:
        for place in places:
            loc_type, loc_id, loc_slug = LOCATIONS[place]
            age = from_age
            while True:
                if not first:
                    time.sleep(delay + random.uniform(0, 1.0))
                first = False
                stats["queries"] += 1
                try:
                    html = fetch(session, build_url(term, loc_type, loc_id, loc_slug, age))
                    listings, total = parse_results(html)
                except GlassdoorBlocked as exc:
                    stats["failed"] += 1
                    stats["errors"].append(f"{term!r}/{place}: {exc}")
                    if verbose:
                        log(f"    ✗ {term!r} / {place} (last {age}d): {exc}")
                    break
                stats["ok"] += 1
                stats["raw"] += len(listings)
                if verbose:
                    log(f"    ✓ {term!r} / {place} (last {age}d): {len(listings)} of {total} results")
                # Keep everything this window returned, then decide whether to look closer.
                for item in listings:
                    job = to_job(item, today=now, format_salary=format_salary)
                    if not job or not keep_title(job["title"]) or not keep_location(job["location"]):
                        continue
                    jobs.setdefault(job["url"], job)
                # Page 1 is all that plain HTTP serves. If the site has more, also search
                # narrower windows: each shows its own newest ~30, so together they reach
                # further back than one wide window could, favouring the freshest roles.
                if total and total > len(listings):
                    narrower = next((s for s in reversed(AGE_STEPS) if s < age), None)
                    if narrower:
                        age = narrower
                        continue
                    stats["truncated"] += 1
                break

    log(
        f"  📊 Glassdoor: {stats['queries']} queries → {stats['ok']} ok / {stats['failed']} failed · "
        f"{stats['raw']} raw, {len(jobs)} matched (window {from_age}d, {stats['truncated']} truncated)"
    )
    for err in stats["errors"][:3]:
        log(f"  ⚠️  Glassdoor {err}")
    return list(jobs.values()), stats
