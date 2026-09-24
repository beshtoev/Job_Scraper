"""LinkedIn's search window must cover the gap since the last real scrape.

Regression: a fixed 60-minute window meant every skipped run (GitHub's scheduler fired
"hourly" ~4x/day; a laptop sleeps) permanently lost the postings from its hour. An
exhaustive search showed the automation had captured only ~a quarter of relevant roles.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

import scrape_jobs as s

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def state_file(tmp_path, monkeypatch):
    path = tmp_path / "scrape_state.json"
    monkeypatch.setattr(s, "SCRAPE_STATE_PATH", str(path))
    return path


def write_last_success(path, minutes_ago):
    stamp = (NOW - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    path.write_text(json.dumps({"linkedin": {"last_success": stamp}}))


def test_no_history_uses_the_base_window(state_file):
    assert s._linkedin_lookback_seconds(NOW) == s.LINKEDIN_LOOKBACK_SECONDS


def test_a_recent_scrape_keeps_the_base_window(state_file):
    write_last_success(state_file, minutes_ago=20)
    assert s._linkedin_lookback_seconds(NOW) == s.LINKEDIN_LOOKBACK_SECONDS


def test_the_window_grows_to_cover_a_gap_plus_a_margin(state_file):
    write_last_success(state_file, minutes_ago=5 * 60)             # the Mac slept for 5 hours
    assert s._linkedin_lookback_seconds(NOW) == 5 * 3600 + s.LINKEDIN_CATCH_UP_MARGIN_SECONDS


def test_the_window_never_exceeds_the_backfill_limit(state_file):
    write_last_success(state_file, minutes_ago=60 * 24 * 30)       # closed for a month
    assert s._linkedin_lookback_seconds(NOW) == s.LINKEDIN_BACKFILL_DAYS * 86400


@pytest.mark.parametrize("content", ["not json", "[]", '{"linkedin": {"last_success": "yesterday"}}', '{"linkedin": null}'])
def test_unreadable_state_falls_back_to_the_base_window(state_file, content):
    state_file.write_text(content)
    assert s._linkedin_lookback_seconds(NOW) == s.LINKEDIN_LOOKBACK_SECONDS


def test_success_marker_is_written_atomically_and_keeps_other_sources(state_file):
    state_file.write_text(json.dumps({"glassdoor": {"last_success": "2026-09-24T10:00:00Z"}}))
    s._mark_scrape_success("linkedin", now=NOW)
    data = json.loads(state_file.read_text())
    assert data["linkedin"]["last_success"] == "2026-09-24T12:00:00Z"
    assert data["glassdoor"]["last_success"] == "2026-09-24T10:00:00Z"
    assert not state_file.with_name("scrape_state.json.tmp").exists()


def test_a_blocked_or_unsaved_scrape_does_not_advance_the_marker(state_file, monkeypatch):
    saved = []
    monkeypatch.setattr(s, "_save_linkedin_snapshot", lambda jobs: saved.append(jobs))
    s._SCRAPE_OK.clear()                       # blocked run: scrape_linkedin_recent never set the flag
    s.save_linkedin_results([])
    assert saved == [[]] and not state_file.exists()
    s._SCRAPE_OK["linkedin"] = True            # a genuine run, once persisted
    s.save_linkedin_results([{"url": "https://x"}])
    assert "linkedin" in json.loads(state_file.read_text())


def test_window_description_is_readable():
    assert s._describe_window(3600) == "1h"
    assert s._describe_window(1800) == "30min"
    assert s._describe_window(5 * 3600 + 900) == "5.2h"


def test_indeed_backfill_is_seven_days_like_the_other_high_volume_boards():
    assert s.INDEED_BACKFILL_DAYS == s.LINKEDIN_BACKFILL_DAYS == 7
