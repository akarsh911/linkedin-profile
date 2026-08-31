#!/usr/bin/env python3
"""
LinkedIn profile extractor -- direct HTTP, cookie-authenticated, no browser.

Confirmed working (see ../docs/FINDINGS.md #7/#8): a plain GET request with only the
`li_at` session cookie against LinkedIn's own page routes (`/in/{slug}/`,
`/in/{slug}/details/{section}/`) returns server-rendered HTML with real profile content.
No fingerprint/anti-bot headers are used or required here -- this is ordinary
cookie-authenticated access to a page you can already view in your own browser, not
bot-detection evasion.

Confidence levels per section (see README.md in this folder for detail):
  - name / headline / location / image / connection degree : HIGH (validated against
    real captured HTML byte-for-byte).
  - about                                                    : MEDIUM (heuristic, no
    positive sample profile with an About section was available while building this).
  - experience                                               : MEDIUM-HIGH (heuristic
    grouping logic, but validated against a real 4-entry, 2-position-group profile).
  - education / skills / certifications / languages / projects : LOW (same URL pattern as
    experience is assumed but NOT individually confirmed; parser is a generic best-effort
    fallback and may return incomplete/empty results).
  - activity / posts                                          : LOW (different URL family
    entirely -- `/recent-activity/...` -- not tested in this session at all; included as
    a best-effort attempt, expect it to need real debugging against actual output).
"""

import argparse
import json
import os
import re
import sys
import time
from getpass import getpass
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

import rsc

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

ACTIVITY_PATHS = {
    "posts": "recent-activity/shares/",
    "all_activity": "recent-activity/all/",
    "comments": "recent-activity/comments/",
}

DATE_RANGE_RE = re.compile(r"\b(19|20)\d{2}\b.*(-|–).*(Present|\b(19|20)\d{2}\b)")
EDU_LABEL_RE = re.compile(r"^(Grade|Activities and societies)\s*:\s*(.*)$", re.I)
CONNECTION_DEGREE_RE = re.compile(r"^·?\s*(1st|2nd|3rd\+?)$")
PRONOUN_RE = re.compile(r"^(He/Him|She/Her|They/Them)$")


# --------------------------------------------------------------------------- #
# Auth / input resolution
# --------------------------------------------------------------------------- #

def get_li_at(cli_value=None):
    """Prefer explicit --li-at, then LI_AT env var, then an interactive hidden prompt."""
    if cli_value:
        return cli_value.strip()
    env_value = os.environ.get("LI_AT")
    if env_value:
        return env_value.strip()
    value = getpass("LinkedIn li_at cookie value (input hidden, not stored): ")
    if not value.strip():
        raise SystemExit("No li_at cookie provided. Set LI_AT env var, pass --li-at, or enter it when prompted.")
    return value.strip()


def vanity_slug_from_url(profile_url):
    path = urlparse(profile_url).path
    parts = [p for p in path.split("/") if p]
    if len(parts) < 2 or parts[0] != "in":
        raise ValueError(f"Not a recognizable LinkedIn profile URL: {profile_url!r}")
    return parts[1]


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

class LinkedInSessionRevokedError(Exception):
    """Raised when LinkedIn has revoked the li_at session server-side (confirmed via
    Set-Cookie: li_at=delete me + Clear-Site-Data on the redirect responses -- see
    docs/FINDINGS.md "automation-triggered session revocation"). Surfaces as
    requests.exceptions.TooManyRedirects since the client keeps resending the now-dead
    cookie and the server keeps redirecting to clear it. Needs a fresh li_at value;
    not recoverable by retrying with the same one."""


