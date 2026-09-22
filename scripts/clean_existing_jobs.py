#!/usr/bin/env python3
"""Remove persisted jobs that no longer match the active personalization.

Dry-run by default. Pass --apply to rewrite job JSON files, prune scores and
notification history, and remove derived Markdown/HTML digests whose contents
would otherwise continue exposing deleted roles. Scrapers regenerate those
digests on their next run.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "output"
sys.path.insert(0, str(ROOT))

import scrape_jobs  # noqa: E402


def eligible(job: dict) -> bool:
    return (
        scrape_jobs.is_target_location(job.get("location", ""))
        and scrape_jobs.role_is_relevant(job.get("title", ""), job.get("company", ""))
        and not scrape_jobs._is_excluded_company(job.get("company", ""))
    )


def atomic_json(path: Path, payload: dict) -> None:
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
        temp_name = handle.name
    os.replace(temp_name, path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="rewrite the persisted data")
    args = parser.parse_args()

    kept_urls: set[str] = set()
    changed_stems: set[str] = set()
    total_before = total_after = 0

    for path in sorted(OUTPUT.glob("*.json")):
        if path.name in {"notified.json", "scores.json"}:
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(data, dict) or "jobs" not in data:
            continue

        jobs = data.get("jobs", [])
        kept = [job for job in jobs if isinstance(job, dict) and eligible(job)]
        total_before += len(jobs)
        total_after += len(kept)
        kept_urls.update(job.get("url", "") for job in kept if job.get("url"))
        if len(kept) == len(jobs):
            continue

        changed_stems.add(path.stem)
        data["jobs"] = kept
        if "new_jobs" in data:
            data["new_jobs"] = [job for job in data.get("new_jobs", []) if job in kept]
        if "total" in data:
            data["total"] = len(kept)
        if "new_count" in data:
            data["new_count"] = len(data.get("new_jobs", []))
        if args.apply:
            atomic_json(path, data)
        print(f"{path.name}: {len(jobs)} -> {len(kept)}")

    scores_path = OUTPUT / "scores.json"
    if args.apply and scores_path.exists():
        try:
            scores = json.loads(scores_path.read_text(encoding="utf-8"))
            scores["scores"] = {
                url: verdict
                for url, verdict in scores.get("scores", {}).items()
                if url in kept_urls
            }
            atomic_json(scores_path, scores)
        except (json.JSONDecodeError, OSError):
            pass

    notified_path = OUTPUT / "notified.json"
    if args.apply and notified_path.exists():
        atomic_json(notified_path, {"ids": []})

    if args.apply:
        for stem in changed_stems:
            for suffix in (".md", ".html"):
                derived = OUTPUT / f"{stem}{suffix}"
                if derived.exists():
                    derived.unlink()

    action = "Removed" if args.apply else "Would remove"
    print(f"{action} {total_before - total_after} of {total_before} persisted job records.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
