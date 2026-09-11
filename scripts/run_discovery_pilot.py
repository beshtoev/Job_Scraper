#!/usr/bin/env python3
"""Run live discovery probes and record source health for the pilot."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNS_PATH = ROOT / "pilot" / "private" / "source_runs.jsonl"
SOURCE_FLAGS = {
    "linkedin-test": ("--linkedin-test", "linkedin_test.json"),
    "linkedin": ("--linkedin-only", "linkedin_jobs.json"),
    "indeed": ("--indeed-only", "indeed_jobs.json"),
    "glassdoor": ("--glassdoor-only", "glassdoor_jobs.json"),
    "ziprecruiter": ("--ziprecruiter-only", "ziprecruiter_jobs.json"),
    "google-jobs": ("--google-jobs-only", "google_jobs.json"),
    "hiringcafe": ("--hiringcafe", "hiringcafe_jobs.json"),
}


def append_run(row: dict) -> None:
    RUNS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with RUNS_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def candidate_count(filename: str, started: float) -> int | None:
    path = ROOT / "output" / filename
    if not path.exists() or path.stat().st_mtime + 1 < started:
        return None
    try:
        return len(json.loads(path.read_text(encoding="utf-8")).get("jobs", []))
    except (OSError, json.JSONDecodeError):
        return None


def run_source(source: str, timeout: int) -> bool:
    flag, filename = SOURCE_FLAGS[source]
    command = [sys.executable, str(ROOT / "scrape_jobs.py"), flag]
    started = time.time()
    started_at = datetime.now(timezone.utc).isoformat()
    try:
        result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, timeout=timeout)
        status = "succeeded" if result.returncode == 0 else "failed"
        output = (result.stdout + "\n" + result.stderr).strip()
        returncode = result.returncode
    except subprocess.TimeoutExpired as exc:
        status = "timeout"
        output = ((exc.stdout or "") + "\n" + (exc.stderr or "")).strip()
        returncode = None
    row = {
        "source": source,
        "started_at": started_at,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "duration_seconds": round(time.time() - started, 1),
        "status": status,
        "returncode": returncode,
        "candidate_count": candidate_count(filename, started),
        "diagnostic_tail": "\n".join(output.splitlines()[-20:]),
    }
    append_run(row)
    print(f"{source}: {status}; candidates={row['candidate_count']}")
    if status != "succeeded" and row["diagnostic_tail"]:
        print(row["diagnostic_tail"])
    return status == "succeeded"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources", nargs="+", choices=tuple(SOURCE_FLAGS), default=["linkedin"])
    parser.add_argument("--timeout", type=int, default=900, help="Per-source timeout in seconds")
    args = parser.parse_args()
    results = [run_source(source, args.timeout) for source in args.sources]
    ok = all(results)
    snapshot = [sys.executable, str(ROOT / "scripts" / "pilot_quality.py"), "snapshot", "--include-test"]
    subprocess.run(snapshot, cwd=ROOT, check=False)
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