class LinkedInSession:
    """Confirmed empirically (see docs/FINDINGS.md): reusing one requests.Session across
    multiple page fetches accumulates LinkedIn's own Set-Cookie values from earlier
    responses (bcookie, bscookie, lang, lidc, sdui_ver, JSESSIONID). Once those
    accumulate, subsequent `/details/*` requests get served a lighter, hydration-only
    response missing the pre-rendered HTML entirely -- while an isolated request with
    only the `li_at` cookie gets the full server-rendered page every time. So: reset the
    cookie jar back to just `li_at` before every single request, even within the same
    session object."""

    def __init__(self, li_at):
        self._li_at = li_at
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.9",
        })

    def get_html(self, path, timeout=15):
        self.session.cookies.clear()
        self.session.cookies.set("li_at", self._li_at, domain=".linkedin.com")
        url = f"https://www.linkedin.com{path}"
        try:
            resp = self.session.get(url, timeout=timeout)
        except requests.exceptions.TooManyRedirects:
            raise LinkedInSessionRevokedError(
                "LinkedIn revoked this li_at session (redirect loop on every request). "
                "Needs a fresh li_at cookie value -- see docs/FINDINGS.md."
            )
        # LinkedIn's response doesn't reliably declare a charset, so `requests` falls
        # back to Latin-1 per RFC 2616 and mangles every multi-byte UTF-8 character
        # (e.g. "·" becomes "Â·"). The body is UTF-8 in practice -- force it.
        resp.encoding = "utf-8"
        return resp.status_code, resp.text if resp.status_code == 200 else None

    def get_csrf_token(self, timeout=15):
        """Derive a csrf-token via the standard double-submit-cookie pattern: a
        legitimate GET sets a real JSESSIONID cookie, and the CSRF header is just that
        value with its surrounding quotes stripped. Confirmed (see docs/FINDINGS.md):
        this plus the cookie is sufficient for `actions/component` -- no fingerprint
        headers needed."""
        self.session.cookies.clear()
        self.session.cookies.set("li_at", self._li_at, domain=".linkedin.com")
        try:
            resp = self.session.get("https://www.linkedin.com/feed/", timeout=timeout)
        except requests.exceptions.TooManyRedirects:
            raise LinkedInSessionRevokedError(
                "LinkedIn revoked this li_at session (redirect loop on every request). "
                "Needs a fresh li_at cookie value -- see docs/FINDINGS.md."
            )
        jsessionid = None
        for c in self.session.cookies:
            if c.name == "JSESSIONID":
                jsessionid = c.value
                break
        if not jsessionid:
            return None
        return jsessionid, jsessionid.strip('"')

    def post_component(self, slug, component_id, jsessionid_raw, csrf_token, timeout=15):
        """POST to `actions/component`, cookie + CSRF only -- no x-li-track or any other
        fingerprint header. See docs/FINDINGS.md: confirmed sufficient auth on its own."""
        self.session.cookies.clear()
        self.session.cookies.set("li_at", self._li_at, domain=".linkedin.com")
        self.session.cookies.set("JSESSIONID", jsessionid_raw, domain=".www.linkedin.com")
        url = "https://www.linkedin.com/flagship-web/rsc-action/actions/component"
        params = {"componentId": component_id, "sduiid": component_id}
        headers = {
            "accept": "*/*",
            "content-type": "application/json",
            "csrf-token": csrf_token,
            "origin": "https://www.linkedin.com",
            "referer": f"https://www.linkedin.com/in/{slug}/",
        }
        body = _component_request_body(slug)
        try:
            resp = self.session.post(url, params=params, headers=headers, json=body, timeout=timeout)
        except requests.exceptions.TooManyRedirects:
            raise LinkedInSessionRevokedError(
                "LinkedIn revoked this li_at session (redirect loop on every request). "
                "Needs a fresh li_at cookie value -- see docs/FINDINGS.md."
            )
        resp.encoding = "utf-8"
        return resp.status_code, resp.text if resp.status_code == 200 else None


def _component_request_body(vanity):
    state_labels = [
        "ShouldRefreshScreen", "FetchFromCache", "ShouldDisplayTabAnchors",
        "ShouldReloadTopCardOnReappear", "DeferredTopCardReloadProfileId",
        "ShouldDisplayStickyHeader", "ShouldRefreshLanguageDetails",
        "LastPerformedActionRef", "ShouldFocusOnReappear", "ShouldFocusFeaturedOnReappear",
        "LastFeaturedActionRef", "ProfileHideCards",
    ]
    state_fields = [
        "shouldRefreshScreenOnReappear", "shouldFetchFromCache", "shouldDisplayTabAnchors",
        "shouldReloadTopCardOnReappear", "deferredTopCardReloadProfileId",
        "shouldDisplayStickyHeader", "shouldRefreshLanguageDetailScreen",
        "lastPerformedActionRef", "shouldFocusOnReappear", "shouldFocusFeaturedOnReappear",
        "lastFeaturedActionRef", "shouldHideProfileCards",
    ]
    profile_component_state = {
        field: {
            "type": "com.linkedin.sdui.components.core.BindingImpl",
            "value": {"key": f"ProfileComponentState{label}{vanity}ProfileComponentState", "namespace": "MemoryNamespace"},
        }
        for field, label in zip(state_fields, state_labels)
    }
    return {
        "clientArguments": {
            "payload": {
                "isSelfView": False,
                "vanityName": vanity,
                "replaceableSectionArgs": {
                    "vanityName": vanity,
                    "hideCardsForGoldenGate": False,
                    "shouldSetupReplaceableComponent": True,
                    "vieweeProfileId": "",
                    "isSelfView": False,
                    "isSelfViewResolved": False,
                },
                "profileComponentState": profile_component_state,
            },
            "states": [],
            "requestMetadata": {"$type": "proto.sdui.common.RequestMetadata"},
            "screenId": "com.linkedin.sdui.flagshipnav.profile.Profile",
            "knownTemplateIds": [],
        }
    }


def _strip_heading(lines, heading_text):
    """RSC section subtrees sometimes (not always -- inconsistent across component
    types) include their own on-page heading as a leaked text leaf; the HTML-based
    parsers already exclude their heading before grouping, so match that here."""
    return [l for l in lines if l.strip().lower() != heading_text.lower()]


COMPONENT_ID_RE = re.compile(r"com\.linkedin\.sdui\.generated\.profile\.dsl\.impl\.[A-Za-z0-9]+")

# Widget componentIds that come back from the page scan but aren't profile-owned content
# (LinkedIn's own "people you may know" / recommended pages / recommended posts widgets).
COMPONENT_ID_SKIP_SUFFIXES = ("RecommendedEntitySection",)


