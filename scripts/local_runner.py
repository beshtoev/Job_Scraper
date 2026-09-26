#!/usr/bin/env python3
"""Run the job scrapers that GitHub's servers cannot run well, from this Mac.

Why: GitHub's scheduler fires an "hourly" workflow only a few times a day (an
exhaustive search showed it captured about a quarter of new LinkedIn roles), and
Glassdoor refuses GitHub's data-centre addresses outright. A Mac on a home
connection has neither problem.

How it works (a background job calls ``tick`` every few minutes):

  1. Decide which sources are due (LinkedIn every 20 min, Indeed every 30, Glassdoor
     hourly, company portals every 3 hours) and run them most urgent first.
  2. In a dedicated, disposable clone (never your dashboard folder): reset to the
     newest origin/main, run the scraper, add geography.
  3. Publish only ``output/`` data files to GitHub, re-merging onto the newest
     remote if another writer got there first. Code is never pushed.
  4. Copy the fresh jobs into the dashboard folder, so localhost:8080 updates.

A sleeping Mac only delays discovery: the LinkedIn search window is sized to the
time since the last successful scrape, so nothing posted meanwhile is lost.

Commands:  tick | run SOURCE | status | sync-dashboard
Files:     ~/.job-scraper/{config.json,state.json,repo/,logs/}  (JOB_SCRAPER_HOME overrides)
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import traceback
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

HOME = Path(os.environ.get("JOB_SCRAPER_HOME") or Path.home() / ".job-scraper")
CONFIG_PATH = HOME / "config.json"
STATE_PATH = HOME / "state.json"
LOCK_PATH = HOME / "runner.lock"
LOG_PATH = HOME / "logs" / "runner.log"
REPO_DIR = HOME / "repo"

DEFAULT_CONFIG = {
    "repo_url": "https://github.com/beshtoev/Job_Scraper.git",
    "branch": "main",
    "python": None,                  # interpreter that runs the scrapers (default: this one)
    "dashboard_dir": None,           # checkout whose output/ the dashboard serves
    "retry_minutes": 5,              # after a failed attempt
    "heartbeat_minutes": 30,         # publish at least this often, even with nothing new
    "dashboard_retention_days": 30,  # same rolling window as the scrapers' master list
    # priority: lower runs first when several are due in one tick, and the due list is re-read
    # after every source, so a LinkedIn slot that comes due during a 20-minute portal poll runs
    # next instead of waiting behind the rest.
    "sources": {
        "linkedin": {"every_minutes": 20, "args": ["--linkedin-only"], "basename": "linkedin_jobs",
                     "timeout_minutes": 45, "priority": 1},
        # Moved off GitHub 2026-09-26: its "hourly" cron fired 8 times in 48h with gaps up to
        # 12.9h, and never in the Toronto morning (the cron window was 15:00-03:00 UTC). From a
        # home connection the same search ran 32 queries with 0 errors. The 24h window matches
        # Indeed's day-resolution dates, so a 30-minute cadence only adds freshness.
        "indeed": {"every_minutes": 30, "args": ["--indeed-only"], "basename": "indeed_jobs",
                   "timeout_minutes": 30, "priority": 2},
        # Glassdoor dates postings by whole days and scores requests for bots: hourly is as
        # fresh as it can get, and more often only adds rate-limit risk.
        "glassdoor": {"every_minutes": 60, "args": ["--glassdoor-only"], "basename": "glassdoor_jobs",
                      "timeout_minutes": 30, "priority": 3},
        # Employer ATS boards (Workday, Greenhouse, Lever...). GitHub polled them once a day.
        # A full poll is ~20 min, so it goes last and every 3 hours.
        "company_portals": {"every_minutes": 180, "script": "portal_scraper.py", "args": [],
                            "basename": "company_portal_jobs",
                            "extra_outputs": ["output/company_portal_status.json"],
                            "timeout_minutes": 60, "priority": 9},
    },
}
PUBLISH_PREFIX = "output/"
ALL_JOBS = "output/all_jobs.json"
GEO_CACHE = "output/geo_cache.json"
SCRAPE_STATE = "output/scrape_state.json"
KEY_LOG_MARKERS = ("📊", "✅", "⛔", "⚠️", "🇨🇦", "🎯", "all_jobs.json:", "Saved ", "Geography:")


def log(message: str) -> None:
    print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  {message}", flush=True)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(text: str | None) -> datetime | None:
    try:
        return datetime.strptime(text or "", "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# Config and state
# --------------------------------------------------------------------------- #

def read_json(path: Path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json_atomic(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


def copy_atomic(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".tmp")
    shutil.copyfile(src, tmp)
    os.replace(tmp, dst)


def load_config(path: Path | None = None) -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    user = read_json(path or CONFIG_PATH, {}) or {}
    for key, value in user.items():
        if key == "sources" and isinstance(value, dict):
            for name, src in value.items():
                cfg["sources"].setdefault(name, {}).update(src)
        else:
            cfg[key] = value
    return cfg


def load_state(path: Path | None = None) -> dict:
    state = read_json(path or STATE_PATH, {}) or {}
    state.setdefault("sources", {})
    return state


def is_due(source_cfg: dict, source_state: dict, now: datetime, retry_minutes: int = 5) -> bool:
    """Start-to-start cadence; a failed attempt is retried sooner than a normal one."""
    last = parse_iso(source_state.get("last_attempt"))
    if last is None:
        return True
    wait = retry_minutes if source_state.get("last_ok") is False else source_cfg["every_minutes"]
    return now - last >= timedelta(minutes=wait)


# --------------------------------------------------------------------------- #
# Merging (pure functions)
# --------------------------------------------------------------------------- #

def mark_success(repo: Path, name: str, started: datetime) -> None:
    """Record this source's success in output/scrape_state.json, which is published with the
    data. GitHub's fallback workflows read it and stand down while the Mac covers a source.
    A later last_success is never moved back: LinkedIn writes its own and sizes its search
    window from it."""
    path = repo / SCRAPE_STATE
    doc = read_json(path, {}) or {}
    have = parse_iso((doc.get(name) or {}).get("last_success"))
    if have is None or have < started:
        doc.setdefault(name, {})["last_success"] = iso(started)
        write_json_atomic(path, doc)


def job_key(job: dict):
    return job.get("url") or "|".join(str(job.get(k, "")) for k in ("company", "title", "location"))


def union_jobs(base_jobs: list[dict], newer_jobs: list[dict]) -> list[dict]:
    """Union by URL. Where both have a job, ``newer`` wins field by field (never with an
    empty value) and the earliest ``first_seen`` is kept."""
    merged = {job_key(j): j for j in base_jobs}
    for job in newer_jobs:
        key = job_key(job)
        if key in merged:
            combined = dict(merged[key])
            combined.update({f: v for f, v in job.items() if v not in (None, "", [])})
            seen = [t for t in (merged[key].get("first_seen"), job.get("first_seen")) if t]
            if seen:
                combined["first_seen"] = min(seen)
            merged[key] = combined
        else:
            merged[key] = job
    return sorted(merged.values(), key=lambda j: j.get("first_seen") or "", reverse=True)


def union_all_jobs(base_doc: dict | None, newer_doc: dict | None) -> dict:
    base_doc, newer_doc = base_doc or {}, newer_doc or {}
    jobs = union_jobs(base_doc.get("jobs") or [], newer_doc.get("jobs") or [])
    doc = {k: v for k, v in {**base_doc, **newer_doc}.items() if k != "jobs"}
    doc["jobs"] = jobs
    doc["total"] = len(jobs)
    return doc


def union_cache(base: dict | None, newer: dict | None) -> dict:
    out = dict(base or {})
    for key, value in (newer or {}).items():
        out[key] = {**(out.get(key) or {}), **value} if isinstance(value, dict) else value
    return out


def merge_state(base: dict | None, newer: dict | None) -> dict:
    """scrape_state.json: per source, keep whichever last_success is later."""
    out = dict(base or {})
    for source, entry in (newer or {}).items():
        mine, theirs = parse_iso((out.get(source) or {}).get("last_success")), parse_iso((entry or {}).get("last_success"))
        if source not in out or (theirs and (mine is None or theirs >= mine)):
            out[source] = entry
    return out


def jobs_digest(doc: dict | None) -> str:
    jobs = sorted((doc or {}).get("jobs") or [], key=job_key)
    return hashlib.sha1(json.dumps(jobs, sort_keys=True, default=str).encode()).hexdigest()


def prune_old(jobs: list[dict], days: int, now: datetime) -> list[dict]:
    cutoff = iso(now - timedelta(days=days))
    return [j for j in jobs if not j.get("first_seen") or j["first_seen"] >= cutoff]


def allowed_publish(path: str) -> bool:
    """Only data files under output/ may ever leave this machine."""
    parts = Path(path).parts
    return path.startswith(PUBLISH_PREFIX) and ".." not in parts and not Path(path).is_absolute()


# --------------------------------------------------------------------------- #
# Git
# --------------------------------------------------------------------------- #

def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, env=env)
    if check and result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {(result.stderr or result.stdout).strip()[:300]}")
    return result


def sync_clone(repo: Path, url: str, branch: str) -> None:
    """A clean checkout of the newest remote: disposable, so nothing local can pile up."""
    if not (repo / ".git").exists():
        repo.parent.mkdir(parents=True, exist_ok=True)
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        done = subprocess.run(["git", "clone", "--quiet", "--depth", "1", "--branch", branch, url, str(repo)],
                              capture_output=True, text=True, env=env)
        if done.returncode != 0:
            raise RuntimeError(f"git clone failed: {done.stderr.strip()[:300]}")
        git(repo, "config", "user.name", "Job Scraper (local runner)")
        git(repo, "config", "user.email", "job-scraper-local@users.noreply.github.com")
        return
    git(repo, "fetch", "--quiet", "--depth", "1", "origin", branch)
    git(repo, "reset", "--quiet", "--hard", "FETCH_HEAD")
    git(repo, "clean", "-fdq")


def rebase_onto_remote(repo: Path, paths: list[str], branch: str) -> None:
    """Another writer pushed first: keep our data, drop our commit, rebuild on the newest remote."""
    ours = {p: (repo / p).read_bytes() for p in paths if (repo / p).exists()}
    git(repo, "fetch", "--quiet", "--depth", "1", "origin", branch)
    git(repo, "reset", "--quiet", "--hard", "FETCH_HEAD")
    git(repo, "clean", "-fdq")
    for path, data in ours.items():
        target = repo / path
        if path == ALL_JOBS:
            write_json_atomic(target, union_all_jobs(read_json(target), json.loads(data)))
        elif path == GEO_CACHE:
            write_json_atomic(target, union_cache(read_json(target), json.loads(data)))
        elif path == SCRAPE_STATE:
            write_json_atomic(target, merge_state(read_json(target), json.loads(data)))
        else:  # a source's own snapshot: ours is the newer one
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)


def publish(repo: Path, paths: list[str], message: str, branch: str = "main", attempts: int = 4) -> str:
    """Commit and push ``paths``; returns "pushed" or "nothing". Only output/ files are allowed."""
    for path in paths:
        if not allowed_publish(path):
            raise ValueError(f"refusing to publish outside output/: {path}")
    existing = [p for p in paths if (repo / p).exists()]
    for attempt in range(1, attempts + 1):
        git(repo, "add", "-f", "--", *existing)
        if git(repo, "diff", "--cached", "--quiet", check=False).returncode == 0:
            return "nothing"
        staged = git(repo, "diff", "--cached", "--name-only").stdout.split("\n")
        stray = [p for p in staged if p and not allowed_publish(p)]
        if stray:
            git(repo, "reset", "--quiet")
            raise RuntimeError(f"unexpected files staged, nothing pushed: {stray}")
        git(repo, "commit", "--quiet", "-m", message)
        pushed = git(repo, "push", "--quiet", "origin", f"HEAD:{branch}", check=False)
        if pushed.returncode == 0:
            return "pushed"
        if attempt == attempts:
            raise RuntimeError(f"push failed after {attempts} attempts: {pushed.stderr.strip()[:300]}")
        log(f"  push raced with another writer; re-merging onto the newest remote (attempt {attempt})")
        rebase_onto_remote(repo, existing, branch)
    return "nothing"


# --------------------------------------------------------------------------- #
# Dashboard folder
# --------------------------------------------------------------------------- #

def mirror(repo: Path, dashboard_dir: str | None, basenames: list[str], retention_days: int,
           now: datetime | None = None) -> str:
    """Bring the dashboard folder's output/ up to date without dropping anything it already has."""
    if not dashboard_dir:
        return "no dashboard folder configured"
    out, src = Path(dashboard_dir) / "output", repo / "output"
    if not out.is_dir():
        return f"dashboard output folder not found: {out}"
    for base in basenames:
        for ext in (".json", ".md", ".html"):
            if (src / f"{base}{ext}").exists():
                copy_atomic(src / f"{base}{ext}", out / f"{base}{ext}")
    merged = union_all_jobs(read_json(out / "all_jobs.json"), read_json(src / "all_jobs.json"))
    merged["jobs"] = prune_old(merged["jobs"], retention_days, now or utcnow())
    merged["total"] = len(merged["jobs"])
    write_json_atomic(out / "all_jobs.json", merged)
    if (src / "geo_cache.json").exists():
        write_json_atomic(out / "geo_cache.json", union_cache(read_json(out / "geo_cache.json"), read_json(src / "geo_cache.json")))
    if (src / "scrape_state.json").exists():
        write_json_atomic(out / "scrape_state.json", merge_state(read_json(out / "scrape_state.json"), read_json(src / "scrape_state.json")))
    return f"dashboard updated ({merged['total']} jobs)"


