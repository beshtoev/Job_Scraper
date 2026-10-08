"""Test _merge_into_all_jobs — master file merge with field preservation."""
import json
from datetime import datetime, timezone
from scrape_jobs import _merge_into_all_jobs


def test_merge_adds_new_jobs(tmp_output_dir, sample_all_jobs):
    """Merging 3 new jobs (2 genuinely new, 1 duplicate) → added == 2."""
    # Write the sample to the temp output dir
    path = tmp_output_dir / "all_jobs.json"
    path.write_text(json.dumps(sample_all_jobs, separators=(",", ":")))

    new_jobs = [
        {"url": "https://www.linkedin.com/jobs/view/9900000001/",
         "company": "NewCo", "title": "Director of Engineering",
         "location": "SF, CA", "ats": "LinkedIn"},
        {"url": "https://www.linkedin.com/jobs/view/9900000002/",
         "company": "OtherCo", "title": "VP of Engineering",
         "location": "NYC, NY", "ats": "LinkedIn"},
        # Duplicate URL of existing job
        {"url": "https://www.linkedin.com/jobs/view/4400000001/",
         "company": "Acme Corp", "title": "Director of Engineering",
         "location": "San Francisco, CA", "ats": "LinkedIn",
         "description": "Updated description"},
    ]
    added = _merge_into_all_jobs(new_jobs)
    assert added == 2


