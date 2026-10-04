"""ZipRecruiter arrives by email: its site blocks every scheduled scraper, so the
pipeline reads the alert emails its saved searches send. These tests pin down the
alert parser (loose by design - layouts change without notice) and the safety
behaviour: a missing mailbox config or an empty week must never erase results.
"""
import json

import pytest

import scrape_jobs as s
import ziprecruiter_email as zre

TRACK = "https://www.ziprecruiter.com/jobs/acme-corp-123/vp-data?lvk=AbC123&tsid=99"

ALERT_HTML = """
<html><head><style> .job a {color: blue} </style></head><body>
<table><tr><td>
  <a href="https://www.ziprecruiter.com/">ZipRecruiter</a>
  <p>12 new jobs for your search "VP Data"</p>
  <a href="%s"><b>VP, Data &amp; Analytics</b></a>
  <div>Acme Corp</div>
  <div>Toronto, ON</div>
  <div>$180,000 - $220,000/year</div>
  <a href="https://www.ziprecruiter.com/jobs/acme-corp-123/vp-data?lvk=AbC123&amp;tsid=77">1-Click Apply</a>
  <a href="https://www.ziprecruiter.com/jobs/beta-inc-77/director-fpa?lvk=XyZ789">Director of FP&amp;A</a>
  <span>Posted today</span>
  <div>Beta Inc</div>
  <div>Remote (Canada)</div>
  <a href="https://www.ziprecruiter.com/account/email-preferences">Manage alerts</a>
  <a href="https://www.ziprecruiter.com/unsubscribe?u=1">Unsubscribe</a>
</td></tr></table>
</body></html>
""" % TRACK


def test_job_cards_are_parsed_with_company_location_and_salary():
    cards = zre.parse_alert(ALERT_HTML)
    assert [c["title"] for c in cards] == ["VP, Data & Analytics", "Director of FP&A"]
    first, second = cards
    assert first == {"title": "VP, Data & Analytics", "url": TRACK, "company": "Acme Corp",
                     "location": "Toronto, ON", "salary": "$180,000 - $220,000/year"}
    assert second["company"] == "Beta Inc" and second["location"] == "Remote (Canada)"


def test_navigation_links_and_boilerplate_are_not_jobs():
    titles = [c["title"] for c in zre.parse_alert(ALERT_HTML)]
    assert "ZipRecruiter" not in titles          # masthead link
    assert "Manage alerts" not in titles and "Unsubscribe" not in titles
    assert "1-Click Apply" not in titles         # apply button for an existing card


def test_tracking_urls_from_different_emails_still_dedupe_by_lvk():
    other_email_link = "https://click.ziprecruiter.com/t/abc?lvk=AbC123&campaign=2"
    assert s._job_identity(TRACK) == s._job_identity(other_email_link)


def test_missing_or_template_config_reads_as_unconfigured(tmp_path):
    assert zre.load_email_config(tmp_path / "absent.json") is None
    template = tmp_path / "email.json"
    template.write_text(json.dumps({"user": "", "app_password": ""}))
    assert zre.load_email_config(template) is None
    template.write_text(json.dumps({"user": "me@x.com", "app_password": "pw"}))
    cfg = zre.load_email_config(template)
    assert cfg["host"] == "imap.gmail.com" and cfg["since_days"] == 7


def test_scrape_merges_cards_across_emails_and_reports_unparsed_ones(monkeypatch):
    monkeypatch.setattr(zre, "fetch_alerts", lambda cfg, imap=None: [
        ("alert one", ALERT_HTML),
        ("alert two", ALERT_HTML),                   # the same jobs again: a repeat digest
        ("broken", "<html><body><p>new layout!</p></body></html>"),
    ])
    warnings = []
    cards, stats = zre.scrape({"since_days": 7}, log=warnings.append)
    assert [c["title"] for c in cards] == ["VP, Data & Analytics", "Director of FP&A"]
    assert stats == {"emails": 3, "cards": 2, "empty_emails": 1}
    assert "broken" in warnings[0]


def test_unconfigured_mailbox_keeps_previous_results(tmp_path, monkeypatch):
    monkeypatch.setattr(s, "OUTPUT_DIR", str(tmp_path))
    prev = [{"title": "Kept Role", "url": "https://x/1", "company": "A", "location": "Toronto, ON"}]
    (tmp_path / "ziprecruiter_jobs.json").write_text(json.dumps({"jobs": prev}))
    monkeypatch.setattr(zre, "load_email_config", lambda path=None: None)
    jobs = s.scrape_ziprecruiter_email()
    assert [j["title"] for j in jobs] == ["Kept Role"]


def test_a_week_with_no_alert_emails_keeps_previous_results(tmp_path, monkeypatch):
    monkeypatch.setattr(s, "OUTPUT_DIR", str(tmp_path))
    prev = [{"title": "Kept Role", "url": "https://x/1", "company": "A", "location": "Toronto, ON"}]
    (tmp_path / "ziprecruiter_jobs.json").write_text(json.dumps({"jobs": prev}))
    monkeypatch.setattr(zre, "load_email_config",
                        lambda path=None: {"user": "u", "app_password": "p", "since_days": 7})
    monkeypatch.setattr(zre, "scrape", lambda cfg, **kw: ([], {"emails": 0, "cards": 0, "empty_emails": 0}))
    jobs = s.scrape_ziprecruiter_email()
    assert [j["title"] for j in jobs] == ["Kept Role"]


def test_alert_cards_become_schema_jobs_through_the_normal_filters(monkeypatch):
    monkeypatch.setattr(zre, "load_email_config",
                        lambda path=None: {"user": "u", "app_password": "p", "since_days": 7})
    monkeypatch.setattr(zre, "scrape", lambda cfg, **kw: ([
        {"title": "VP, Data & Analytics", "url": TRACK, "company": "Acme Corp",
         "location": "Toronto, ON", "salary": "$180,000 - $220,000/year"},
        {"title": "Forklift Operator", "url": "https://www.ziprecruiter.com/jobs/x?lvk=Q1",
         "company": "Depot", "location": "Toronto, ON", "salary": ""},
    ], {"emails": 1, "cards": 2, "empty_emails": 0}))
    jobs = s.scrape_ziprecruiter_email()
    assert [j["title"] for j in jobs] == ["VP, Data & Analytics"]   # the off-profile card is filtered
    job = jobs[0]
    assert job["ats"] == "ZipRecruiter" and job["salary"] == "$180,000 - $220,000/year"
    assert job["work_arrangement"]                                   # classified, not left blank