# --------------------------------------------------------------------------- #
# Running a source
# --------------------------------------------------------------------------- #

def run_command(cmd: list[str], cwd: Path, timeout_s: int) -> tuple[int, list[str]]:
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    try:
        done = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout_s, env=env)
    except subprocess.TimeoutExpired as exc:
        tail = ((exc.stdout or b"").decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")).splitlines()
        return 124, tail[-15:] + [f"timed out after {timeout_s // 60} min"]
    return done.returncode, (done.stdout + "\n" + done.stderr).splitlines()


def key_lines(lines: list[str]) -> list[str]:
    return [ln.strip() for ln in lines if any(m in ln for m in KEY_LOG_MARKERS)][:14]


def run_source(name: str, cfg: dict, state: dict, now: datetime, dry_run: bool = False) -> dict:
    spec = cfg["sources"][name]
    python = cfg.get("python") or sys.executable
    basename = spec["basename"]
    log(f"{name}: starting")
    sync_clone(REPO_DIR, cfg["repo_url"], cfg["branch"])
    before = jobs_digest(read_json(REPO_DIR / ALL_JOBS))

    script = spec.get("script", "scrape_jobs.py")
    rc, lines = run_command([python, script, *spec["args"]], REPO_DIR, spec.get("timeout_minutes", 45) * 60)
    for ln in key_lines(lines):
        log(f"  {ln}")
    if rc != 0:
        for ln in lines[-12:]:
            log(f"  | {ln}")
        raise RuntimeError(f"{name} scraper exited with code {rc}")
    mark_success(REPO_DIR, name, now)

    rc, lines = run_command([python, "enrich_geography.py", f"output/{basename}.json", ALL_JOBS], REPO_DIR, 15 * 60)
    for ln in key_lines(lines):
        log(f"  {ln}")
    if rc != 0:
        log("  ⚠️  geography step failed (non-fatal); publishing without it")

    snapshot = read_json(REPO_DIR / f"output/{basename}.json", {}) or {}
    changed = jobs_digest(read_json(REPO_DIR / ALL_JOBS)) != before
    last_publish = parse_iso(state.get("last_publish"))
    heartbeat_due = last_publish is None or now - last_publish >= timedelta(minutes=cfg["heartbeat_minutes"])
    paths = [f"output/{basename}{ext}" for ext in (".json", ".md", ".html")] + [ALL_JOBS, GEO_CACHE, SCRAPE_STATE]
    paths += [p for p in spec.get("extra_outputs", []) if (REPO_DIR / p).exists()]
    outcome, dashboard_problem = "dry run: nothing pushed", None
    if not dry_run:
        if changed or heartbeat_due:
            message = f"chore: update {name} listings [local runner {now.strftime('%Y-%m-%d %H:%MZ')}]"
            outcome = publish(REPO_DIR, paths, message, cfg["branch"])
            if outcome == "pushed":
                state["last_publish"] = iso(now)
        else:
            outcome = "no new jobs; not published"
        log(f"  publish: {outcome}")
        # The scrape and the GitHub publish already succeeded; a dashboard-folder problem (for example
        # a macOS privacy restriction on iCloud Drive) must not turn the whole run into a failure.
        try:
            log(f"  {mirror(REPO_DIR, cfg.get('dashboard_dir'), [basename], cfg['dashboard_retention_days'], now)}")
        except Exception as exc:  # noqa: BLE001
            dashboard_problem = f"{type(exc).__name__}: {exc}"[:200]
            log(f"  ⚠️  could not update the dashboard folder: {dashboard_problem}")
    return {"total": snapshot.get("total"), "new": snapshot.get("new_count"), "changed": changed,
            "publish": outcome, "dashboard_problem": dashboard_problem}


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #

@contextmanager
def single_instance(path: Path | None = None):
    """Yield True if we hold the lock, False if another tick is already running."""
    path = path or LOCK_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "w")
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            got = True
        except OSError:
            got = False
        yield got
    finally:
        handle.close()