def discover_component_ids(profile_html):
    """Find every componentId this specific profile's page references. Confirmed (see
    docs/FINDINGS.md): which sections get bundled under which Part number is NOT fixed --
    it varies per profile -- so this has to be discovered per-request, not hardcoded."""
    found = sorted(set(COMPONENT_ID_RE.findall(profile_html)))
    return [
        cid for cid in found
        if not cid.rsplit(".", 1)[-1].endswith(COMPONENT_ID_SKIP_SUFFIXES)
        and cid.rsplit(".", 1)[-1] not in ("profileCardsAboveActivity", "profileCardsActivity", "profileCardsExperienceOnly")
    ]


def fetch_bundled_sections(session, slug, profile_html, on_progress=None):
    """Fetch every `actions/component` bundle this profile references and merge their
    resolved sections (keyed by observabilityIdentifier name, e.g. "educationTopLevelSection")
    into one dict, along with each section's own logo/image URLs. A single component
    response can bundle several unrelated sections (e.g. Education + Certifications +
    Projects together) -- see docs/FINDINGS.md."""
    def progress(msg):
        if on_progress:
            on_progress(msg)

    csrf = session.get_csrf_token()
    fetch_status = {}
    if not csrf:
        progress("Could not derive a CSRF token — session may be dead")
        return {}, {}, fetch_status
    jsessionid_raw, csrf_token = csrf

    component_ids = discover_component_ids(profile_html)
    progress(f"Discovered {len(component_ids)} profile component(s) to fetch")
    sections = {}
    section_logos = {}
    for cid in component_ids:
        short_name = cid.rsplit(".", 1)[-1]
        progress(f"Fetching component {short_name}…")
        status, body = session.post_component(slug, cid, jsessionid_raw, csrf_token)
        fetch_status[short_name] = status
        _maybe_dump(f"component_{short_name}", body)
        if status == 200 and body:
            root, _table = rsc.resolve_root(body)
            found_here = rsc.find_sections(root)
            progress(f"  {short_name} → {', '.join(found_here.keys()) or '(no sub-sections)'}")
            for name, node in found_here.items():
                if name not in sections:
                    sections[name] = node
                    section_logos[name] = rsc.section_logo_urls(node)
        else:
            progress(f"  {short_name} failed (HTTP {status})")
        _pace()
    return sections, section_logos, fetch_status


DEBUG_DIR = os.environ.get("EXTRACTOR_DEBUG_DIR")


def fetch_section(session, slug, section=None):
    path = f"/in/{slug}/" if section is None else f"/in/{slug}/details/{section}/"
    status, html = session.get_html(path)
    _maybe_dump(section or "profile", html)
    return status, html


def _maybe_dump(name, html):
    if not DEBUG_DIR or not html:
        return
    os.makedirs(DEBUG_DIR, exist_ok=True)
    with open(os.path.join(DEBUG_DIR, f"{name}.html"), "w", encoding="utf-8") as f:
        f.write(html)


def fetch_activity(session, slug, kind="posts"):
    path = f"/in/{slug}/{ACTIVITY_PATHS[kind]}"
    status, html = session.get_html(path)
    _maybe_dump(kind, html)
    return status, html


# --------------------------------------------------------------------------- #
# Top card (name / headline / location / image)
# --------------------------------------------------------------------------- #

def parse_top_card(html):
    soup = BeautifulSoup(html, "lxml")
    card = soup.find(attrs={"data-view-name": "profile-top-card"})
    if card is None:
        return {}, soup

    data = {}

    name_el = card.find("h2")
    data["name"] = name_el.get_text(strip=True) if name_el else None

    img = None
    photo_wrap = card.find(attrs={"data-view-name": "profile-top-card-member-photo"})
    if photo_wrap:
        img_tag = photo_wrap.find("img")
        if img_tag and img_tag.get("src"):
            img = img_tag["src"]
    if img is None:
        m = re.search(r'https://media\.licdn\.com/dms/image/[^"\']+profile-displayphoto[^"\']*', str(card))
        img = m.group(0).replace("&amp;", "&") if m else None
    data["image_url"] = img

    texts = [p.get_text(" ", strip=True) for p in card.find_all("p")]
    texts = [t for t in texts if t]

    connection_degree = None
    for t in texts:
        if CONNECTION_DEGREE_RE.match(t):
            connection_degree = t.lstrip("·").strip()
            break
    data["connection_degree"] = connection_degree

    core = [
        t for t in texts
        if not CONNECTION_DEGREE_RE.match(t)
        and not PRONOUN_RE.match(t)
        and t not in ("·", "Contact info")
        and not t.startswith("http")
    ]

    data["headline"] = core[0] if len(core) > 0 else None
    data["current_company_school_summary"] = core[1] if len(core) > 1 and " · " in core[1] else None
    remaining = core[2:] if data["current_company_school_summary"] else core[1:]
    data["location"] = remaining[0] if remaining else None

    return data, soup


