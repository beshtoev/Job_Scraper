"""Dashboard integration links owned by this fork."""

from pathlib import Path


DASHBOARD_PATH = Path(__file__).parent.parent / "triage.html"


def test_resume_matcher_link_uses_beshtoev_fork():
    dashboard = DASHBOARD_PATH.read_text(encoding="utf-8")

    assert 'href="https://github.com/beshtoev/Resume-Matcher"' in dashboard
    assert 'href="https://github.com/srbhr/Resume-Matcher"' not in dashboard