def test_preserves_existing_fields_on_duplicate(tmp_output_dir, sample_all_jobs):
    """Existing downstream fields (bookmarked, notes) must be preserved when merging a duplicate."""
    path = tmp_output_dir / "all_jobs.json"
    # Keep the fixture inside the production 30-day retention window. Without
    # this, the test starts failing as wall-clock time advances past its fixed
    # 2026-08-11 timestamp before it can assert field preservation.
    target = next(
        j for j in sample_all_jobs["jobs"]
        if j["url"] == "https://www.linkedin.com/jobs/view/4400000001/"
    )
    target["first_seen"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    path.write_text(json.dumps(sample_all_jobs, separators=(",", ":")))

    # Merge a job that duplicates an existing bookmarked job
    new_jobs = [
        {"url": "https://www.linkedin.com/jobs/view/4400000001/",
         "company": "Acme Corp", "title": "Director of Engineering",
         "location": "San Francisco, CA", "ats": "LinkedIn"},
    ]
    _merge_into_all_jobs(new_jobs)

    data = json.loads(path.read_text())
    job = next(j for j in data["jobs"] if j["url"] == "https://www.linkedin.com/jobs/view/4400000001/")
    assert job.get("bookmarked") is True
    assert job.get("notes") == "preferred"


def test_preserves_false_tag_on_duplicate(tmp_output_dir, sample_all_jobs):
    """Existing false-valued downstream fields must be preserved when merging a duplicate."""
    path = tmp_output_dir / "all_jobs.json"
    # Keep the fixture inside the production 30-day retention window. Without
    # this, the test starts failing as wall-clock time advances past its fixed
    # 2026-08-11 timestamp before it can assert field preservation.
    target = next(
        j for j in sample_all_jobs["jobs"]
        if j["url"] == "https://www.linkedin.com/jobs/view/4400000008/"
    )
    target["first_seen"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    path.write_text(json.dumps(sample_all_jobs, separators=(",", ":")))

    new_jobs = [
        {"url": "https://www.linkedin.com/jobs/view/4400000008/",
         "company": "SalesForce", "title": "Director of Sales",
         "location": "Chicago, IL", "ats": "LinkedIn"},
    ]
    _merge_into_all_jobs(new_jobs)

    data = json.loads(path.read_text())
    job = next(j for j in data["jobs"] if j["url"] == "https://www.linkedin.com/jobs/view/4400000008/")
    assert job.get("bookmarked") is False
    assert job.get("notes") == "no"


def test_sets_first_seen_on_new_jobs(tmp_output_dir, sample_all_jobs):
    """New jobs should get a first_seen timestamp."""
    path = tmp_output_dir / "all_jobs.json"
    path.write_text(json.dumps(sample_all_jobs, separators=(",", ":")))

    new_jobs = [
        {"url": "https://www.linkedin.com/jobs/view/9900000099/",
         "company": "NewCo", "title": "Director of Engineering",
         "location": "SF, CA", "ats": "LinkedIn"},
    ]
    _merge_into_all_jobs(new_jobs)

    data = json.loads(path.read_text())
    job = next(j for j in data["jobs"] if j["url"] == "https://www.linkedin.com/jobs/view/9900000099/")
    assert "first_seen" in job
    assert job["first_seen"]


def test_output_is_valid_json(tmp_output_dir, sample_all_jobs):
    """Output file should be valid JSON with updated_at and jobs keys."""
    path = tmp_output_dir / "all_jobs.json"
    path.write_text(json.dumps(sample_all_jobs, separators=(",", ":")))

    _merge_into_all_jobs([])

    data = json.loads(path.read_text())
    assert "updated_at" in data
    assert "jobs" in data
    assert isinstance(data["jobs"], list)


def test_empty_master_file(tmp_output_dir):
    """Merging into a non-existent master should create it."""
    new_jobs = [
        {"url": "https://www.linkedin.com/jobs/view/9900000001/",
         "company": "NewCo", "title": "Director of Engineering",
         "location": "SF, CA", "ats": "LinkedIn"},
    ]
    added = _merge_into_all_jobs(new_jobs)
    assert added == 1

    data = json.loads((tmp_output_dir / "all_jobs.json").read_text())
    assert len(data["jobs"]) == 1


def test_aged_jobs_move_to_the_archive_and_are_never_deleted(tmp_output_dir):
    """Past ALL_JOBS_PRUNE_DAYS a job leaves all_jobs.json but lands in all_jobs_archive.json."""
    (tmp_output_dir / "all_jobs.json").write_text(json.dumps({"jobs": [
        {"url": "https://example.com/old", "title": "Director, Old", "company": "A", "first_seen": "2020-01-01T00:00:00Z"},
        {"url": "https://example.com/new", "title": "Director, New", "company": "B", "first_seen": "2099-01-01T00:00:00Z"},
    ]}))
    (tmp_output_dir / "all_jobs_archive.json").write_text(json.dumps({"jobs": [
        {"url": "https://example.com/older", "title": "Director, Older", "company": "C", "first_seen": "2019-01-01T00:00:00Z"},
    ]}))
    _merge_into_all_jobs([])
    master = json.loads((tmp_output_dir / "all_jobs.json").read_text())
    archive = json.loads((tmp_output_dir / "all_jobs_archive.json").read_text())
    assert [j["url"] for j in master["jobs"]] == ["https://example.com/new"]
    assert {j["url"] for j in archive["jobs"]} == {"https://example.com/old", "https://example.com/older"}
    assert archive["total"] == 2


def test_archiving_twice_keeps_one_copy_and_the_earliest_first_seen(tmp_output_dir):
    from scrape_jobs import _archive_jobs
    now = datetime(2026, 10, 7, tzinfo=timezone.utc)
    _archive_jobs([{"url": "https://example.com/x", "first_seen": "2026-08-01T00:00:00Z", "salary": "$200k"}], now)
    _archive_jobs([{"url": "https://example.com/x", "first_seen": "2026-08-05T00:00:00Z", "salary": ""}], now)
    archive = json.loads((tmp_output_dir / "all_jobs_archive.json").read_text())
    assert len(archive["jobs"]) == 1
    assert archive["jobs"][0]["first_seen"] == "2026-08-01T00:00:00Z" and archive["jobs"][0]["salary"] == "$200k"


def test_an_archive_file_always_exists_after_a_merge(tmp_output_dir):
    """Workflows `git add` the archive, so it must exist even before anything has aged out."""
    _merge_into_all_jobs([])
    assert json.loads((tmp_output_dir / "all_jobs_archive.json").read_text())["jobs"] == []
