import json

import portal_scraper as portals
from scripts.sync_portal_sources import BLOCKED_DOMAINS, build_inventory


def _entry(ats, slug="example"):
    return {
        "company": "Example Co",
        "archetype": "A",
        "ats": ats,
        "slug": slug,
        "careers_url": f"https://example.test/{slug}",
        "enabled": True,
    }


def test_inventory_keeps_resolved_and_unresolved_sources():
    inventory = build_inventory([
        {"company": "Resolved", "ats": "ashby", "slug": "resolved", "careers_url": "https://jobs.ashbyhq.com/resolved"},
        {"company": "Unknown", "ats": "", "slug": "", "careers_url": ""},
    ], "fixture")
    assert inventory["counts"] == {"companies": 2, "pollable": 1, "unresolved": 1}
    assert inventory["companies"][1]["resolution_status"] == "unresolved"
    assert "linodeobjects.com" in BLOCKED_DOMAINS


def test_ashby_parser_preserves_description_and_remote(monkeypatch):
    monkeypatch.setattr(portals, "http_json", lambda _url: {
        "jobs": [{
            "id": "abc",
            "title": "VP, Data & AI",
            "location": "Remote (Canada)",
            "publishedAt": "2026-09-10T10:00:00Z",
            "isListed": True,
            "isRemote": True,
            "jobUrl": "https://jobs.ashbyhq.com/example/abc",
            "descriptionHtml": "<p>Lead <strong>enterprise AI</strong>.</p>",
        }]
    })
    jobs = portals.fetch_ashby(_entry("ashby"))
    assert jobs[0]["description"] == "Lead enterprise AI."
    assert jobs[0]["is_remote"] is True
    assert jobs[0]["date_posted"] == "2026-09-10"


def test_workable_parser_uses_current_jobs_response_shape(monkeypatch):
    monkeypatch.setattr(portals, "http_json", lambda _url: {
        "jobs": [{
            "title": "Head of Data",
            "shortcode": "ABC",
            "shortlink": "https://apply.workable.com/j/ABC",
            "published_on": "2026-09-09",
            "telecommuting": True,
            "location": {"city": "Toronto", "state": "Ontario", "country": "Canada"},
            "description": "<p>Own data governance.</p>",
        }]
    })
    jobs = portals.fetch_workable(_entry("workable"))
    assert jobs[0]["url"] == "https://apply.workable.com/j/ABC"
    assert jobs[0]["location"] == "Toronto, Ontario, Canada"
    assert jobs[0]["description"] == "Own data governance."


def test_personio_accepts_top_level_array(monkeypatch):
    monkeypatch.setattr(portals, "http_json", lambda _url: [{
        "id": 42,
        "name": "Director, Data Governance",
        "office": "Toronto",
        "offices": ["Toronto"],
        "description": "Governance mandate",
    }])
    jobs = portals.fetch_personio(_entry("personio"))
    assert jobs[0]["req_id"] == "42"
    assert jobs[0]["title"] == "Director, Data Governance"


def test_workday_url_parses_both_public_shapes():
    assert portals.parse_workday_url("https://rbc.wd3.myworkdayjobs.com/RBCGLOBAL1") == (
        "rbc", "wd3", "RBCGLOBAL1")
    assert portals.parse_workday_url("https://wd3.myworkdaysite.com/recruiting/mdlz/External") == (
        "mdlz", "wd3", "External")


def test_blocked_source_is_never_fetched(monkeypatch):
    monkeypatch.setattr(portals, "http_text", lambda _url: (_ for _ in ()).throw(AssertionError("network called")))
    entry = _entry("")
    entry["careers_url"] = "https://linodeobjects.com/phishing"
    try:
        portals.fetch_company(entry, BLOCKED_DOMAINS)
    except portals.SourceFailure as exc:
        assert "blocked" in str(exc)
    else:
        raise AssertionError("blocked source was accepted")


def test_committed_inventory_is_well_formed():
    inventory = json.loads(portals.INVENTORY_PATH.read_text(encoding="utf-8"))
    assert inventory["counts"]["companies"] >= 330
    assert inventory["counts"]["pollable"] >= 190
    assert len(inventory["companies"]) == inventory["counts"]["companies"]
