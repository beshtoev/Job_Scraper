"""LinkedIn search: nested geos searched broadest first, and a short page ends a term.

A 20-minute LinkedIn run took ~15 min (~145 pages at 3-5 s each). Measured 2026-09-26 on a 3h
window: Toronto/GTA and Ontario added 1 relevant job Canada missed, and only where Canada hit
its page cap; no page of fewer than 10 cards was ever followed by more. These tests pin the two
rules the speed-up rests on, with the network faked.
"""
import urllib.parse as up

import pytest

import scrape_jobs as S

CANADA = {"name": "Canada", "location": "Canada", "geoId": "101174742"}
ONTARIO = {"name": "Ontario", "location": "Ontario, Canada", "geoId": ""}
GTA = {"name": "Toronto / GTA", "location": "Greater Toronto Area, Canada", "geoId": ""}


@pytest.fixture
def fake(monkeypatch):
    """results[(location, term)] = total cards the search has; each card is relevant."""
    calls, results = [], {}

    def fetch(url, **_):
        q = dict(up.parse_qsl(up.urlsplit(url).query))
        loc, term, start = q["location"], q["keywords"], int(q["start"])
        calls.append((loc, term, start))
        n = max(0, min(10, results.get((loc, term), 0) - start))
        # Past the last result LinkedIn serves a page with markup but no cards, never an empty
        # body (0 "Empty response" pauses in 100 runner runs and a 311-page measurement).
        return "|".join(f"{loc}:{term}:{start + i}" for i in range(n)) or "<!-- no cards -->"

    def parse(html):
        cards = [c for c in html.strip().split("|") if c and not c.startswith("<!--")]
        return [{"id": c, "company": "Acme", "title": "VP Data", "location": "Toronto, ON",
                 "date_posted": ""} for c in cards], len(cards)

    monkeypatch.setattr(S, "fetch", fetch)
    monkeypatch.setattr(S, "_parse_linkedin_cards", parse)
    monkeypatch.setattr(S.time, "sleep", lambda s: None)
    monkeypatch.setattr(S, "role_is_relevant", lambda t, c: True)
    monkeypatch.setattr(S, "_RATE_LIMITED", False)
    return calls, results


def test_geo_tree_nests_by_location_suffix():
    roots, children = S._linkedin_geo_tree([GTA, ONTARIO, CANADA])
    assert roots == [CANADA]
    assert children[id(CANADA)] == [GTA, ONTARIO]


def test_unrelated_geos_all_stay_roots():
    a = {"name": "A", "location": "Toronto, Ontario", "geoId": ""}
    b = {"name": "B", "location": "Vancouver, British Columbia", "geoId": ""}
    roots, children = S._linkedin_geo_tree([a, b])
    assert roots == [a, b] and children == {}


def test_innermost_container_is_the_parent():
    city = {"name": "Toronto", "location": "Toronto, Ontario, Canada", "geoId": ""}
    roots, children = S._linkedin_geo_tree([CANADA, ONTARIO, city])
    assert roots == [CANADA]
    assert children[id(CANADA)] == [ONTARIO] and children[id(ONTARIO)] == [city]


def test_narrower_geos_are_skipped_when_the_broad_search_is_complete(fake):
    calls, results = fake
    results[("Canada", "VP Data")] = 7
    jobs, raw = S._linkedin_search(["VP Data"], 3600, geos=[GTA, ONTARIO, CANADA], max_results=100)
    assert [c[0] for c in calls] == ["Canada"]          # one page, no Ontario, no GTA
    assert len(jobs) == 7 and raw == 7


def test_a_capped_broad_search_drills_into_the_geos_inside_it(fake):
    calls, results = fake
    results[("Canada", "Digital Transformation")] = 250           # more than 10 pages' worth
    results[("Ontario, Canada", "Digital Transformation")] = 40
    results[("Greater Toronto Area, Canada", "Digital Transformation")] = 3
    jobs, _ = S._linkedin_search(["Digital Transformation"], 3600, geos=[GTA, ONTARIO, CANADA],
                                 max_results=100)
    geos_hit = [c[0] for c in calls]
    assert geos_hit.count("Canada") == 10                           # capped at max_results
    assert "Ontario, Canada" in geos_hit and "Greater Toronto Area, Canada" in geos_hit
    assert len(jobs) == 100 + 40 + 3


def test_a_short_page_ends_the_term_without_an_extra_request(fake):
    calls, results = fake
    results[("Canada", "VP Data")] = 23                             # 10, 10, 3
    S._linkedin_search(["VP Data"], 3600, geos=[CANADA], max_results=100)
    assert [c[2] for c in calls] == [0, 10, 20]


def test_an_exactly_full_last_page_costs_one_empty_confirmation(fake):
    calls, results = fake
    results[("Canada", "VP Data")] = 20
    S._linkedin_search(["VP Data"], 3600, geos=[CANADA], max_results=100)
    assert [c[2] for c in calls] == [0, 10, 20]


def test_every_term_is_searched(fake):
    calls, results = fake
    S._linkedin_search(["VP Data", "Head of AI"], 3600, geos=[GTA, ONTARIO, CANADA], max_results=100)
    assert {c[1] for c in calls} == {"VP Data", "Head of AI"}