def network_up(host: str = "github.com", port: int = 443) -> bool:
    try:
        socket.create_connection((host, port), timeout=5).close()
        return True
    except OSError:
        return False


def rotate_log(limit_bytes: int = 5 * 1024 * 1024) -> None:
    try:
        if LOG_PATH.stat().st_size > limit_bytes:
            os.replace(LOG_PATH, LOG_PATH.with_suffix(".log.1"))
    except OSError:
        pass


def cmd_tick(args) -> int:
    rotate_log()
    with single_instance() as got:
        if not got:
            log("another tick is still running; skipping")
            return 0
        cfg, state = load_config(), load_state()
        now = utcnow()
        only = getattr(args, "only", None)
        if only and only not in cfg["sources"]:
            log(f"unknown source {only!r}")
            return 0
        if not (only or due_sources(cfg, state, now)):
            return 0
        if not network_up():
            log("no network; will try again next tick")
            return 0
        done: set[str] = set()
        while True:
            if only:
                name = None if only in done else only
            else:
                name = next((n for n in due_sources(cfg, state, utcnow()) if n not in done), None)
            if name is None:
                break
            done.add(name)
            entry = state["sources"].setdefault(name, {})
            entry["last_attempt"] = iso(utcnow())
            try:
                result = run_source(name, cfg, state, utcnow(), dry_run=getattr(args, "dry_run", False))
                entry.update(last_ok=True, last_success=iso(utcnow()), last_message=json.dumps(result))
                log(f"{name}: done ({result['total']} in window, {result['new']} new)")
            except Exception as exc:  # never let one source stop the others or the job itself
                entry.update(last_ok=False, last_message=f"{type(exc).__name__}: {exc}"[:300])
                log(f"{name}: FAILED - {exc}")
                log(traceback.format_exc().strip().splitlines()[-1])
            write_json_atomic(STATE_PATH, state)
    return 0


