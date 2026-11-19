"""A "North America" label includes Canada, so a Canada search keeps it."""
import scrape_jobs
from scrape_jobs import is_target_location


def _canada_only(monkeypatch):
    monkeypatch.setattr(scrape_jobs, "TARGET_COUNTRIES", {"canada"})


def test_north_america_labels_pass_for_canada(monkeypatch):
    _canada_only(monkeypatch)
    for label in ["North America", "Remote, North America", "North America (Remote)",
                  "Remote - North America"]:
        assert is_target_location(label), label


def test_more_specific_places_still_win(monkeypatch):
    _canada_only(monkeypatch)
    assert not is_target_location("Austin, Texas, North America")
    assert not is_target_location("United States, North America")
    assert not is_target_location("South America")
    assert not is_target_location("Remote")


def test_north_america_not_accepted_without_canada(monkeypatch):
    monkeypatch.setattr(scrape_jobs, "TARGET_COUNTRIES", {"united kingdom"})
    assert not is_target_location("North America")
