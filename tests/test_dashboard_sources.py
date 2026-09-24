"""The dashboard must name every collection channel, including company sites.

Regression: the Source filter listed only the original project's boards, so the
~270 jobs collected from employer career sites and Built In were labelled
"Other" (no filter button) or "Priority", and the dashboard looked as if LinkedIn
and Indeed were the only sources.
"""

from pathlib import Path

DASHBOARD = (Path(__file__).parent.parent / "triage.html").read_text(encoding="utf-8")


def test_company_site_platforms_get_their_own_source_names():
    assert "return 'Built In'" in DASHBOARD
    assert "return 'Executive search'" in DASHBOARD
    assert "return 'Company sites'" in DASHBOARD
    # Greenhouse/Lever/Ashby are employer career sites, not the upstream "Priority" list.
    assert "j.ats === 'Greenhouse' || j.ats === 'Lever' || j.ats === 'Ashby') return 'Priority'" not in DASHBOARD


def test_source_filter_lists_every_channel_and_always_shows_core_sources():
    for name in ("Glassdoor", "ZipRecruiter", "GoogleJobs", "Built In", "Company sites", "Executive search"):
        assert f"'{name}'" in DASHBOARD.split("renderFilterGroup('filter-source'")[1].split(");")[0], name
    # A channel that returns nothing must show as "0", not vanish from the filter.
    assert "CORE_SOURCES.has(v)" in DASHBOARD
