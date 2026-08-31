# LinkedIn Profile API — Reverse-Engineering Findings

Live investigation against an authenticated LinkedIn web session (real account) plus
unauthenticated `curl` checks, 2026-08-31. Documents only requests and responses actually
observed and reproduced — no guessed endpoints, no fabricated fingerprint headers. See
"Detailed evidence trail" below for the full reasoning and every test that led here; this
top section is the current, correct state — later findings supersede earlier ones where
they conflict.

**Assignment constraint** (Tross Careers): the deployed solution must directly hit
LinkedIn endpoints over HTTP — no browser automation in the final service.

## Summary: what works, what doesn't, and why

LinkedIn's web client has three architecturally distinct surfaces, not two as originally
modeled — this was corrected mid-investigation (see "Correction" below):

1. **Plain server-rendered page routes** (`/in/{slug}/`, `/in/{slug}/details/experience/`)
   — real HTML, reachable with only the `li_at` session cookie, no other headers
   required. This is what `direct-api/extractor.py` uses for base profile + Experience.
2. **`flagship-web/rsc-action/actions/pagination`** — gated behind client-fingerprint
   headers (`x-li-track` and several per-session correlation IDs) that only a genuine,
   undisguised browser instance can produce. Confirmed closed (three separate
   legitimate-auth-only tests all failed).
3. **`flagship-web/rsc-action/actions/component`** (`componentId=...
   profileCardsBelowActivityPart1WithoutExp`) — on the surface, looks identical to #2
   (same action family, same URL pattern), but **works with only cookie + a properly-
   derived CSRF token, no fingerprint headers at all**. Confirmed directly: real
   Education content (institution, degree, dates, activities) returned this way,
   verified against the actual test profile's real data. Retested `pagination` on the
   *exact same* session immediately after — still `500` — so this is a genuine
   per-action difference, not a session fluke.

| Section | Status | Why |
|---|---|---|
| Base profile (name, headline, location, image, connection degree) | ✅ Works, live-validated | Plain SSR page route, cookie-only |
| Experience | ✅ Works, live-validated (5-entry, multi-position sample, byte-for-byte match) | Same |
| Education | ✅ Works — cookie + CSRF only, no fingerprint headers | `actions/component`, verified against real data |
| Certifications | ✅ Reachable the same way (resolved in the same response as Education) | Empty on the test profile, but the mechanism is confirmed, not blocked |
| Skills (standalone section) | ❓ Not yet retested against `component` | Previously found blocked via `pagination`; may behave like Education instead — untested |
| Projects | ❓ Not yet retested against `component` | Same caveat — was lazy (`initialContent:"$undefined"`) in the one bundled response seen so far |
| Honors & Awards | ❓ Not yet retested against `component` | Not seen in the bundled response yet — may need a different `componentId` |
| Recommendations | ⚪ Genuinely empty on test profile | Confirmed real empty state ("You haven't received a recommendation yet") |
| Languages | ⚪ Unclear — no content, no explicit empty-state message either | Lower confidence than the others; not fully resolved |
| Full ("+N more") skill lists per entry | ❌ Blocked (via the `pagination`-based overlay path tested so far) | Worth retesting via `component` given the reversal above — not yet done |
| Company/school logo URLs | ⚪ Mixed — absent from the Experience response, present in this Education response | `_extract_logo_urls_in_order()` already handles the format when present |
| Posts / activity | ❌ Broken | `/recent-activity/shares/` doesn't resolve correctly (generic title, no `<main>`); not solved, not guessed further |

**Correction: "blocked" was not a closed question — it was specific to one action.**
The original three-test closure (below) was real and correct *for `pagination`*, but was
incorrectly generalized to "the whole `rsc-action` surface." An external second opinion
(brought in by the project owner, verified directly against real response data rather
than taken on faith) identified `actions/component` as behaving differently. Worth
stating plainly: this was a real gap in the investigation's rigor, not a case of new
information becoming available — the `component` endpoint was observable the whole time
in earlier browser captures, it just wasn't tested with the same minimal-auth discipline
applied to `pagination` until directly prompted to.

