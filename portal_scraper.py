#!/usr/bin/env python3
"""Poll the configured employer portals and Canadian discovery boards."""

from __future__ import annotations

import argparse
import os
from concurrent.futures import ThreadPoolExecutor
import html
import http.client
import json
import random
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent
INVENTORY_PATH = ROOT / "sources" / "company_portals.json"
STATUS_PATH = ROOT / "output" / "company_portal_status.json"
USER_AGENT = "Mozilla/5.0 (compatible; MuratJobDiscovery/1.0; +https://github.com/beshtoev/Job_Scraper)"
TIMEOUT = 20
WORKDAY_TERMS = ("data", "artificial intelligence", "analytics", "transformation", "FP&A", "automation", "governance", "technology")


try:  # the python.org build's default store misses some issuers (Munich Re: CERTIFICATE_VERIFY_FAILED)
    import certifi
    SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    SSL_CONTEXT = ssl.create_default_context()


class SourceFailure(RuntimeError):
    pass


def strip_html(value: str) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"(?i)<\s*br\s*/?\s*>|</\s*(?:p|li|div|section|h\d)\s*>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\s+([.,;:!?])", r"\1", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def _request(url: str, payload: dict | list | None = None, retries: int = 2,
             extra_headers: dict | None = None) -> bytes:
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json,text/html;q=0.9"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    headers.update(extra_headers or {})
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, headers=headers, method="POST" if data is not None else "GET")
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT, context=SSL_CONTEXT) as response:
                return response.read()
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
            if attempt == retries:
                raise SourceFailure(f"{type(exc).__name__}: {exc}") from exc
            time.sleep(1.5 * (attempt + 1))
    raise SourceFailure("request failed")


def http_json(url: str, payload: dict | list | None = None, headers: dict | None = None) -> dict | list:
    try:
        return json.loads(_request(url, payload, extra_headers=headers).decode("utf-8", "replace"))
    except json.JSONDecodeError as exc:
        raise SourceFailure(f"invalid JSON: {exc}") from exc


def http_text(url: str) -> str:
    return _request(url).decode("utf-8", "replace")


def _location(*parts) -> str:
    return ", ".join(dict.fromkeys(str(part).strip() for part in parts if str(part or "").strip()))


def posting(entry: dict, *, title: str, url: str, location: str = "", req_id: str = "",
            date_posted: str = "", description: str = "", remote=None, department: str = "") -> dict:
    return {
        "company": entry.get("company", ""),
        "title": str(title or "").strip(),
        "location": str(location or "").strip(),
        "url": str(url or entry.get("careers_url", "")).strip(),
        "direct_url": str(url or entry.get("careers_url", "")).strip(),
        "req_id": str(req_id or "").strip(),
        "date_posted": str(date_posted or "")[:10],
        "date_seen": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "description": str(description or "").strip(),
        "is_remote": remote,
        "department": str(department or "").strip(),
        "ats": str(entry.get("ats") or "Company portal").title(),
        "source": "company_portal",
        "archetype": entry.get("archetype", ""),
    }


def fetch_greenhouse(entry: dict) -> list[dict]:
    data = http_json(f"https://boards-api.greenhouse.io/v1/boards/{entry['slug']}/jobs?content=true")
    if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
        raise SourceFailure("missing jobs array")
    return [posting(entry, title=item.get("title"), url=item.get("absolute_url"),
                    location=(item.get("location") or {}).get("name", ""),
                    req_id=item.get("requisition_id") or item.get("id"),
                    date_posted=item.get("first_published") or item.get("updated_at"),
                    description=strip_html(item.get("content", ""))) for item in data["jobs"]]


def fetch_ashby(entry: dict) -> list[dict]:
    data = http_json(f"https://api.ashbyhq.com/posting-api/job-board/{entry['slug']}")
    if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
        raise SourceFailure("missing jobs array")
    def places(item):
        # Ashby lists extra sites in secondaryLocations ("New York City" + Toronto).
        names = [item.get("location", "")] + [
            (extra or {}).get("location", "") for extra in item.get("secondaryLocations") or []]
        return "; ".join(dict.fromkeys(str(name).strip() for name in names if str(name or "").strip()))

    return [posting(entry, title=item.get("title"), url=item.get("jobUrl") or item.get("applyUrl"),
                    location=places(item), req_id=item.get("id"),
                    date_posted=item.get("publishedAt", ""),
                    description=strip_html(item.get("descriptionHtml", "")),
                    remote=item.get("isRemote"), department=item.get("department", ""))
            for item in data["jobs"] if item.get("isListed", True)]


def fetch_lever(entry: dict) -> list[dict]:
    # EU-hosted boards (jobs.eu.lever.co/<slug>) are served by api.eu.lever.co; the global
    # API returns nothing for them.
    eu = entry.get("region") == "eu" or ".eu.lever.co" in str(entry.get("careers_url", ""))
    api = "https://api.eu.lever.co" if eu else "https://api.lever.co"
    data = http_json(f"{api}/v0/postings/{entry['slug']}?mode=json")
    if not isinstance(data, list):
        raise SourceFailure("response is not an array")
    out = []
    for item in data:
        cats = item.get("categories") or {}
        description = item.get("descriptionPlain") or "\n".join(
            strip_html(section.get("content", "")) for section in item.get("lists", []) if isinstance(section, dict)
        )
        created = item.get("createdAt")
        if isinstance(created, (int, float)):
            created = datetime.fromtimestamp(created / 1000, timezone.utc).isoformat()
        out.append(posting(entry, title=item.get("text"), url=item.get("hostedUrl") or item.get("applyUrl"),
                           location=cats.get("location", ""), req_id=item.get("id"),
                           date_posted=created or "", description=description,
                           remote="remote" in str(cats.get("location", "")).lower(),
                           department=cats.get("team", "")))
    return out


RIPPLING_DETAIL_CAP = 60  # per-job detail calls per board; past this, listings go out without text


def fetch_rippling(entry: dict) -> list[dict]:
    base = f"https://api.rippling.com/platform/api/ats/v1/board/{entry['slug']}/jobs"
    data = http_json(base)
    if not isinstance(data, list):
        raise SourceFailure("response is not an array")
    out = []
    for index, item in enumerate(data):
        # The list carries no dates, text or pay; each job's detail endpoint does.
        detail = {}
        if index < RIPPLING_DETAIL_CAP:
            try:
                detail = http_json(f"{base}/{item['uuid']}") or {}
            except Exception:
                detail = {}      # the listing alone is still a valid posting
            time.sleep(0.2)
        desc = detail.get("description")
        description = (" ".join(strip_html(str(part)) for part in desc.values())
                       if isinstance(desc, dict) else strip_html(str(desc or "")))
        location = ((item.get("workLocation") or {}).get("label", "")
                    or ", ".join(str(loc) for loc in (detail.get("workLocations") or [])))
        out.append(posting(entry, title=item.get("name"), url=item.get("url"),
                           location=location, req_id=item.get("uuid"),
                           date_posted=detail.get("createdOn", ""), description=description,
                           remote="remote" in location.lower(),
                           department=(item.get("department") or {}).get("label", "")))
    return out


def fetch_workable(entry: dict) -> list[dict]:
    data = http_json(f"https://apply.workable.com/api/v1/widget/accounts/{entry['slug']}?details=true")
    if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
        raise SourceFailure("missing jobs array")
    out = []
    for item in data["jobs"]:
        loc = item.get("location") or {}
        where = _location(loc.get("city"), loc.get("state"), loc.get("country"))
        out.append(posting(entry, title=item.get("title"), url=item.get("shortlink") or item.get("url"),
                           location=where, req_id=item.get("shortcode"),
                           date_posted=item.get("published_on") or item.get("created_at"),
                           description=strip_html(item.get("description", "")),
                           remote=item.get("telecommuting"), department=item.get("department", "")))
    return out


def fetch_smartrecruiters(entry: dict) -> list[dict]:
    data = http_json(f"https://api.smartrecruiters.com/v1/companies/{entry['slug']}/postings?limit=100")
    if not isinstance(data, dict) or not isinstance(data.get("content"), list):
        raise SourceFailure("missing content array")
    out = []
    for item in data["content"]:
        loc = item.get("location") or {}
        title = item.get("name", "")
        description = ""
        if _title_relevant(title):
            try:
                detail = http_json(item.get("ref") or f"https://api.smartrecruiters.com/v1/companies/{entry['slug']}/postings/{item.get('id')}")
                sections = ((detail or {}).get("jobAd") or {}).get("sections") or {}
                description = "\n\n".join(strip_html(v.get("text", "")) for v in sections.values() if isinstance(v, dict))
            except SourceFailure:
                pass
        out.append(posting(entry, title=title,
                           url=f"https://jobs.smartrecruiters.com/{entry['slug']}/{item.get('id', '')}",
                           location=loc.get("fullLocation") or _location(loc.get("city"), loc.get("region"), loc.get("country")),
                           req_id=item.get("refNumber") or item.get("id"), date_posted=item.get("releasedDate"),
                           description=description, remote=loc.get("remote"),
                           department=(item.get("function") or {}).get("label", "")))
    return out


