import portal_scraper as ps


def _capture(monkeypatch):
    seen = []
    monkeypatch.setattr(ps, "http_json", lambda url: seen.append(url) or [])
    return seen


def test_eu_board_uses_eu_api(monkeypatch):
    seen = _capture(monkeypatch)
    ps.fetch_lever({"slug": "coinspaid", "careers_url": "https://jobs.eu.lever.co/coinspaid"})
    assert seen == ["https://api.eu.lever.co/v0/postings/coinspaid?mode=json"]


def test_global_board_unchanged(monkeypatch):
    seen = _capture(monkeypatch)
    ps.fetch_lever({"slug": "electricmind", "careers_url": "https://jobs.lever.co/electricmind"})
    assert seen == ["https://api.lever.co/v0/postings/electricmind?mode=json"]
