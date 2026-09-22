"""Geography enrichment: categories, evidence order, and distance ranking (offline)."""

import json

import enrich_geography as geo


def _cfg(**research):
    cfg = geo.load_config()
    cfg["research"] = {**cfg["research"], "enabled": False, **research}
    return cfg


def _enrich(job, cache=None, **research):
    resolver = geo.Resolver(cache or {}, offline=True, research_cfg=_cfg(**research)["research"])
    return geo.enrich_job(job, resolver, _cfg(**research))


def test_categories_follow_toronto_gta_ontario_order():
    toronto = _enrich({"location": "Toronto, Ontario, Canada"})
    north_york = _enrich({"location": "North York, Ontario, Canada"})
    mississauga = _enrich({"location": "Mississauga, ON"})
    ottawa = _enrich({"location": "Ottawa, Ontario, Canada"})
    vancouver = _enrich({"location": "Vancouver, British Columbia, Canada"})

    assert toronto["geo_category"] == north_york["geo_category"] == "Toronto"
    assert mississauga["geo_category"] == "GTA"
    assert ottawa["geo_category"] == "Ontario"
    assert vancouver["geo_label"] == "Other Canada (BC)"
    ranks = [j["geo_rank"] for j in (north_york, toronto, mississauga, ottawa, vancouver)]
    assert ranks == sorted(ranks)


def test_distance_is_measured_from_home_postal_code():
    north_york = _enrich({"location": "North York, Ontario, Canada"})
    oakville = _enrich({"location": "Oakville, Ontario, Canada"})
    assert north_york["geo_distance_km"] < 10
    assert 30 < oakville["geo_distance_km"] < 45
    assert north_york["geo_rank"] < _enrich({"location": "Toronto, Ontario, Canada"})["geo_rank"] + 1


def test_remote_is_its_own_category_and_keeps_province():
    on_remote = _enrich({"location": "Toronto, ON (Remote)"})
    national = _enrich({"location": "Canada", "description": "This is a fully remote role."})
    assert on_remote["geo_label"] == "Canada Remote (ON)"
    assert national["geo_label"] == "Canada Remote (Canada-wide)"
    assert on_remote["work_mode"] == "Remote"


def test_hybrid_toronto_role_stays_in_toronto():
    job = _enrich({"location": "Toronto, Ontario, Canada",
                   "description": "Hybrid: 3 days per week in our downtown office."})
    assert job["work_mode"] == "Hybrid"
    assert job["geo_category"] == "Toronto"


def test_posting_address_beats_city_and_nearest_office_wins():
    cache = {"geocode": {
        "483 bay street, mississauga, on, canada": [43.6532, -79.3832],
        "3 robert speck parkway, mississauga, on, canada": [43.5955, -79.6380],
    }}
    job = _enrich({"location": "Mississauga, ON",
                   "description": "Offices at 483 Bay Street and 3 Robert Speck Parkway."}, cache)
    assert job["geo_source"] == "posting-address"
    assert job["geo_detail"] == "3 Robert Speck Parkway"
    assert job["geo_confidence"] == "high"


def test_street_words_must_be_whole_words():
    assert geo.find_addresses("Deliver the 2030 Plan and a 24 Months Strategy.") == []
    assert geo.find_addresses("Join us at 181 Bay Street, Toronto.") == ["181 Bay Street, Toronto"]


def test_cached_research_places_a_vague_posting():
    cache = {"research": {"acme corp|ontario": {
        "status": "ok", "address": None, "city": "Markham", "province": "ON",
        "remote": False, "confidence": "medium", "source_url": "https://acme.example/careers"}}}
    job = _enrich({"company": "Acme Corp", "location": "Ontario, Canada"}, cache, enabled=True)
    assert job["geo_source"] == "research"
    assert job["geo_category"] == "GTA"
    assert job["geo_city"] == "Markham"


def test_vague_location_falls_back_to_description_then_inference():
    named = _enrich({"location": "Ontario, Canada",
                     "description": "The role is based in our Vaughan head office."})
    bare = _enrich({"location": "Ontario, Canada"})
    assert named["geo_source"] == "description" and named["geo_category"] == "GTA"
    assert bare["geo_source"] == "inferred" and bare["geo_confidence"] == "low"
    assert bare["geo_distance_km"] is None


def test_enrich_files_is_idempotent(tmp_path):
    path = tmp_path / "linkedin_jobs.json"
    path.write_text(json.dumps({"jobs": [{"url": "https://x/1", "location": "Markham, ON"}]}))
    resolver = geo.Resolver({}, offline=True, research_cfg=_cfg()["research"])

    first = geo.enrich_files([str(path)], resolver, _cfg())
    second = geo.enrich_files([str(path)], resolver, _cfg())

    assert first["changed_files"] == 1 and second["changed_files"] == 0
    assert json.loads(path.read_text())["jobs"][0]["geo_category"] == "GTA"


def test_first_run_always_creates_geo_cache_even_with_no_network_calls(tmp_path, monkeypatch):
    """Regression: on a brand-new repo checkout (no prior geo_cache.json), CI's
    commit step does `git add -f output/geo_cache.json` unconditionally. If that
    file is never written — because this run's jobs all resolved via the
    built-in gazetteer with zero geocode/research calls — the commit step fails
    outright on a missing pathspec and the whole scrape is lost. The cache file
    must exist after main() runs at least once, regardless of whether anything
    was actually looked up over the network."""
    jobs_path = tmp_path / "linkedin_jobs.json"
    # A location the gazetteer resolves directly — no geocode() or research()
    # call needed at all, which is exactly the scenario that broke in prod.
    jobs_path.write_text(json.dumps({"jobs": [{"url": "https://x/1", "location": "Toronto, Ontario, Canada"}]}))
    cache_path = tmp_path / "geo_cache.json"
    monkeypatch.setattr(geo, "CACHE_PATH", str(cache_path))
    monkeypatch.setattr(geo, "CONFIG_PATH", str(tmp_path / "nonexistent-config.json"))

    assert not cache_path.exists()
    rc = geo.main(["--offline", str(jobs_path)])

    assert rc == 0
    assert cache_path.exists(), "geo_cache.json must be created on the very first run"

    # A second, later run with nothing new to add must NOT rewrite the file
    # (that's the existing no-op guard) — but it must also not delete it.
    before_mtime = cache_path.stat().st_mtime_ns
    geo.main(["--offline", str(jobs_path)])
    assert cache_path.exists()
    assert cache_path.stat().st_mtime_ns == before_mtime, "unchanged cache must not be rewritten"