# Image URLs are split into a base + a separate imageRenditions[] array of size-specific
# suffixUrl values (LinkedIn avoids repeating the shared base per rendition size), e.g.:
#   https://media.licdn.com/dms/image/v2/{id}/company-logo-\",\"imageRenditions\":
#     [{\"width\":100,...,\"suffixUrl\":\"scale_100_100/{token}?e=...\\u0026v=beta...\"}, ...]
# Concatenating base + first (smallest) suffixUrl reconstructs a real, working image URL.
IMAGE_BASE_AND_SUFFIX_RE = re.compile(
    r'(https://media\.licdn\.com/dms/image/v2/[^\\"]+-)\\?",\\?"imageRenditions\\?":\[\{\\?"width\\?":\d+,\\?"height\\?":\d+,\\?"suffixUrl\\?":\\?"([^\\"]+)\\?"'
)


def _extract_logo_urls_in_order(html):
    """Company/school logo URLs, in document order.

    These are NOT present as live <img src> in the server-rendered HTML -- the
    <figure data-view-name="image"> for each entry only contains a
    <span data-dynamic-icon-loading> placeholder; the actual logo data (base URL +
    size renditions) is embedded elsewhere in the document for client-side hydration
    (confirmed on real captured HTML). Pulled positionally (one per company/school)
    rather than scoped per entry -- a reasonable heuristic since document order should
    match entry order, but not guaranteed the way the skill-overlay hrefs (which ARE
    live <a> tags) are."""
    seen = []
    for m in IMAGE_BASE_AND_SUFFIX_RE.finditer(html):
        base, suffix = m.group(1), m.group(2)
        if "logo" not in base.lower():
            continue
        url = (base + suffix).replace("\\/", "/").replace("\\u0026", "&")
        if url not in seen:
            seen.append(url)
    return seen


def parse_skill_overlay(html):
    """Full skill list from a '/overlay/{id}/skill-associations-details/' page."""
    soup = BeautifulSoup(html, "lxml")
    heading = next((h for h in soup.find_all(["h1", "h2"]) if h.get_text(strip=True).startswith("Skills for")), None)
    if heading is None:
        return []
    skills = []
    for el in heading.find_all_next(["div", "span", "p"]):
        text = el.get_text(strip=True)
        if text == "Learn more about these skills":
            break
        if el.find(["div", "span", "p"]):
            continue  # only leaf-ish nodes, avoid duplicating parent text
        if text and text not in skills:
            skills.append(text)
    return skills


def parse_about(soup):
    for tag in soup.find_all(attrs={"data-view-name": True}):
        dv = tag["data-view-name"]
        if dv.startswith("profile") and "about" in dv.lower():
            text = tag.get_text(" ", strip=True)
            if text:
                return text
    for heading in soup.find_all(["h2", "h3"]):
        if heading.get_text(strip=True) == "About":
            sib = heading.find_next(["p", "div", "span"])
            if sib:
                text = sib.get_text(" ", strip=True)
                if text and text != "About":
                    return text
    return None


# --------------------------------------------------------------------------- #
# Experience (validated against a real captured profile -- see docs/FINDINGS.md)
# --------------------------------------------------------------------------- #

def parse_experience(html):
    soup = BeautifulSoup(html, "lxml")
    logos = soup.find_all(attrs={"data-view-name": "experience-company-logo-click"})
    entries = []
    logo_iter = iter(_extract_logo_urls_in_order(html))

    for logo in logos:
        el = logo
        for _ in range(4):
            if el.parent is None:
                break
            el = el.parent
        lines = [t.strip() for t in el.stripped_strings if t.strip()]
        if not lines:
            continue

        logo_url = next(logo_iter, None)
        # Skill-overlay links appear in the same document order as the positions
        # that have a truncated "+N skills" line -- zip them together positionally.
        overlay_paths = [a["href"] for a in el.find_all("a", href=re.compile(r"/overlay/\d+/skill-associations-details/"))]

        date_count = sum(1 for l in lines if DATE_RANGE_RE.search(l))

        if date_count >= 2:
            # Multiple positions grouped under one company.
            company = lines[0]
            positions = _split_grouped_positions(lines[1:])
            overlay_iter = iter(overlay_paths)
            for pos in positions:
                pos["company"] = company
                pos["logo_url"] = logo_url
                pos["skills_overlay_path"] = next(overlay_iter, None) if pos.get("skills") else None
                entries.append(pos)
        else:
            pos = _parse_single_position(lines)
            pos["logo_url"] = logo_url
            pos["skills_overlay_path"] = overlay_paths[0] if overlay_paths else None
            entries.append(pos)

    return entries


def _split_grouped_positions(lines):
    """Split the lines *after* the company name into one dict per position.

    Walks the lines in document order. A date-range line closes out the
    preceding pending line as the new position's title. Exactly one line
    immediately following a date-range line (if it isn't a skills label/list)
    is captured as that position's location -- after that, further plain
    lines become the *next* position's pending title, not this one's location.
    Company-level summary lines that appear before the first date range
    (e.g. total duration, company-level location) are intentionally dropped
    here since they aren't per-position data.
    """
    positions = []
    pending_title = None
    current = None
    awaiting_location = False
    for line in lines:
        if DATE_RANGE_RE.search(line):
            current = {"title": pending_title, "date_range": line, "location": None, "skills": None}
            positions.append(current)
            pending_title = None
            awaiting_location = True
            continue
        if line.lower() == "skills:":
            awaiting_location = False
            continue
        if current is not None and _looks_like_skill_list(line):
            current["skills"] = line
            awaiting_location = False
            continue
        if current is not None and awaiting_location and current["location"] is None:
            current["location"] = line
            awaiting_location = False
            continue
        pending_title = line
    return positions


