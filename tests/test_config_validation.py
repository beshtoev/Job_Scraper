"""Validation for the fork-owner Phase 1 discovery configuration."""

import json
import re
from pathlib import Path

import scrape_jobs


CONFIG_PATH = Path(__file__).parent.parent / "config.json"


def _config():
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def test_config_is_valid_json_object():
    assert isinstance(_config(), dict)


def test_config_overrides_all_candidate_specific_sections():
    config = _config()
    assert config["keywords"]["include"]
    assert config["keywords"]["exclude"]
    assert config["search_terms"]["linkedin"]
    assert config["search_terms"]["indeed"]
    assert config["locations"]["linkedin"]
    assert config["locations"]["indeed"]
    assert config["location_filter"]["terms"]
    assert config["priority_topics"]["terms"]
    assert config["role_categories"]["terms"]


def test_config_is_canadian_and_not_upstream_environmental_profile():
    text = CONFIG_PATH.read_text(encoding="utf-8").lower()
    assert "toronto" in text
    assert "canada" in text
    assert "toxicolog" not in text
    assert "microplastic" not in text
    assert "california" not in text


def test_dashboard_regexes_compile():
    config = _config()
    patterns = [pattern for _, pattern in config["priority_topics"]["terms"]]
    patterns += [pattern for _, pattern in config["role_categories"]["terms"]]
    for pattern in patterns:
        re.compile(pattern, re.IGNORECASE)


def test_configured_filter_keeps_executive_engineering_but_not_ic_roles():
    assert scrape_jobs.role_is_relevant("VP, Data Engineering") is True
    assert scrape_jobs.role_is_relevant("Head of Enterprise Data Platforms") is True
    assert scrape_jobs.role_is_relevant("Senior Data Scientist") is False
    assert scrape_jobs.role_is_relevant("Machine Learning Engineer") is False
    assert scrape_jobs.role_is_relevant("Senior AI/ML Delivery Manager") is False