def fetch_bamboohr(entry: dict) -> list[dict]:
    data = http_json(f"https://{entry['slug']}.bamboohr.com/careers/list")
    if not isinstance(data, dict) or not isinstance(data.get("result"), list):
        raise SourceFailure("missing result array")
    out = []
    for item in data["result"]:
        # "location" is often present but all-null while "atsLocation" holds the
        # real place; take the first one that names anything.
        loc = next((cand for cand in (item.get("location"), item.get("atsLocation"))
                    if isinstance(cand, dict) and any(cand.values())), {})
        title, req_id = item.get("jobOpeningName", ""), item.get("id", "")
        description = ""
        if _title_relevant(title):
            try:
                detail = http_json(f"https://{entry['slug']}.bamboohr.com/careers/{req_id}/detail")
                description = strip_html((detail.get("result") or {}).get("jobDescription", "")) if isinstance(detail, dict) else ""
            except SourceFailure:
                pass
        out.append(posting(entry, title=title,
                           url=f"https://{entry['slug']}.bamboohr.com/careers/{req_id}",
                           location=_location(loc.get("city"), loc.get("state") or loc.get("province"), loc.get("country")),
                           req_id=req_id, description=description, remote=item.get("isRemote"),
                           department=item.get("departmentLabel", "")))
    return out


def fetch_recruitee(entry: dict) -> list[dict]:
    data = http_json(f"https://{entry['slug']}.recruitee.com/api/offers/")
    if not isinstance(data, dict) or not isinstance(data.get("offers"), list):
        raise SourceFailure("missing offers array")
    out = []
    for item in data["offers"]:
        translations = item.get("translations") or {}
        translated = next(iter(translations.values()), {}) if isinstance(translations, dict) else {}
        locs = item.get("locations") or []
        where = "; ".join(_location(loc.get("city"), loc.get("state"), loc.get("country")) for loc in locs)
        out.append(posting(entry, title=item.get("title") or item.get("name") or item.get("slug", "").replace("-", " "),
                           url=f"https://{entry['slug']}.recruitee.com/o/{item.get('slug', '')}",
                           location=where or _location(item.get("city"), item.get("state_name"), item.get("country")),
                           req_id=item.get("id") or item.get("position"), date_posted=item.get("published_at"),
                           description=strip_html(translated.get("description") or item.get("description", "")),
                           remote=item.get("remote") or item.get("hybrid")))
    return out


def fetch_personio(entry: dict) -> list[dict]:
    data = http_json(f"https://{entry['slug']}.jobs.personio.com/search.json")
    rows = data if isinstance(data, list) else data.get("positions", []) if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise SourceFailure("response is not a position array")
    return [posting(entry, title=item.get("name"),
                    url=f"https://{entry['slug']}.jobs.personio.com/job/{item.get('id', '')}",
                    location="; ".join(item.get("offices") or [item.get("office", "")]),
                    req_id=item.get("id"), description=strip_html(item.get("description", "")),
                    remote="remote" in str(item.get("office", "")).lower(), department=item.get("department", ""))
            for item in rows]


def parse_workday_url(url: str) -> tuple[str, str, str] | None:
    parts = urllib.parse.urlsplit(url)
    match = re.match(r"^([^.]+)\.(wd\d+)\.myworkdayjobs\.com$", parts.netloc)
    if match:
        segments = [part for part in parts.path.split("/") if part and not re.fullmatch(r"[a-z]{2}(?:-[A-Z]{2})?", part)]
        return (match.group(1), match.group(2), segments[0]) if segments else None
    match = re.match(r"^(wd\d+)\.myworkdaysite\.com$", parts.netloc)
    segments = [part for part in parts.path.split("/") if part]
    if match and len(segments) >= 3 and segments[0] == "recruiting":
        return segments[1], match.group(1), segments[2]
    return None


WORKDAY_MAX_PAGES = 50  # 1,000 jobs per filtered board
_WORKDAY_MULTI_LOCATION_RE = re.compile(r"^\d+ Locations?$", re.I)
_WORKDAY_NON_FACET_PARAMS = {"q", "source", "gh_src", "ref", "lang"}


def workday_facets(url: str) -> dict:
    """Facet filters carried in a Workday careers link, e.g. ?locationCountry=<id>."""
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    return {key: values for key, values in query.items()
            if key not in _WORKDAY_NON_FACET_PARAMS and not key.startswith("utm_") and values}


