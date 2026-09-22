from datetime import datetime, timezone

from scripts import pilot_quality


def _job(url: str, title: str = "VP Data", posted: str = "2026-09-01") -> dict:
    return {
        "url": url,
        "title": title,
        "company": "Example",
        "location": "Toronto, ON, Canada",
        "date_posted": posted,
    }


def test_linkedin_tracking_urls_share_identity():
    first = _job("https://ca.linkedin.com/jobs/view/12345?trk=public_jobs")
    second = _job("https://www.linkedin.com/jobs/view/12345?trackingId=abc")
    assert pilot_quality.job_id(first) == pilot_quality.job_id(second)


def test_load_candidates_dedupes_url_variants(tmp_path):
    source = tmp_path / "linkedin_jobs.json"
    source.write_text(
        __import__("json").dumps({"jobs": [
            _job("https://www.linkedin.com/jobs/view/12345?trk=one"),
            _job("https://ca.linkedin.com/jobs/view/12345?trk=two"),
        ]}),
        encoding="utf-8",
    )
    raw, unique = pilot_quality.load_candidates([source])
    assert len(raw) == 2
    assert len(unique) == 1


def test_load_candidates_clusters_same_day_multilocation_postings(tmp_path):
    source = tmp_path / "linkedin_jobs.json"
    first = _job("https://www.linkedin.com/jobs/view/111")
    second = _job("https://www.linkedin.com/jobs/view/222")
    second["location"] = "Mississauga, ON, Canada"
    source.write_text(__import__("json").dumps({"jobs": [first, second]}), encoding="utf-8")
    raw, unique = pilot_quality.load_candidates([source])
    assert len(raw) == 2
    assert len(unique) == 1
    assert unique[0]["_pilot_locations"] == ["Toronto, ON, Canada", "Mississauga, ON, Canada"]


def test_metrics_report_precision_duplicates_staleness_and_failures(monkeypatch):
    monkeypatch.setattr(pilot_quality, "TOP_K", 2)
    jobs = [
        {**_job("https://example.test/1"), "job_id": "one"},
        {**_job("https://example.test/2", posted="2026-07-01"), "job_id": "two"},
    ]
    labels = {
        "one": {"verdict": "relevant", "scope": "top_queue", "status": "active"},
        "two": {"verdict": "borderline", "scope": "top_queue", "status": "unknown"},
        "miss": {"verdict": "relevant", "scope": "recall_check", "discovered": False},
    }
    metrics = pilot_quality.calculate_metrics(
        [jobs[0], jobs[0], jobs[1]], jobs, labels,
        [{"status": "succeeded"}, {"status": "failed"}],
        datetime(2026, 9, 10, tzinfo=timezone.utc),
    )
    assert metrics["duplicate_rate"] == 1 / 3
    assert metrics["strict_precision_at_10"] == 0.5
    assert metrics["useful_precision_at_10"] == 1.0
    assert metrics["outside_queue_reviewed"] == 1
    assert metrics["missed_relevant_count"] == 1
    assert metrics["missed_relevant_rate"] == 1.0
    assert metrics["missed_useful_count"] == 1
    assert metrics["missed_useful_rate"] == 1.0
    assert metrics["stale_count"] == 1
    assert metrics["source_failure_rate"] == 0.5
