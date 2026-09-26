"""The local runner: scheduling, merging, publishing (real git, two racing writers) and mirroring.

No network: the "remote" is a bare repository on disk and the scrapers are small stubs.
"""

import importlib.util
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "scripts" / "local_runner.py"
spec = importlib.util.spec_from_file_location("local_runner", SCRIPT)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
LINKEDIN = {"every_minutes": 20}


# --------------------------------------------------------------------------- scheduling

def test_a_source_that_never_ran_is_due():
    assert runner.is_due(LINKEDIN, {}, NOW)


def test_cadence_is_measured_from_the_last_attempt():
    ran = lambda minutes: {"last_attempt": runner.iso(NOW - timedelta(minutes=minutes)), "last_ok": True}
    assert not runner.is_due(LINKEDIN, ran(10), NOW)
    assert runner.is_due(LINKEDIN, ran(20), NOW)


def test_a_failed_attempt_is_retried_sooner_than_a_normal_cadence():
    failed = lambda minutes: {"last_attempt": runner.iso(NOW - timedelta(minutes=minutes)), "last_ok": False}
    assert not runner.is_due(LINKEDIN, failed(3), NOW, retry_minutes=5)
    assert runner.is_due(LINKEDIN, failed(6), NOW, retry_minutes=5)


def test_user_config_overrides_defaults_without_dropping_other_sources(tmp_path):
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({"sources": {"linkedin": {"every_minutes": 15}}, "dashboard_dir": "/x"}))
    cfg = runner.load_config(cfg_file)
    assert cfg["sources"]["linkedin"]["every_minutes"] == 15
    assert cfg["sources"]["linkedin"]["args"] == ["--linkedin-only"]      # untouched default
    assert "glassdoor" in cfg["sources"] and cfg["dashboard_dir"] == "/x"


# --------------------------------------------------------------------------- merging

def test_union_keeps_everything_and_newer_fields_win_but_never_with_blanks():
    base = [{"url": "a", "title": "Old", "salary": "$200k", "first_seen": "2026-09-20T00:00:00Z"},
            {"url": "b", "title": "Only in base", "first_seen": "2026-09-19T00:00:00Z"}]
    newer = [{"url": "a", "title": "New", "salary": "", "first_seen": "2026-09-24T00:00:00Z"},
             {"url": "c", "title": "Brand new", "first_seen": "2026-09-24T01:00:00Z"}]
    merged = {j["url"]: j for j in runner.union_jobs(base, newer)}
    assert set(merged) == {"a", "b", "c"}
    assert merged["a"]["title"] == "New"                       # newer wins
    assert merged["a"]["salary"] == "$200k"                    # a blank never erases a value
    assert merged["a"]["first_seen"] == "2026-09-20T00:00:00Z"  # earliest sighting kept
    assert [j["url"] for j in runner.union_jobs(base, newer)][0] == "c"   # newest first


def test_union_all_jobs_keeps_document_fields_and_recounts():
    doc = runner.union_all_jobs({"updated_at": "x", "jobs": [{"url": "a"}]}, {"updated_at": "y", "jobs": [{"url": "b"}]})
    assert doc["updated_at"] == "y" and doc["total"] == 2


def test_scrape_state_merge_keeps_the_later_success_per_source():
    base = {"linkedin": {"last_success": "2026-09-24T12:00:00Z"}, "glassdoor": {"last_success": "2026-09-24T09:00:00Z"}}
    newer = {"linkedin": {"last_success": "2026-09-24T11:00:00Z"}}
    merged = runner.merge_state(base, newer)
    assert merged["linkedin"]["last_success"] == "2026-09-24T12:00:00Z"     # an older write cannot rewind it
    assert merged["glassdoor"]["last_success"] == "2026-09-24T09:00:00Z"


def test_digest_ignores_document_metadata_but_notices_any_job_change():
    a = {"updated_at": "1", "jobs": [{"url": "a", "title": "x"}]}
    assert runner.jobs_digest(a) == runner.jobs_digest({"updated_at": "2", "total": 1, "jobs": [{"url": "a", "title": "x"}]})
    assert runner.jobs_digest(a) != runner.jobs_digest({"jobs": [{"url": "a", "title": "y"}]})


def test_only_output_data_files_may_be_published():
    assert runner.allowed_publish("output/all_jobs.json")
    for bad in ("scrape_jobs.py", "triage.html", "output/../config.json", "/etc/passwd", ".github/workflows/x.yml"):
        assert not runner.allowed_publish(bad), bad


