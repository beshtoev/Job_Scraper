import portal_scraper as ps


def test_parses_feed(monkeypatch):
    seen = []
    feed = {"data": [
        {"id": 7, "title": "Sr. Director, BI", "url": "https://acme.pinpointhq.com/en/jobs/7", "requisition_id": "T1",
         "description": "<div>Lead BI.</div>", "key_responsibilities_header": "Key Responsibilities",
         "key_responsibilities": "<ul><li>Build the team</li></ul>", "skills_knowledge_expertise": None,
         "compensation": "CA$145,000 - CA$165,000 / year", "compensation_visible": True,
         "workplace_type": "hybrid", "location": {"name": "Toronto, ON"}, "department": {"name": "BI"}},
        {"id": 8, "title": "Analyst", "path": "/en/jobs/8", "description": "", "workplace_type": "remote",
         "location": None, "compensation_visible": False}]}
    monkeypatch.setattr(ps, "http_json", lambda url, payload=None, headers=None: seen.append(url) or feed)
    jobs = ps.fetch_pinpoint({"company": "Acme", "ats": "pinpoint", "slug": "acme",
                              "careers_url": "https://acme.org/careers"})
    assert seen == ["https://acme.pinpointhq.com/jobs.json"]
    a, b = jobs
    assert a["title"] == "Sr. Director, BI" and a["location"] == "Toronto, ON" and a["req_id"] == "T1"
    assert "Lead BI." in a["description"] and "Build the team" in a["description"]
    assert "Compensation: CA$145,000 - CA$165,000 / year" in a["description"]
    assert b["url"] == "https://acme.pinpointhq.com/en/jobs/8" and b["location"] == "Remote" and b["req_id"] == "8"