def _parse_single_position(lines):
    entry = {"title": None, "company": None, "date_range": None, "location": None, "skills": None}
    if len(lines) > 0:
        entry["title"] = lines[0]
    if len(lines) > 1:
        entry["company"] = lines[1].split(" · ")[0].strip()
    for line in lines[2:]:
        if DATE_RANGE_RE.search(line):
            entry["date_range"] = line
        elif line.lower() == "skills:":
            continue
        elif _looks_like_skill_list(line):
            entry["skills"] = line
        elif entry["location"] is None:
            entry["location"] = line
    return entry


def _looks_like_title(line):
    return not DATE_RANGE_RE.search(line) and "skills" not in line.lower() and len(line) < 100


def _looks_like_skill_list(line):
    return bool(re.search(r"\+\d+\s+skills?$", line)) or line.lower().startswith("skills")


# --------------------------------------------------------------------------- #
# IMPORTANT CAVEAT for education/skills/certifications/languages/projects (see
# docs/FINDINGS.md "known content-delivery gap"): these functions are validated
# against real *rendered DOM* HTML captured from a live browser tab -- NOT against
# a plain `curl`/`requests` fetch. A cold, no-browser GET of these `/details/*` routes
# reliably returns a response missing this content entirely (confirmed repeatedly).
# The content only appeared in-browser after a viewport resize event, with **no new
# network request** observed at that moment -- so its actual delivery mechanism for a
# non-browser client is still unresolved. Treat extractor.py as validated for base
# profile + experience only; these functions are here so the parsing logic is ready
# once (if) a working fetch path is found, but calling fetch_section() for these
# sections today will return empty results, honestly, via _meta.fetch_status.
# --------------------------------------------------------------------------- #

def parse_education(html):
    """Validated against one real captured entry (see docs/FINDINGS.md). Field order
    observed: institution, degree, date range (note: en-dash '–', not hyphen),
    optional 'Grade: ...', optional 'Activities and societies: ...', optional
    'Skills:' label + skills line. Only tested against a single-entry profile --
    multi-entry grouping is inferred, not confirmed the way experience's was."""
    soup = BeautifulSoup(html, "lxml")
    heading = next((h for h in soup.find_all(["h1", "h2"]) if h.get_text(strip=True) == "Education"), None)
    if heading is None:
        return []
    container = heading.find_parent("section") or heading.parent
    if container is None:
        return []

    lines = [t.strip() for t in container.stripped_strings if t.strip()]
    lines = [l for l in lines if l != "Education"]

    logo_urls = _extract_logo_urls_in_order(html)
    overlay_paths = [a["href"] for a in container.find_all("a", href=re.compile(r"/overlay/\d+/skill-associations-details/"))]
    return _group_education(lines, logo_urls, overlay_paths)


def _group_education(lines, logo_urls=None, overlay_paths=None):
    logo_iter = iter(logo_urls or [])
    overlay_iter = iter(overlay_paths or [])

    entries = []
    current = None
    awaiting_skills = False

    def is_complete(e):
        return e and e["institution"] and e["degree"] and e["date_range"]

    def new_entry():
        return {"institution": None, "degree": None, "date_range": None,
                "grade": None, "activities": None, "skills": None,
                "logo_url": None, "skills_overlay_path": None}

    for line in lines:
        if DATE_RANGE_RE.search(line):
            if current is None:
                current = new_entry()
            current["date_range"] = line
            awaiting_skills = False
            continue
        m = EDU_LABEL_RE.match(line)
        if m and current:
            key = "grade" if m.group(1).lower() == "grade" else "activities"
            current[key] = m.group(2).strip()
            awaiting_skills = False
            continue
        if line.lower() == "skills:":
            awaiting_skills = True
            continue
        if current and awaiting_skills:
            current["skills"] = line
            current["skills_overlay_path"] = next(overlay_iter, None)
            awaiting_skills = False
            continue
        if current and _looks_like_skill_list(line):
            current["skills"] = line
            current["skills_overlay_path"] = next(overlay_iter, None)
            continue
        # institution / degree line
        if is_complete(current):
            entries.append(current)
            current = None
        if current is None:
            current = new_entry()
            current["logo_url"] = next(logo_iter, None)
        if current["institution"] is None:
            current["institution"] = line
        elif current["degree"] is None:
            current["degree"] = line

    # A trailing entry with only "institution" set and nothing else is a leftover
    # field-of-study/label fragment for the *previous* entry (no dedicated slot for it
    # currently), not a genuine new institution -- a real entry always has at least a
    # degree or date_range. Drop it rather than emit a fake third entry.
    if current and current["institution"] and (current["degree"] or current["date_range"]):
        entries.append(current)
    return entries


