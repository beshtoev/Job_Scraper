import portal_scraper as ps

URL = ("https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html"
       "?cid=abc-123&ccId=9200523617456_2&type=JS&lang=en_CA")


def req(i, title, internal=False):
    return {"itemID": f"92_{i}", "requisitionTitle": title, "postDate": "2026-11-21T01:02:00",
            "requisitionLocations": [{"address": {"cityName": "Toronto", "countrySubdivisionLevel1": {"codeValue": "ON"}},
                                      "nameCode": {"shortName": " Toronto, ON, CA"}}],
            "customFieldGroup": {"stringFields": [{"nameCode": {"codeValue": "ExternalJobID"}, "stringValue": f"5{i}"}],
                                 "indicatorFields": [{"nameCode": {"codeValue": "InternalPostingFlag"}, "indicatorValue": internal}]}}


def test_pages_and_parses(monkeypatch):
    pages = {0: [req(1, "Director, Finance"), req(2, "Dental Hygienist")], 2: [req(3, "Internal Role", internal=True)]}
    seen = []

    def fake(url):
        seen.append(url)
        if "/92_" in url:
            return {"requisitionDescription": "<p>Lead FP&amp;A.</p>"}
        skip = int(url.split("%24skip=")[1].split("&")[0])
        return {"jobRequisitions": pages.get(skip, []), "meta": {"totalNumber": 3}}

    monkeypatch.setattr(ps, "http_json", fake)
    monkeypatch.setattr(ps, "_title_relevant", lambda t: t.startswith("Director"))
    jobs = ps.fetch_adp({"company": "123Dentist", "ats": "adp", "careers_url": URL})
    assert [j["title"] for j in jobs] == ["Director, Finance", "Dental Hygienist"]
    j = jobs[0]
    assert j["location"] == "Toronto, ON, CA" and j["date_posted"] == "2026-11-21" and j["req_id"] == "51"
    assert "jobId=51" in j["url"] and "cid=abc-123" in j["url"]
    assert j["description"] == "Lead FP&A." and jobs[1]["description"] == ""
    assert "cid=abc-123" in seen[0] and "ccId=9200523617456_2" in seen[0]


def test_missing_ids_fail(monkeypatch):
    try:
        ps.fetch_adp({"careers_url": "https://workforcenow.adp.com/x?lang=en_CA"})
    except ps.SourceFailure:
        return
    raise AssertionError("expected SourceFailure")
