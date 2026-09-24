"""Salary parsing and salary-band matching in the dashboard, run as real JS.

Pulls parseSalary() and salaryInBand() out of triage.html and executes them
with Node (preinstalled on GitHub's ubuntu runners) or Bun, so these are
behavioural tests, not string checks.

Regressions covered (real postings):
- "$10M" of capital projects was read as $21k/yr (hourly $10).
- "approximately 54,000 employees" was read as $54k/yr.
- "$160\\-200K/year" (markdown-escaped hyphen) next to "Senior HR" was read
  as hourly $160 -> $333k/yr.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

DASHBOARD = (Path(__file__).parent.parent / "triage.html").read_text(encoding="utf-8")
JS_RUNTIME = shutil.which("node") or shutil.which("bun")


def _between(start, end):
    return start + DASHBOARD.split(start, 1)[1].split(end, 1)[0]


PARSER_JS = _between("// ---------- Salary parsing & harmonization ----------", "  function enrich(j) {")
BANDS_JS = _between("const SAL_BANDS = [", "  // `except` skips")


def _run(parse_cases, band_cases):
    if not JS_RUNTIME:
        pytest.skip("needs node or bun to execute the dashboard's JS")
    script = PARSER_JS + "\n" + BANDS_JS + f"""
const P = {json.dumps(parse_cases)};
const B = {json.dumps(band_cases)};
console.log(JSON.stringify({{
  parse: P.map(j => {{ const r = parseSalary(j); return r ? [r.min, r.max] : null; }}),
  bands: B.map(([lo, hi, label]) => salaryInBand(lo, hi, SAL_BANDS.find(b => b.label === label))),
}}));
"""
    out = subprocess.run([JS_RUNTIME, "-e", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


PARSE_CASES = [
    # (job fields, expected [min, max] annual, or None)
    # --- the three real misreads ---
    ({"title": "Chief Financial Officer (CFO) Strategic Business Executive",
      "description": "Compensation commensurate with experience. Oversaw capital projects exceeding $10M.\n"
                     "Demonstrated success managing banking and financing relationships."}, None),
    ({"title": "Vice President, Data Scientist",
      "description": "BMO is one of the largest diversified financial services providers in North America "
                     "with US$1.0 trillion total assets and approximately 54,000 employees as of January 31, 2025. "
                     "About the Role We are seeking a talented and experienced Data Scientist."}, None),
    ({"title": "Vice President, Data Scientist",
      "description": "approximately 54,000 employees as of January 31, 2025. ... "
                     "Base Salary: $120,000-$150,000 CAD (subject to negotiation)"}, [120000, 150000]),
    ({"title": "Director - Total Rewards, HR Transformation & Technology",
      "description": "***Salary: $160\\-200K/year***\n\n\n\n**Senior HR leadership \\| Rewards strategy "
                     "\\| HR technology \\| Enterprise transformation**"}, [160000, 200000]),
    # --- amounts must not be cut in half by the description window ---
    ({"title": "VP, Global Markets",
      "description": "Line of Business: TD Securities Pay Details: $110,000 - $135,000 CAD TD is committed to "
                     "providing fair and equitable compensation opportunities to all colleagues."}, [110000, 135000]),
    # --- a K after a full figure is redundant ---
    ({"title": "Technology Lead",
      "description": "Salary Range: $140,000K/year - $145,000K/year CAD."}, [140000, 145000]),
    # --- edge cases ---
    ({"salary": "$150,000"}, [150000, 150000]),                              # single figure
    ({"salary": "$50 - $60/hr"}, [104000, 124800]),                          # hourly range
    ({"salary": "$62.50/hr"}, [130000, 130000]),                             # hourly, decimals
    ({"salary": "$6,963 - $8,724 per month"}, [83556, 104688]),              # monthly
    ({"title": "Director, Data ($175K – $250K)"}, [175000, 250000]),         # K range
    ({"salary": "$150-200k"}, [150000, 200000]),                             # K on the upper bound only
    ({"salary": "$120,000.00/yr - CA$130,000.00/yr"}, [120000, 130000]),
    ({"title": "Head of FP&A ($80/hr, up to $1,600/week)"}, [166400, 166400]),  # mixed periods: keep the first
    ({"title": "Director of HR", "salary": "$150,000 - $180,000"}, [150000, 180000]),  # "HR" is not hourly
    ({"title": "Director", "description": "Competitive salary, 401k matching and more."}, None),
    ({"title": "Director", "description": "Salary Range: 110,000/110 000 - 180,000/180 000 Job Category: Sales"},
     [110000, 180000]),
    ({"title": "Director", "description": "Salary: $250,000 - $300,000 per year."}, [250000, 300000]),  # ends on a band edge
]

BAND_CASES = [
    # (salMin, salMax, band label, expected)
    (230000, 250000, "$200–250k", True),   # range ending exactly on an edge
    (230000, 250000, "$250–300k", False),
    (100000, 125000, "$100–125k", True),
    (100000, 125000, "$125–150k", False),
    (100000, 125000, "Under $100k", False),
    (250000, 250000, "$250–300k", True),   # single figure: lower edge inclusive
    (250000, 250000, "$200–250k", False),
    (90000, 120000, "Under $100k", True),  # spans two bands
    (90000, 120000, "$100–125k", True),
    (300000, 350000, "$300k+", True),
    (300000, 350000, "$250–300k", False),
    (180000, 350000, "$300k+", True),
    (300000, 300000, "$300k+", True),
    (None, None, "Under $100k", False),    # unlisted never matches a band
]


def test_parse_salary_real_regressions_and_edge_cases():
    got = _run([c for c, _ in PARSE_CASES], [])["parse"]
    for (case, want), have in zip(PARSE_CASES, got):
        assert have == want, (case, have)


def test_salary_band_edges():
    got = _run([], [[lo, hi, label] for lo, hi, label, _ in BAND_CASES])["bands"]
    for (lo, hi, label, want), have in zip(BAND_CASES, got):
        assert have is want, (lo, hi, label, have)
