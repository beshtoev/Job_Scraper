"""GitHub fallback workflows stand down only while the local runner's stamp is fresh."""
import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("skip_if_fresh", ROOT / "scripts" / "skip_if_fresh.py")
sif = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sif)


def state(tmp_path, source, minutes_ago):
    t = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    p = tmp_path / "scrape_state.json"
    p.write_text(json.dumps({source: {"last_success": t}}))
    return str(p)


def test_fresh_stamp_stands_down(tmp_path, monkeypatch):
    monkeypatch.delenv("BACKFILL", raising=False)
    assert sif.decide("indeed", 75, state(tmp_path, "indeed", 20))[0] is True


def test_stale_stamp_scrapes(tmp_path, monkeypatch):
    monkeypatch.delenv("BACKFILL", raising=False)
    assert sif.decide("indeed", 75, state(tmp_path, "indeed", 120))[0] is False


def test_another_sources_stamp_does_not_count(tmp_path, monkeypatch):
    monkeypatch.delenv("BACKFILL", raising=False)
    assert sif.decide("indeed", 75, state(tmp_path, "linkedin", 5))[0] is False


def test_missing_or_broken_state_scrapes_never_fails(tmp_path, monkeypatch):
    monkeypatch.delenv("BACKFILL", raising=False)
    assert sif.decide("indeed", 75, str(tmp_path / "nope.json"))[0] is False
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert sif.decide("indeed", 75, str(bad))[0] is False


def test_manual_backfill_always_scrapes(tmp_path, monkeypatch):
    monkeypatch.setenv("BACKFILL", "true")
    assert sif.decide("company_portals", 360, state(tmp_path, "company_portals", 1))[0] is False


def test_writes_github_output(tmp_path, monkeypatch):
    monkeypatch.delenv("BACKFILL", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "output").mkdir()
    state(tmp_path / "output", "indeed", 10)
    out = tmp_path / "gh_out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.setattr("sys.argv", ["skip_if_fresh.py", "indeed", "75"])
    sif.main()
    assert out.read_text() == "skip=true\n"