# --------------------------------------------------------------------------- git (real repositories)

@pytest.fixture
def git_env(monkeypatch, tmp_path):
    """Isolate git from the developer's own config (credential helpers, signing...)."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/dev/null")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "t")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "t")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@example.com")


def sh(*cmd, cwd=None):
    return subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True).stdout


def write(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj))


def make_remote(tmp_path, files):
    """A bare 'GitHub' plus a seed commit containing ``files`` ({path: json-able})."""
    remote = tmp_path / "remote.git"
    sh("git", "init", "--bare", "-b", "main", str(remote))
    seed = tmp_path / "seed"
    sh("git", "clone", "-q", str(remote), str(seed))
    for rel, obj in files.items():
        write(seed / rel, obj)
    (seed / "README.md").write_text("code")
    sh("git", "add", "-A", cwd=seed)
    sh("git", "commit", "-qm", "seed", cwd=seed)
    sh("git", "push", "-q", "origin", "HEAD:main", cwd=seed)
    return remote


def clone(remote, dest):
    sh("git", "clone", "-q", "--branch", "main", str(remote), str(dest))
    return dest


def remote_json(remote, tmp_path, rel):
    check = tmp_path / "check"
    if check.exists():
        sh("rm", "-rf", str(check))
    sh("git", "clone", "-q", str(remote), str(check))
    return json.loads((check / rel).read_text())


def test_publish_pushes_data_and_reports_nothing_when_unchanged(git_env, tmp_path):
    remote = make_remote(tmp_path, {"output/all_jobs.json": {"jobs": [{"url": "a"}]}})
    repo = clone(remote, tmp_path / "repo")
    write(repo / "output/all_jobs.json", {"jobs": [{"url": "a"}, {"url": "b"}]})
    assert runner.publish(repo, ["output/all_jobs.json"], "update") == "pushed"
    assert [j["url"] for j in remote_json(remote, tmp_path, "output/all_jobs.json")["jobs"]] == ["a", "b"]
    assert runner.publish(repo, ["output/all_jobs.json"], "again") == "nothing"


def test_publish_refuses_code_and_anything_else_outside_output(git_env, tmp_path):
    remote = make_remote(tmp_path, {"output/all_jobs.json": {"jobs": []}})
    repo = clone(remote, tmp_path / "repo")
    with pytest.raises(ValueError):
        runner.publish(repo, ["scrape_jobs.py"], "x")
    (repo / "README.md").write_text("tampered")
    sh("git", "add", "README.md", cwd=repo)                 # something else already staged
    write(repo / "output/all_jobs.json", {"jobs": [{"url": "z"}]})
    with pytest.raises(RuntimeError, match="unexpected files staged"):
        runner.publish(repo, ["output/all_jobs.json"], "x")
    assert sh("git", "log", "--oneline", "origin/main", cwd=repo).count("\n") == 1     # nothing was pushed


def test_a_racing_writer_loses_no_jobs(git_env, tmp_path):
    """Two writers start from the same commit. The second to push must re-merge, not overwrite."""
    remote = make_remote(tmp_path, {"output/all_jobs.json": {"jobs": [{"url": "seed"}]},
                                    "output/scrape_state.json": {"linkedin": {"last_success": "2026-09-24T10:00:00Z"}}})
    ours, theirs = clone(remote, tmp_path / "ours"), clone(remote, tmp_path / "theirs")

    # GitHub's own workflow gets there first, adding a job and a newer success marker.
    write(theirs / "output/all_jobs.json", {"jobs": [{"url": "seed"}, {"url": "from-github"}]})
    write(theirs / "output/scrape_state.json", {"linkedin": {"last_success": "2026-09-24T11:30:00Z"}})
    write(theirs / "output/indeed_jobs.json", {"jobs": [{"url": "indeed-1"}]})
    assert runner.publish(theirs, ["output/all_jobs.json", "output/scrape_state.json", "output/indeed_jobs.json"], "github") == "pushed"

    # The local runner, still on the old base, finishes its scrape and publishes.
    write(ours / "output/all_jobs.json", {"jobs": [{"url": "seed"}, {"url": "from-mac"}]})
    write(ours / "output/scrape_state.json", {"linkedin": {"last_success": "2026-09-24T11:00:00Z"}})
    write(ours / "output/linkedin_jobs.json", {"jobs": [{"url": "from-mac"}], "total": 1})
    paths = ["output/all_jobs.json", "output/scrape_state.json", "output/linkedin_jobs.json"]
    assert runner.publish(ours, paths, "mac") == "pushed"

    merged = remote_json(remote, tmp_path, "output/all_jobs.json")
    assert {j["url"] for j in merged["jobs"]} == {"seed", "from-github", "from-mac"} and merged["total"] == 3
    assert remote_json(remote, tmp_path, "output/scrape_state.json")["linkedin"]["last_success"] == "2026-09-24T11:30:00Z"
    assert remote_json(remote, tmp_path, "output/linkedin_jobs.json")["total"] == 1
    assert remote_json(remote, tmp_path, "output/indeed_jobs.json")["jobs"][0]["url"] == "indeed-1"   # theirs survived


# --------------------------------------------------------------------------- dashboard folder

def test_mirror_updates_the_dashboard_without_dropping_its_history_and_prunes_the_old(tmp_path):
    fresh_repo, dash = tmp_path / "repo", tmp_path / "dash"
    write(fresh_repo / "output/all_jobs.json", {"jobs": [{"url": "new", "first_seen": "2026-09-24T11:00:00Z"}]})
    write(fresh_repo / "output/linkedin_jobs.json", {"jobs": [], "total": 0})
    write(dash / "output/all_jobs.json", {"jobs": [{"url": "history", "first_seen": "2026-09-10T00:00:00Z"},
                                                    {"url": "ancient", "first_seen": "2026-07-01T00:00:00Z"}]})
    message = runner.mirror(fresh_repo, str(dash), ["linkedin_jobs"], retention_days=30, now=NOW)
    jobs = json.loads((dash / "output/all_jobs.json").read_text())
    assert {j["url"] for j in jobs["jobs"]} == {"new", "history"} and jobs["total"] == 2
    assert (dash / "output/linkedin_jobs.json").exists() and "2 jobs" in message


def test_mirror_is_a_noop_without_a_dashboard_folder(tmp_path):
    assert "no dashboard" in runner.mirror(tmp_path, None, [], 30)
    assert "not found" in runner.mirror(tmp_path, str(tmp_path / "missing"), [], 30)


# --------------------------------------------------------------------------- a full source run

STUB_SCRAPER = '''
import json, sys, os
from datetime import datetime, timezone
os.makedirs("output", exist_ok=True)
jobs = json.load(open("STUB_JOBS.json"))
json.dump({"scraped_at": "now", "total": len(jobs), "new_count": len(jobs), "jobs": jobs}, open("output/linkedin_jobs.json", "w"))
try: doc = json.load(open("output/all_jobs.json"))
except Exception: doc = {"jobs": []}
have = {j["url"] for j in doc["jobs"]}
doc["jobs"] += [j for j in jobs if j["url"] not in have]
json.dump(doc, open("output/all_jobs.json", "w"))
json.dump({"linkedin": {"last_success": "2026-09-24T12:00:00Z"}}, open("output/scrape_state.json", "w"))
print("  ✅ LinkedIn: %d role(s)" % len(jobs))
'''


@pytest.fixture
def stubbed(git_env, tmp_path, monkeypatch):
    remote = make_remote(tmp_path, {"output/all_jobs.json": {"jobs": []}})
    seed = tmp_path / "seed"
    (seed / "scrape_jobs.py").write_text(STUB_SCRAPER)
    (seed / "enrich_geography.py").write_text("print('Geography: stub')")
    write(seed / "STUB_JOBS.json", [{"url": "https://x/1", "title": "VP Data", "first_seen": "2026-09-24T11:59:00Z"}])
    sh("git", "add", "-A", cwd=seed)
    sh("git", "commit", "-qm", "stubs", cwd=seed)
    sh("git", "push", "-q", "origin", "HEAD:main", cwd=seed)
    dash = tmp_path / "dash"
    write(dash / "output/all_jobs.json", {"jobs": [{"url": "old-local", "first_seen": "2026-09-20T00:00:00Z"}]})
    monkeypatch.setattr(runner, "REPO_DIR", tmp_path / "runner-repo")
    cfg = json.loads(json.dumps(runner.DEFAULT_CONFIG))
    cfg.update(repo_url=str(remote), python=sys.executable, dashboard_dir=str(dash))
    return remote, cfg, dash, tmp_path


def test_a_source_run_scrapes_publishes_and_updates_the_dashboard(stubbed):
    remote, cfg, dash, tmp_path = stubbed
    state = {"sources": {}}
    result = runner.run_source("linkedin", cfg, state, NOW)
    assert result["publish"] == "pushed" and result["new"] == 1
    assert [j["url"] for j in remote_json(remote, tmp_path, "output/all_jobs.json")["jobs"]] == ["https://x/1"]
    local = {j["url"] for j in json.loads((dash / "output/all_jobs.json").read_text())["jobs"]}
    assert local == {"https://x/1", "old-local"}                      # fresh job added, local history kept
    assert state["last_publish"] == runner.iso(NOW)


def test_a_dashboard_folder_problem_is_a_warning_not_a_failed_run(stubbed, monkeypatch):
    remote, cfg, dash, tmp_path = stubbed

    def denied(*args, **kwargs):
        raise PermissionError("[Errno 1] Operation not permitted")
    monkeypatch.setattr(runner, "mirror", denied)
    result = runner.run_source("linkedin", cfg, {"sources": {}}, NOW)
    assert result["publish"] == "pushed"                        # the GitHub side still happened
    assert "PermissionError" in result["dashboard_problem"]
    assert [j["url"] for j in remote_json(remote, tmp_path, "output/all_jobs.json")["jobs"]] == ["https://x/1"]


def test_nothing_new_is_not_published_until_the_heartbeat_is_due(stubbed):
    remote, cfg, dash, tmp_path = stubbed
    state = {"sources": {}}
    runner.run_source("linkedin", cfg, state, NOW)
    quiet = runner.run_source("linkedin", cfg, state, NOW + timedelta(minutes=10))
    assert quiet["publish"] == "no new jobs; not published"
    beat = runner.run_source("linkedin", cfg, state, NOW + timedelta(minutes=40))     # heartbeat is 30 min
    assert beat["publish"] in ("pushed", "nothing")


PORTAL_STUB = '''
import json, os
os.makedirs("output", exist_ok=True)
jobs = [{"url": "https://boards.example/1", "title": "VP Finance", "first_seen": "2026-09-24T11:00:00Z"}]
json.dump({"total": 1, "new_count": 1, "jobs": jobs}, open("output/company_portal_jobs.json", "w"))
json.dump({"acme": "ok"}, open("output/company_portal_status.json", "w"))
doc = json.load(open("output/all_jobs.json"))
doc["jobs"] += jobs
json.dump(doc, open("output/all_jobs.json", "w"))
'''


def test_a_source_with_its_own_script_publishes_its_extra_outputs_and_marks_success(stubbed):
    remote, cfg, dash, tmp_path = stubbed
    seed = tmp_path / "seed"
    (seed / "portal_scraper.py").write_text(PORTAL_STUB)
    sh("git", "add", "-A", cwd=seed)
    sh("git", "commit", "-qm", "portal stub", cwd=seed)
    sh("git", "push", "-q", "origin", "HEAD:main", cwd=seed)
    result = runner.run_source("company_portals", cfg, {"sources": {}}, NOW)
    assert result["publish"] == "pushed" and result["new"] == 1
    assert remote_json(remote, tmp_path, "output/company_portal_status.json") == {"acme": "ok"}
    state = remote_json(remote, tmp_path, "output/scrape_state.json")
    assert state["company_portals"]["last_success"] == runner.iso(NOW)


def test_a_failing_scraper_is_reported_and_publishes_nothing(stubbed):
    remote, cfg, dash, tmp_path = stubbed
    cfg["sources"]["linkedin"]["args"] = ["--definitely-not-a-flag-and-crash"]
    seed = tmp_path / "seed"
    (seed / "scrape_jobs.py").write_text("import sys; print('boom'); sys.exit(3)")
    sh("git", "commit", "-qam", "break", cwd=seed)
    sh("git", "push", "-q", "origin", "HEAD:main", cwd=seed)
    with pytest.raises(RuntimeError, match="exited with code 3"):
        runner.run_source("linkedin", cfg, {"sources": {}}, NOW)
    assert remote_json(remote, tmp_path, "output/all_jobs.json")["jobs"] == []


# --------------------------------------------------------------------------- tick

@pytest.fixture
def home(tmp_path, monkeypatch):
    for name in ("CONFIG_PATH", "STATE_PATH", "LOCK_PATH", "LOG_PATH"):
        monkeypatch.setattr(runner, name, tmp_path / "home" / name.lower())
    monkeypatch.setattr(runner, "network_up", lambda *a, **k: True)
    return tmp_path / "home"


def test_one_failing_source_does_not_stop_the_others_and_is_retried_soon(home, monkeypatch):
    def fake_run(name, cfg, state, now, dry_run=False):
        if name == "linkedin":
            raise RuntimeError("blocked")
        return {"total": 4, "new": 1}
    monkeypatch.setattr(runner, "run_source", fake_run)
    assert runner.main(["tick"]) == 0
    state = runner.load_state(runner.STATE_PATH)["sources"]
    assert state["linkedin"]["last_ok"] is False and "blocked" in state["linkedin"]["last_message"]
    assert state["glassdoor"]["last_ok"] is True


def test_sources_that_are_not_due_are_left_alone(home, monkeypatch):
    calls = []
    monkeypatch.setattr(runner, "run_source", lambda name, *a, **k: calls.append(name) or {"total": 0, "new": 0})
    recent = runner.iso(runner.utcnow() - timedelta(minutes=5))
    runner.write_json_atomic(runner.STATE_PATH, {"sources": {"linkedin": {"last_attempt": recent, "last_ok": True}}})
    runner.main(["tick"])
    assert calls == ["indeed", "glassdoor", "company_portals"]   # linkedin ran 5 minutes ago; the rest never did


def test_due_sources_run_most_urgent_first(home, monkeypatch):
    calls = []
    monkeypatch.setattr(runner, "run_source", lambda name, *a, **k: calls.append(name) or {"total": 0, "new": 0})
    runner.main(["tick"])
    assert calls == ["linkedin", "indeed", "glassdoor", "company_portals"]


def test_a_source_that_comes_due_mid_tick_runs_next_not_last(home, monkeypatch):
    """A 20-minute portal poll must not leave LinkedIn waiting behind everything else."""
    calls = []
    clock = {"now": runner.utcnow()}

    def fake_run(name, cfg, state, now, dry_run=False):
        calls.append(name)
        clock["now"] += timedelta(minutes=25)               # every scrape takes 25 minutes
        return {"total": 0, "new": 0}
    monkeypatch.setattr(runner, "run_source", fake_run)
    monkeypatch.setattr(runner, "utcnow", lambda: clock["now"])
    runner.write_json_atomic(runner.STATE_PATH, {"sources": {
        "linkedin": {"last_attempt": runner.iso(clock["now"] - timedelta(minutes=10)), "last_ok": True},
        "indeed": {"last_attempt": runner.iso(clock["now"] - timedelta(minutes=10)), "last_ok": True},
        "glassdoor": {"last_attempt": runner.iso(clock["now"] - timedelta(minutes=10)), "last_ok": True}}})
    runner.main(["tick"])
    # portals first (the only one due), then linkedin the moment it comes due, each once per tick
    assert calls == ["company_portals", "linkedin", "indeed", "glassdoor"]


def test_run_one_source_by_name_runs_only_that_one(home, monkeypatch):
    calls = []
    monkeypatch.setattr(runner, "run_source", lambda name, *a, **k: calls.append(name) or {"total": 0, "new": 0})
    runner.main(["run", "indeed"])
    assert calls == ["indeed"]


def test_success_marker_is_written_but_never_moved_back(tmp_path):
    (tmp_path / "output").mkdir()
    runner.mark_success(tmp_path, "indeed", NOW)
    assert json.loads((tmp_path / "output/scrape_state.json").read_text())["indeed"]["last_success"] == runner.iso(NOW)
    later = NOW + timedelta(minutes=30)
    runner.write_json_atomic(tmp_path / "output/scrape_state.json", {"linkedin": {"last_success": runner.iso(later)}})
    runner.mark_success(tmp_path, "linkedin", NOW)     # the scraper's own, later stamp wins
    assert json.loads((tmp_path / "output/scrape_state.json").read_text())["linkedin"]["last_success"] == runner.iso(later)


def test_a_second_tick_while_one_is_running_does_nothing(home, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(runner, "run_source", lambda name, *a, **k: calls.append(name) or {"total": 0, "new": 0})
    with runner.single_instance() as got:
        assert got
        assert runner.main(["tick"]) == 0
    assert calls == [] and "another tick is still running" in capsys.readouterr().out


def test_no_network_means_a_quiet_skip_not_a_failure(home, monkeypatch):
    monkeypatch.setattr(runner, "network_up", lambda *a, **k: False)
    monkeypatch.setattr(runner, "run_source", lambda *a, **k: pytest.fail("must not run offline"))
    assert runner.main(["tick"]) == 0
    assert runner.load_state(runner.STATE_PATH)["sources"] == {}      # offline is not counted as an attempt