def fetch_workday(entry: dict) -> list[dict]:
    parsed = parse_workday_url(entry.get("careers_url", ""))
    if not parsed:
        raise SourceFailure("unrecognized Workday URL")
    tenant, wd, site = parsed
    api_root = f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}"
    public_root = f"https://{tenant}.{wd}.myworkdayjobs.com/en-US/{site}"
    seen = {}
    facets = workday_facets(entry.get("careers_url", ""))
    if facets:
        # A filtered link (e.g. ?locationCountry=<Canada id> on a global board):
        # page through every job the filter returns instead of keyword samples.
        offset = 0
        for _ in range(WORKDAY_MAX_PAGES):
            data = http_json(f"{api_root}/jobs", {"appliedFacets": facets, "limit": 20, "offset": offset, "searchText": ""})
            if not isinstance(data, dict) or not isinstance(data.get("jobPostings"), list):
                raise SourceFailure(f"missing jobPostings array for facets {facets!r}")
            for item in data["jobPostings"]:
                external = item.get("externalPath", "")
                if external:
                    seen[external] = item
            offset += len(data["jobPostings"])
            total = data.get("total")
            if not data["jobPostings"] or (isinstance(total, int) and total and offset >= total):
                break
    for term in ([] if facets else WORKDAY_TERMS):
        data = http_json(f"{api_root}/jobs", {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": term})
        if not isinstance(data, dict) or not isinstance(data.get("jobPostings"), list):
            raise SourceFailure(f"missing jobPostings array for {term!r}")
        for item in data["jobPostings"]:
            external = item.get("externalPath", "")
            if external:
                seen[external] = item
    out = []
    for external, item in seen.items():
        title, description, posted_at = item.get("title", ""), "", item.get("postedOn", "")
        location = item.get("locationsText", "")
        # The list shows "2 Locations" for multi-site jobs; the real places are
        # only in the detail record, and the location filter needs them.
        multi_site = bool(_WORKDAY_MULTI_LOCATION_RE.match(location))
        if multi_site or _title_relevant(title):
            try:
                detail = http_json(api_root + external)
                info = (detail or {}).get("jobPostingInfo") or {}
                description = strip_html(info.get("jobDescription", ""))
                posted_at = info.get("startDate") or posted_at
                if multi_site:
                    places = [info.get("location")] + list(info.get("additionalLocations") or [])
                    location = "; ".join(dict.fromkeys(str(p) for p in places if p)) or location
            except SourceFailure:
                pass
        bullets = item.get("bulletFields") or []
        out.append(posting(entry, title=title, url=public_root + external,
                           location=location, req_id=bullets[0] if bullets else "",
                           date_posted=posted_at, description=description))
    return out


GEM_API = "https://jobs.gem.com/api/public/graphql/batch"
GEM_LIST_QUERY = ("query JobBoardList($boardId: String!) { oatsExternalJobPostings(boardId: $boardId) "
                  "{ jobPostings { id extId title locations { name city isoCountry isRemote } "
                  "job { department { name } locationType employmentType } } } "
                  "jobBoardExternal(vanityUrlPath: $boardId) { teamDisplayName } }")


def fetch_gem(entry: dict) -> list[dict]:
    data = http_json(GEM_API, [{"operationName": "JobBoardList", "query": GEM_LIST_QUERY,
                                "variables": {"boardId": entry["slug"]}}])
    if not isinstance(data, list) or not data:
        raise SourceFailure("missing GraphQL reply")
    root = data[0].get("data") or {}
    rows = (root.get("oatsExternalJobPostings") or {}).get("jobPostings")
    if not isinstance(rows, list):
        raise SourceFailure("missing jobPostings array")
    return [posting(entry, title=item.get("title"),
                    url=f"https://jobs.gem.com/{entry['slug']}/{item.get('extId', '')}",
                    location="; ".join(loc.get("name", "") for loc in item.get("locations") or []),
                    req_id=item.get("extId"),
                    remote=(item.get("job") or {}).get("locationType") == "REMOTE",
                    department=((item.get("job") or {}).get("department") or {}).get("name", ""))
            for item in rows]


def fetch_freshteam(entry: dict) -> list[dict]:
    """Freshteam boards (<slug>.freshteam.com/jobs) serve every open job, with its full
    description, at /hire/widgets/jobs.json. A job's location is its branch (office)
    unless it is remote. Added 2026-10-14 for Stacktics."""
    data = http_json(f"https://{entry['slug']}.freshteam.com/hire/widgets/jobs.json")
    if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
        raise SourceFailure("missing jobs array in widget feed")
    branches = {b.get("id"): b for b in data.get("branches") or []}
    roles = {r.get("id"): r.get("name", "") for r in data.get("job_roles") or []}
    out = []
    for item in data["jobs"]:
        if item.get("deleted"):
            continue
        branch = branches.get(item.get("branch_id")) or {}
        office = ", ".join(x for x in (branch.get("city"), branch.get("state"), branch.get("country_code")) if x)
        remote = bool(item.get("remote"))
        where = (item.get("preferred_remote_job_locations") or "").strip()
        title = item.get("title", "")
        out.append(posting(entry, title=title,
                           url=item.get("url") or f"https://{entry['slug']}.freshteam.com/jobs/{item.get('unique_id', '')}",
                           location=("Remote" + (f" ({where})" if where else "")) if remote else office,
                           req_id=str(item.get("id", "")), date_posted=item.get("created_at") or "",
                           description=strip_html(item.get("description", "")) if _title_relevant(title) else "",
                           remote=remote, department=roles.get(item.get("job_role_id"), "")))
    return out


_JAZZ_ITEM = re.compile(r"<li class=\"list-group-item\">(.*?)</li>\s*(?=<li class=\"list-group-item\">|</ul>)", re.I | re.S)
_JAZZ_LINK = re.compile(r"<a\s+href=\"(https?://[^\"]*?/apply/[^\"]+)\"[^>]*>(.*?)</a>", re.I | re.S)
_JAZZ_LOC = re.compile(r"fa-map-marker'?\"?></i>\s*([^<]{2,90})", re.I)
# applytojobs.ca, JazzHR's Canadian product, is a different page (rows are <article class="job-posting">).
_JAZZ_CA_ITEM = re.compile(r"<article class=\"job-posting.*?</article>", re.I | re.S)
_JAZZ_CA_LINK = re.compile(r"<a class=\"text-lg[^\"]*\"\s*href=\"([^\"]+)\"[^>]*>(.*?)</a>", re.I | re.S)
_JAZZ_CA_META = re.compile(r"<div class=\"text-xs[^\"]*\">(.*?)</div>", re.I | re.S)
_JAZZ_CA_BADGE = re.compile(r"rounded-full[^>]*>\s*([a-z -]{3,20})\s*</span>", re.I)


def _jazzhr_description(url: str) -> str:
    try:
        page = http_text(url)
    except SourceFailure:
        return ""
    # JazzHR posting pages carry the text in schema.org JobPosting JSON-LD.
    for raw in re.findall(r"(?is)<script[^>]+application/ld\+json[^>]*>(.*?)</script>", page):
        try:
            data = json.loads(raw.strip())
        except json.JSONDecodeError:
            continue
        for node in _jsonld_nodes(data):
            if node.get("@type") == "JobPosting" and node.get("description"):
                return strip_html(node["description"])
    m = re.search(r"(?is)<div[^>]+class=\"[^\"]*job-description[^\"]*\"[^>]*>(.*?)</div>\s*<", page)
    return strip_html(m.group(1)) if m else ""


def fetch_jazzhr(entry: dict) -> list[dict]:
    """JazzHR boards (<slug>.applytojob.com/apply/, or the Canadian <slug>.applytojobs.ca/).
    No public JSON feed, so the board page is parsed; descriptions come from each posting
    page, fetched only for titles that pass the title check. Ported 2026-10-14 from
    job-capture's ats.JazzHR (added there 2026-09-28 for Mevotech)."""
    slug = (entry.get("slug") or "").strip().strip("/")
    host = urllib.parse.urlsplit(slug).netloc if slug.startswith("http") else (slug if "." in slug else f"{slug}.applytojob.com")
    board = f"https://{host}/" if host.endswith(".applytojobs.ca") else f"https://{host}/apply/"
    page = http_text(board)
    rows = []
    for chunk in _JAZZ_CA_ITEM.findall(page):
        m = _JAZZ_CA_LINK.search(chunk)
        if not m or not strip_html(m.group(2)):
            continue
        path, title = m.group(1).strip(), strip_html(m.group(2))
        badge = (_JAZZ_CA_BADGE.search(chunk).group(1).strip().lower() if _JAZZ_CA_BADGE.search(chunk) else "")
        meta = _JAZZ_CA_META.search(chunk)
        text = strip_html(meta.group(1)) if meta else ""
        if badge and text.lower().startswith(badge):
            text = text[len(badge):].strip()
        bits = [x.strip() for x in text.split(".") if x.strip()]
        rid = re.search(r"/(\d{3,})\s*$", path)
        rows.append(dict(title=title, url=f"https://{host}{path}" if path.startswith("/") else path,
                         location=bits[0] if bits else "", date_posted=bits[-1] if len(bits) >= 3 else "",
                         req_id=rid.group(1) if rid else "", remote=True if "remote" in badge else None))
    if not rows:
        for chunk in _JAZZ_ITEM.findall(page):
            m = _JAZZ_LINK.search(chunk)
            if not m or not strip_html(m.group(2)):
                continue
            url, title = m.group(1).strip(), strip_html(m.group(2))
            loc = _JAZZ_LOC.search(chunk)
            rid = re.search(r"/apply/([A-Za-z0-9]{6,})/", url)
            rows.append(dict(title=title, url=url, location=html.unescape(loc.group(1)).strip() if loc else "",
                             date_posted="", req_id=rid.group(1) if rid else "", remote=None))
    if not rows:
        raise SourceFailure("no openings parsed from the JazzHR board page")
    return [posting(entry, title=r["title"], url=r["url"], location=r["location"], req_id=r["req_id"],
                    date_posted=r["date_posted"], remote=r["remote"],
                    description=_jazzhr_description(r["url"]) if _title_relevant(r["title"]) else "")
            for r in rows]


_OPENCATS_ROW = re.compile(r'(?is)<tr[^>]*>\s*<td>\s*<a href="([^"]*p=showJob[^"]*)"[^>]*>(.*?)</a>\s*</td>\s*<td>(.*?)</td>')


def fetch_opencats(entry: dict) -> list[dict]:
    """OpenCATS, the self-hosted open-source ATS (.../careers/index.php?m=careers&p=showAll):
    a plain table of title links and locations; each job page states the description inline
    after "Description:". Added 2026-10-14 for Vectaur (a recruiting agency)."""
    url = entry.get("careers_url", "")
    base = url.split("index.php")[0] if "index.php" in url else url.rstrip("/") + "/"
    page = http_text(base + "index.php?m=careers&p=showAll")
    out = []
    for href, title, loc in _OPENCATS_ROW.findall(page):
        title = strip_html(title)
        if not title or title.lower().startswith("upload resume"):
            continue
        job_url = base + html.unescape(href).lstrip("/")
        rid = re.search(r"ID=(\d+)", job_url)
        description = ""
        if _title_relevant(title):
            try:
                text = strip_html(http_text(job_url))
                i = text.find("Description:")
                description = text[i + len("Description:"):].strip() if i >= 0 else text
            except SourceFailure:
                pass
        out.append(posting(entry, title=title, url=job_url, location=strip_html(loc),
                           req_id=rid.group(1) if rid else "", description=description))
    if not out and "showJob" not in page:
        raise SourceFailure("no job table on the OpenCATS careers page")
    return out


def _ultipro_board(host: str, tenant: str) -> str:
    """UKG/UltiPro: the board GUID appears only in the redirect from the tenant root."""
    req = urllib.request.Request(f"https://{host}/{tenant}/", headers={
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,*/*", "Accept-Language": "en-CA,en;q=0.9"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=SSL_CONTEXT) as resp:
            final = resp.geturl()
    except Exception:  # noqa: BLE001
        return ""
    m = re.search(r"/JobBoard/([0-9a-fA-F-]{36})", final or "")
    return m.group(1) if m else ""


def _ultipro_description(url: str) -> str:
    """The requisition text is in the detail page's embedded `var opportunity = {...}`
    (the list call's BriefDescription is a company blurb, identical on every posting)."""
    try:
        body = http_text(url)
    except SourceFailure:
        return ""
    m = re.search(r"var\s+opportunity\s*=\s*", body)
    if not m or "{" not in body[m.end():]:
        return ""
    start = body.index("{", m.end())
    try:
        model, _ = json.JSONDecoder().raw_decode(body[start:])
    except ValueError:
        return ""
    return strip_html(model.get("Description") or "")


def fetch_ultipro(entry: dict) -> list[dict]:
    """UKG/UltiPro boards (recruiting.ultipro.com/<TENANT> or the Canadian recruiting.ultipro.ca).
    A tenant with no board on .com is retried on .ca: Canadian tenants live only there.
    Ported 2026-10-19 from job-capture's ats.UltiPro."""
    parts = urllib.parse.urlsplit(entry.get("careers_url", ""))
    host = parts.netloc or "recruiting.ultipro.com"
    tenant = (parts.path.strip("/").split("/") or [""])[0] or entry.get("slug", "")
    guid = _ultipro_board(host, tenant)
    if not guid and host.endswith(".com"):
        host = "recruiting.ultipro.ca"
        guid = _ultipro_board(host, tenant)
    if not guid:
        raise SourceFailure("no board GUID in the UltiPro redirect")
    api = f"https://{host}/{tenant}/JobBoard/{guid}/JobBoardView/LoadSearchResults"
    out, skip, total = [], 0, None
    while True:
        data = http_json(api, {"opportunitySearch": {"Top": 100, "Skip": skip, "QueryString": "",
                                                      "OrderBy": [{"Value": "postedDateDesc"}], "Filters": []}})
        if not isinstance(data, dict) or "opportunities" not in data:
            raise SourceFailure("missing opportunities array")
        batch = data.get("opportunities") or []
        total = data.get("totalCount") or len(batch) if total is None else total
        for item in batch:
            places = []
            for loc in item.get("Locations") or []:
                addr = loc.get("Address") or {}
                bits = [loc.get("LocalizedDescription") or loc.get("LocalizedName") or "", addr.get("City") or "",
                        (addr.get("State") or {}).get("Name") or "", (addr.get("Country") or {}).get("Name") or ""]
                seen, keep = set(), []
                for b in (x.strip() for x in bits):
                    if b and b.lower() not in seen:
                        seen.add(b.lower()); keep.append(b)
                if keep:
                    places.append(", ".join(keep))
            home = any((loc.get("LocalizedDescription") or "").strip().lower().startswith("home")
                       for loc in item.get("Locations") or [])
            title = item.get("Title", "")
            url = f"https://{host}/{tenant}/JobBoard/{guid}/OpportunityDetail?opportunityId={item.get('Id', '')}"
            out.append(posting(entry, title=title, url=url, location="; ".join(places),
                               req_id=str(item.get("RequisitionNumber") or item.get("Id") or ""),
                               date_posted=item.get("PostedDate") or "", remote=True if home else None,
                               description=_ultipro_description(url) if _title_relevant(title) else ""))
        skip += len(batch)
        if not batch or skip >= (total or 0):
            break
    return out


EPAM_API = "https://careers.epam.com/api/jobs/v2/search/careers-i18n"
EPAM_CANADA = "4000741400000172696"


def fetch_epam(entry: dict) -> list[dict]:
    """EPAM Systems' own careers platform. Its jobs API's "careers-i18n" listing spans every
    role; facets=country=<id> keeps Canada (51 roles on 2026-10-19). Records carry full text,
    cities and created_at. Ported 2026-10-19 from job-capture's ats.Epam."""
    def as_list(v):
        if isinstance(v, str):
            try:
                v = json.loads(v)
            except ValueError:
                return []
        return v if isinstance(v, list) else []
    out, frm, total = [], 0, None
    while True:
        query = urllib.parse.urlencode({"lang": "en", "size": "50", "from": str(frm),
                                        "facets": f"country={EPAM_CANADA}"})
        data = (http_json(f"{EPAM_API}?{query}") or {}).get("data")
        if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
            raise SourceFailure("missing jobs array from careers.epam.com")
        total = data.get("total") or 0 if total is None else total
        for item in data["jobs"]:
            cities = [c.get("name", "") for c in as_list(item.get("city")) if isinstance(c, dict)]
            vtype = (item.get("vacancy_type") or "").strip()
            loc = ", ".join([c for c in cities if c] + ["Canada"])
            seo = item.get("seo")
            seo = json.loads(seo) if isinstance(seo, str) else (seo or {})
            out.append(posting(entry, title=item.get("name", ""),
                               url="https://careers.epam.com" + (seo.get("url") or ""),
                               location=f"{loc} ({vtype})" if vtype else loc,
                               req_id=str(item.get("uid") or ""), date_posted=item.get("created_at") or "",
                               description=item.get("text") or "", remote=vtype.lower() == "remote",
                               department=", ".join(str(x) for x in as_list(item.get("job_specialization")))))
        frm += len(data["jobs"])
        if not data["jobs"] or frm >= total:
            break
    return out


def _jsonld_nodes(value):
    if isinstance(value, list):
        for item in value:
            yield from _jsonld_nodes(item)
    elif isinstance(value, dict):
        yield value
        yield from _jsonld_nodes(value.get("@graph", []))


def fetch_generic_html(entry: dict) -> list[dict]:
    body = http_text(entry["careers_url"])
    out = []
    for raw in re.findall(r'(?is)<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', body):
        try:
            data = json.loads(html.unescape(raw))
        except json.JSONDecodeError:
            continue
        for item in _jsonld_nodes(data):
            if item.get("@type") != "JobPosting":
                continue
            org = item.get("hiringOrganization") or {}
            location = item.get("jobLocation") or {}
            location = location[0] if isinstance(location, list) and location else location
            address = location.get("address", {}) if isinstance(location, dict) else {}
            out.append(posting(
                {**entry, "company": org.get("name") or entry.get("company")},
                title=item.get("title"), url=item.get("url") or entry["careers_url"],
                location=_location(address.get("addressLocality"), address.get("addressRegion"), address.get("addressCountry")),
                req_id=(item.get("identifier") or {}).get("value") if isinstance(item.get("identifier"), dict) else "",
                date_posted=item.get("datePosted", ""), description=strip_html(item.get("description", "")),
                remote=str(item.get("jobLocationType", "")).upper() == "TELECOMMUTE"))
    if not out:
        raise SourceFailure(f"no supported adapter for {entry.get('ats') or 'unknown HTML'}; no JobPosting JSON-LD found")
    return out


WEBFLOW_DEFAULT_CLASSES = {"date": "text-block-54", "title": "text-block-56", "location": "text-block-55"}


def fetch_webflow(entry: dict) -> list[dict]:
    """Webflow CMS job board: one <a href="/jobs/<slug>"> card per role, text in classed divs.

    Built for Toast (trytoast.ca/get-hired), which publishes no feed and no JobPosting JSON-LD.
    Class names can be overridden with "webflow": {"title": ..., "date": ..., "location": ...}.
    "Hybrid (Toronto)" becomes "Toronto (Hybrid)"; "Hybrid (Multiple Locations)" falls back to
    the entry's "default_location" (default "Canada") so the location filter can still judge it.
    """
    classes = {**WEBFLOW_DEFAULT_CLASSES, **(entry.get("webflow") or {})}
    url = entry["careers_url"]
    root = re.match(r"https?://[^/]+", url).group(0)
    body = http_text(url)
    out, seen = [], set()
    for match in re.finditer(r'(?is)<a[^>]+href="(/jobs/[^"#?]+)"[^>]*>(.*?)</a>', body):
        path, card = match.group(1), match.group(2)
        if path in seen:
            continue

        def text(cls: str) -> str:
            found = re.search(r'(?is)<div[^>]*class="[^"]*\b' + re.escape(cls) + r'\b[^"]*"[^>]*>(.*?)</div>', card)
            return html.unescape(strip_html(found.group(1))).strip() if found else ""

        title = text(classes["title"])
        if not title or title.lower() in {"test", "job test"}:
            continue
        seen.add(path)
        where = text(classes["location"])
        mode = re.match(r"\s*(remote|hybrid|on-?site|in[- ]office)\s*\((.*)\)\s*$", where, re.I)
        if mode:
            place = mode.group(2).strip()
            if not place or re.search(r"multiple|various|tbd", place, re.I):
                place = entry.get("default_location", "Canada")
            where = f"{place} ({mode.group(1).title()})"
        out.append(posting(entry, title=title, url=root + path, location=where,
                           req_id=path.rsplit("/", 1)[-1], date_posted=_sf_date(text(classes["date"]))))
    if not out:
        raise SourceFailure("no Webflow job cards parsed")
    return out


def _jsonld_postings(entry: dict, body: str, fallback_url: str) -> list[dict]:
    out = []
    for raw in re.findall(r'(?is)<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', body):
        try:
            data = json.loads(html.unescape(raw))
        except json.JSONDecodeError:
            continue
        for item in _jsonld_nodes(data):
            if item.get("@type") != "JobPosting":
                continue
            locs = item.get("jobLocation") or []
            locs = locs if isinstance(locs, list) else [locs]
            places = []
            for loc in locs:
                address = loc.get("address", {}) if isinstance(loc, dict) else {}
                place = _location(address.get("addressLocality"), address.get("addressRegion"), address.get("addressCountry"))
                if place and place not in places:
                    places.append(place)
            out.append(posting(entry, title=html.unescape(item.get("title") or ""), url=item.get("url") or fallback_url,
                               location="; ".join(places), date_posted=str(item.get("datePosted", ""))[:10],
                               description=strip_html(item.get("description", "")),
                               remote=str(item.get("jobLocationType", "")).upper() == "TELECOMMUTE"))
    return out


def fetch_lighthouse(entry: dict) -> list[dict]:
    """Lighthouse Hiring boards (jobs.lighthousehiring.ai/<company slug>/<company uuid>).
    The board page is server-rendered with one <a href="/<slug>/<company uuid>/<posting uuid>"> per
    role; each posting page carries schema.org JobPosting JSON-LD (title, place, datePosted,
    description, baseSalary). Added 2026-11-23 for Secure Digital Markets."""
    board = entry["careers_url"].split("?")[0].rstrip("/")
    root = re.match(r"https?://[^/]+", board).group(0)
    base = urllib.parse.urlsplit(board).path
    paths = list(dict.fromkeys(re.findall(r'href="(' + re.escape(base) + r'/[0-9a-f-]{36})"', http_text(board))))
    if not paths:
        raise SourceFailure("no openings found on the Lighthouse Hiring board page")
    out = []
    for path in paths:
        url = root + path
        body = http_text(url)
        jobs = _jsonld_postings(entry, body, url)
        for raw in re.findall(r'(?is)<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', body):
            try:
                pay = (json.loads(html.unescape(raw)).get("baseSalary") or {})
            except (json.JSONDecodeError, AttributeError):
                continue
            val = pay.get("value") or {} if isinstance(pay, dict) else {}
            if jobs and isinstance(val, dict) and val.get("minValue") and str(val.get("unitText", "")).upper() == "YEAR":
                hi = val.get("maxValue") or val["minValue"]
                jobs[0]["description"] = (f"Salary: {pay.get('currency', '')} ${float(val['minValue']):,.0f} - "
                                          f"${float(hi):,.0f} per year.\n\n" + jobs[0]["description"])
        for j in jobs:
            j["req_id"] = path.rsplit("/", 1)[-1]
        out.extend(jobs)
    return out


_ADP_API = "https://workforcenow.adp.com/mascsr/default/careercenter/public/events/staffing/v1/job-requisitions"


def fetch_adp(entry: dict) -> list[dict]:
    """ADP Workforce Now career centers (workforcenow.adp.com/mascsr/default/mdf/recruitment/
    recruitment.html?cid=<uuid>&ccId=<id>). The page is an app over a public JSON API keyed by the
    same cid/ccId, paged with $top/$skip; descriptions come from each requisition's own call,
    fetched only for titles that pass the title check. Added 2026-11-23 for 123Dentist."""
    qs = urllib.parse.parse_qs(urllib.parse.urlsplit(entry["careers_url"]).query)
    cid, cc = (qs.get("cid") or [""])[0], (qs.get("ccId") or [""])[0]
    lang = (qs.get("lang") or ["en_CA"])[0]
    if not cid or not cc:
        raise SourceFailure("ADP careers_url needs cid and ccId")
    base = {"cid": cid, "ccId": cc, "lang": lang, "locale": lang}
    rows, skip, total = [], 0, None
    while total is None or skip < total:
        data = http_json(f"{_ADP_API}?{urllib.parse.urlencode({**base, '$top': 50, '$skip': skip})}")
        page = data.get("jobRequisitions") or []
        total = int((data.get("meta") or {}).get("totalNumber") or 0)
        if not page:
            break
        rows.extend(page)
        skip += len(page)
    if not rows:
        raise SourceFailure("no requisitions returned by the ADP career center")
    out, seen = [], set()
    for r in rows:
        rid = str(r.get("itemID") or "")
        if not rid or rid in seen:
            continue
        seen.add(rid)
        fields = {f.get("nameCode", {}).get("codeValue"): f.get("stringValue")
                  for f in (r.get("customFieldGroup") or {}).get("stringFields", [])}
        if any(f.get("nameCode", {}).get("codeValue") == "InternalPostingFlag" and f.get("indicatorValue")
               for f in (r.get("customFieldGroup") or {}).get("indicatorFields", [])):
            continue
        places = []
        for loc in r.get("requisitionLocations") or []:
            name = ((loc.get("nameCode") or {}).get("shortName") or "").strip()
            if not name:
                a = loc.get("address") or {}
                name = _location(a.get("cityName"), (a.get("countrySubdivisionLevel1") or {}).get("codeValue"), "CA")
            if name and name not in places:
                places.append(name)
        title = (r.get("requisitionTitle") or "").strip()
        job_id = fields.get("ExternalJobID") or rid
        url = ("https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html?"
               + urllib.parse.urlencode({"cid": cid, "ccId": cc, "jobId": job_id, "lang": lang, "type": "JS"}))
        desc = ""
        if _title_relevant(title):
            try:
                detail = http_json(f"{_ADP_API}/{urllib.parse.quote(rid)}?{urllib.parse.urlencode(base)}")
                desc = strip_html(detail.get("requisitionDescription", ""))
            except SourceFailure:
                desc = ""
        out.append(posting(entry, title=title, url=url, location="; ".join(places), req_id=job_id,
                           date_posted=r.get("postDate") or "", description=desc))
    return out


def _taleo_desc(page: str) -> str:
    """Taleo puts the description in the hidden initialHistory field: '!|!'-separated parts, the
    rich-text ones prefixed '!*!' and URL-encoded (each appears twice)."""
    m = re.search(r'id="initialHistory" value="([^"]*)"', page)
    if not m:
        return ""
    parts = []
    for part in html.unescape(m.group(1)).split("!|!"):
        if part.startswith("!*!"):
            text = urllib.parse.unquote(part[3:]).replace("\\:", ":").replace("\\", "")
            if text not in parts:
                parts.append(text)
    return strip_html(" ".join(parts))


def fetch_taleo(entry: dict) -> list[dict]:
    """Oracle Taleo career sections (<org>.taleo.net/careersection/<code>/jobsearch.ftl). The
    page's portalNo keys a public JSON search (POST /careersection/rest/jobboard/searchjobs),
    25 per page; descriptions come from each jobdetail.ftl page, fetched only for titles that
    pass the title check. The date column is the closing date, so no date_posted. Added
    2026-11-24 for Agnico Eagle."""
    parts = urllib.parse.urlsplit(entry["careers_url"])
    origin = f"{parts.scheme}://{parts.netloc}"
    m = re.search(r"/careersection/([^/]+)/", parts.path)
    code = m.group(1) if m else "2"
    lang = (urllib.parse.parse_qs(parts.query).get("lang") or ["en"])[0]
    portal = str(entry.get("slug") or "")
    if not portal.isdigit():
        m = re.search(r"portalNo:\s*'(\d+)'|portal=(\d+)", http_text(entry["careers_url"]))
        if not m:
            raise SourceFailure("Taleo portal number not found on the career section page")
        portal = m.group(1) or m.group(2)
    api = f"{origin}/careersection/rest/jobboard/searchjobs?lang={lang}&portal={portal}"
    rows, page_no, total = [], 1, None
    while (total is None or len(rows) < total) and page_no <= 40:
        body = {"multilineEnabled": False,
                "sortingSelection": {"sortBySelectionParam": "3", "ascendingSortingOrder": "false"},
                "fieldData": {"fields": {"KEYWORD": "", "LOCATION": ""}, "valid": True},
                "filterSelectionParam": {"searchFilterSelections": []},
                "advancedSearchFiltersSelectionParam": {"searchFilterSelections": []},
                "pageNo": page_no}
        data = http_json(api, body, headers={"tz": "GMT-05:00"})  # Taleo answers 500 without it
        page = data.get("requisitionList") or []
        total = int((data.get("pagingData") or {}).get("totalCount") or 0)
        if not page:
            break
        rows.extend(page)
        page_no += 1
    if not rows:
        raise SourceFailure("no requisitions returned by the Taleo career section")
    out, seen = [], set()
    for r in rows:
        contest = str(r.get("contestNo") or r.get("jobId") or "")
        if not contest or contest in seen:
            continue
        seen.add(contest)
        cols = r.get("column") or []
        title = str(cols[0] if cols else "").strip()
        places = []
        for i in r.get("locationsColumns") or [1]:
            raw = cols[i] if i < len(cols) else ""
            try:
                vals = json.loads(raw) if str(raw).startswith("[") else [raw]
            except json.JSONDecodeError:
                vals = [raw]
            places += [str(v).strip() for v in vals if str(v).strip()]
        url = f"{origin}/careersection/{code}/jobdetail.ftl?job={urllib.parse.quote(contest)}&lang={lang}"
        desc = ""
        if _title_relevant(title):
            try:
                desc = _taleo_desc(http_text(url))
            except SourceFailure:
                desc = ""
        out.append(posting(entry, title=title, url=url, location="; ".join(dict.fromkeys(places)),
                           req_id=contest, description=desc))
    return out


_ZOHO_ROW = re.compile(r'(?is)<tr id="zr-joblist-detail_(\d+)"[^>]*>(.*?)</tr>')
_ZOHO_DESC = re.compile(r'"fieldLabel":"Job Description","uitype":\d+,"value":"((?:[^"\\]|\\.)*)"')


def _zoho_desc(page: str) -> str:
    """The detail page embeds the description as JS-escaped HTML ("\\x22", "\\/", "\\-")."""
    m = _ZOHO_DESC.search(page)
    if not m:
        return ""
    raw = re.sub(r"\\x([0-9a-fA-F]{2})", lambda x: chr(int(x.group(1), 16)), m.group(1))
    raw = raw.replace("\\n", " ").replace("\\t", " ")
    raw = re.sub(r"\\(.)", r"\1", raw)
    return strip_html(raw)


def fetch_zoho(entry: dict) -> list[dict]:
    """Zoho Recruit public portals (<org>.zohorecruit.com/recruit/Portal.na?digest=...): one
    server-rendered table of all openings, columns job type / title / date opened (MM/DD/YYYY)
    / city. Descriptions come from each PortalDetail.na page, fetched only for titles that pass
    the title check. Added 2026-11-26 for Corefactor (staffing firm)."""
    url = entry["careers_url"]
    parts = urllib.parse.urlsplit(url)
    origin = f"{parts.scheme}://{parts.netloc}"
    out, seen = [], set()
    for m in _ZOHO_ROW.finditer(http_text(url)):
        job_id, body = m.group(1), m.group(2)
        if job_id in seen:
            continue
        seen.add(job_id)
        cells = [html.unescape(re.sub(r"<[^>]+>", "", c)).strip() for c in re.findall(r"(?is)<td[^>]*>(.*?)</td>", body)]
        if len(cells) < 2 or not cells[1]:
            continue
        href = re.search(r"href='([^']*PortalDetail\.na[^']*)'", body)
        link = urllib.parse.urljoin(origin + "/", html.unescape(href.group(1))) if href else url
        posted = ""
        dm = re.fullmatch(r"(\d{2})/(\d{2})/(\d{4})", cells[2] if len(cells) > 2 else "")
        if dm:
            posted = f"{dm.group(3)}-{dm.group(1)}-{dm.group(2)}"
        title = cells[1]
        desc = ""
        if _title_relevant(title):
            try:
                desc = _zoho_desc(http_text(link))
            except SourceFailure:
                desc = ""
        out.append(posting(entry, title=title, url=link, location=cells[3] if len(cells) > 3 else "",
                           req_id=job_id, date_posted=posted, description=desc))
    if not out:
        raise SourceFailure("no openings found on the Zoho Recruit portal")
    return out


_XNORTH_CARD = re.compile(r'(?is)<a href="(https?://[^"]+/job/[^"]+)" class="job-card[^"]*">(.*?)</a>')
_XNORTH_TEXT = re.compile(r'(?is)<(?:article|div) class="[^"]*article-section__text[^"]*">(.*?)(?:<div class="[^"]*share|<section[^>]*recent-jobs|<footer)')


def fetch_xnorth(entry: dict) -> list[dict]:
    """xNorth Group careers (xnorthgroup.com/careers?page=N): server-rendered job-card links
    (title in h3, location / contract / duration in the card list). A page past the end serves
    page 1 again, so paging stops when a page adds nothing new. Descriptions come from each
    posting page's article text, fetched only for titles that pass the title check.
    Added 2026-11-23 at Murat's request."""
    base = entry["careers_url"].split("?")[0]
    cards = {}
    for page in range(1, 21):
        body = http_text(base if page == 1 else f"{base}?page={page}")
        new = 0
        for url, card in _XNORTH_CARD.findall(body):
            if url in cards:
                continue
            title = re.search(r'(?is)<h3[^>]*job-card__title[^>]*>(.*?)</h3>', card)
            bits = [strip_html(b) for b in re.findall(r'(?is)<li[^>]*>(.*?)</li>', card)]
            cards[url] = (strip_html(title.group(1)) if title else "", bits)
            new += 1
        if not new:
            break
    if not cards:
        raise SourceFailure("no xNorth job cards parsed")
    out = []
    for url, (title, bits) in cards.items():
        if not title:
            continue
        desc = ""
        if _title_relevant(title):
            m = _XNORTH_TEXT.search(http_text(url))
            desc = strip_html(m.group(1)) if m else ""
        extra = " · ".join(b for b in bits[1:] if b)
        if extra:
            desc = f"Engagement: {extra}\n\n{desc}".strip()
        out.append(posting(entry, title=title, url=url, location=bits[0] if bits else "",
                           req_id=url.rstrip("/").rsplit("/", 1)[-1], description=desc))
    return out


_PUSH_HOST = "https://app-elb.pushoperations.com"
_PUSH_COMPANY_ID = re.compile(r'data-company-id="(\d+)"')


def _push_text(body) -> str:
    """Push Operations stores descriptions as Quill delta JSON ({"ops": [{"insert": ...}]})."""
    try:
        ops = json.loads(body).get("ops", []) if isinstance(body, str) else []
        return "".join(str(o.get("insert", "")) for o in ops if isinstance(o.get("insert"), str)).strip()
    except (ValueError, AttributeError):
        return strip_html(str(body or ""))


def fetch_pushoperations(entry: dict) -> list[dict]:
    """Push Operations ATS (app-elb.pushoperations.com/ats/job-postings/<company uuid>), used by
    hospitality groups. The openings page is a React app over a public JSON endpoint keyed by the
    numeric company id: slug holds that id, or it is read from the page's data-company-id.
    Added 2026-11-23 for Harlo Entertainment."""
    cid = str(entry.get("slug") or "").strip()
    if not cid.isdigit():
        m = _PUSH_COMPANY_ID.search(http_text(entry["careers_url"]))
        if not m:
            raise SourceFailure("no data-company-id on the Push Operations openings page")
        cid = m.group(1)
    data = http_json(f"{_PUSH_HOST}/api/v2/ats/job-postings?company={cid}&status=published&include=position.location")
    rows = data.get("data", []) if isinstance(data, dict) else []
    out = []
    for r in rows:
        if not r.get("displayExternally", 1) or r.get("hirePrivately"):
            continue
        desc = _push_text(r.get("body"))
        lo, hi = r.get("minSalary"), r.get("maxSalary")
        if r.get("salaryType") == "salary" and lo and hi:
            desc = f"Salary: ${float(lo):,.0f} - ${float(hi):,.0f} per year.\n\n{desc}"
        out.append(posting(entry, title=r.get("externalPositionName") or r.get("description") or "",
                           url=f"{_PUSH_HOST}/ats/job-postings/{r.get('uuid')}/public",
                           location=r.get("region") or "", req_id=str(r.get("id") or ""),
                           date_posted=r.get("createdAt") or "", description=desc))
    return out


def fetch_avature(entry: dict) -> list[dict]:
    """Avature portal (e.g. jobs.slalom.com): the SearchJobs RSS feed lists the newest 20 roles
    (it ignores paging and filters) with no location, so each JobDetail page is read for its
    JobPosting JSON-LD. Polled every few hours, the newest 20 cover every new posting."""
    base = re.sub(r"/SearchJobs.*$", "", entry["careers_url"].split("?")[0])
    feed = http_text(base + "/SearchJobs/feed/?listFilterMode=1")
    links = list(dict.fromkeys(html.unescape(u).strip() for u in re.findall(r"(?is)<link>(.*?)</link>", feed)
                               if "JobDetail" in u))
    if not links:
        raise SourceFailure("Avature feed carried no JobDetail links")
    out = []
    for link in links:
        try:
            found = _jsonld_postings(entry, http_text(link), link)
        except Exception:  # noqa: BLE001 - one bad detail page must not drop the rest
            continue
        for item in found:
            item["url"] = link
            item["req_id"] = link.rstrip("/").rsplit("/", 1)[-1]
        out.extend(found)
    if not out:
        raise SourceFailure("no Avature job details parsed")
    return out


ICIMS_MAX_PAGES = 40


def _icims_place(raw: str) -> str:
    """'CA-ON-Greater Toronto Area' -> 'Greater Toronto Area, ON, Canada'; several sites joined by '; '."""
    places = []
    for part in re.split(r"\s*\|\s*|\s{2,}|\n", raw.strip()):
        bits = [b.strip() for b in part.split("-", 2)]
        country = {"CA": "Canada", "US": "United States"}.get(bits[0].upper(), bits[0]) if len(bits[0]) == 2 else ""
        if len(bits) == 3 and country:
            places.append(f"{bits[2]}, {bits[1]}, {country}")
        elif len(bits) == 2 and country:          # "CA-Remote" (Kinaxis layout)
            places.append(f"{bits[1]}, {country}")
        elif part:
            places.append(part)
    return "; ".join(places)


def fetch_icims(entry: dict) -> list[dict]:
    """iCIMS career portal (careers-<tenant>.icims.com): paged job cards on /jobs/search."""
    root = re.match(r"https?://[^/]+", entry["careers_url"]).group(0)
    out, seen = [], set()
    for page in range(ICIMS_MAX_PAGES):
        body = http_text(f"{root}/jobs/search?ss=1&in_iframe=1&pr={page}")
        cards = re.findall(r'(?is)<li class="iCIMS_JobCardItem">(.*?)</li>', body)
        new = 0
        for card in cards:
            link = re.search(r'(?is)<a href="([^"]+/jobs/(\d+)/[^"]+)"[^>]*class="iCIMS_Anchor"[^>]*>.*?<h3[^>]*>(.*?)</h3>', card)
            if not link or link.group(2) in seen:
                continue
            seen.add(link.group(2))
            new += 1
            # Two card layouts: "Job Locations" + header date (Mackenzie), "Location" + dt/dd date (Kinaxis).
            loc = re.search(r'(?is)>\s*(?:Job\s+)?Locations?\s*</span>\s*<span[^>]*>(.*?)</span>', card)
            posted = re.search(r'(?is)Posted Date</(?:span|dt)>.*?<span title="([^"]+)"', card)
            date = ""
            if posted:
                try:
                    date = datetime.strptime(posted.group(1).split()[0], "%m/%d/%Y").date().isoformat()
                except ValueError:
                    date = ""
            out.append(posting(entry, title=html.unescape(strip_html(link.group(3))).strip(),
                               url=html.unescape(link.group(1)).replace("?in_iframe=1", ""),
                               location=_icims_place(html.unescape(strip_html(loc.group(1))) if loc else ""),
                               req_id=link.group(2), date_posted=date))
        if not new:
            break
    if not out:
        raise SourceFailure("no iCIMS job cards parsed")
    return out


PHENOM_PAGE_SIZE = 50
PHENOM_MAX_PAGES = 40  # 2,000 jobs; a site-scoped board is far smaller


def fetch_phenom(entry: dict) -> list[dict]:
    """Phenom People career sites (e.g. jobs-ca.pwc.com/ca/en/search-results).

    The site's own /widgets search API returns every job the site lists, already
    scoped to that site (PwC Canada's site lists only Canadian roles, while the
    Workday board behind it is global). Country and language come from the
    careers_url path (/ca/en/...).
    """
    parsed = urllib.parse.urlparse(entry.get("careers_url", ""))
    parts = [part for part in parsed.path.split("/") if part]
    if not parsed.netloc or len(parts) < 2:
        raise SourceFailure("Phenom careers_url needs /<country>/<lang>/ in its path")
    country, lang = parts[0], parts[1]
    root = f"{parsed.scheme or 'https'}://{parsed.netloc}"
    out, seen, offset = [], set(), 0
    for _ in range(PHENOM_MAX_PAGES):
        data = http_json(f"{root}/widgets", {
            "lang": f"{lang}_{country}", "deviceType": "desktop", "country": country,
            "pageName": "search-results", "ddoKey": "refineSearch", "sortBy": "", "subsearch": "",
            "from": offset, "jobs": True, "counts": False, "all_fields": [], "size": PHENOM_PAGE_SIZE,
            "clearAll": False, "jdsource": "facets", "isSliderEnable": False, "siteType": "external",
            "keywords": "", "global": True, "selected_fields": {}, "locationData": {}})
        refine = (data or {}).get("refineSearch") if isinstance(data, dict) else None
        jobs = ((refine or {}).get("data") or {}).get("jobs")
        if not isinstance(jobs, list):
            raise SourceFailure("missing refineSearch.data.jobs array")
        for item in jobs:
            job_id = str(item.get("jobId") or item.get("reqId") or "")
            if not job_id or job_id in seen:
                continue
            seen.add(job_id)
            out.append(posting(entry, title=item.get("title"),
                               url=f"{root}/{country}/{lang}/job/{job_id}",
                               location=_location(item.get("city"), item.get("state"), item.get("country")),
                               req_id=item.get("reqId") or job_id,
                               date_posted=item.get("postedDate") or item.get("dateCreated") or "",
                               description=item.get("descriptionTeaser", ""),
                               remote="remote" in str(item.get("location", "")).lower(),
                               department=item.get("category", "")))
        offset += len(jobs)
        total = refine.get("totalHits")
        if not jobs or (isinstance(total, int) and offset >= total):
            break
    return out


TEAMTAILOR_MAX_PAGES = 20


def fetch_teamtailor(entry: dict) -> list[dict]:
    """Teamtailor career sites (e.g. careers.kindsight.io).

    Every Teamtailor site publishes its open jobs as a public JSON Feed at
    /jobs.json; each item carries the posting's schema.org JobPosting, which
    holds the locations and remote type. next_url pages are followed if present.
    """
    parsed = urllib.parse.urlparse(entry.get("careers_url", ""))
    if not parsed.netloc:
        raise SourceFailure("Teamtailor careers_url needs a host")
    url = f"{parsed.scheme or 'https'}://{parsed.netloc}/jobs.json"
    out, seen = [], set()
    for _ in range(TEAMTAILOR_MAX_PAGES):
        data = http_json(url)
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            raise SourceFailure("missing JSON Feed items array")
        for item in data["items"]:
            job_url = item.get("url", "")
            if not job_url or job_url in seen:
                continue
            seen.add(job_url)
            ld = item.get("_jobposting") or {}
            places = ld.get("jobLocation") or []
            places = places if isinstance(places, list) else [places]
            names = []
            for place in places:
                address = (place or {}).get("address") or {}
                name = _location(address.get("addressLocality"), address.get("addressRegion"))
                if name and name not in names:
                    names.append(name)
            remote = str(ld.get("jobLocationType", "")).upper() == "TELECOMMUTE"
            location = "; ".join(f"{name} (Remote)" if remote else name for name in names)
            identifier = ld.get("identifier") if isinstance(ld.get("identifier"), dict) else {}
            out.append(posting(entry, title=item.get("title") or ld.get("title"), url=job_url,
                               location=location or ("Remote" if remote else ""),
                               req_id=identifier.get("value") or item.get("id", ""),
                               date_posted=item.get("date_published") or ld.get("datePosted", ""),
                               description=item.get("content_text") or strip_html(item.get("content_html", "")),
                               remote=remote))
        url = data.get("next_url")
        if not url:
            break
    return out


SF_MAX_PAGES = 80  # per layout: 800 API jobs or 2,000 table rows
# sortBy "date" is required: with no sort, page order is unstable and paging returns repeats
# while skipping others (Deloitte 2026-11-18: 294 unique of 426 unsorted, 426 by date).
SF_LOCALES = ("en_US", "fr_CA")  # Deloitte has 23 French-only postings; absent locales return 0
_SF_PAYLOAD = {"locale": "en_US", "sortBy": "date", "keywords": "", "location": "", "facetFilters": {},
               "brand": "", "skills": [], "categoryId": 0, "alertId": "", "rcmCandidateId": ""}


def _sf_date(value: str) -> str:
    value = re.sub(r"\s+", " ", str(value or "")).strip()
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%m/%d/%y", "%m/%d/%Y", "%d %b %Y"):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue
    return ""


def fetch_successfactors(entry: dict) -> list[dict]:
    """SAP SuccessFactors career sites (jobs2web / RMK), e.g. jobs.canadalife.com.

    Newer "Unify" sites answer POST /services/recruiting/v1/jobs (10 per page).
    Classic sites return nothing there; their /search/ results table is paged
    25 rows at a time instead. The RSS feed is not used: it holds only the
    newest 20 jobs.
    """
    parsed = urllib.parse.urlparse(entry.get("careers_url", ""))
    if not parsed.netloc:
        raise SourceFailure("SuccessFactors careers_url needs a host")
    root = f"{parsed.scheme or 'https'}://{parsed.netloc}"
    out, seen = [], set()

    api_used = False
    for locale in SF_LOCALES:
        payload = {**_SF_PAYLOAD, "locale": locale}
        try:
            first = http_json(f"{root}/services/recruiting/v1/jobs", {**payload, "pageNumber": 0})
        except SourceFailure:
            continue
        total = first.get("totalJobs") if isinstance(first, dict) else None
        if not (isinstance(total, int) and total > 0):
            continue
        api_used = True
        data, page, got = first, 0, 0
        while page < SF_MAX_PAGES:
            results = data.get("jobSearchResult") or []
            got += len(results)
            for wrapped in results:
                item = (wrapped or {}).get("response") or {}
                job_id = str(item.get("id") or "")
                if not job_id or job_id in seen:
                    continue
                seen.add(job_id)
                places = [re.sub(r"<[^>]+>", "", str(p)).strip() for p in item.get("jobLocationShort") or []]
                places += [str(p).strip() for p in item.get("mfield1") or []]
                url_title = item.get("urlTitle") or item.get("unifiedUrlTitle") or "job"
                out.append(posting(entry, title=item.get("unifiedStandardTitle") or item.get("title"),
                                   url=f"{root}/job/{url_title}/{job_id}-{locale}/",
                                   location="; ".join(dict.fromkeys(p for p in places if p)),
                                   req_id=job_id, date_posted=_sf_date(item.get("unifiedStandardStart", "")),
                                   remote=any("remote" in str(m).lower() for m in item.get("workModel") or [])))
            page += 1
            if not results or got >= total:
                break
            data = http_json(f"{root}/services/recruiting/v1/jobs", {**payload, "pageNumber": page})
            if not isinstance(data, dict):
                raise SourceFailure("jobs API returned no object")
    if api_used:
        return out

    # The page size is the site's choice: Deloitte serves 25 rows, Magellan 20. Advance by
    # the rows actually served, or a 20-row site stops after its first page (20 of 82).
    start = 0
    for page in range(SF_MAX_PAGES):
        body = http_text(f"{root}/search/?q=&locale=en_US&startrow={start}")
        rows = re.findall(r'(?is)<tr class="data-row[^"]*">(.*?)</tr>', body)
        if not rows and page == 0:
            raise SourceFailure("no jobs API results and no search results table")
        for row in rows:
            link = re.search(r'(?is)<a[^>]+href="(/job/[^"]+)"[^>]*class="jobTitle-link"[^>]*>(.*?)</a>', row) or \
                re.search(r'(?is)<a[^>]+class="jobTitle-link"[^>]+href="(/job/[^"]+)"[^>]*>(.*?)</a>', row)
            if not link or link.group(1) in seen:
                continue
            seen.add(link.group(1))
            loc = re.search(r'(?is)<span class="jobLocation">(.*?)</span>', row)
            loc_text = re.sub(r"(?is)<small.*?</small>", "", loc.group(1)) if loc else ""
            date = re.search(r'(?is)<span class="jobDate[^"]*">(.*?)</span>', row)
            dept = re.search(r'(?is)<span class="jobDepartment">(.*?)</span>', row)
            req = re.search(r"/(\d+)/?$", link.group(1))
            out.append(posting(entry, title=html.unescape(strip_html(link.group(2))), url=root + link.group(1),
                               location=html.unescape(strip_html(loc_text)), req_id=req.group(1) if req else "",
                               date_posted=_sf_date(strip_html(date.group(1)) if date else ""),
                               department=html.unescape(strip_html(dept.group(1))) if dept else ""))
        total_match = re.search(r"(?is)of\s*<b>\s*([\d,]+)\s*</b>", body)
        total_rows = int(total_match.group(1).replace(",", "")) if total_match else None
        start += len(rows)
        if not rows or (total_rows is not None and start >= total_rows) or \
                (total_rows is None and len(rows) < 20):
            break
    return out


FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "ashby": fetch_ashby,
    "lever": fetch_lever,
    "rippling": fetch_rippling,
    "workable": fetch_workable,
    "smartrecruiters": fetch_smartrecruiters,
    "bamboohr": fetch_bamboohr,
    "recruitee": fetch_recruitee,
    "personio": fetch_personio,
    "workday": fetch_workday,
    "gem": fetch_gem,
    "freshteam": fetch_freshteam,
    "jazzhr": fetch_jazzhr,
    "opencats": fetch_opencats,
    "ultipro": fetch_ultipro,
    "epam": fetch_epam,
    "phenom": fetch_phenom,
    "teamtailor": fetch_teamtailor,
    "successfactors": fetch_successfactors,
    "icims": fetch_icims,
    "webflow": fetch_webflow,
    "avature": fetch_avature,
    "pushoperations": fetch_pushoperations,
    "lighthouse": fetch_lighthouse,
    "adp": fetch_adp,
    "xnorth": fetch_xnorth,
    "taleo": fetch_taleo,
    "zoho": fetch_zoho,
    "j2w": fetch_successfactors,
}


def _title_relevant(title: str) -> bool:
    try:
        import scrape_jobs
        return scrape_jobs.role_is_relevant(title)
    except (ImportError, SystemExit):
        return True


def fetch_company(entry: dict, blocked: list[str]) -> list[dict]:
    url = entry.get("careers_url", "")
    if any(domain.lower() in url.lower() for domain in blocked):
        raise SourceFailure("blocked by source safety policy")
    fetcher = FETCHERS.get(entry.get("ats"), fetch_generic_html)
    return fetcher(entry)


def fetch_workable_board(config: dict) -> list[dict]:
    entry = {"company": "", "ats": "workable-discovery", "archetype": "D"}
    out = []
    for query in config.get("queries", []):
        params = urllib.parse.urlencode({"query": query, "location": config.get("location", "Canada")})
        data = http_json(f"https://jobs.workable.com/api/v1/jobs?{params}")
        if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
            raise SourceFailure(f"missing jobs array for {query!r}")
        for item in data["jobs"]:
            loc = item.get("location") or {}
            company = (item.get("company") or {}).get("title", "")
            out.append(posting({**entry, "company": company}, title=item.get("title"), url=item.get("url"),
                               location=_location(loc.get("city"), loc.get("subregion"), loc.get("countryName")),
                               req_id=item.get("id"), date_posted=item.get("created"),
                               description=strip_html(item.get("description", "")),
                               remote=item.get("workplace") == "remote", department=item.get("department", "")))
    return out


_BUILTIN_AGE = re.compile(r'fa-clock[^>]*></i>\s*(?:Reposted\s+)?([^<]+?)\s*</span>', re.I)


def _builtin_age_days(text: str) -> int | None:
    """Built In card tags read 'Yesterday', '8 Days Ago', 'Reposted 2 Months Ago',
    '5 Hours Ago'. Returns the age in whole days, or None when unreadable."""
    t = (text or "").strip().lower()
    if not t:
        return None
    if t in {"today", "just now"} or re.search(r"\b(?:minutes?|hours?)\s+ago", t):
        return 0
    if t == "yesterday":
        return 1
    m = re.search(r"(\d+|an?)\s+(day|week|month|year)s?\s+ago", t)
    if not m:
        return None
    n = 1 if m.group(1) in {"a", "an"} else int(m.group(1))
    return n * {"day": 1, "week": 7, "month": 30, "year": 365}[m.group(2)]


def fetch_builtin_board(config: dict) -> list[dict]:
    """Built In Toronto search pages. Cards older than max_age_days (default 14, the
    tracker's own aging rule closes anything older) are skipped, and each kept card
    carries its posted date so downstream freshness checks can read it."""
    out, seen = [], set()
    max_age = int(config.get("max_age_days", 14))
    today = datetime.now(timezone.utc).date()
    title_re = re.compile(r'href="(/job/[^"]+)"[^>]*data-id="job-card-title"[^>]*>([^<]+)<')
    company_re = re.compile(r'data-id="company-title"[^>]*>.*?<span>([^<]+)</span>', re.S)
    for query in config.get("queries", []):
        for page in range(1, int(config.get("page_cap", 5)) + 1):
            url = "https://builtintoronto.com/jobs?" + urllib.parse.urlencode({"search": query, "page": page})
            body = http_text(url)
            matches = list(title_re.finditer(body))
            if not matches:
                break
            companies = [(m.start(), html.unescape(m.group(1)).strip()) for m in company_re.finditer(body)]
            new = 0
            for idx, match in enumerate(matches):
                path, title = match.group(1), html.unescape(match.group(2)).strip()
                if path in seen:
                    continue
                seen.add(path)
                new += 1
                card_end = matches[idx + 1].start() if idx + 1 < len(matches) else len(body)
                age_m = _BUILTIN_AGE.search(body, match.end(), card_end)
                age = _builtin_age_days(html.unescape(age_m.group(1))) if age_m else None
                if age is not None and age > max_age:
                    continue
                before = [company for pos, company in companies if pos < match.start()]
                out.append(posting({"company": before[-1] if before else "", "ats": "builtin", "archetype": "D"},
                                   title=title, url="https://builtintoronto.com" + path,
                                   location="Toronto, Ontario, Canada",
                                   date_posted=(today - timedelta(days=age)).isoformat() if age is not None else ""))
            if not new:
                break
    if not seen:
        raise SourceFailure("no Built In Toronto job cards parsed")
    return out


def fetch_summit_board(config: dict) -> list[dict]:
    body = http_text(config.get("sitemap", "https://summitsearchgroup.com/job-sitemap.xml"))
    out = []
    for url in re.findall(r"<loc>(.*?)</loc>", body):
        url = html.unescape(url.strip())
        if "/job/" not in url:
            continue
        slug = url.rstrip("/").rsplit("/", 1)[-1]
        title = " ".join(part.capitalize() for part in slug.split("-") if not re.fullmatch(r"\d+|[a-z]{2}", part))
        out.append(posting({"company": "Summit Search Group", "ats": "summitsearch", "archetype": "D"},
                           title=title, url=url, location="Canada"))
    if not out:
        raise SourceFailure("job sitemap carried no postings")
    return out


def dedupe(jobs: list[dict]) -> list[dict]:
    seen, out = set(), []
    for item in jobs:
        key = item.get("url") or "|".join(str(item.get(field, "")).lower() for field in ("company", "title", "location"))
        if key and key not in seen:
            seen.add(key)
            out.append(item)
    return out


def run(args) -> tuple[list[dict], dict]:
    inventory = json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))
    blocked = inventory.get("blocked_domains", [])
    companies = list(inventory.get("companies", []))
    if args.company:
        wanted = args.company.lower()
        companies = [entry for entry in companies if wanted in entry.get("company", "").lower()]
    if args.ats:
        companies = [entry for entry in companies if entry.get("ats") in args.ats]
    if args.limit:
        companies = companies[:args.limit]

    def poll(entry: dict) -> tuple[list[dict], dict | None]:
        started = time.monotonic()
        if not entry.get("enabled"):
            return [], {"company": entry.get("company"), "ats": entry.get("ats"),
                        "status": "unresolved", "job_count": 0,
                        "reason": "no careers URL or ATS tenant resolved"}
        if entry.get("ats") in {"builtin", "summitsearch"}:
            return [], None
        try:
            found = fetch_company(entry, blocked)
            status = {"company": entry["company"], "ats": entry.get("ats"),
                      "status": "succeeded", "job_count": len(found),
                      "duration_seconds": round(time.monotonic() - started, 2), "reason": ""}
        except Exception as exc:
            found = []
            status = {"company": entry.get("company"), "ats": entry.get("ats"),
                      "status": "failed", "job_count": 0,
                      "duration_seconds": round(time.monotonic() - started, 2),
                      "reason": f"{type(exc).__name__}: {exc}"}
        if not args.no_delay:
            delay = 8.0 if entry.get("ats") == "lever" else 0.4
            time.sleep(delay + random.uniform(0, 0.25))
        return found, status

    # Companies are polled PORTAL_WORKERS at a time (default 8; 1 = the old sequential pass).
    # Lever rate-limits by IP, so all Lever boards stay in one sequential lane with their 8 s
    # gap. Sequential, a 258-company pass took ~40 min, ~30 of them Workday (~23 s a board);
    # the planned ~1,150-company inventory would not fit the runner's 60-minute slot.
    # Results keep inventory order, so output is the same as a sequential pass.
    workers = max(1, int(os.environ.get("PORTAL_WORKERS", "8") or 8))
    results: list = [None] * len(companies)
    lever = [i for i, e in enumerate(companies) if e.get("ats") == "lever" and e.get("enabled")]
    others = [i for i in range(len(companies)) if i not in set(lever)]

    def lever_lane() -> None:
        for i in lever:
            results[i] = poll(companies[i])

    if workers == 1:
        for i in range(len(companies)):
            results[i] = poll(companies[i])
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            lane = pool.submit(lever_lane)
            for i, res in zip(others, pool.map(lambda i: poll(companies[i]), others)):
                results[i] = res
            lane.result()
    jobs, statuses = [], []
    for found, status in results:
        jobs.extend(found)
        if status is not None:
            statuses.append(status)

    aggregators = inventory.get("aggregators", {})
    aggregator_fetchers = {
        "workable": fetch_workable_board,
        "builtin_toronto": fetch_builtin_board,
        "summit_search": fetch_summit_board,
    }
    if not args.company and not args.ats:
        for name, fetcher in aggregator_fetchers.items():
            config = aggregators.get(name, {})
            if not config.get("enabled"):
                continue
            started = time.monotonic()
            try:
                found = fetcher(config)
                jobs.extend(found)
                statuses.append({"company": name, "ats": "aggregator", "status": "succeeded",
                                 "job_count": len(found), "duration_seconds": round(time.monotonic() - started, 2), "reason": ""})
            except Exception as exc:
                statuses.append({"company": name, "ats": "aggregator", "status": "failed", "job_count": 0,
                                 "duration_seconds": round(time.monotonic() - started, 2),
                                 "reason": f"{type(exc).__name__}: {exc}"})

    unique = dedupe(jobs)
    counts = Counter(status["status"] for status in statuses)
    report = {
        "run_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "inventory_count": len(inventory.get("companies", [])),
        "selected_count": len(companies),
        "raw_jobs": len(jobs),
        "unique_jobs": len(unique),
        "status_counts": dict(counts),
        "web_discovery": {
            "status": "scheduled_separately",
            "queries": (aggregators.get("web_discovery") or {}).get("queries", []),
        },
        "sources": statuses,
    }
    return unique, report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--company", help="Run matching company names only")
    parser.add_argument("--ats", action="append", choices=sorted(set(FETCHERS) | {"builtin", "summitsearch"}))
    parser.add_argument("--limit", type=int)
    parser.add_argument("--no-save", action="store_true", help="Probe without changing output files")
    parser.add_argument("--no-delay", action="store_true", help="Skip courtesy delays in tests/probes")
    args = parser.parse_args()
    if not INVENTORY_PATH.exists():
        raise SystemExit("sources/company_portals.json is missing; run scripts/sync_portal_sources.py")
    jobs, report = run(args)
    print(json.dumps(report["status_counts"], sort_keys=True))
    print(f"{report['raw_jobs']} raw jobs; {report['unique_jobs']} unique")
    for source in report["sources"]:
        if source["status"] == "failed":
            print(f"FAILED {source['company']} [{source.get('ats') or 'unknown'}]: {source['reason']}")
    if args.no_save:
        return
    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATUS_PATH.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    import scrape_jobs
    scrape_jobs.save_jobs_output(
        jobs,
        basename="company_portal_jobs",
        title="Company Portals — Executive Data, AI & Transformation Roles",
        subtitle="Toronto/GTA, Ontario, and Canada-remote direct-employer sources",
        accent="#7c3aed",
        empty_message="No matching roles found in today's company-portal run.",
        window_label="current portal openings",
    )


if __name__ == "__main__":
    main()
