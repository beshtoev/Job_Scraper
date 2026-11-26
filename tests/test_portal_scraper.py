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


def test_rippling_parser_joins_detail_description_and_tolerates_detail_failure(monkeypatch):
    def fake_http_json(url):
        if url.endswith("/jobs"):
            return [
                {"uuid": "u1", "name": "Director, Data & AI",
                 "department": {"label": "IT"}, "url": "https://ats.rippling.com/x/jobs/u1",
                 "workLocation": {"label": "Toronto, ON"}},
                {"uuid": "u2", "name": "VP Analytics",
                 "department": {"label": "Analytics"}, "url": "https://ats.rippling.com/x/jobs/u2",
                 "workLocation": None},
            ]
        if url.endswith("/jobs/u1"):
            return {"description": {"company": "<p>About us.</p>", "role": "<p>Lead <b>data</b>.</p>"},
                    "createdOn": "2026-09-17T13:13:36-07:00", "workLocations": ["Toronto, ON"]}
        raise RuntimeError("detail down")           # u2's detail call fails

    monkeypatch.setattr(portals, "http_json", fake_http_json)
    monkeypatch.setattr(portals.time, "sleep", lambda s: None)
    jobs = portals.fetch_rippling(_entry("rippling"))
    assert [j["title"] for j in jobs] == ["Director, Data & AI", "VP Analytics"]
    assert jobs[0]["location"] == "Toronto, ON" and jobs[0]["date_posted"].startswith("2026-09-17")
    assert "About us. Lead data." in jobs[0]["description"].replace("  ", " ")
    assert jobs[1]["url"] == "https://ats.rippling.com/x/jobs/u2"   # listing alone still a posting
    assert jobs[1]["description"] == "" and jobs[1]["date_posted"] == ""


def test_phenom_pages_through_site_scoped_search(monkeypatch):
    calls = []

    def fake_http_json(url, payload=None):
        calls.append((url, payload))
        start = payload["from"]
        jobs = [{"jobId": f"{n}WD", "reqId": f"{n}WD", "title": f"Director {n}", "city": "Toronto",
                 "state": "Ontario", "country": "Canada", "postedDate": "2026-11-01T00:00:00.000+0000",
                 "descriptionTeaser": "Lead the practice", "category": "Consulting"}
                for n in range(start, min(start + 2, 3))]
        return {"refineSearch": {"totalHits": 3, "data": {"jobs": jobs}}}

    monkeypatch.setattr(portals, "http_json", fake_http_json)
    monkeypatch.setattr(portals, "PHENOM_PAGE_SIZE", 2)
    entry = {**_entry("phenom"), "careers_url": "https://jobs-ca.example.test/ca/en/search-results"}
    jobs = portals.fetch_phenom(entry)
    assert [job["title"] for job in jobs] == ["Director 0", "Director 1", "Director 2"]
    assert jobs[0]["url"] == "https://jobs-ca.example.test/ca/en/job/0WD"
    assert jobs[0]["location"] == "Toronto, Ontario, Canada"
    assert jobs[0]["date_posted"] == "2026-11-01"
    assert calls[0][0] == "https://jobs-ca.example.test/widgets"
    assert calls[0][1]["country"] == "ca" and calls[0][1]["lang"] == "en_ca"
    assert len(calls) == 2


def test_teamtailor_reads_json_feed_locations_and_remote(monkeypatch):
    def place(locality, region, country):
        return {"@type": "Place", "address": {"addressLocality": locality, "addressRegion": region,
                                              "addressCountry": country}}

    monkeypatch.setattr(portals, "http_json", lambda url, payload=None: {
        "items": [{
            "id": "uuid-1", "title": "Senior Director, Solutions Engineering",
            "url": "https://careers.example.test/jobs/661424-senior-director",
            "date_published": "2026-07-13T16:51:46-04:00", "content_html": "<p>Lead the team</p>",
            "_jobposting": {"jobLocation": [place("Canada", "Canada", "CA"),
                                            place("United States", "United States", "US")],
                            "jobLocationType": "TELECOMMUTE",
                            "identifier": {"name": "Example", "value": "661424"}},
        }]} if url == "https://careers.example.test/jobs.json" else None)
    entry = {**_entry("teamtailor"), "careers_url": "https://careers.example.test/#jobs"}
    [job] = portals.fetch_teamtailor(entry)
    assert job["location"] == "Canada (Remote); United States (Remote)"
    assert job["is_remote"] is True
    assert job["req_id"] == "661424"
    assert job["date_posted"] == "2026-07-13"
    assert job["description"] == "Lead the team"