_BOILERPLATE_LINES = {"message", "connect", "follow", "following", "contact info"}

_SKILLS_CATEGORY_TABS = {"all", "industry knowledge", "tools & technologies",
                          "interpersonal skills", "languages", "other skills"}

PROJECT_ASSOCIATED_RE = re.compile(r"^Associated with (.+)$")


def parse_projects_section(html):
    """Validated against a real 10-project sample (see docs/FINDINGS.md). Fields per
    project: name (line before the date range), date_range, optional 'Associated with
    {org}', a description line, optional 'Skills: ...', and a catch-all `links` list
    for everything else (GitHub repo links, Devfolio links, team names, contributor
    counts like '+2') since that trailing content varies a lot per project."""
    soup = BeautifulSoup(html, "lxml")
    heading = next((h for h in soup.find_all(["h1", "h2"]) if h.get_text(strip=True) == "Projects"), None)
    if heading is None:
        return []
    container = heading.find_parent("section") or heading.parent
    if container is None:
        return []

    lines = [t.strip() for t in container.stripped_strings if t.strip()]
    lines = [l for l in lines if l != "Projects"]
    return _group_projects(lines)


def _group_projects(lines, thumbnail_urls=None):
    thumbnail_iter = iter(thumbnail_urls or [])
    projects = []
    pending_name = None
    current = None
    awaiting_skills = False
    for line in lines:
        if DATE_RANGE_RE.search(line):
            if current:
                # the last "link" tentatively appended was actually this new
                # project's name, not a trailing link of the previous one.
                if current["links"] and current["links"][-1] == pending_name:
                    current["links"].pop()
                projects.append(current)
            current = {"name": pending_name, "date_range": line, "associated_with": None,
                       "description": None, "skills": None, "links": [],
                       "thumbnail_url": next(thumbnail_iter, None)}
            pending_name = None
            awaiting_skills = False
            continue
        if current is None:
            pending_name = line
            continue
        m = PROJECT_ASSOCIATED_RE.match(line)
        if m:
            current["associated_with"] = m.group(1)
            continue
        if line.lower() == "skills:":
            awaiting_skills = True
            continue
        if awaiting_skills:
            current["skills"] = line
            awaiting_skills = False
            continue
        if line == "Other contributors" or re.match(r"^\+\d+$", line):
            continue
        if current["description"] is None and len(line) > 30:
            current["description"] = line
            continue
        # ambiguous: could be a trailing link/label of this project, or the NEXT
        # project's name -- tentatively record both; reconciled above once we
        # know (a following date-range line confirms it was actually the name).
        current["links"].append(line)
        pending_name = line
    if current:
        projects.append(current)
    return projects


ISSUED_BY_RE = re.compile(r"^Issued by (.+?)\s*·\s*(.+)$")
HONORS_ASSOCIATED_RE = re.compile(r"^Associated with (.+)$")


def parse_honors_section(html):
    """Validated against a real 8-award sample (see docs/FINDINGS.md). Heading is
    'Honors & awards' (lowercase 'awards') not 'Honors & Awards'. Fields per award:
    title (line before 'Issued by ... · date'), issuer, date, optional 'Associated
    with {org}', optional description, and a catch-all `tags` list for short trailing
    labels (e.g. event short-name badges)."""
    soup = BeautifulSoup(html, "lxml")
    heading = next((h for h in soup.find_all(["h1", "h2"]) if h.get_text(strip=True).lower() == "honors & awards"), None)
    if heading is None:
        return []
    container = heading.find_parent("section") or heading.parent
    if container is None:
        return []

    lines = [t.strip() for t in container.stripped_strings if t.strip()]
    lines = [l for l in lines if l.lower() != "honors & awards"]
    return _group_honors(lines)


def _group_honors(lines):
    awards = []
    pending_title = None
    current = None
    for line in lines:
        m = ISSUED_BY_RE.match(line)
        if m:
            if current:
                # the last "tag" tentatively appended was actually this new
                # award's title, not a trailing tag of the previous one.
                if current["tags"] and current["tags"][-1] == pending_title:
                    current["tags"].pop()
                awards.append(current)
            current = {"title": pending_title, "issuer": m.group(1).strip(), "date": m.group(2).strip(),
                       "associated_with": None, "description": None, "tags": []}
            pending_title = None
            continue
        if current is None:
            pending_title = line
            continue
        am = HONORS_ASSOCIATED_RE.match(line)
        if am:
            current["associated_with"] = am.group(1)
            continue
        if current["description"] is None and len(line) > 30:
            current["description"] = line
            continue
        current["tags"].append(line)
        pending_title = line
    if current:
        awards.append(current)
    return awards


def parse_skills_section(html):
    """Validated against real captured content (see docs/FINDINGS.md): each skill is
    followed by free-form association lines (where it was used: a role/school, project
    names, 'Passed LinkedIn Skill Assessment', 'Show all N details' if truncated) and
    always terminated by a literal 'Endorse' line -- used here as the reliable
    per-skill delimiter, since there's no per-skill wrapper element to scope on."""
    soup = BeautifulSoup(html, "lxml")
    heading = next((h for h in soup.find_all(["h1", "h2"]) if h.get_text(strip=True) == "Skills"), None)
    if heading is None:
        return []
    container = heading.find_parent("section") or heading.parent
    if container is None:
        return []

    lines = [t.strip() for t in container.stripped_strings if t.strip()]
    lines = [l for l in lines if l != "Skills" and l.lower() not in _SKILLS_CATEGORY_TABS]
    return _group_skills(lines)


