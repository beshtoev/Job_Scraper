import portal_scraper as ps

BASE = "https://www.xnorthgroup.com/careers"


def card(slug, title, loc):
    return (f'<a href="https://www.xnorthgroup.com/job/{slug}" class="job-card "><h3 class="job-card__title"> {title} </h3>'
            f'<ul class="job-card__list"><li><i></i><span>{loc}</span></li><li><span>Interim</span></li>'
            f'<li><span>6 months</span></li></ul></a>')


PAGES = {BASE: card("interim-cio", "Interim Chief Information Officer", "Toronto, Canada"),
         BASE + "?page=2": card("interim-cfo", "Interim CFO", "Vancouver, Canada")}


def fake(url):
    if url in PAGES:
        return PAGES[url]
    if "/job/" in url:
        return '<article class="content-element article-section__text"><p>Lead IT.</p></article><section class="recent-jobs-section">x</section>'
    return PAGES[BASE]   # past the end: page 1 again


def test_pages_until_nothing_new(monkeypatch):
    monkeypatch.setattr(ps, "http_text", fake)
    monkeypatch.setattr(ps, "_title_relevant", lambda t: "Information" in t)
    jobs = ps.fetch_xnorth({"company": "xNorth Group", "ats": "xnorth", "careers_url": BASE})
    assert [j["title"] for j in jobs] == ["Interim Chief Information Officer", "Interim CFO"]
    j = jobs[0]
    assert j["location"] == "Toronto, Canada" and j["req_id"] == "interim-cio"
    assert j["description"].startswith("Engagement: Interim · 6 months") and "Lead IT." in j["description"]
    assert jobs[1]["description"] == "Engagement: Interim · 6 months"
