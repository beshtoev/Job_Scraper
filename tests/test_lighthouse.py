import json

import portal_scraper as ps

BOARD = "https://jobs.lighthousehiring.ai/acme/b2db3e5c-8c64-4105-a9b2-afcc1948cc84"
PID = "2b139455-89cd-4806-b185-c9ad910bfb6e"
LD = {"@context": "https://schema.org", "@type": "JobPosting", "title": "Chief Financial Officer",
      "datePosted": "2026-11-20", "description": "<p>Lead finance.</p>", "url": f"{BOARD}/{PID}",
      "jobLocation": {"@type": "Place", "address": {"addressLocality": "Toronto", "addressRegion": "ON", "addressCountry": "CA"}},
      "baseSalary": {"currency": "CAD", "value": {"unitText": "YEAR", "minValue": 225000, "maxValue": 300000}}}


def fake_text(url):
    if url == BOARD:
        return (f'<a class="block" href="/acme/b2db3e5c-8c64-4105-a9b2-afcc1948cc84/{PID}">x</a>'
                f'<a href="/acme/b2db3e5c-8c64-4105-a9b2-afcc1948cc84/{PID}">dup</a>')
    assert url == f"{BOARD}/{PID}"
    return f'<script type="application/ld+json">{json.dumps(LD)}</script>'


def test_board_to_postings(monkeypatch):
    monkeypatch.setattr(ps, "http_text", fake_text)
    jobs = ps.fetch_lighthouse({"company": "Acme", "ats": "lighthouse", "careers_url": BOARD})
    assert len(jobs) == 1
    j = jobs[0]
    assert j["title"] == "Chief Financial Officer" and j["req_id"] == PID and j["date_posted"] == "2026-11-20"
    assert "Toronto" in j["location"] and j["url"] == f"{BOARD}/{PID}"
    assert j["description"].startswith("Salary: CAD $225,000 - $300,000 per year.") and "Lead finance." in j["description"]


def test_empty_board_is_a_failure(monkeypatch):
    monkeypatch.setattr(ps, "http_text", lambda url: "<html></html>")
    try:
        ps.fetch_lighthouse({"careers_url": BOARD})
    except ps.SourceFailure:
        return
    raise AssertionError("expected SourceFailure")
