"""Regression tests for Murat's Canada/executive runtime personalization."""

from pathlib import Path

import notify
import scrape_jobs
import triage_agent


ROOT = Path(__file__).parent.parent


def test_live_config_targets_canada_and_executive_roles():
    assert scrape_jobs.TARGET_COUNTRIES == {"canada"}
    assert scrape_jobs.REQUIRE_COUNTRY_EVIDENCE_FOR_REMOTE is True
    assert scrape_jobs.is_target_location("Toronto, Ontario, Canada")
    assert not scrape_jobs.is_target_location("New York, NY, United States")
    assert not scrape_jobs.is_target_location("Remote")

    assert scrape_jobs.role_is_relevant("Vice President, Data & AI")
    assert scrape_jobs.role_is_relevant("Director, Finance Transformation")
    assert not scrape_jobs.role_is_relevant("Environmental Toxicologist")
    assert not scrape_jobs.role_is_relevant("Senior Data Scientist")


def test_triage_prompt_uses_executive_taxonomy_and_canada_gate():
    prompt = triage_agent.build_static_prefix("Target executive profile", "")
    lowered = prompt.lower()

    assert "ai-leadership" in lowered
    assert "finance-transformation" in lowered
    assert "based in canada" in lowered
    assert "us-only" in lowered
    assert "toxicology" not in lowered
    assert "environmental-science" not in lowered
    assert "medical-imaging" not in lowered


def test_triage_input_gate_rejects_us_and_off_profile_jobs():
    jobs = [
        {
            "title": "Vice President, Data & AI",
            "company": "Canadian Enterprise",
            "location": "Toronto, Ontario, Canada",
        },
        {
            "title": "Vice President, Data & AI",
            "company": "US Enterprise",
            "location": "New York, NY, United States",
        },
        {
            "title": "Environmental Toxicologist",
            "company": "Canadian Lab",
            "location": "Toronto, Ontario, Canada",
        },
    ]

    assert triage_agent._eligible_jobs(jobs) == jobs[:1]


def test_notify_missing_personalization_warns_and_disables(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(notify, "CONFIG_PATH", str(tmp_path / "missing-config.json"))
    monkeypatch.setattr(notify, "SCORING_PROFILE_PATH", str(tmp_path / "missing-score.json"))
    monkeypatch.setattr(notify, "_CONFIG", None)
    monkeypatch.setattr(notify, "_SCORING_PROFILE", None)

    relevant, stars, score = notify.relevance(
        {
            "title": "Vice President, Data & AI",
            "company": "Example",
            "description": "Enterprise AI transformation",
        }
    )

    assert (relevant, stars, score) == (False, [], 0)
    warning = capsys.readouterr().out
    assert "CONFIGURATION REQUIRED" in warning
    assert "disabled" in warning


def test_dashboard_has_no_legacy_domain_fallbacks():
    dashboard = (ROOT / "triage.html").read_text(encoding="utf-8").lower()

    assert "ispharma" not in dashboard
    assert "microplastics" not in dashboard
    assert "ecotoxicology" not in dashboard
    assert "configuration required: config.json" in dashboard
    assert "configuration required: scoring_profile.json" in dashboard
    assert "istargetlocation(j.location)" in dashboard
    assert "istargetrole(j.title)" in dashboard


def test_notification_module_has_no_example_profile_fallback():
    source = (ROOT / "notify.py").read_text(encoding="utf-8").lower()

    assert "scoring_profile_example_path" not in source
    assert "config_example_path" not in source
    assert "toxicolog" not in source
    assert "microplastic" not in source