def _group_skills(lines):
    skills = []
    current_name = None
    current_details = []
    for line in lines:
        if line == "Endorse":
            if current_name:
                skills.append({"name": current_name, "details": current_details})
            current_name = None
            current_details = []
            continue
        if current_name is None:
            current_name = line
        else:
            current_details.append(line)
    return skills


def _group_skills_from_items(item_line_lists):
    """RSC path only: builds on the collection's own item boundaries instead of the
    "Endorse" text delimiter -- see rsc.collection_item_lines()."""
    skills = []
    for lines in item_line_lists:
        lines = [l for l in lines if l.lower() not in _SKILLS_CATEGORY_TABS and l != "Endorse"]
        if not lines:
            continue
        skills.append({"name": lines[0], "details": lines[1:]})
    return skills


def parse_generic_detail_section(html, section_name):
    """NOTE: `data-view-name="profile-{section}-details-view"` is NOT a content
    container -- confirmed (see docs/FINDINGS.md) it's the sticky-header tracking
    scope, which only ever contains the profile's own name/headline/Message button.
    Using it as primary produced exactly that boilerplate as fake "content" for
    Skills/Certifications/Languages/Projects. Heading-text match is the reliable
    scope (same approach parse_education() uses); boilerplate is filtered as a
    second line of defense regardless of which path finds the container."""
    soup = BeautifulSoup(html, "lxml")
    heading = None
    for h in soup.find_all(["h1", "h2"]):
        if h.get_text(strip=True).lower() == section_name.lower():
            heading = h
            break

    if heading is None:
        # No fallback to the "profile-{section}-details-view" marker: confirmed
        # (see docs/FINDINGS.md) it's always just the sticky header, never content.
        return []
    container = heading.find_parent("section") or heading.parent
    if container is None:
        return []

    text = container.get_text("\n", strip=True)
    lines = [l for l in text.split("\n") if l and l.lower() != section_name.lower()]
    return _filter_generic_detail_lines(lines, section_name)


def _filter_generic_detail_lines(lines, section_name):
    lines = [l for l in lines if l.lower() != section_name.lower()]
    lines = [l for l in lines if l.lower() not in _BOILERPLATE_LINES
             and not CONNECTION_DEGREE_RE.match(l) and not PRONOUN_RE.match(l)]
    return lines


# --------------------------------------------------------------------------- #
# Activity / posts -- UNVERIFIED URL family (`/recent-activity/...`), not tested this
# session at all. Best-effort generic block extraction only.
# --------------------------------------------------------------------------- #

def parse_activity(html):
    soup = BeautifulSoup(html, "lxml")
    main = soup.find("main") or soup.find(attrs={"aria-label": "Primary content"}) or soup

    posts = []
    # Try common LinkedIn post-container markers first; fall back to a generic
    # "list item" scan if none match -- this is a guess at structure, not a
    # confirmed selector, and should be the first thing fixed once real HTML
    # from this route is inspected.
    candidates = main.find_all(attrs={"data-view-name": True})
    seen_texts = set()
    for el in candidates:
        dv = el.get("data-view-name", "")
        if "feed" in dv.lower() or "post" in dv.lower() or "update" in dv.lower():
            text = el.get_text(" ", strip=True)
            if text and text not in seen_texts and len(text) > 20:
                seen_texts.add(text)
                posts.append({"text": text, "source_marker": dv})

    if not posts:
        # Fallback: just return distinct paragraph-length text blocks from <main>,
        # deduplicated -- unstructured, but better than nothing until this route's
        # real markup is inspected and this function is rewritten against it.
        for el in main.find_all(["p", "span", "div"]):
            text = el.get_text(" ", strip=True)
            if text and len(text) > 40 and text not in seen_texts:
                seen_texts.add(text)
                posts.append({"text": text, "source_marker": None})

    return posts


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #

REQUEST_DELAY_SECONDS = float(os.environ.get("EXTRACTOR_REQUEST_DELAY", "2.0"))


def _pace():
    """Sleep between page fetches. Confirmed empirically (see docs/FINDINGS.md): firing
    8 requests back-to-back in under a second causes LinkedIn to serve a degraded,
    hydration-only response for the `/details/*` routes (missing the pre-rendered HTML
    entirely) even though the same route returns full content as an isolated request.
    This isn't fingerprint spoofing -- it's just not hammering the server unrealistically
    fast, which is both reasonable etiquette and required for this to work at all."""
    if REQUEST_DELAY_SECONDS > 0:
        time.sleep(REQUEST_DELAY_SECONDS)


