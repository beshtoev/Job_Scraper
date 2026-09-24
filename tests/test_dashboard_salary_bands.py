"""The dashboard offers salary quick-filter bands alongside the salary slider.

Eight multi-select bands, matched by range overlap against each job's annualized
_salMin/_salMax (lower edge inclusive, upper edge exclusive, "$300k+" open-ended).
"""

import re
from pathlib import Path

DASHBOARD = (Path(__file__).parent.parent / "triage.html").read_text(encoding="utf-8")

EXPECTED_BANDS = [
    ("Under $100k", 0, 100000),
    ("$100–125k", 100000, 125000),
    ("$125–150k", 125000, 150000),
    ("$150–175k", 150000, 175000),
    ("$175–200k", 175000, 200000),
    ("$200–250k", 200000, 250000),
    ("$250–300k", 250000, 300000),
    ("$300k+", 300000, None),
]


def _bands_block():
    return DASHBOARD.split("const SAL_BANDS = [")[1].split("];")[0]


def test_eight_salary_bands_with_labels_and_edges_in_order():
    found = re.findall(r"\{\s*label:\s*'([^']+)',\s*min:\s*(\d+),\s*max:\s*(\d+|null)\s*\}", _bands_block())
    parsed = [(label, int(lo), None if hi == "null" else int(hi)) for label, lo, hi in found]
    assert parsed == EXPECTED_BANDS


def test_band_overlap_function_exists_and_drives_the_filter():
    assert "function salaryInBand(salMin, salMax, band)" in DASHBOARD
    body = DASHBOARD.split("function matchesFilters(j, except)")[1].split("\n  }\n")[0]
    assert "salaryInBand(" in body
    assert "filters.salBands" in body


def test_band_buttons_render_in_the_salary_row_and_clear_filters_resets_them():
    salary_row = DASHBOARD.split('<span class="pillgroup-label">Salary</span>')[1].split("</div>\n  </div>")[0]
    assert 'id="filter-salband"' in salary_row
    assert 'id="sal-min"' in salary_row  # the slider is still there
    assert "filters.salBands.clear()" in DASHBOARD.split("$('clear-filters').onclick")[1]