def test_bamboohr_uses_ats_location_when_location_is_all_null(monkeypatch):
    monkeypatch.setattr(portals, "_title_relevant", lambda title: False)
    monkeypatch.setattr(portals, "http_json", lambda url, payload=None: {"result": [{
        "id": "38", "jobOpeningName": "Director, Data & Technology", "departmentLabel": "Technology",
        "location": {"city": None, "state": None},
        "atsLocation": {"country": "Canada", "state": "Ontario", "province": None, "city": "Toronto"},
        "isRemote": None,
    }]})
    [job] = portals.fetch_bamboohr(_entry("bamboohr", slug="intercept"))
    assert job["location"] == "Toronto, Ontario, Canada"
    assert job["url"] == "https://intercept.bamboohr.com/careers/38"


def test_workday_filtered_link_pages_through_facets(monkeypatch):
    calls = []

    def fake_http_json(url, payload=None):
        calls.append(payload)
        if payload is None:
            return {"jobPostingInfo": {}}
        start = payload["offset"]
        posts = [{"title": f"Analyst {n}", "externalPath": f"/job/Toronto/Analyst_{n}",
                  "locationsText": "Toronto, Ontario, Canada", "bulletFields": [f"R{n}"]}
                 for n in range(start, min(start + 20, 25))]
        return {"total": 25, "jobPostings": posts}

    monkeypatch.setattr(portals, "http_json", fake_http_json)
    monkeypatch.setattr(portals, "_title_relevant", lambda title: False)
    entry = {**_entry("workday"), "careers_url":
             "https://acme.wd1.myworkdayjobs.com/en-US/External?locationCountry=abc123&utm_source=x"}
    jobs = portals.fetch_workday(entry)
    assert len(jobs) == 25
    assert [c["appliedFacets"] for c in calls] == [{"locationCountry": ["abc123"]}] * 2
    assert [c["offset"] for c in calls] == [0, 20]
    assert all(c["searchText"] == "" for c in calls)


def test_workday_unfiltered_link_keeps_keyword_search(monkeypatch):
    calls = []
    monkeypatch.setattr(portals, "http_json",
                        lambda url, payload=None: calls.append(payload) or {"total": 5000, "jobPostings": []})
    portals.fetch_workday({**_entry("workday"), "careers_url": "https://acme.wd1.myworkdayjobs.com/en-US/External"})
    # one size probe, then keyword samples because the board is large
    assert [c["searchText"] for c in calls] == [""] + list(portals.WORKDAY_TERMS)
    assert calls[0]["limit"] == 1
    assert all(c["appliedFacets"] == {} for c in calls)


def test_workday_small_unfiltered_board_is_paged_in_full(monkeypatch):
    calls = []

    def fake_http_json(url, payload=None):
        calls.append(payload)
        start, limit = payload["offset"], payload["limit"]
        posts = [{"title": f"Analyst {n}", "externalPath": f"/job/x/Analyst_{n}", "locationsText": "Toronto"}
                 for n in range(start, min(start + limit, 22))]
        return {"total": 22, "jobPostings": posts}

    monkeypatch.setattr(portals, "http_json", fake_http_json)
    monkeypatch.setattr(portals, "_title_relevant", lambda title: False)
    jobs = portals.fetch_workday({**_entry("workday"), "careers_url": "https://acme.wd1.myworkdayjobs.com/en-US/External"})
    assert len(jobs) == 22
    assert [(c["offset"], c["limit"]) for c in calls] == [(0, 1), (0, 20), (20, 20)]
    assert all(c["searchText"] == "" and c["appliedFacets"] == {} for c in calls)


def test_workday_multi_location_label_is_resolved_from_detail(monkeypatch):
    def fake_http_json(url, payload=None):
        if payload is None:
            return {"jobPostingInfo": {"location": "Toronto, Ontario, Canada",
                                       "additionalLocations": ["Milwaukee, Wisconsin, United States"]}}
        return {"jobPostings": [{"title": "Engineer", "externalPath": "/job/x/Engineer_R1",
                                 "locationsText": "2 Locations", "bulletFields": ["R1"]}]}

    monkeypatch.setattr(portals, "http_json", fake_http_json)
    monkeypatch.setattr(portals, "_title_relevant", lambda title: False)
    monkeypatch.setattr(portals, "WORKDAY_TERMS", ("data",))
    [job] = portals.fetch_workday({**_entry("workday"), "careers_url": "https://acme.wd1.myworkdayjobs.com/en-US/External"})
    assert job["location"] == "Toronto, Ontario, Canada; Milwaukee, Wisconsin, United States"


def test_ashby_joins_secondary_locations(monkeypatch):
    monkeypatch.setattr(portals, "http_json", lambda url, payload=None: {"jobs": [{
        "id": "a1", "title": "Director of Governance", "jobUrl": "https://jobs.ashbyhq.com/x/a1",
        "location": "New York City", "secondaryLocations": [{"location": "San Francisco"}, {"location": "Toronto"}],
    }]})
    [job] = portals.fetch_ashby(_entry("ashby"))
    assert job["location"] == "New York City; San Francisco; Toronto"