**`pagination`-specific closure (still stands for that one action):** three independent
tests against the pagination endpoint, each adding only legitimate (non-fingerprint)
auth, all failed: cookie alone → `403 CSRF validation failed`; add a properly-derived
`csrf-token` → `500`;
add static/structural headers (no fingerprint, no per-session IDs) → still `500`. Separately,
replaying a real DevTools-captured request (genuine browser-generated `x-li-track` and
correlation IDs) *did* work — proving the mechanism, but only because those values were
values a real Chrome instance had already produced; a script cannot generate them
independently. Fabricating them is bot-detection evasion and out of scope. Full detail
in "Detailed evidence trail" below.

**A browser-automation (Playwright) track was built and abandoned.** It could reach the
gated sections in principle, but a Playwright-driven browser gets its LinkedIn session
revoked by the server within 1-2 page loads — reproduced 3 times, including with a
persistent browser profile (real accumulated history across runs, not a fresh throwaway
one) and slower request pacing. Neither helped. Worse: the revocation is at the
session/account level, not scoped to whichever client triggered it — a Playwright run
failing partway then poisons the direct-HTTP track's access to the same session too. The
remaining fix (disguising the browser as non-automated — patching `navigator.webdriver`,
stealth plugins, synthetic human-like timing) is the same evasion category already
declined for the fingerprint header, just at the browser-engine layer. Declined for the
same reason. Code remains at `playwright-scraper/` for reference but is not the
recommended path.

**Verified, changes the Track A conclusion**: profile sections are served *bundled*
(multiple sections per request, e.g. Education + Certifications together via
`actions/component`), not one-per-request as `pagination` suggested — see "Correction +
new lead" in the evidence trail, and the per-action distinction above. This directly
unlocked Education (and Certifications) on Track A with zero evasion. Skills, Projects,
Honors, and the full-skill-list overlay were previously tested against `pagination` only
and found blocked there — they have **not** yet been retested against `component` or
other `componentId` values with the same minimal-auth approach, so "blocked" for those
should be read as "blocked via the one action tested so far," not closed the way
`pagination` itself is.

**Bottom line for the submission (updated):** direct HTTP (`direct-api/`) now reliably
delivers base profile, Experience, and Education (Certifications when populated) with no
evasion of any kind. Skills/Projects/Honors/full-skill-lists remain open questions worth
retesting the same way before writing them off — this investigation's own mistake
(generalizing one action's closure to the whole surface) is a reason for a bit more
humility here, not less rigor.

## Two real bugs found and fixed while building the extractor

1. **Character encoding**: `requests` doesn't get an explicit charset from LinkedIn's
   response headers and falls back to Latin-1 per RFC 2616, mangling every multi-byte
   UTF-8 character (`·` becomes `Â·`), corrupting several regex-based field splits. Fix:
   force `resp.encoding = "utf-8"` before reading `.text`.
2. **Session cookie-jar contamination**: reusing one `requests.Session` across multiple
   page fetches accumulates LinkedIn's own `Set-Cookie` values (`bcookie`, `bscookie`,
   `lang`, `lidc`, `sdui_ver`, `JSESSIONID`). Once accumulated, subsequent `/details/*`
   requests get served a lighter, hydration-only response missing the pre-rendered HTML
   entirely — confirmed by isolating the same request with only `li_at` set, which
   reliably gets the full page. Fix: reset the cookie jar to just `li_at` before every
   single request, even within the same session object. Pacing alone did **not** fix
   this — it's the accumulated cookies, not request rate.

Plus a third, found via a live run against the deployed extractor: `parse_generic_detail_section()`
had a fallback to `profile-{section}-details-view` (proven to always be just the sticky
header, never real content) that produced false-positive "content" — your own
name/headline — for genuinely empty sections. Fixed by removing the fallback entirely.

