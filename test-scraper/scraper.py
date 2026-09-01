#!/usr/bin/env python3
"""
LinkedIn profile extractor -- Track B, real browser (Playwright).

Exists because Track A (direct HTTP, see ../direct-api/) hit a confirmed, evidence-complete
wall: Education/Skills/Projects/Honors/Certifications-when-populated only load through
LinkedIn's internal `flagship-web/rsc-action/*` action surface, which requires client
fingerprint headers (`x-li-track` etc.) that only a real browser instance can legitimately
generate -- see ../docs/FINDINGS.md for the full evidence trail. A real Playwright browser
sidesteps that wall by being genuine, the same reason github.com/joeyism/linkedin_scraper
works. This conflicts with the client's stated "no browser" requirement -- see project notes;
confirm with them before treating this as the actual submission path.

Reuses every parser already built and validated in ../direct-api/extractor.py: Playwright's
job here is only to produce real rendered HTML for each route, then the SAME BeautifulSoup
parsers extract structured fields, exactly as they do for the direct-HTTP responses.

Known quirk this file works around (see docs/FINDINGS.md "resize-trigger"): Education/
Skills/Projects/Honors do not render on a plain page load, even at a normal desktop
viewport -- they only appear after an actual viewport *resize event* fires, for reasons
that remain only partially understood. A fresh Playwright page never naturally fires one
(viewport is set once at context creation), so this file deliberately fires a 1px resize
after each of those routes loads, mirroring exactly what was manually verified to work.
This is not evasion -- Playwright already carries a legitimate browser fingerprint just by
existing; the resize is working around a frontend rendering quirk, not bypassing security.
"""

import argparse
import json
import os
import sys
import time
from getpass import getpass

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "direct-api"))
import extractor  # noqa: E402  (reuses parse_top_card, parse_experience, parse_education, ...)

from playwright.sync_api import sync_playwright

USER_AGENT = extractor.USER_AGENT

# Routes confirmed (see docs/FINDINGS.md) to need the resize-trigger workaround.
RESIZE_TRIGGER_SECTIONS = {"education", "skills", "projects", "honors", "certifications"}


def get_li_at(cli_value=None):
    return extractor.get_li_at(cli_value)


PROFILE_DIR = os.path.join(os.path.dirname(__file__), ".browser_profile")


def _launch_persistent_context(p, li_at, headless):
    """Uses a persistent user-data-dir (see docs/FINDINGS.md "automation detection")
    instead of a fresh throwaway context every run. A real user's browser isn't blank
    either -- it has real accumulated history/cache/cookies. This lets that build up
    naturally across runs rather than starting from a pristine, obviously-just-created
    profile every single time. Not a fingerprint patch, not stealth -- just not
    discarding state that would normally persist."""
    os.makedirs(PROFILE_DIR, exist_ok=True)
    context = p.chromium.launch_persistent_context(
        PROFILE_DIR,
        headless=headless,
        user_agent=USER_AGENT,
        viewport={"width": 1400, "height": 900},
    )
    context.add_cookies([{
        "name": "li_at",
        "value": li_at,
        "domain": ".linkedin.com",
        "path": "/",
    }])
    return context


REQUEST_DELAY_SECONDS = float(os.environ.get("SCRAPER_REQUEST_DELAY", "4.0"))


def fetch_rendered_html(page, url, needs_resize_trigger=False, timeout=20000):
    if REQUEST_DELAY_SECONDS > 0:
        time.sleep(REQUEST_DELAY_SECONDS)
    page.goto(url, wait_until="domcontentloaded", timeout=timeout)
    page.wait_for_timeout(1500)
    if needs_resize_trigger:
        # Fire a real resize event -- confirmed necessary and sufficient (see
        # docs/FINDINGS.md); a page that loads directly at a fixed viewport never
        # gets one on its own.
        size = page.viewport_size
        page.set_viewport_size({"width": size["width"] + 1, "height": size["height"]})
        page.wait_for_timeout(3000)
    return page.content()


def extract_profile_browser(profile_url, li_at, headless=True):
    slug = extractor.vanity_slug_from_url(profile_url)

    result = {
        "profile": {
            "url": f"https://www.linkedin.com/in/{slug}/",
            "name": None, "headline": None, "location": None, "about": None,
            "image_url": None, "connection_degree": None, "current_company_school_summary": None,
        },
        "experience": [],
        "education": [],
        "skills": [],
        "certifications": [],
        "languages": [],
        "projects": [],
        "honors_awards": [],
        "recommendations": [],
        "_meta": {"fetch_status": {}},
    }

    with sync_playwright() as p:
        context = _launch_persistent_context(p, li_at, headless)
        page = context.new_page() if not context.pages else context.pages[0]

        try:
            html = fetch_rendered_html(page, f"https://www.linkedin.com/in/{slug}/")
            result["_meta"]["fetch_status"]["profile"] = "ok"
            top_card, soup = extractor.parse_top_card(html)
            result["profile"].update(top_card)
            result["profile"]["about"] = extractor.parse_about(soup)

            html = fetch_rendered_html(page, f"https://www.linkedin.com/in/{slug}/details/experience/")
            result["_meta"]["fetch_status"]["experience"] = "ok"
            result["experience"] = extractor.parse_experience(html)

            html = fetch_rendered_html(page, f"https://www.linkedin.com/in/{slug}/details/education/", needs_resize_trigger=True)
            result["_meta"]["fetch_status"]["education"] = "ok"
            result["education"] = extractor.parse_education(html)

            html = fetch_rendered_html(page, f"https://www.linkedin.com/in/{slug}/details/skills/", needs_resize_trigger=True)
            result["_meta"]["fetch_status"]["skills"] = "ok"
            result["skills"] = extractor.parse_skills_section(html)

            html = fetch_rendered_html(page, f"https://www.linkedin.com/in/{slug}/details/projects/", needs_resize_trigger=True)
            result["_meta"]["fetch_status"]["projects"] = "ok"
            result["projects"] = extractor.parse_projects_section(html)

            html = fetch_rendered_html(page, f"https://www.linkedin.com/in/{slug}/details/honors/", needs_resize_trigger=True)
            result["_meta"]["fetch_status"]["honors_awards"] = "ok"
            result["honors_awards"] = extractor.parse_honors_section(html)

            html = fetch_rendered_html(page, f"https://www.linkedin.com/in/{slug}/details/certifications/", needs_resize_trigger=True)
            result["_meta"]["fetch_status"]["certifications"] = "ok"
            result["certifications"] = extractor.parse_generic_detail_section(html, "Licenses & certifications")

            html = fetch_rendered_html(page, f"https://www.linkedin.com/in/{slug}/details/languages/", needs_resize_trigger=True)
            result["_meta"]["fetch_status"]["languages"] = "ok"
            result["languages"] = extractor.parse_generic_detail_section(html, "Languages")

            html = fetch_rendered_html(page, f"https://www.linkedin.com/in/{slug}/details/recommendations/", needs_resize_trigger=True)
            result["_meta"]["fetch_status"]["recommendations"] = "ok"
            result["recommendations"] = extractor.parse_generic_detail_section(html, "Recommendations")

        finally:
            context.close()

    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile_url", help="e.g. https://www.linkedin.com/in/aksrv09/")
    parser.add_argument("--li-at", dest="li_at", default=None,
                         help="li_at cookie value (prefer LI_AT env var instead)")
    parser.add_argument("--headed", action="store_true", help="run with a visible browser window (debugging)")
    args = parser.parse_args()

    li_at = get_li_at(args.li_at)
    result = extract_profile_browser(args.profile_url, li_at, headless=not args.headed)
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