def due_sources(cfg: dict, state: dict, now: datetime) -> list[str]:
    """Due sources, most urgent first."""
    due = [n for n, s in cfg["sources"].items() if is_due(s, state["sources"].get(n, {}), now, cfg["retry_minutes"])]
    return sorted(due, key=lambda n: cfg["sources"][n].get("priority", 5))


def cmd_status(_args) -> int:
    cfg, state, now = load_config(), load_state(), utcnow()
    print(f"home: {HOME}\nlog:  {LOG_PATH}\nrepo: {cfg['repo_url']} ({cfg['branch']})\ndashboard: {cfg.get('dashboard_dir')}")
    for name, spec in cfg["sources"].items():
        entry = state["sources"].get(name, {})
        last = parse_iso(entry.get("last_attempt"))
        ago = f"{int((now - last).total_seconds() // 60)} min ago" if last else "never"
        due = "due now" if is_due(spec, entry, now, cfg["retry_minutes"]) else "waiting"
        ok = {True: "ok", False: "FAILED", None: "-"}[entry.get("last_ok")]
        print(f"\n{name}: every {spec['every_minutes']} min | last attempt {ago} ({ok}) | {due}")
        if entry.get("last_message"):
            print(f"  {entry['last_message'][:200]}")
    return 0


def cmd_sync_dashboard(args) -> int:
    """Move the dashboard folder's code to the newest main without losing its job data.

    The folder's output/ files are tracked in git and also committed by GitHub, so a plain
    pull collides. This backs them up, takes the newest, and merges your data back."""
    cfg = load_config()
    repo = Path(args.dir or cfg.get("dashboard_dir") or "")
    if not (repo / ".git").exists():
        print("give --dir (or set dashboard_dir in config.json) pointing at the repo checkout")
        return 2
    branch_now = git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    if branch_now != cfg["branch"]:
        print(f"refusing: the dashboard folder is on branch {branch_now!r}, not {cfg['branch']!r}")
        return 2
    dirty = [ln[3:] for ln in git(repo, "status", "--porcelain", "--untracked-files=no").stdout.splitlines()
             if ln[3:] and not ln[3:].startswith(PUBLISH_PREFIX)]
    if dirty:
        print("refusing: uncommitted changes outside output/ would be lost:\n  " + "\n  ".join(dirty))
        return 2
    out = repo / "output"
    backup = HOME / "backups" / datetime.now().strftime("output-%Y%m%dT%H%M%S")
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(out, backup)
    local_jobs, local_cache, local_state = read_json(out / "all_jobs.json"), read_json(out / "geo_cache.json"), read_json(out / "scrape_state.json")
    git(repo, "checkout", "--", "output/")
    git(repo, "pull", "--quiet", "--ff-only", "origin", cfg["branch"])
    merged = union_all_jobs(local_jobs, read_json(out / "all_jobs.json"))      # the newest remote wins overlaps
    merged["jobs"] = prune_old(merged["jobs"], cfg["dashboard_retention_days"], utcnow())
    merged["total"] = len(merged["jobs"])
    write_json_atomic(out / "all_jobs.json", merged)
    write_json_atomic(out / "geo_cache.json", union_cache(local_cache, read_json(out / "geo_cache.json")))
    write_json_atomic(out / "scrape_state.json", merge_state(local_state, read_json(out / "scrape_state.json")))
    print(f"synced to {git(repo, 'rev-parse', '--short', 'HEAD').stdout.strip()}; {merged['total']} jobs kept; backup: {backup}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    tick = sub.add_parser("tick", help="run whatever is due (this is what the background job calls)")
    tick.add_argument("--dry-run", action="store_true", help="scrape but do not push or touch the dashboard")
    run = sub.add_parser("run", help="run one source now, whether or not it is due")
    run.add_argument("only", metavar="SOURCE")
    run.add_argument("--dry-run", action="store_true")
    sub.add_parser("status", help="show schedule and last results")
    sync = sub.add_parser("sync-dashboard", help="update the dashboard folder's code, keeping its job data")
    sync.add_argument("--dir")
    args = parser.parse_args(argv)
    return {"tick": cmd_tick, "run": cmd_tick, "status": cmd_status, "sync-dashboard": cmd_sync_dashboard}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
