#!/usr/bin/env python3
"""Generate the committed company-portal inventory from job-capture/universe.json."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = ROOT.parent / "job-capture" / "universe.json"
DEFAULT_OUTPUT = ROOT / "sources" / "company_portals.json"

WORKABLE_QUERIES = [
    "director data",
    "director analytics",
    "director ai",
    "vp data",
    "head of data",
    "director financial planning",
]

WEB_DISCOVERY_QUERIES = [
    "site:jobs.lever.co (director OR vice president OR head) Canada",
    "site:job-boards.greenhouse.io (director OR vice president OR head) Canada",
    "site:jobs.ashbyhq.com (director OR vice president OR head) Canada",
    "site:bamboohr.com/careers (director OR vice president OR head) Canada",
]

BLOCKED_DOMAINS = [
    "careerhound.io",
    "remote.co",
    "apply.workable.com/hadley-designs",
    "linodeobjects.com",
    "ubisoft-calendar.com",
    "awstrack.me",
]


def build_inventory(rows: list[dict], source_label: str) -> dict:
    companies = []
    for row in sorted(rows, key=lambda item: str(item.get("company", "")).lower()):
        careers_url = str(row.get("careers_url") or "").strip()
        companies.append({
            "company": str(row.get("company") or "").strip(),
            "archetype": str(row.get("archetype") or "other").strip(),
            "ats": str(row.get("ats") or "").strip().lower(),
            "slug": str(row.get("slug") or "").strip(),
            "careers_url": careers_url,
            "source": str(row.get("source") or "").strip(),
            "include": row.get("include") or [],
            "exclude": row.get("exclude") or [],
            "searches": row.get("searches") or [],
            "enabled": bool(careers_url),
            "resolution_status": "resolved" if careers_url else "unresolved",
        })
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "generated_from": source_label,
        "policy": {
            "geography": "Toronto/GTA first; Ontario and Canada only with Canadian eligibility evidence",
            "role_taxonomy": "Executive Data, AI, Analytics, Finance, Governance, Technology, Platform, Automation, and Transformation",
            "linkedin_and_indeed": "ingest email alerts; do not scrape directly in this portal watcher",
        },
        "aggregators": {
            "workable": {"enabled": True, "location": "Canada", "queries": WORKABLE_QUERIES},
            "builtin_toronto": {"enabled": True, "queries": ["director", "vice president", "head of"], "page_cap": 5},
            "summit_search": {"enabled": True, "sitemap": "https://summitsearchgroup.com/job-sitemap.xml"},
            "web_discovery": {"enabled": True, "queries": WEB_DISCOVERY_QUERIES, "frequency": "daily"},
        },
        "blocked_domains": BLOCKED_DOMAINS,
        "counts": {
            "companies": len(companies),
            "pollable": sum(item["enabled"] for item in companies),
            "unresolved": sum(not item["enabled"] for item in companies),
        },
        "companies": companies,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    try:
        rows = json.loads(args.source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Could not load source universe {args.source}: {exc}")
    if not isinstance(rows, list):
        raise SystemExit("Source universe must be a JSON array.")
    inventory = build_inventory(rows, "job-capture/universe.json")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(inventory, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    counts = inventory["counts"]
    print(f"Wrote {args.output}: {counts['pollable']} pollable, {counts['unresolved']} unresolved")


if __name__ == "__main__":
    main()
