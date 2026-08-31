# LinkedIn Profile API

Accepts a LinkedIn profile URL, returns structured JSON: name, headline, location, about,
experience, education, skills, certifications, languages, and profile image — reverse-engineered
via direct HTTP requests, no browser automation.

## Setup

```bash
cd direct-api
pip install -r requirements.txt
```

You need your own LinkedIn `li_at` session cookie (DevTools → Application → Cookies →
`linkedin.com` → `li_at`). Set it as an environment variable — never commit it, never pass it on
the command line where it could land in shell history:

```bash
export LI_AT="your_li_at_value_here"
```

or create `direct-api/.env` (gitignored) with `LI_AT=your_li_at_value_here` and load it before
running (`set -a && source .env && set +a`).

## Running the API

```bash
cd direct-api
uvicorn api:app --host 0.0.0.0 --port 8000
```

Then open **http://localhost:8000/** for a small web UI — paste a profile URL, nothing
else. Credentials are never entered in the browser; the server reads `LI_AT` from its own
environment.

Or call the API directly:

```bash
curl -X POST http://localhost:8000/profile \
  -H "Content-Type: application/json" \
  -d '{"profile_url": "https://www.linkedin.com/in/example/"}'
```

`GET /health` returns `{"status": "ok"}` for liveness checks.

## API documentation

### `POST /profile`

**Request body**
```json
{ "profile_url": "https://www.linkedin.com/in/example/" }
```

**Response** (`200`)
```json
{
  "profile": {
    "url": "...", "name": "...", "headline": "...", "location": "...",
    "about": "...", "image_url": "...", "connection_degree": "1st",
    "current_company_school_summary": "..."
  },
  "experience": [
    { "title": "...", "company": "...", "date_range": "...", "location": "...",
      "skills": "...", "logo_url": null, "skills_overlay_path": "..." }
  ],
  "education": [], "skills": [], "certifications": [], "languages": [],
  "projects": [], "honors_awards": [], "recommendations": [],
  "_meta": { "fetch_status": { "profile": 200, "experience": 200, "...": "..." } }
}
```

`_meta.fetch_status` reports the real HTTP status per section fetched, so a caller can tell
"genuinely empty on this profile" apart from "blocked" (see Known Limitations).

**Errors**

| Status | Meaning |
|---|---|
| `400` | `profile_url` isn't a valid `linkedin.com/in/{slug}/` URL |
| `429` | Rate limit exceeded (10 requests/minute per IP, configurable via `RATE_LIMIT_PER_MINUTE`) |
| `502` | Fetch or parse failure for reasons other than session revocation |
| `503` | Server misconfigured (`LI_AT` not set) **or** the LinkedIn session has been revoked and needs a fresh `li_at` (see Known Limitations) |

Responses are cached in-memory per `profile_url` for `CACHE_TTL_SECONDS` (default 3600s).

## Approach

Everything is documented, with the actual evidence (captured HTML, exact HTTP responses,
step-by-step reasoning) in [`docs/FINDINGS.md`](docs/FINDINGS.md). Summary:

LinkedIn's web client has two distinct surfaces:

1. **Plain server-rendered page routes** (`/in/{slug}/`, `/in/{slug}/details/experience/`) — real
   HTML, reachable with just the `li_at` session cookie, no other headers required. This is what
   `direct-api/extractor.py` uses.
2. **An internal SPA action API** (`flagship-web/rsc-action/actions/*`, including the pagination
   endpoint that loads Education/Skills/Projects/Honors/Certifications) — gated behind a battery
   of client-fingerprint headers (`x-li-track` and others) that only a genuine, undisguised
   browser instance can produce. Confirmed directly: replaying a real DevTools-captured request
   worked; three separate attempts using only legitimate auth (cookie, then a properly-derived
   CSRF token, then static non-fingerprint headers) all failed. This is a closed, evidenced
   finding, not a guess.

Two real bugs were found and fixed by testing against live responses rather than assuming: (1)
`requests` mis-decodes LinkedIn's response as Latin-1 instead of UTF-8, corrupting non-ASCII
characters; (2) reusing one `requests.Session` across fetches accumulates LinkedIn's own
`Set-Cookie` values, which degrades subsequent responses to a lighter, content-missing variant —
fixed by resetting the cookie jar to just `li_at` before every request.

A Playwright-based (real browser) approach was also built and tested (`playwright-scraper/`,
now inactive) specifically to reach the fingerprint-gated sections. It was abandoned: a
Playwright-driven browser gets its session revoked by LinkedIn's server within 1-2 page loads,
confirmed reproducible even with a persistent browser profile (accumulated real history/cache
across runs, not a fresh throwaway one) and slower request pacing. Worse, once triggered, the
same session then fails for the direct-HTTP client too — it's a session/account-level response,
not scoped to whichever client triggered it. The remaining fix would be disguising the browser as
non-automated (patching `navigator.webdriver`, stealth plugins, synthetic human-like timing),
which is the same category of anti-bot evasion already ruled out for the fingerprint headers,
just at a different layer. Declined for the same reason.

## Known limitations

- **Education, Skills, Projects, Honors & Awards, and Certifications-when-populated are not
  currently extractable.** They come back as empty arrays, honestly reflected in
  `_meta.fetch_status`, not fabricated. This is a confirmed architectural limitation (see
  Approach above and `docs/FINDINGS.md`), not a bug to be fixed with more parsing logic — the
  data genuinely isn't reachable via direct HTTP without reproducing browser-fingerprint
  scoring, which this project treats as out of scope.
- **Full ("+N more") skill lists** on Experience/Education entries are truncated in what direct
  HTTP can reach; the full list lives behind the same fingerprint-gated surface above.
  `skills_overlay_path` is captured in the output for reference even though it can't currently
  be auto-resolved.
- **Company/school logo URLs** are not present in the direct-HTTP response body at all for
  Experience entries (confirmed by checking); `logo_url` will be `null` in practice.
- **The `li_at` session can be revoked by LinkedIn** under load or anomalous request patterns,
  independent of which client (this API or the abandoned browser-based one) triggers it. The API
  surfaces this as a clear `503` rather than crashing, but recovering requires an operator to
  manually supply a fresh `li_at`. There is no automatic re-authentication.
- **Rate limiting and caching are in-memory only** — reset on restart, not shared across multiple
  instances of the service. Fine for a single-process deployment; would need a shared store
  (Redis, etc.) for real horizontal scaling.
- **Posts/activity** (`/recent-activity/...`) was investigated and found not to resolve
  correctly with the routes tried; not included in the output.
- Deploying to serve **arbitrary third-party profiles** at scale would need addressing all of the
  above plus LinkedIn's terms of use, which restrict automated data collection — this project
  treats that as the requester's responsibility to evaluate for their use case, not something
  addressed by the code itself.
