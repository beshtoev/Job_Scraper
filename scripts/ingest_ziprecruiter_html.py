#!/usr/bin/env python3
"""Turn saved ZipRecruiter alert emails (.html files) into ziprecruiter_jobs.json.

The IMAP path (scrape_jobs.py --ziprecruiter-email) needs mailbox credentials,
which Microsoft 365 no longer grants to plain scripts. So a scheduled Claude task
reads the alerts through the Microsoft 365 connector instead, saves each email's
HTML body to a file, and runs this script, which does everything after the mailbox:
parse the cards (ziprecruiter_email.parse_alert), filter and normalize them exactly
like the IMAP path, merge them into the current snapshot (alerts arrive
incrementally, so previous jobs are kept), and write through the normal output
path (location filter, all_jobs merge, md/html).

Usage: python scripts/ingest_ziprecruiter_html.py DIR_OR_HTML_FILES...
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import scrape_jobs as s           # noqa: E402
import ziprecruiter_email as zre  # noqa: E402


def main(argv: list[str]) -> int:
    paths: list[Path] = []
    for arg in argv:
        p = Path(arg)
        paths.extend(sorted(p.glob("*.html")) if p.is_dir() else [p])
    if not paths:
        print("usage: ingest_ziprecruiter_html.py DIR_OR_HTML_FILES...  (no .html files found)")
        return 2

    cards, unparsed = [], 0
    for path in paths:
        found = zre.parse_alert(path.read_text(errors="replace"))
        if not found:
            unparsed += 1
            print(f"  ⚠️  No job cards recognized in {path.name} - the layout may have changed")
        cards.extend(found)

    jobs = s.ziprecruiter_cards_to_jobs(cards)
    prev = s._load_prev_jobs(os.path.join(s.OUTPUT_DIR, "ziprecruiter_jobs.json"))
    merged = {s._job_identity(job["url"]): job for job in prev}
    merged.update({s._job_identity(job["url"]): job for job in jobs})
    print(f"  📊 ZipRecruiter email files: {len(paths)} file(s) → {len(cards)} card(s), "
          f"{len(jobs)} matched, snapshot {len(merged)} ({unparsed} unparsed)")
    s.save_ziprecruiter_results(list(merged.values()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