def test_successfactors_api_pages_by_date_and_reads_both_locales(monkeypatch):
    calls = []

    def fake_http_json(url, payload=None):
        calls.append(payload)
        assert payload["sortBy"] == "date"
        if payload["locale"] == "fr_CA":
            rows = [{"id": "9", "unifiedStandardTitle": "Directeur", "urlTitle": "Directeur",
                     "mfield1": ["Montreal, QC"], "unifiedStandardStart": "11/3/26"}] if payload["pageNumber"] == 0 else []
            return {"totalJobs": 1, "jobSearchResult": [{"response": r} for r in rows]}
        page = payload["pageNumber"]
        rows = [{"id": str(n), "unifiedStandardTitle": f"Director {n}", "urlTitle": f"Director-{n}",
                 "jobLocationShort": ["Toronto, ON, ON, CAN, M5C 3G7<br/>"], "mfield1": ["Ottawa, ON"],
                 "unifiedStandardStart": "10/2/26"} for n in range(page * 2, min(page * 2 + 2, 3))]
        return {"totalJobs": 3, "jobSearchResult": [{"response": r} for r in rows]}

    monkeypatch.setattr(portals, "http_json", fake_http_json)
    jobs = portals.fetch_successfactors({**_entry("successfactors"), "careers_url": "https://careers.example.test/"})
    assert [j["title"] for j in jobs] == ["Director 0", "Director 1", "Director 2", "Directeur"]
    assert jobs[0]["location"] == "Toronto, ON, ON, CAN, M5C 3G7; Ottawa, ON"
    assert jobs[0]["url"] == "https://careers.example.test/job/Director-0/0-en_US/"
    assert jobs[3]["url"] == "https://careers.example.test/job/Directeur/9-fr_CA/"
    assert jobs[0]["date_posted"] == "2026-10-02"


def test_successfactors_falls_back_to_search_table(monkeypatch):
    row = ('<tr class="data-row"><td class="colTitle"><span class="jobTitle hidden-phone">'
           '<a href="/job/Toronto-Lead-Analyst-ON/1414086433/" class="jobTitle-link">Lead Analyst</a></span></td>'
           '<td class="colLocation"><span class="jobLocation"> Toronto, ON, CA <small class="nobr">+2 more&hellip;</small> </span></td>'
           '<td class="colDate"><span class="jobDate">Nov 18, 2026 </span></td>'
           '<td><span class="jobDepartment">Corporate Functions</span></td></tr>')
    monkeypatch.setattr(portals, "http_json", lambda url, payload=None: {"totalJobs": 0, "jobSearchResult": []})
    monkeypatch.setattr(portals, "http_text", lambda url: f"Results <b>1 - 1</b> of <b>1</b>{row}")
    [job] = portals.fetch_successfactors({**_entry("successfactors"), "careers_url": "https://jobs.example.test/go/All-Jobs/1/"})
    assert job["title"] == "Lead Analyst"
    assert job["location"] == "Toronto, ON, CA"
    assert job["url"] == "https://jobs.example.test/job/Toronto-Lead-Analyst-ON/1414086433/"
    assert job["req_id"] == "1414086433"
    assert job["date_posted"] == "2026-11-18"
    assert job["department"] == "Corporate Functions"


def test_successfactors_table_pages_by_rows_served(monkeypatch):
    def row(i):
        return (f'<tr class="data-row"><td><a href="/job/Toronto-Role-{i}-ON/{1000 + i}/" class="jobTitle-link">Role {i}</a>'
                f'<span class="jobLocation">Toronto, ON, CA</span><span class="jobDate">Nov 18, 2026</span></td></tr>')
    starts = []

    def fake_text(url):
        start = int(url.rsplit("startrow=", 1)[1])
        starts.append(start)
        return f"Results of <b>45</b>" + "".join(row(i) for i in range(start, min(start + 20, 45)))
    monkeypatch.setattr(portals, "http_json", lambda url, payload=None: {"totalJobs": 0, "jobSearchResult": []})
    monkeypatch.setattr(portals, "http_text", fake_text)
    jobs = portals.fetch_successfactors({**_entry("successfactors"), "careers_url": "https://careers.example.test/search/"})
    assert len(jobs) == 45
    assert starts == [0, 20, 40]


