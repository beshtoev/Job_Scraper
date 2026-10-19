#!/usr/bin/env python3
"""Poll the configured employer portals and Canadian discovery boards."""

from __future__ import annotations

import argparse
import html
import http.client
import json
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent
INVENTORY_PATH = ROOT / "sources" / "company_portals.json"
STATUS_PATH = ROOT / "output" / "company_portal_status.json"
USER_AGENT = "Mozilla/5.0 (compatible; MuratJobDiscovery/1.0; +https://github.com/beshtoev/Job_Scraper)"
TIMEOUT = 20
WORKDAY_TERMS = ("data", "artificial intelligence", "analytics", "transformation", "FP&A", "automation", "governance", "technology")


class SourceFailure(RuntimeError):
    pass


def strip_html(value: str) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"(?i)<\s*br\s*/?\s*>|</\s*(?:p|li|div|section|h\d)\s*>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\s+([.,;:!?])", r"\1", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def _request(url: str, payload: dict | list | None = None, retries: int = 2) -> bytes:
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json,text/html;q=0.9"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, headers=headers, method="POST" if data is not None else "GET")
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as response:
                return response.read()
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
            if attempt == retries:
                raise SourceFailure(f"{type(exc).__name__}: {exc}") from exc
            time.sleep(1.5 * (attempt + 1))
    raise SourceFailure("request failed")


def http_json(url: str, payload: dict | list | None = None) -> dict | list:
    try:
        return json.loads(_request(url, payload).decode("utf-8", "replace"))
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
    return [posting(entry, title=item.get("title"), url=item.get("jobUrl") or item.get("applyUrl"),
                    location=item.get("location", ""), req_id=item.get("id"),
                    date_posted=item.get("publishedAt", ""),
                    description=strip_html(item.get("descriptionHtml", "")),
                    remote=item.get("isRemote"), department=item.get("department", ""))
            for item in data["jobs"] if item.get("isListed", True)]


def fetch_lever(entry: dict) -> list[dict]:
    data = http_json(f"https://api.lever.co/v0/postings/{entry['slug']}?mode=json")
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
        loc = item.get("location") or item.get("atsLocation") or {}
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


def fetch_workday(entry: dict) -> list[dict]:
    parsed = parse_workday_url(entry.get("careers_url", ""))
    if not parsed:
        raise SourceFailure("unrecognized Workday URL")
    tenant, wd, site = parsed
    api_root = f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}"
    public_root = f"https://{tenant}.{wd}.myworkdayjobs.com/en-US/{site}"
    seen = {}
    for term in WORKDAY_TERMS:
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
        if _title_relevant(title):
            try:
                detail = http_json(api_root + external)
                info = (detail or {}).get("jobPostingInfo") or {}
                description = strip_html(info.get("jobDescription", ""))
                posted_at = info.get("startDate") or posted_at
            except SourceFailure:
                pass
        bullets = item.get("bulletFields") or []
        out.append(posting(entry, title=title, url=public_root + external,
                           location=item.get("locationsText", ""), req_id=bullets[0] if bullets else "",
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
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
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


def fetch_builtin_board(config: dict) -> list[dict]:
    out, seen = [], set()
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
            for match in matches:
                path, title = match.group(1), html.unescape(match.group(2)).strip()
                if path in seen:
                    continue
                seen.add(path)
                before = [company for pos, company in companies if pos < match.start()]
                out.append(posting({"company": before[-1] if before else "", "ats": "builtin", "archetype": "D"},
                                   title=title, url="https://builtintoronto.com" + path,
                                   location="Toronto, Ontario, Canada"))
                new += 1
            if not new:
                break
    if not out:
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

    jobs, statuses = [], []
    for entry in companies:
        started = time.monotonic()
        if not entry.get("enabled"):
            statuses.append({"company": entry.get("company"), "ats": entry.get("ats"),
                             "status": "unresolved", "job_count": 0,
                             "reason": "no careers URL or ATS tenant resolved"})
            continue
        if entry.get("ats") in {"builtin", "summitsearch"}:
            continue
        try:
            found = fetch_company(entry, blocked)
            jobs.extend(found)
            statuses.append({"company": entry["company"], "ats": entry.get("ats"),
                             "status": "succeeded", "job_count": len(found),
                             "duration_seconds": round(time.monotonic() - started, 2), "reason": ""})
        except Exception as exc:
            statuses.append({"company": entry.get("company"), "ats": entry.get("ats"),
                             "status": "failed", "job_count": 0,
                             "duration_seconds": round(time.monotonic() - started, 2),
                             "reason": f"{type(exc).__name__}: {exc}"})
        if not args.no_delay:
            delay = 8.0 if entry.get("ats") == "lever" else 0.4
            time.sleep(delay + random.uniform(0, 0.25))

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
