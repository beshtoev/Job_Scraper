import portal_scraper as ps

URL = "https://jobs.dayforcehcm.com/en-US/acme/CANDIDATEPORTAL"


def post(pid, title, locs, virtual=False):
    return {"jobPostingId": pid, "jobReqId": pid + 1000, "jobTitle": title, "hasVirtualLocation": virtual,
            "postingStartTimestampUTC": "2026-11-26T06:00:00+00:00", "jobDescription": "<p>Lead &amp; build.</p>",
            "postingLocations": [{"formattedAddress": a} for a in locs]}


def test_pages_with_csrf_and_parses(monkeypatch):
    calls = []
    pages = {0: [post(1, "Director, Finance", ["Greater Toronto Area, ON, Canada", "Calgary, AB, Canada"]),
                 post(2, "Driver", ["Barrie, ON, Canada"])],
             2: [post(3, "Analyst", [], virtual=True), post(1, "dup", [])]}

    def fake_json(url, payload=None, headers=None):
        calls.append((url, payload, headers))
        return {"jobPostings": pages.get(payload["paginationStart"], []), "maxCount": 4}

    monkeypatch.setattr(ps, "_dayforce_session", lambda origin: ("a=1; b=2", "tok"))
    monkeypatch.setattr(ps, "http_json", fake_json)
    jobs = ps.fetch_dayforce({"company": "Acme", "ats": "dayforce", "careers_url": URL})
    assert [j["title"] for j in jobs] == ["Director, Finance", "Driver", "Analyst"]
    j = jobs[0]
    assert j["url"] == "https://jobs.dayforcehcm.com/en-US/acme/CANDIDATEPORTAL/jobs/1"
    assert j["location"] == "Greater Toronto Area, ON, Canada; Calgary, AB, Canada"
    assert j["req_id"] == "1001" and j["date_posted"] == "2026-11-26" and j["description"] == "Lead & build."
    assert jobs[2]["location"] == "Remote"
    url, body, headers = calls[0]
    assert url == "https://jobs.dayforcehcm.com/api/geo/acme/jobposting/search"
    assert body["jobBoardCode"] == "CANDIDATEPORTAL" and body["cultureCode"] == "en-US"
    assert headers == {"X-CSRF-TOKEN": "tok", "Cookie": "a=1; b=2"}
    assert [c[1]["paginationStart"] for c in calls] == [0, 2]


def test_bad_url_fails():
    try:
        ps.fetch_dayforce({"company": "Acme", "careers_url": "https://jobs.dayforcehcm.com/en-US/acme"})
    except ps.SourceFailure:
        return
    raise AssertionError("expected SourceFailure")