def test_webflow_cards(monkeypatch):
    card = ('<a href="/jobs/director-x" class="link-block"><div class="text-block-54">October 26, 2026</div>'
            '<div class="text-block-56">Director, Data &amp; AI</div></div><div class="text-block-55">Hybrid (Toronto)</div></a>'
            '<a href="/jobs/arch" class="link-block"><div class="text-block-54">October 2, 2026</div>'
            '<div class="text-block-56">Architect</div><div class="text-block-55">Hybrid (Multiple Locations)</div></a>')
    monkeypatch.setattr(portals, "http_text", lambda url: card)
    jobs = portals.fetch_webflow({**_entry("webflow"), "careers_url": "https://www.example.test/get-hired",
                                  "default_location": "Toronto, ON, Canada"})
    assert [j["title"] for j in jobs] == ["Director, Data & AI", "Architect"]
    assert jobs[0]["location"] == "Toronto (Hybrid)"
    assert jobs[0]["url"] == "https://www.example.test/jobs/director-x"
    assert jobs[0]["date_posted"] == "2026-10-26"
    assert jobs[1]["location"] == "Toronto, ON, Canada (Hybrid)"


def test_avature_feed_then_detail(monkeypatch):
    feed = ('<rss><channel><link>https://jobs.example.test/careersmarketplace</link><item>'
            '<link>https://jobs.example.test/careersmarketplace/JobDetail/Client-Partner/4269</link></item></channel></rss>')
    detail = ('<script type="application/ld+json">{"@type":"JobPosting","title":"Client Partner, Director",'
              '"datePosted":"2026-11-18","jobLocation":[{"@type":"Place","address":{"addressLocality":"Toronto",'
              '"addressRegion":"ON","addressCountry":"Canada"}}]}</script>')
    monkeypatch.setattr(portals, "http_text", lambda url: feed if "/feed/" in url else detail)
    [job] = portals.fetch_avature({**_entry("avature"), "careers_url": "https://jobs.example.test/en_US/careersmarketplace/SearchJobs"})
    assert job["title"] == "Client Partner, Director"
    assert "Toronto" in job["location"]
    assert job["req_id"] == "4269"
    assert job["url"].endswith("/JobDetail/Client-Partner/4269")


def test_icims_cards_and_paging(monkeypatch):
    card = ('<li class="iCIMS_JobCardItem"><div class="row"><div class="col-xs-6 header left">'
            '<span class="sr-only field-label">Job Locations</span> <span > CA-ON-Greater Toronto Area</span></div>'
            '<div class="col-xs-6 header right"><span class="sr-only field-label">Posted Date</span>'
            '<span title="11/17/2026 12:00 PM"> 1 day ago</span></div><div class="col-xs-12 title">'
            '<a href="https://careers-x.icims.com/jobs/6382/director/job?in_iframe=1" class="iCIMS_Anchor" title="6382 - Director">'
            '<span class="sr-only field-label">Job Title</span> <h3 > Director, Data Science</h3></a></div></div></li>')
    pages = []

    def fake(url):
        pages.append(url)
        return card if url.endswith("pr=0") else ""
    monkeypatch.setattr(portals, "http_text", fake)
    [job] = portals.fetch_icims({**_entry("icims"), "careers_url": "https://careers-x.icims.com/jobs/search"})
    assert job["title"] == "Director, Data Science"
    assert job["location"] == "Greater Toronto Area, ON, Canada"
    assert job["url"] == "https://careers-x.icims.com/jobs/6382/director/job"
    assert job["req_id"] == "6382" and job["date_posted"] == "2026-11-17"
    assert len(pages) == 2


def test_icims_kinaxis_layout(monkeypatch):
    card = ('<li class="iCIMS_JobCardItem"><div class="row"><div class="col-xs-6 header left">'
            '<span class="sr-only field-label">Location</span> <span > CA-Remote</span></div>'
            '<div class="col-xs-12 title"><a href="https://careers-k.icims.com/jobs/36078/head/job?in_iframe=1" class="iCIMS_Anchor" title="x">'
            '<h3 > Head of Operations Research</h3></a></div><dl><dt class="iCIMS_JobHeaderField">Posted Date</dt>'
            '<dd class="iCIMS_JobHeaderData"><span title="11/18/2026 2:22 PM"> 3 hours ago</span></dd></dl></div></li>')
    monkeypatch.setattr(portals, "http_text", lambda url: card if url.endswith("pr=0") else "")
    [job] = portals.fetch_icims({**_entry("icims"), "careers_url": "https://careers-k.icims.com/jobs/search"})
    assert job["location"] == "Remote, Canada"
    assert job["date_posted"] == "2026-11-18"


def test_workday_size_probe_failure_falls_back_to_keywords(monkeypatch):
    calls = []

    def fake_http_json(url, payload=None):
        calls.append(payload)
        if payload["searchText"] == "":
            raise portals.SourceFailure("HTTPError: HTTP Error 422")
        return {"jobPostings": []}

    monkeypatch.setattr(portals, "http_json", fake_http_json)
    portals.fetch_workday({**_entry("workday"), "careers_url": "https://acme.wd1.myworkdayjobs.com/en-US/External"})
    assert [c["searchText"] for c in calls] == [""] + list(portals.WORKDAY_TERMS)
