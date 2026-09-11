"""Regression checks for Murat's deterministic executive scoring profile."""

import json
import re
from pathlib import Path

import notify


PROFILE_PATH = Path(__file__).parent.parent / "scoring_profile.json"


def _profile() -> dict:
    return json.loads(PROFILE_PATH.read_text(encoding="utf-8"))


def test_scoring_profile_rules_compile():
    profile = _profile()

    assert profile["version"] == 1
    assert profile["fit_terms"]
    assert profile["signature_terms"]
    assert profile["poor_fit_terms"]

    for rule in profile["fit_terms"] + profile["poor_fit_terms"]:
        re.compile(rule["pattern"], re.IGNORECASE)
    for pattern in profile["signature_terms"]:
        re.compile(pattern, re.IGNORECASE)


def test_scoring_profile_has_strong_borderline_and_poor_cases():
    cases = _profile()["test_cases"]

    strong = [c for c in cases if c["expected_score_range"][0] >= 80]
    borderline = [
        c for c in cases
        if c["expected_score_range"][0] < 80
        and c["expected_score_range"][1] > 20
    ]
    poor = [c for c in cases if c["expected_score_range"][1] <= 20]

    assert len(strong) >= 5
    assert len(borderline) >= 3
    assert len(poor) >= 5


def test_scoring_profile_cases_match_production_scorer(monkeypatch):
    profile = _profile()
    monkeypatch.setattr(notify, "SCORING_PROFILE_PATH", str(PROFILE_PATH))
    monkeypatch.setattr(notify, "_SCORING_PROFILE", None)

    failures = []
    for case in profile["test_cases"]:
        score = notify._fit(
            case["title"],
            f"{case['company']} {case['description']}",
        )
        low, high = case["expected_score_range"]
        if not low <= score <= high:
            failures.append(
                f"{case['title']!r}: score {score}, expected {low}-{high}"
            )

    assert not failures, "\n".join(failures)
