"""A LinkedIn run that runs out of time keeps what it found and resumes next run.

Regression, 2026-10-18: after a 5-day throttle each run tried to catch up the whole gap, hit
the runner's 45-minute kill and lost everything it had collected (100 relevant jobs on the
last attempt), so the gap only grew: 17 failed runs in a row.
"""

import json

import pytest

import scrape_jobs as s

TERMS = ["Term A", "Term B", "Term C"]


@pytest.fixture
def env(tmp_path, monkeypatch):
    out = tmp_path / "output"
    out.mkdir()
    (out / "all_jobs.json").write_text(json.dumps({"jobs": [
        {"url": "https://www.linkedin.com/jobs/view/2/", "description": "on file", "salary": "$1"}]}))
    monkeypatch.setattr(s, "OUTPUT_DIR", str(out))
    monkeypatch.setattr(s, "SCRAPE_STATE_PATH", str(out / "scrape_state.json"))
    monkeypatch.setattr(s, "LINKEDIN_SEARCH_TERMS", TERMS)
    monkeypatch.setattr(s, "is_target_location", lambda loc: True)
    enriched = []
    monkeypatch.setattr(s, "_enrich_linkedin_postings", lambda jobs, deadline=None: enriched.append(len(jobs)))
    return out


def fake_search(out_of_time_on=None):
    """One job per term; flags out-of-time on the named term, as the real search does."""
    def search(terms, lookback, max_results=100, deadline=None, geos=None):
        term = terms[0]
        idx = TERMS.index(term) + 1
        s._LINKEDIN_OUT_OF_TIME = term == out_of_time_on
        return [{"url": f"https://www.linkedin.com/jobs/view/{idx}/", "title": term, "date_posted": ""}], 10
    return search


def cursor(out):
    return json.loads((out / "scrape_state.json").read_text())["linkedin_cursor"]


def test_running_out_of_time_saves_the_jobs_and_remembers_where_it_stopped(env, monkeypatch):
    monkeypatch.setattr(s, "_linkedin_search", fake_search(out_of_time_on="Term B"))
    jobs = s.scrape_linkedin_recent()
    assert {j["title"] for j in jobs} == {"Term A", "Term B"}         # nothing found is lost
    c = cursor(env)
    assert c["partial"] is True and c["next_term"] == 1              # Term B is repeated next run
    assert s._SCRAPE_OK.get("linkedin") is False                     # the window is not closed
    s.save_linkedin_results(jobs)
    assert "linkedin" not in json.loads((env / "scrape_state.json").read_text())


def test_the_next_run_resumes_and_a_full_pass_stamps_when_the_pass_began(env, monkeypatch):
    monkeypatch.setattr(s, "_linkedin_search", fake_search(out_of_time_on="Term B"))
    s.scrape_linkedin_recent()
    started = cursor(env)["cycle_started"]
    seen = []
    def search(terms, lookback, max_results=100, deadline=None, geos=None):
        seen.append(terms[0])
        return fake_search()(terms, lookback)
    monkeypatch.setattr(s, "_linkedin_search", search)
    jobs = s.scrape_linkedin_recent()
    assert seen == ["Term B", "Term C"]                              # resumed, not restarted
    s.save_linkedin_results(jobs)
    state = json.loads((env / "scrape_state.json").read_text())
    assert state["linkedin"]["last_success"] == started              # covered since the pass began
    assert state["linkedin_cursor"]["next_term"] == 0 and state["linkedin_cursor"]["partial"] is False


def test_known_postings_reuse_details_on_file(env, monkeypatch):
    monkeypatch.setattr(s, "_linkedin_search", fake_search())
    jobs = s.scrape_linkedin_recent()
    on_file = next(j for j in jobs if j["url"].endswith("/2/"))
    assert on_file["description"] == "on file" and on_file["salary"] == "$1"


def test_the_catch_up_window_is_capped_at_six_days(env, monkeypatch):
    windows = []
    monkeypatch.setattr(s, "_linkedin_lookback_seconds", lambda now=None: 9 * 86400)
    def search(terms, lookback, max_results=100, deadline=None, geos=None):
        windows.append(lookback)
        return fake_search()(terms, lookback)
    monkeypatch.setattr(s, "_linkedin_search", search)
    s.scrape_linkedin_recent()
    assert set(windows) == {6 * 86400}


def test_a_run_always_moves_past_its_first_term(env, monkeypatch):
    monkeypatch.setattr(s, "_linkedin_search", fake_search(out_of_time_on="Term A"))
    s.scrape_linkedin_recent()
    assert cursor(env)["next_term"] == 1      # never stuck repeating one slow term
