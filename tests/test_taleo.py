import portal_scraper as ps

URL = "https://acme.taleo.net/careersection/2/jobsearch.ftl?lang=en"


def row(no, title, loc):
    return {"jobId": no, "contestNo": f"HEA{no}", "column": [title, f'["{loc}"]', "Dec 10, 2026"],
            "locationsColumns": [1]}


DETAIL = ('<input type="hidden" name="initialHistory" id="initialHistory" value="a!|!'
          '!*!%3Cp%3ELead finance systems%5C: ERP.%3C/p%3E!|!!*!%3Cp%3ELead finance systems%5C: ERP.%3C/p%3E!|!b" />')


def test_pages_and_parses(monkeypatch):
    pages = {1: [row("1", "Director, Finance", "Ontario-Toronto-Head office"), row("2", "Electrician", "Nunavut-Baker Lake")],
             2: [row("3", "Geologist", "Quebec-Val-d'Or")]}
    posts, gets = [], []

    def fake_json(url, payload=None, headers=None):
        assert headers == {"tz": "GMT-05:00"}
        posts.append((url, payload))
        return {"requisitionList": pages.get(payload["pageNo"], []), "pagingData": {"totalCount": 3}}

    def fake_text(url):
        gets.append(url)
        return DETAIL

    monkeypatch.setattr(ps, "http_json", fake_json)
    monkeypatch.setattr(ps, "http_text", fake_text)
    monkeypatch.setattr(ps, "_title_relevant", lambda t: t.startswith("Director"))
    jobs = ps.fetch_taleo({"company": "Acme", "ats": "taleo", "slug": "101", "careers_url": URL})
    assert [j["title"] for j in jobs] == ["Director, Finance", "Electrician", "Geologist"]
    j = jobs[0]
    assert j["location"] == "Ontario-Toronto-Head office" and j["req_id"] == "HEA1"
    assert j["url"] == "https://acme.taleo.net/careersection/2/jobdetail.ftl?job=HEA1&lang=en"
    assert j["description"] == "Lead finance systems: ERP." and jobs[1]["description"] == ""
    assert posts[0][0] == "https://acme.taleo.net/careersection/rest/jobboard/searchjobs?lang=en&portal=101"
    assert len(posts) == 2 and gets == [j["url"]]


def test_portal_number_read_from_page(monkeypatch):
    monkeypatch.setattr(ps, "http_text", lambda url: "var x = {portalNo: '555'};")
    seen = []
    monkeypatch.setattr(ps, "http_json", lambda url, payload=None, headers=None: seen.append(url) or
                        {"requisitionList": [row("9", "Analyst", "Toronto")], "pagingData": {"totalCount": 1}})
    monkeypatch.setattr(ps, "_title_relevant", lambda t: False)
    jobs = ps.fetch_taleo({"company": "Acme", "careers_url": URL})
    assert len(jobs) == 1 and seen[0].endswith("portal=555")
