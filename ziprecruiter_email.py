#!/usr/bin/env python3
"""Ingest ZipRecruiter's job-alert emails.

ZipRecruiter's site sits behind an interactive Cloudflare check, so no
scheduled scraper can search it. Its *email alerts* carry the same postings,
so the pipeline reads them instead: keep saved searches on ziprecruiter.com
that send alerts to a mailbox, and this module pulls those alerts over IMAP
(read-only - nothing is marked read, moved or deleted) and turns each job
card in them into a row for the normal filters.

Configuration lives OUTSIDE the repo, in ``~/.job-scraper/email.json``
(``JOB_SCRAPER_HOME`` moves it), and is never committed:

    {
      "user": "you@example.com",
      "app_password": "xxxx xxxx xxxx xxxx",
      "host": "imap.gmail.com",
      "folder": "INBOX",
      "since_days": 7
    }

Gmail requires a per-app password (Google Account -> Security -> 2-Step
Verification -> App passwords); the account's real password will not work.
Only ``user`` and ``app_password`` are required.

Alert layouts change without notice, so the parser is deliberately loose:
any ZipRecruiter-hosted link with a title-looking text starts a job card,
and the text after it is sniffed for a location, a salary and the company.
An email that yields no cards is reported, never silently dropped.
"""
from __future__ import annotations

import email
import email.header
import imaplib
import json
import os
import re
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path

MAX_EMAILS = 50          # newest alerts read per run; a week of alerts is far fewer
CONTEXT_LINES = 8        # text snippets after a title that may hold company/location/salary

# Links in alerts that are navigation, not jobs.
_NOT_A_JOB = re.compile(
    r"unsubscribe|email.?preferences|manage.?alerts?|privacy|terms|account|"
    r"app.?store|play\.google|ziprecruiter\.com/?$|/candidate|/login|/signup|/about",
    re.I,
)
_BOILERPLATE_TEXT = re.compile(
    r"^(view|see|browse|show)\b.*\bjobs?\b|^ziprecruiter$|^new$|^apply$|^1-click apply$|"
    r"^easy apply$|^quick apply$|^posted\b|^ago$|^save\b|^more\b",
    re.I,
)
_LOCATION = re.compile(r"^([Rr]emote\b.*|[A-Za-z][A-Za-z0-9 .'()/-]*,\s*[A-Z]{2}\b.*)$")
_SALARY = re.compile(r"\$\s?\d")


def default_config_path() -> Path:
    home = Path(os.environ.get("JOB_SCRAPER_HOME", Path.home() / ".job-scraper"))
    return home / "email.json"


def load_email_config(path: Path | None = None) -> dict | None:
    """The mailbox config, or None when it is absent or still the template."""
    path = path or default_config_path()
    try:
        cfg = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    if not cfg.get("user") or not cfg.get("app_password"):
        return None
    cfg.setdefault("host", "imap.gmail.com")
    cfg.setdefault("folder", "INBOX")
    cfg.setdefault("since_days", 7)
    return cfg


# --- parsing one alert email --------------------------------------------------------------

def _is_job_link(href: str) -> bool:
    return bool(href) and "ziprecruiter" in href.lower() and not _NOT_A_JOB.search(href)


def _looks_like_title(text: str) -> bool:
    return len(text) >= 6 and not _BOILERPLATE_TEXT.match(text)


