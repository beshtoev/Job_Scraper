import json

import portal_scraper as ps

ROW = {"id": 28049, "uuid": "u-1", "description": "Head of Communications and Marketing",
       "externalPositionName": None, "region": "Toronto, Ontario", "minSalary": "120000.00",
       "maxSalary": "170000.00", "salaryType": "salary", "displayExternally": 1, "hirePrivately": 0,
       "createdAt": "2026-10-15 09:59:07",
       "body": json.dumps({"ops": [{"insert": "Lead brand "}, {"attributes": {"bold": True}, "insert": "strategy."}]})}


def test_parses_published_postings(monkeypatch):
    seen = []
    hidden = dict(ROW, id=1, hirePrivately=1)
    monkeypatch.setattr(ps, "http_json", lambda url: seen.append(url) or {"data": [ROW, hidden]})
    jobs = ps.fetch_pushoperations({"company": "Harlo Entertainment", "ats": "pushoperations", "slug": "25748"})
    assert seen == ["https://app-elb.pushoperations.com/api/v2/ats/job-postings?company=25748&status=published&include=position.location"]
    assert len(jobs) == 1
    j = jobs[0]
    assert j["title"] == "Head of Communications and Marketing"
    assert j["url"] == "https://app-elb.pushoperations.com/ats/job-postings/u-1/public"
    assert j["location"] == "Toronto, Ontario" and j["date_posted"] == "2026-10-15" and j["req_id"] == "28049"
    assert j["description"].startswith("Salary: $120,000 - $170,000 per year.") and "Lead brand strategy." in j["description"]


def test_company_id_read_from_page_when_slug_missing(monkeypatch):
    seen = []
    monkeypatch.setattr(ps, "http_text", lambda url: '<div id="app" data-company-id="25748">')
    monkeypatch.setattr(ps, "http_json", lambda url: seen.append(url) or {"data": []})
    ps.fetch_pushoperations({"careers_url": "https://app-elb.pushoperations.com/ats/job-postings/abc"})
    assert "company=25748&" in seen[0]


def test_hourly_pay_not_written_as_salary(monkeypatch):
    monkeypatch.setattr(ps, "http_json", lambda url: {"data": [dict(ROW, salaryType="hourly")]})
    assert not ps.fetch_pushoperations({"slug": "25748"})[0]["description"].startswith("Salary:")
