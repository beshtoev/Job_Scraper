import portal_scraper as ps

URL = "https://acme.zohorecruit.com/recruit/Portal.na?iframe=false&digest=abc"


def row(jid, kind, title, date, city):
    return (f'<tr id="zr-joblist-detail_{jid}" class="jobDetailRow"><td><a class=\'jobdetail\' '
            f'href=\'/recruit/PortalDetail.na?iframe=true&digest=abc&jobid={jid}&amp;widgetid=9\'>{kind}</a></td>'
            f'<td>{title}</td><td>{date}</td><td>{city}</td><td><input type="button" value="Apply Now" /></td> </tr>')


LIST = "<table><tr><th>Job Type</th></tr>" + row("11", "Full time", "Director, Finance &amp; Tax", "11/20/2026", "Toronto") \
    + row("12", "Contract", "Deskside Support", "11/22/2026", "Mississauga") + row("11", "Full time", "dup", "", "") + "</table>"
DETAIL = ('x"jobDescriptionDetails":[{"richText":{"fieldLabel":"Job Description","uitype":5501,'
          '"value":"<html><body><span id=\\x22spandesc\\x22><p>Lead FP\\&amp;A \\- 2 days\\/week.<\\/p></span></body></html>"}}]')


def test_parses_rows_and_descriptions(monkeypatch):
    gets = []

    def fake_text(url):
        gets.append(url)
        return LIST if url == URL else DETAIL

    monkeypatch.setattr(ps, "http_text", fake_text)
    monkeypatch.setattr(ps, "_title_relevant", lambda t: t.startswith("Director"))
    jobs = ps.fetch_zoho({"company": "Acme", "ats": "zoho", "slug": "acme", "careers_url": URL})
    assert [j["title"] for j in jobs] == ["Director, Finance & Tax", "Deskside Support"]
    j = jobs[0]
    assert j["url"] == "https://acme.zohorecruit.com/recruit/PortalDetail.na?iframe=true&digest=abc&jobid=11&widgetid=9"
    assert j["location"] == "Toronto" and j["req_id"] == "11" and j["date_posted"] == "2026-11-20"
    assert j["description"] == "Lead FP&A - 2 days/week." and jobs[1]["description"] == ""
    assert gets == [URL, j["url"]]


def test_empty_portal_fails(monkeypatch):
    monkeypatch.setattr(ps, "http_text", lambda url: "<table></table>")
    try:
        ps.fetch_zoho({"company": "Acme", "careers_url": URL})
    except ps.SourceFailure:
        return
    raise AssertionError("expected SourceFailure")