class _AlertParser(HTMLParser):
    """Collect (job link, link text) anchors and the loose text that follows each one."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.cards: list[dict] = []
        self._in_anchor = 0
        self._href = ""
        self._anchor_text: list[str] = []
        self._context: list[str] | None = None
        self._skip = 0   # inside <style>/<script>

    def handle_starttag(self, tag, attrs):
        if tag in ("style", "script"):
            self._skip += 1
        elif tag == "a":
            self._in_anchor += 1
            self._href = dict(attrs).get("href") or ""
            self._anchor_text = []

    def handle_endtag(self, tag):
        if tag in ("style", "script") and self._skip:
            self._skip -= 1
        elif tag == "a" and self._in_anchor:
            self._in_anchor -= 1
            text = " ".join(" ".join(self._anchor_text).split())
            if _is_job_link(self._href) and _looks_like_title(text):
                self._context = []
                self.cards.append({"title": text, "url": self._href, "_context": self._context})

    def handle_data(self, data):
        if self._skip:
            return
        text = " ".join(data.split())
        if not text:
            return
        if self._in_anchor:
            self._anchor_text.append(text)
        elif self._context is not None and len(self._context) < CONTEXT_LINES:
            self._context.append(text)


def parse_alert(html: str) -> list[dict]:
    """Job cards {title, url, company, location, salary} found in one alert email."""
    parser = _AlertParser()
    parser.feed(html)
    cards, seen = [], set()
    for raw in parser.cards:
        card = {"title": raw["title"], "url": raw["url"], "company": "", "location": "", "salary": ""}
        for line in raw["_context"]:
            stripped = line.strip(" -•|·")
            if not stripped or _BOILERPLATE_TEXT.match(stripped):
                continue
            if not card["salary"] and _SALARY.search(stripped):
                card["salary"] = stripped
            elif not card["location"] and _LOCATION.match(stripped):
                card["location"] = stripped
            elif not card["company"] and len(stripped) < 80:
                card["company"] = stripped
        key = (card["title"].lower(), card["company"].lower())
        if key not in seen:
            seen.add(key)
            cards.append(card)
    return cards


# --- reading the mailbox ------------------------------------------------------------------

def _best_html(msg) -> str:
    """The HTML body of a message (or its text body as a fallback)."""
    html_part, text_part = "", ""
    parts = msg.walk() if msg.is_multipart() else [msg]
    for part in parts:
        if part.get_content_maintype() == "multipart":
            continue
        try:
            payload = part.get_payload(decode=True) or b""
            body = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        except Exception:
            continue
        if part.get_content_type() == "text/html" and not html_part:
            html_part = body
        elif part.get_content_type() == "text/plain" and not text_part:
            text_part = body
    return html_part or text_part


def _subject(msg) -> str:
    try:
        return str(email.header.make_header(email.header.decode_header(msg.get("Subject", ""))))
    except Exception:
        return "(unreadable subject)"


def fetch_alerts(cfg: dict, imap=None) -> list[tuple[str, str]]:
    """(subject, html) for each recent ZipRecruiter email. Read-only: uses PEEK."""
    own_connection = imap is None
    if own_connection:
        imap = imaplib.IMAP4_SSL(cfg["host"])
        imap.login(cfg["user"], cfg["app_password"])
    try:
        imap.select(cfg.get("folder", "INBOX"), readonly=True)
        since = (datetime.now(timezone.utc) - timedelta(days=cfg.get("since_days", 7)))
        typ, data = imap.search(None, "FROM", '"ziprecruiter"', "SINCE", since.strftime("%d-%b-%Y"))
        ids = data[0].split() if typ == "OK" and data and data[0] else []
        alerts = []
        for mid in ids[-MAX_EMAILS:]:
            typ, parts = imap.fetch(mid, "(BODY.PEEK[])")
            if typ != "OK" or not parts or not parts[0] or not isinstance(parts[0], tuple):
                continue
            msg = email.message_from_bytes(parts[0][1])
            body = _best_html(msg)
            if body:
                alerts.append((_subject(msg), body))
        return alerts
    finally:
        if own_connection:
            try:
                imap.logout()
            except Exception:
                pass


def scrape(cfg: dict, *, log=print, imap=None) -> tuple[list[dict], dict]:
    """All job cards from recent alert emails, plus stats the caller can reason about."""
    alerts = fetch_alerts(cfg, imap=imap)
    stats = {"emails": len(alerts), "cards": 0, "empty_emails": 0}
    cards, seen = [], set()
    for subject, html in alerts:
        found = parse_alert(html)
        if not found:
            stats["empty_emails"] += 1
            log(f"  ⚠️  No job cards recognized in alert {subject!r} - the layout may have changed")
            continue
        for card in found:
            key = (card["title"].lower(), card["company"].lower())
            if key not in seen:
                seen.add(key)
                cards.append(card)
    stats["cards"] = len(cards)
    return cards, stats