def extract_profile(profile_url, li_at, on_progress=None):
    """on_progress(message: str), if given, is called at each meaningful checkpoint --
    used by api.py's async job endpoint to surface a live progress log to the frontend
    while a cold (uncached) fetch is in flight."""
    def progress(msg):
        if on_progress:
            on_progress(msg)

    slug = vanity_slug_from_url(profile_url)
    session = LinkedInSession(li_at)

    result = {
        "profile": {
            "url": f"https://www.linkedin.com/in/{slug}/",
            "name": None,
            "headline": None,
            "location": None,
            "about": None,
            "image_url": None,
            "connection_degree": None,
            "current_company_school_summary": None,
        },
        "experience": [],
        "education": [],
        "skills": [],
        "certifications": [],
        "languages": [],
        "projects": [],
        "honors_awards": [],
        "recommendations": [],
        "posts": [],
        "_meta": {"fetch_status": {}},
    }

    progress("Fetching base profile…")
    status, profile_html = fetch_section(session, slug, section=None)
    result["_meta"]["fetch_status"]["profile"] = status
    if status == 200 and profile_html:
        top_card, soup = parse_top_card(profile_html)
        result["profile"].update(top_card)
        result["profile"]["about"] = parse_about(soup)
        progress(f"Base profile fetched — {top_card.get('name') or slug}")
    else:
        progress(f"Base profile fetch failed (HTTP {status})")

    _pace()
    progress("Fetching Experience…")
    status, html = fetch_section(session, slug, section="experience")
    result["_meta"]["fetch_status"]["experience"] = status
    if status == 200 and html:
        result["experience"] = parse_experience(html)
        progress(f"Experience — {len(result['experience'])} entries")
    else:
        progress(f"Experience fetch failed (HTTP {status})")

    # Education, Skills, Projects, Honors & Awards, Certifications, Languages, and
    # Recommendations are NOT reachable via the `/details/{section}/` page routes above
    # (confirmed dead end -- see docs/FINDINGS.md). They're served bundled together via
    # `actions/component`, discovered per-profile since which sections bundle under which
    # componentId varies profile to profile. Confirmed reachable with cookie + CSRF only,
    # no fingerprint headers -- see docs/FINDINGS.md "Correction + new lead".
    sections, section_logos, component_fetch_status = ({}, {}, {})
    if status == 200 and profile_html:
        sections, section_logos, component_fetch_status = fetch_bundled_sections(
            session, slug, profile_html, on_progress=progress
        )
    result["_meta"]["fetch_status"].update({f"component:{k}": v for k, v in component_fetch_status.items()})

    if "educationTopLevelSection" in sections:
        lines = _strip_heading(rsc.section_lines(sections["educationTopLevelSection"]), "Education")
        result["education"] = _group_education(lines, section_logos.get("educationTopLevelSection"))
        progress(f"Parsed Education — {len(result['education'])} entries")

    if "skillsSection" in sections:
        item_lines = rsc.collection_item_lines(sections["skillsSection"])
        if not item_lines:
            # Short skill lists aren't always wrapped in an initialItems collection --
            # fall back to per-skill componentKey boundaries (see rsc.py).
            item_lines = rsc.component_item_lines(sections["skillsSection"], "com.linkedin.sdui.profile.skill(")
        result["skills"] = _group_skills_from_items(item_lines)
        progress(f"Parsed Skills — {len(result['skills'])} entries")

    if "projectsSection" in sections:
        lines = _strip_heading(rsc.section_lines(sections["projectsSection"]), "Projects")
        result["projects"] = _group_projects(lines, section_logos.get("projectsSection"))
        progress(f"Parsed Projects — {len(result['projects'])} entries")

    if "honorsSection" in sections:
        lines = _strip_heading(rsc.section_lines(sections["honorsSection"]), "Honors & awards")
        result["honors_awards"] = _group_honors(lines)
        progress(f"Parsed Honors & Awards — {len(result['honors_awards'])} entries")

    if "certificationTopLevelSection" in sections:
        lines = rsc.section_lines(sections["certificationTopLevelSection"])
        result["certifications"] = _filter_generic_detail_lines(lines, "Licenses & certifications")
        progress(f"Parsed Certifications — {len(result['certifications'])} entries")

    if "languageTopLevelSection" in sections:
        lines = rsc.section_lines(sections["languageTopLevelSection"])
        result["languages"] = _filter_generic_detail_lines(lines, "Languages")
        progress(f"Parsed Languages — {len(result['languages'])} entries")

    if "recommendationsTopLevelSection" in sections:
        lines = rsc.section_lines(sections["recommendationsTopLevelSection"])
        result["recommendations"] = _filter_generic_detail_lines(lines, "Recommendations")
        progress(f"Parsed Recommendations — {len(result['recommendations'])} entries")

    _pace()
    progress("Fetching recent posts…")
    status, html = fetch_activity(session, slug, kind="posts")
    result["_meta"]["fetch_status"]["posts"] = status
    if status == 200 and html:
        result["posts"] = parse_activity(html)

    progress("Done.")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile_url", help="e.g. https://www.linkedin.com/in/satyanadella/")
    parser.add_argument("--li-at", dest="li_at", default=None,
                         help="li_at cookie value (prefer LI_AT env var instead)")
    args = parser.parse_args()

    li_at = get_li_at(args.li_at)
    result = extract_profile(args.profile_url, li_at)
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