---

# Detailed evidence trail

This section preserves the full investigation, in the order it happened, including
hypotheses that were later revised or overturned — kept for anyone who wants to see the
actual reasoning and verify nothing was guessed. The Summary above is the authoritative
current state; where this section and the Summary disagree, trust the Summary.

## Confirmed test results — early endpoint survey

| # | Path tested | Method | Result | Auth required |
|---|---|---|---|---|
| 1 | `voyager/api/identity/profiles/{vanityName}/profileView` (classic REST, historically used by most public LinkedIn-scraping writeups) | GET | **410 Gone** — `{"status":410}`. Deprecated server-side. | n/a (dead) |
| 2 | `voyager/api/graphql` queryId `voyagerIdentityDashProfiles.b5c27c04968c409fc0ed3546575b9b7a`, `variables=(memberIdentity:<vanityName>)` | GET | **200 OK** — resolves vanity name → canonical profile URN (`entityUrn`, `versionTag` only). No content fields. | `li_at`/`JSESSIONID` cookie + `csrf-token` header (= JSESSIONID value). No fingerprint headers. |
| 3 | `flagship-web/in/{vanityName}/` (modern web client's main profile route) | POST | **200 OK**, returns React Server Components ("Flight") wire-format stream (~400KB+), not JSON. | Cookie **+ ~9 custom `x-li-*` headers**, most notably `x-li-track` (opaque ~218-char client/device fingerprint blob). Anti-bot fingerprint scoring, not ordinary auth. |
| 4 | `flagship-web/rsc-action/actions/server-request` — "Save to PDF" | POST | **200 OK**, returns a signed download link under `linkedin.com/ambry/...`. | Same `rsc-action` surface — same fingerprint headers as #3. |
| 5 | `flagship-web/rsc-action/actions/navigation` — "Contact info" overlay | POST | **200 OK** | Same `rsc-action` surface — same fingerprint headers as #3. |
| 6 | Public profile URL, fully unauthenticated (`curl`, no cookies, browser UA) | GET | **HTTP 999** (LinkedIn's dedicated anti-bot block code) + JS redirect to `/authwall`. No SEO/crawler HTML. | Blocked outright |
| 7 | `/in/{vanityName}/details/experience/` — plain `curl` outside the browser, `li_at` cookie only | GET | **HTTP 200**, ~1.1MB HTML containing full rendered experience content. Real, non-evasive, cookie-only success. | `li_at` cookie only |
| 8 | `/in/{vanityName}/` (base profile route) — same plain `curl`, cookie only | GET | **HTTP 200**, ~1.0MB HTML, real headline text confirmed. | `li_at` cookie only |

**Content format of #7/#8**: the visible profile text sits inside real, pre-rendered
semantic HTML (`<p class="_02484ad3..."><span>Software Engineer at SalesCode.ai</span></p>`),
not the React Flight wire format — a separate embedded blob duplicates the content in
Flight format for client-side hydration, but it can be ignored entirely. Class names are
opaque build hashes, so parsing relies on tag structure/order, not class names.

**Revised conclusion at this point**: the `x-li-track` fingerprint gating found in #3–#5
applies to the internal SPA client-transition API, not to a plain top-level GET of the
page route — which is what a non-browser HTTP client naturally does anyway. (Later
findings refined this further — see below.)

## `/details/{section}/` routes — first pass

`/details/education/`, `/details/skills/`, `/details/certifications/`,
`/details/languages/`, `/details/projects/` initially appeared to work the same way
(each returns a real, distinctly-marked `data-view-name="profile-{section}-details-view"`
container) — but the test profile had no populated entries in any of them, so this
"confirmed working" was actually a false signal (see below).

`/recent-activity/shares/` (posts) does not work as expected: generic `<title>LinkedIn</title>`,
no `<main>` element. Flagged as a known gap, not solved.

## Education: the real investigation

The test profile (`aksrv09`) does have real, populated Education content (verified
visually in-browser: Thapar Institute, degree, dates, GPA, activities, skills), but a
cold `curl`/`requests` GET of `/in/aksrv09/details/education/` shows **no education text
anywhere in the response** — not live HTML, not the escaped Flight blob. A genuinely
fresh browser tab with no prior navigation history shows the same: nothing, even after
20+ seconds. It rendered once in a different tab that had a long prior browsing history
in the same session — but with no corresponding network request explaining it.

**Root cause, first found**: content depends on the tab's viewport having actually fired
a **resize event** — not elapsed time, scrolling, or accumulated cookies/storage/
Service-Worker cache (all individually ruled out). A tab stuck at ~150px wide (DevTools
docked open) never shows it even after a full reload; the same URL freshly loaded at a
proper ~1470px viewport from the start still doesn't show it — but firing a resize (even
1px) makes it appear immediately, with **no new network request** observed.

**Root cause, fully solved**: the resize triggers a real, distinct request that isn't a
normal page GET:

```
POST /flagship-web/rsc-action/actions/pagination?sduiid=com.linkedin.sdui.pagers.profile.details.education
```

— on the same fingerprint-gated `flagship-web/rsc-action/*` surface as #3–#5 above,
carrying the full header battery (`x-li-track`, `x-li-page-instance`,
`x-li-application-instance`, `x-li-page-instance-tracking-id`,
`x-li-traceparent`/`x-li-tracestate`, `x-li-anchor-page-key`). Replaying a real
DevTools-captured version of this request worked — full Flight-format payload, all 3
education entries, plus (bonus) real company/school logo URLs in a
`rootUrl` + `imageRenditions[].suffixUrl` format. This confirms the mechanism but not a
repeatable path: those header values only exist because a real Chrome instance generated
them.

**Confirmed minimal-auth is genuinely insufficient** (three isolated tests, no
fingerprint fabrication in any): (1) cookie only → `403 CSRF validation failed`; (2) add
a properly-derived `csrf-token` (JSESSIONID from a preflight GET, the same pattern that
already works for the GraphQL identity endpoint) → `500 Server internal error`; (3) add
two more static/structural headers (`x-li-rsc-stream: true`, `x-li-anchor-page-key`,
neither device-derived nor per-session) → still generic `500`. The only untested headers
left are the fingerprint and random per-pageview correlation IDs — deliberately not
fabricated. This closes the question rather than leaving it as merely untested.

**Education parser**: once rendered, `aksrv09` actually has 3 entries (Thapar Institute
BE Computer Engineering, plus two City Montessori School entries). Used this real text to
fix a real grouping bug — an un-truncated "Skills: Computer Science" line (no "+N skills"
suffix) was misread as a new institution name, corrupting entries 2 and 3. Fixed with a
one-shot `awaiting_skills` flag, same pattern as `awaiting_location` in the experience
parser. Verified against a synthetic reconstruction of the real 3-entry structure.

## Full skill lists, logos, and the false-positive bug

The inline "...+N skills" text on every experience/education entry is always truncated.
Each has a real link to `/in/{slug}/overlay/{numericId}/skill-associations-details/`
showing the complete list (confirmed live: modal "Skills for {entity}", 6 skills for the
Thapar entry vs. 2 shown inline). `extractor.py` captures these hrefs
(`skills_overlay_path`) and has `parse_skill_overlay()` ready, but fetching that URL cold
returns `HTTP 500` regardless of auth attempted — not wired into the pipeline.

Company/school logos: the `<figure data-view-name="image">` next to each entry only
contains a `<span data-dynamic-icon-loading>` placeholder — no live `<img src>`. Other
images in the document use a split base-URL + `imageRenditions[].suffixUrl` encoding
(`_extract_logo_urls_in_order()` reconstructs these), but of 18 such images found in the
Experience response, all were people's profile photos — zero were logos.

**False-positive content bug**: a live run against the deployed extractor showed
Skills/Certifications/Languages/Projects (all empty on the test profile) returning
`["Akarsh Srivastava", "Software Engineer at SalesCode.ai", "Message"]` — looked like
real data but was the sticky-header boilerplate, because `parse_generic_detail_section()`
fell back to the `profile-{section}-details-view` marker (proven to always be the sticky
header) whenever the heading-text match failed. Fixed by removing the fallback; empty
sections now correctly return `[]`.

## Skills / Projects / Honors & Awards / Recommendations — full investigation

Same resize-trigger methodology as Education, confirmed real content in three more
sections and genuine emptiness in two:

- **Skills**: 10 skills (C++, Java, PHP, Dart, Nginx, Spring Boot, Postman API, GCP,
  Next.js, Burp Suite), each with rich context (where used, projects, assessment
  badges). `parse_skills_section()` groups by the literal "Endorse" line that terminates
  every entry — validated against a synthetic reconstruction of the real sample.
- **Projects**: 10 real projects with dates, descriptions, "Associated with {org}",
  skills, GitHub/Devfolio links. `parse_projects_section()` had a real bug — a project's
  name line leaked into the *previous* project's trailing links, since nothing in the
  flat text distinguishes "next project's name" from "this project's trailing link" —
  fixed with a tentative-append + retroactive-pop pattern once the next date-range line
  confirms which it was. Hit the same un-truncated-skill-line bug as Education, same fix.
- **Honors & Awards**: 8 real awards — title, issuer, date, optional "Associated with",
  description, tags. Real heading is "Honors & awards" (lowercase "awards"), would have
  silently failed to match as "Honors & Awards". Hit and fixed the identical
  name-leaking-into-previous-entry bug as Projects.
- **Certifications**: genuinely empty — real "Nothing to see for now" empty state. Real
  heading is "Licenses & certifications", not "Certifications" — fixed.
- **Recommendations**: genuinely empty — "You haven't received a recommendation yet."
  Route confirmed real, with Received/Given tabs.
- **Languages**: still shows no content and no heading at all after the resize-trigger —
  unlike the others, no explicit empty-state message seen either. Lower confidence.

All confirmed blocked via isolated cold fetches the same way as Education — `200` status,
no real content, systemic across the whole `/details/{section}/` family.

**Secondary finding**: attempted to batch-check four sections at once by opening one tab
per section and firing a single window resize, reasoning that all tabs share one OS
window. It didn't work — only the tab that was actually focused at resize time got a
live layout update; background tabs kept stale state regardless of which tab ID the
resize call named. Reverted to one tab, checked sequentially.

## Automation-triggered session revocation (why Track B was abandoned)

Initially looked like a pacing issue — it isn't. A `li_at` session confirmed working via
plain direct-HTTP immediately beforehand was revoked by LinkedIn's server
(`Set-Cookie: li_at=delete me; ...Expires=1970` + `Clear-Site-Data: "storage"`) within
1-2 page loads of a Playwright-driven browser touching it. Reproduced 3 times, including
with `SCRAPER_REQUEST_DELAY` raised to 8s and with a persistent browser profile
(`launch_persistent_context`, real accumulated history/cache across runs instead of a
fresh throwaway context) specifically to test whether profile freshness was the trigger.
Neither helped — same failure point every time (the 2nd navigation).

**Critical detail**: the revocation is at the session/account level, not scoped to the
client that triggered it. After a Playwright run fails partway through, the *same* `li_at`
cookie then fails on a plain `requests` call too — confirmed directly. So merely having a
Playwright instance touch the session, even briefly, poisons it for the direct-HTTP track
as well. There's no safe "mix both tracks" pattern.

The remaining fix would be disguising the browser as non-automated — patching
`navigator.webdriver`, stealth plugins, synthetic human-like interaction timing — the
same evasion category already declined for the `x-li-track` fingerprint header, just at
the browser-engine layer. Declined for the same reason. Track B is not being pursued
further as a result, not paused pending more ideas.

## Correction + new lead: profile sections are bundled, not one-per-request

A second-opinion review (external, via the project owner) correctly identified something
this document had missed: a **different** action than the `pagination` one analyzed
above — `POST flagship-web/rsc-action/actions/component?componentId=...
profileCardsBelowActivityPart1WithoutExp` — returns a bundle covering multiple
below-the-fold sections in one response: `educationTopLevelSection`,
`certificationTopLevelSection`, `connectedAccountTopLevelSection`, `projectsSection`,
`volunteerExperienceTopLevelSection`. Verified directly against a real captured response
(different profile than used elsewhere in this doc): Education and Certifications came
back with `initialContent` fully resolved (real institution/dates/credential IDs/logo
URLs for both), while Connected Accounts, Projects, and Volunteer Experience were still
`initialContent:"$undefined"` (lazy).

**Correction, verified in a follow-up test: this DOES change the Track A conclusion, for
this specific action.** The `pagination` closure above was written as if it applied to the
whole `flagship-web/rsc-action/actions/*` surface. It doesn't -- it was only ever tested
against the `pagination` action. `component` was never independently tested with
minimal, legitimate-only auth until this follow-up. Doing that test properly (same
methodology as the `pagination` closure: cookie-only, then +CSRF token derived from a
legitimate JSESSIONID preflight, **no fingerprint headers at all**) got a real result:

- `component` with cookie only -> `403`
- `component` with cookie + derived `csrf-token`, no `x-li-track`/`x-li-page-instance`/etc
  -> **`200 OK`**, with real, resolved Education content in the body (verified against
  previously-validated real text for this profile).
- Immediately re-testing `pagination` on the *identical* session, right after -> still
  `500`. This rules out "the session just started working" as an explanation -- it's a
  genuine per-action difference, not a fluke.

So: **Education and Certifications are confirmed extractable via Track A (direct HTTP, no
browser, no evasion)**, through `component`, not `pagination`. The earlier "closed
question" framing was a real gap in that investigation's rigor -- `component` was assumed
to behave like `pagination` because it's nominally "the same URL family," and that
assumption was never tested until now. It should have been tested, not assumed.

**Skills is a promising additional lead, not yet confirmed.** A separate captured request
(`componentId=...profileCardsBelowActivityPart7`) shows Skills is served by the same
bundled `component` mechanism, with real resolved endorsement data (skill names, endorser
names, endorsement counts). But that capture still carried the full `x-li-track`
fingerprint battery -- it's a real DevTools capture, not a minimal-auth test -- so it
doesn't yet prove Skills is reachable with cookie+CSRF alone the way Education was just
proven. Needs the identical controlled test (cookie-only -> +CSRF, no fingerprint headers)
before it can be called confirmed one way or the other. Projects and Honors & Awards are
unverified in either direction.

**Matters for Track B too**, independent of the above: if a single base profile page load
bundles multiple sections per `component` call instead of needing one hard navigation per
section, that's fewer round-trips per session -- which may reduce (not necessarily
eliminate) the session-revocation risk documented below. This is a secondary point; the
primary result is that Track A now covers more ground than previously documented.

**Also note**: this response contained real company/school logo URLs (`rootUrl` +
`imageRenditions[].suffixUrl`, same format `_extract_logo_urls_in_order()` already
handles) for Education entries -- contradicting the earlier "logos not present in this
response" finding for *Experience*. Logos may be present for some sections/endpoints and
not others; not yet fully mapped.

## Reference implementation cross-check

[`joeyism/linkedin_scraper`](https://github.com/joeyism/linkedin_scraper) — the most
prominent public LinkedIn scraper — confirmed via its own README to use Playwright (a
real automated browser with a saved authenticated session), not direct API calls.
Consistent with everything above: a real browser sidesteps the fingerprint wall by
generating a genuine signature rather than by finding an undiscovered lightweight API —
though this investigation additionally found that a *freshly automated* real browser
still gets flagged and revoked, which that project's README doesn't address.
