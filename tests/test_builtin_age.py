from datetime import date, timedelta

import portal_scraper as ps


def _card(path, title, age):
    return (f'<a href="{path}" class="x" data-id="job-card-title" >{title}</a>'
            f'<div data-id="company-title"><span>Acme</span></div>'
            f'<span class="tag"><i class="fa-regular fa-clock fs-xs"></i>{age}</span>')


def test_age_text():
    assert ps._builtin_age_days("Yesterday") == 1
    assert ps._builtin_age_days("5 Hours Ago") == 0
    assert ps._builtin_age_days("8 Days Ago") == 8
    assert ps._builtin_age_days("2 Months Ago") == 60
    assert ps._builtin_age_days("a month ago") == 30
    assert ps._builtin_age_days("Remote") is None


def test_old_cards_skipped_fresh_cards_dated(monkeypatch):
    page1 = "".join([_card("/job/a/1", "Director Data", "Reposted Yesterday"),
                     _card("/job/b/2", "VP Finance", "Reposted 2 Months Ago"),
                     _card("/job/c/3", "Head of AI", "8 Days Ago"),
                     _card("/job/d/4", "Chief of Staff", "Easy Apply")])
    monkeypatch.setattr(ps, "http_text", lambda url: page1 if "page=1" in url else "")
    jobs = ps.fetch_builtin_board({"queries": ["director"], "page_cap": 3})
    by_title = {j["title"]: j for j in jobs}
    assert set(by_title) == {"Director Data", "Head of AI", "Chief of Staff"}
    assert by_title["Director Data"]["date_posted"] == (date.today() - timedelta(days=1)).isoformat()
    assert by_title["Chief of Staff"]["date_posted"] == ""  # unreadable age is kept, undated


def test_all_old_is_not_a_source_failure(monkeypatch):
    page1 = _card("/job/b/2", "VP Finance", "3 Months Ago")
    monkeypatch.setattr(ps, "http_text", lambda url: page1 if "page=1" in url else "")
    assert ps.fetch_builtin_board({"queries": ["vp"], "page_cap": 2}) == []
