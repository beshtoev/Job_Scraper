#!/usr/bin/env python3
"""Build and label a private discovery-quality pilot scorecard."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
OUTPUT_DIR = ROOT / "output"
PRIVATE_DIR = ROOT / "pilot" / "private"
DAILY_DIR = PRIVATE_DIR / "daily"
LABELS_PATH = PRIVATE_DIR / "labels.jsonl"
RUNS_PATH = PRIVATE_DIR / "source_runs.jsonl"
QUEUE_PATH = PRIVATE_DIR / "queue.json"
REPORT_PATH = PRIVATE_DIR / "report.md"
STALE_DAYS = 30
TOP_K = 10


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def canonical_url(value: str) -> str:
    value = str(value or "").strip()
    if not value:
        return ""
    parts = urlsplit(value)
    path = parts.path.rstrip("/")
    linkedin = re.search(r"/jobs/view/(\d+)", path)
    if linkedin:
        return f"linkedin:{linkedin.group(1)}"
    kept_query = [
        (key, val) for key, val in parse_qsl(parts.query)
        if key.lower() not in {"trk", "trackingid", "ref", "refid", "utm_source", "utm_medium", "utm_campaign"}
    ]
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, urlencode(kept_query), ""))


def job_id(job: dict) -> str:
    identity = canonical_url(job.get("url", ""))
    if not identity:
        identity = "|".join(_norm(job.get(key, "")) for key in ("company", "title", "location"))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]


def dedupe_key(job: dict) -> str:
    """Cluster URL variants and same-day multi-location copies of one role."""
    company = _norm(job.get("company", ""))
    title = _norm(job.get("title", ""))
    posted = str(job.get("date_posted", "")).strip()
    if company and title and posted:
        return f"role:{company}|{title}|{posted}"
    return canonical_url(job.get("url", "")) or f"role:{company}|{title}|{_norm(job.get('location', ''))}"


def parse_date(value: str) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
            return datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def _load_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def input_paths(extra: list[str] | None = None, include_test: bool = False) -> list[Path]:
    paths = sorted(p for p in OUTPUT_DIR.glob("*jobs.json") if p.name != "all_jobs.json")
    if include_test and (OUTPUT_DIR / "linkedin_test.json").exists():
        paths.append(OUTPUT_DIR / "linkedin_test.json")
    for value in extra or []:
        path = Path(value).expanduser().resolve()
        if path.exists() and path not in paths:
            paths.append(path)
    return paths


def load_candidates(paths: list[Path]) -> tuple[list[dict], list[dict]]:
    raw = []
    for path in paths:
        for job in _load_json(path).get("jobs", []):
            if not isinstance(job, dict):
                continue
            copy = dict(job)
            copy["_pilot_source"] = path.stem
            raw.append(copy)
    unique: dict[str, dict] = {}
    for job in raw:
        key = dedupe_key(job)
        if key not in unique:
            unique[key] = dict(job)
            unique[key]["_pilot_sources"] = [job["_pilot_source"]]
            unique[key]["_pilot_locations"] = [job.get("location", "")]
        else:
            if job["_pilot_source"] not in unique[key]["_pilot_sources"]:
                unique[key]["_pilot_sources"].append(job["_pilot_source"])
            if job.get("location", "") not in unique[key]["_pilot_locations"]:
                unique[key]["_pilot_locations"].append(job.get("location", ""))
    return raw, list(unique.values())


def score_job(job: dict) -> int:
    try:
        import notify
        return int(notify.relevance(job)[2])
    except Exception:
        return 0


def latest_labels() -> dict[str, dict]:
    labels: dict[str, dict] = {}
    for row in _read_jsonl(LABELS_PATH):
        if row.get("job_id"):
            labels[row["job_id"]] = row
    return labels


def build_queue(jobs: list[dict], labels: dict[str, dict]) -> list[dict]:
    ranked = []
    for job in jobs:
        item = dict(job)
        item["job_id"] = job_id(job)
        item["pilot_score"] = score_job(job)
        item["label"] = labels.get(item["job_id"])
        ranked.append(item)
    ranked.sort(
        key=lambda j: (j["pilot_score"], j.get("date_posted", ""), j.get("first_seen", "")),
        reverse=True,
    )
    for rank, item in enumerate(ranked, 1):
        item["rank"] = rank
    return ranked


def calculate_metrics(raw: list[dict], queue: list[dict], labels: dict[str, dict], runs: list[dict], now: datetime | None = None) -> dict:
    now = now or _now()
    top = queue[:TOP_K]
    top_labels = [labels.get(item["job_id"]) for item in top]
    top_labels = [label for label in top_labels if label]
    relevant = sum(label.get("verdict") == "relevant" for label in top_labels)
    useful = sum(label.get("verdict") in {"relevant", "borderline"} for label in top_labels)
    outside_queue = [
        label for label in labels.values()
        if label.get("scope") in {"recall_check", "rejected_sample"}
    ]
    missed_strict = [
        label for label in outside_queue
        if label.get("verdict") == "relevant"
        and label.get("status") not in {"stale", "closed"}
        and label.get("discovered") is False
    ]
    missed_useful = [
        label for label in outside_queue
        if label.get("verdict") in {"relevant", "borderline"}
        and label.get("status") not in {"stale", "closed"}
        and label.get("discovered") is False
    ]
    stale = 0
    unknown_dates = 0
    for job in queue:
        label = labels.get(job["job_id"], {})
        if label.get("status") in {"stale", "closed"}:
            stale += 1
            continue
        posted = parse_date(job.get("date_posted", ""))
        if posted is None:
            unknown_dates += 1
        elif now - posted > timedelta(days=STALE_DAYS):
            stale += 1
    attempts = len(runs)
    failures = sum(run.get("status") != "succeeded" for run in runs)
    return {
        "raw_candidates": len(raw),
        "unique_candidates": len(queue),
        "duplicates": max(0, len(raw) - len(queue)),
        "duplicate_rate": (len(raw) - len(queue)) / len(raw) if raw else None,
        "top_k": TOP_K,
        "top_reviewed": len(top_labels),
        "top_review_coverage": len(top_labels) / min(TOP_K, len(top)) if top else None,
        "strict_precision_at_10": relevant / len(top_labels) if top_labels else None,
        "useful_precision_at_10": useful / len(top_labels) if top_labels else None,
        "outside_queue_reviewed": len(outside_queue),
        "missed_relevant_count": len(missed_strict),
        "missed_relevant_rate": len(missed_strict) / len(outside_queue) if outside_queue else None,
        "missed_useful_count": len(missed_useful),
        "missed_useful_rate": len(missed_useful) / len(outside_queue) if outside_queue else None,
        "stale_count": stale,
        "stale_rate": stale / len(queue) if queue else None,
        "unknown_date_count": unknown_dates,
        "source_attempts": attempts,
        "source_failures": failures,
        "source_failure_rate": failures / attempts if attempts else None,
    }


def _pct(value) -> str:
    return "pending" if value is None else f"{value:.0%}"


def _missed_summary(metrics: dict) -> str:
    reviewed = metrics["outside_queue_reviewed"]
    if not reviewed:
        return "pending (no outside-queue sample reviewed yet)"
    return (
        f"{metrics['missed_relevant_count']} strict / {metrics['missed_useful_count']} useful "
        f"of {reviewed} reviewed ({_pct(metrics['missed_relevant_rate'])} / "
        f"{_pct(metrics['missed_useful_rate'])})"
    )


def write_report(snapshot: dict) -> None:
    daily = []
    for path in sorted(DAILY_DIR.glob("*.json")):
        data = _load_json(path)
        if data:
            daily.append(data)
    metrics = snapshot["metrics"]
    lines = [
        "# Discovery-quality pilot scorecard",
        "",
        f"Updated: {snapshot['created_at']}  ",
        "Pilot window: September 11–17, 2026  ",
        "Scope: Toronto/GTA, Ontario, and Canada-remote executive roles",
        "",
        "## Current result",
        "",
        f"- Unique candidates: {metrics['unique_candidates']} ({metrics['raw_candidates']} raw)",
        f"- Strict precision@10: {_pct(metrics['strict_precision_at_10'])} ({metrics['top_reviewed']} reviewed)",
        f"- Useful precision@10: {_pct(metrics['useful_precision_at_10'])}",
        f"- Missed relevant roles: {_missed_summary(metrics)}",
        f"- Duplicate rate: {_pct(metrics['duplicate_rate'])}",
        f"- Stale-posting rate: {_pct(metrics['stale_rate'])} ({metrics['unknown_date_count']} unknown dates)",
        f"- Source failure rate: {_pct(metrics['source_failure_rate'])} ({metrics['source_failures']}/{metrics['source_attempts']})",
        "",
        "## Daily snapshots",
        "",
        "| Date | Unique | Reviewed top 10 | Strict P@10 | Useful P@10 | Missed strict/useful | Duplicates | Stale | Source failures |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for day in daily:
        m = day["metrics"]
        lines.append(
            f"| {day['pilot_date']} | {m['unique_candidates']} | {m['top_reviewed']} | "
            f"{_pct(m['strict_precision_at_10'])} | {_pct(m['useful_precision_at_10'])} | "
            f"{m['missed_relevant_count']}/{m['missed_useful_count']} | "
            f"{_pct(m['duplicate_rate'])} | {_pct(m['stale_rate'])} | "
            f"{m['source_failures']}/{m['source_attempts']} |"
        )
    lines += [
        "",
        "## Recalibration decision",
        "",
        "Pending the September 17 review. JobMatchAI and Resume-Matcher remain out of scope until then.",
        "",
    ]
    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")


def snapshot(args) -> dict:
    PRIVATE_DIR.mkdir(parents=True, exist_ok=True)
    DAILY_DIR.mkdir(parents=True, exist_ok=True)
    paths = input_paths(args.input, args.include_test)
    raw, unique = load_candidates(paths)
    labels = latest_labels()
    queue = build_queue(unique, labels)
    runs = _read_jsonl(RUNS_PATH)
    now = _now()
    data = {
        "pilot_date": datetime.now().astimezone().date().isoformat(),
        "created_at": now.isoformat(),
        "inputs": [str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path) for path in paths],
        "metrics": calculate_metrics(raw, queue, labels, runs, now),
        "top_queue": queue[:TOP_K],
    }
    QUEUE_PATH.write_text(json.dumps({"created_at": data["created_at"], "jobs": queue}, indent=2, ensure_ascii=False), encoding="utf-8")
    (DAILY_DIR / f"{data['pilot_date']}.json").write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    write_report(data)
    return data


def print_queue() -> None:
    data = _load_json(QUEUE_PATH)
    jobs = data.get("jobs", [])[:TOP_K]
    if not jobs:
        print("Queue is empty. Run a live source and then snapshot the pilot.")
        return
    for job in jobs:
        label = (job.get("label") or {}).get("verdict", "unreviewed")
        print(f"{job['rank']:>2}. {job['job_id']}  {job['pilot_score']:>3}/100  {label}")
        print(f"    {job.get('title', 'Untitled')} — {job.get('company', 'Unknown')} — {job.get('location', 'Unknown')}")
        print(f"    {job.get('url', '')}")


def label_job(args) -> None:
    queue = _load_json(QUEUE_PATH).get("jobs", [])
    job = next((item for item in queue if item.get("job_id") == args.job_id), None)
    if job is None and not args.url:
        raise SystemExit(f"Unknown job id {args.job_id!r}; provide --url for a recall/rejected candidate.")
    row = {
        "job_id": args.job_id,
        "reviewed_at": _now().isoformat(),
        "verdict": args.verdict,
        "status": args.status,
        "scope": args.scope,
        "discovered": args.discovered == "yes",
        "notes": args.notes,
        "title": (job or {}).get("title", args.title),
        "company": (job or {}).get("company", args.company),
        "location": (job or {}).get("location", args.location),
        "url": (job or {}).get("url", args.url),
    }
    _append_jsonl(LABELS_PATH, row)
    print(f"Recorded {args.verdict} label for {args.job_id}.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    snap = sub.add_parser("snapshot", help="Rebuild the private queue and scorecard")
    snap.add_argument("--input", action="append", default=[], help="Additional result JSON")
    snap.add_argument("--include-test", action="store_true", help="Include output/linkedin_test.json")
    sub.add_parser("queue", help="Print the current top-10 review queue")
    label = sub.add_parser("label", help="Record or replace a review label")
    label.add_argument("job_id")
    label.add_argument("--verdict", choices=("relevant", "borderline", "not_relevant"), required=True)
    label.add_argument("--status", choices=("active", "stale", "closed", "unknown"), default="unknown")
    label.add_argument("--scope", choices=("top_queue", "rejected_sample", "recall_check"), default="top_queue")
    label.add_argument("--discovered", choices=("yes", "no"), default="yes")
    label.add_argument("--notes", default="")
    label.add_argument("--title", default="")
    label.add_argument("--company", default="")
    label.add_argument("--location", default="")
    label.add_argument("--url", default="")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.command == "snapshot":
        data = snapshot(args)
        print(json.dumps(data["metrics"], indent=2))
    elif args.command == "queue":
        print_queue()
    else:
        label_job(args)


if __name__ == "__main__":
    main()
