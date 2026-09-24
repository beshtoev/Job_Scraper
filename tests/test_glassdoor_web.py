"""Glassdoor web reader: parsing, URLs, retry and coverage behaviour (no network).

Pages are built to mirror the structure of the real search page: results embedded
in Next.js flight chunks, one ``jobview`` per listing.
"""

import json
from datetime import datetime, timezone

import pytest

import glassdoor_web as gw

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)


def fmt(lo, hi, interval):
    return f"{lo}-{hi}/{interval}"


def listing(listing_id, title, *, city="Toronto", age=1, employer="Royal Bank of Canada", short="RBC",
            pay=None, source="EMPLOYER_PROVIDED", period="ANNUAL", expired=False, fragments=None):
    header = {
        "ageInDays": age, "employer": {"name": employer}, "employerNameFromSearch": short,
        "expired": expired, "jobTitleText": title, "locationName": city, "payCurrency": "CAD",
        "payPeriod": period, "salarySource": source,
    }
    if pay:
        header["payPeriodAdjustedPay"] = {"p10": pay[0], "p50": sum(pay) // 2, "p90": pay[1]}
    return {"jobview": {"header": header,
                        "job": {"listingId": listing_id, "jobTitleText": title,
                                "descriptionFragmentsText": fragments or []}}}


def page(listings, total=None):
    """A search page whose result list lives in a flight chunk, like the real site."""
    block = {"jobListings": {"jobListings": listings, "totalJobsCount": total if total is not None else len(listings),
                             "paginationCursors": []}}
    chunk = json.dumps("1:" + json.dumps(block))[1:-1]     # the JS string literal's contents
    return f'<html><head><title>x</title></head><body><script>self.__next_f.push([1,"{chunk}"])</script></body></html>'


class Resp:
    def __init__(self, status=200, text="", headers=None):
        self.status_code, self.text, self.headers = status, text, headers or {}


class FakeSession:
    """Serves queued responses (or a function of the URL) and records every URL asked for."""

    def __init__(self, responses=None, by_url=None):
        self.queue = list(responses or [])
        self.by_url = by_url
        self.urls = []

    def get(self, url):
        self.urls.append(url)
        return self.by_url(url) if self.by_url else self.queue.pop(0)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(gw.time, "sleep", lambda s: None)


# --- URLs and windows -----------------------------------------------------------------

def test_search_urls_match_the_ones_the_site_itself_redirects_to():
    toronto = gw.build_url("vice president data", "C", 2281069, "toronto-on", 3)
    assert toronto == ("https://www.glassdoor.ca/Job/toronto-on-vice-president-data-jobs-"
                       "SRCH_IL.0,10_IC2281069_KO11,30.htm?fromAge=3")
    canada = gw.build_url("vice president", "N", 3, "canada", None)
    assert canada == "https://www.glassdoor.ca/Job/canada-vice-president-jobs-SRCH_IL.0,6_IN3_KO7,21.htm"


def test_recency_window_is_widened_to_a_supported_step_and_never_below_three_days():
    assert gw.age_days_for(1) == 3       # a job posted late yesterday must not fall between runs
    assert gw.age_days_for(24) == 3
    assert gw.age_days_for(72) == 3
    assert gw.age_days_for(168) == 7     # a 7-day backfill stays a 7-day backfill
    assert gw.age_days_for(24 * 20) == 30


def test_configured_places_map_to_glassdoor_locations():
    assert gw.location_key({"location": "Toronto, ON"}) == "toronto"
    assert gw.location_key({"location": "Mississauga, ON"}) == "mississauga"
    assert gw.location_key({"location": "Markham, ON"}) == "canada"      # no id known -> national search
    assert gw.location_key({"location": "Remote"}) == "canada"


# --- parsing ---------------------------------------------------------------------------

def test_listing_is_normalized_to_the_repo_job_schema():
    items, total = gw.parse_results(page([listing(1010172577036, "Vice President, Data & AI",
                                                  pay=(250000, 300000), fragments=["Lead <b>data</b> strategy"])], total=22))
    assert total == 22
    job = gw.to_job(items[0], today=NOW, format_salary=fmt)
    assert job == {
        "company": "RBC", "title": "Vice President, Data & AI", "location": "Toronto, Canada",
        "url": "https://www.glassdoor.ca/job-listing/j?jl=1010172577036",
        "date_posted": "2026-09-23", "description": "Lead  data  strategy".replace("  ", "  "),
        "salary": "250000-300000/yearly", "salary_source": "employer", "salary_currency": "CAD", "ats": "Glassdoor",
    } | {"description": job["description"]}
    assert "<b>" not in job["description"]


def test_hourly_pay_is_labelled_hourly_not_passed_off_as_annual():
    items, _ = gw.parse_results(page([listing(2, "Director, Data", pay=(57, 100), period="HOURLY")]))
    assert gw.to_job(items[0], today=NOW, format_salary=fmt)["salary"] == "57-100/hourly"


def test_glassdoors_own_pay_estimate_is_not_shown_as_the_postings_salary():
    items, _ = gw.parse_results(page([listing(3, "Director, Data", pay=(150000, 200000), source="GLASSDOOR_EST")]))
    assert gw.to_job(items[0], today=NOW, format_salary=fmt)["salary"] == ""


def test_expired_and_untitled_listings_are_dropped_and_national_location_is_kept_plain():
    items, _ = gw.parse_results(page([listing(4, "Director, Data", expired=True),
                                      listing(5, "Director, Data", city="Canada")]))
    jobs = [gw.to_job(i, today=NOW, format_salary=fmt) for i in items]
    assert jobs[0] is None
    assert jobs[1]["location"] == "Canada"


def test_a_challenge_page_is_reported_as_blocked_not_as_zero_jobs():
    with pytest.raises(gw.GlassdoorBlocked):
        gw.parse_results("<html><title>Just a moment...</title></html>")


# --- fetching ----------------------------------------------------------------------------

def test_intermittent_403s_are_retried_until_the_page_comes_back():
    session = FakeSession([Resp(403), Resp(429), Resp(200, "ok")])
    assert gw.fetch(session, "https://x/") == "ok"
    assert len(session.urls) == 3


def test_persistent_403_gives_up_and_says_so():
    session = FakeSession([Resp(403)] * 3)
    with pytest.raises(gw.GlassdoorBlocked, match="403.*after 3 attempts"):
        gw.fetch(session, "https://x/")


def test_other_errors_are_not_retried_and_redirects_are_followed():
    with pytest.raises(gw.GlassdoorBlocked, match="404"):
        gw.fetch(FakeSession([Resp(404)]), "https://x/")
    session = FakeSession([Resp(308, headers={"location": "/moved"}), Resp(200, "landed")])
    assert gw.fetch(session, "https://www.glassdoor.ca/a") == "landed"
    assert session.urls[-1] == "https://www.glassdoor.ca/moved"


# --- end to end ---------------------------------------------------------------------------

def run(session, terms=("director data",), geos=({"location": "Toronto, ON"},), hours_old=24, **kw):
    return gw.scrape(list(terms), list(geos), hours_old, session=session, delay=0, now=NOW, log=lambda m: None,
                     keep_title=kw.get("keep_title", lambda t: True), keep_location=kw.get("keep_location", lambda l: True),
                     format_salary=fmt)


def test_results_from_every_place_are_merged_without_duplicates_and_filters_apply():
    rbc, bank = listing(10, "Director, Data"), listing(11, "Director, Marketing")

    def serve(url):
        return Resp(200, page([rbc, bank]))          # the same two jobs from every place
    jobs, stats = run(FakeSession(by_url=serve), keep_title=lambda t: "Data" in t)
    assert [j["title"] for j in jobs] == ["Director, Data"]
    assert stats["ok"] == 2 and stats["failed"] == 0        # toronto + the national search that is always added
    assert stats["raw"] == 4


def test_a_search_with_more_results_than_one_page_also_searches_narrower_windows():
    def serve(url):
        if "fromAge=3" in url:
            return Resp(200, page([listing(20, "Director, Data", age=2)], total=90))     # truncated
        if "fromAge=1" in url:
            return Resp(200, page([listing(21, "Director, Data", age=0)], total=1))
        raise AssertionError(url)
    session = FakeSession(by_url=serve)
    jobs, stats = run(session, geos=[{"location": "Canada"}])
    # The wide window's job is kept AND the narrower window adds the freshest one.
    assert {j["url"].split("jl=")[1] for j in jobs} == {"20", "21"}
    assert stats["truncated"] == 0
    assert [("fromAge=3" in u, "fromAge=1" in u) for u in session.urls] == [(True, False), (False, True)]


def test_a_search_still_truncated_at_one_day_is_counted_not_hidden():
    jobs, stats = run(FakeSession(by_url=lambda url: Resp(200, page([listing(30, "Director, Data")], total=500))),
                      geos=[{"location": "Canada"}])
    assert stats["truncated"] == 1
    assert len(jobs) == 1


def test_when_every_query_fails_the_stats_say_zero_ok_so_the_caller_can_keep_old_results():
    jobs, stats = run(FakeSession(by_url=lambda url: Resp(403)))
    assert jobs == [] and stats["ok"] == 0 and stats["failed"] == stats["queries"] > 0
    assert "403" in stats["errors"][0]
