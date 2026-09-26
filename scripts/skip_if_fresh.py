#!/usr/bin/env python3
"""Should a GitHub fallback workflow stand down? Writes skip=true|false to $GITHUB_OUTPUT.

    python scripts/skip_if_fresh.py SOURCE MAX_AGE_MINUTES

The local runner (scripts/local_runner.py) scrapes from a home connection on a steady cadence
and publishes output/scrape_state.json with each source's last_success. While that stamp is
younger than MAX_AGE_MINUTES there is nothing for GitHub to add, and running would only race
it for the commit slot. When the Mac sleeps or is off, the stamp ages out and the workflow
resumes as the fallback. A manual backfill (BACKFILL=true) always runs.
"""
import json
import os
import sys
from datetime import datetime, timezone


def decide(source: str, max_age_min: float, state_path: str = "output/scrape_state.json") -> tuple[bool, str]:
    if os.environ.get("BACKFILL") == "true":
        return False, "manual backfill: scraping"
    try:
        with open(state_path, encoding="utf-8") as f:
            last = json.load(f)[source]["last_success"]
        then = datetime.strptime(last, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except Exception as exc:  # noqa: BLE001  no usable state is a reason to scrape, never to fail
        return False, f"no usable {source} scrape state ({exc}); scraping"
    age = (datetime.now(timezone.utc) - then).total_seconds() / 60
    skip = age < max_age_min
    return skip, f"last successful {source} scrape {age:.0f} min ago -> {'skipping' if skip else 'scraping'}"


def main() -> int:
    source, max_age = sys.argv[1], float(sys.argv[2])
    skip, why = decide(source, max_age)
    print(why)
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"skip={'true' if skip else 'false'}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
