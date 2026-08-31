#!/usr/bin/env python3
"""
Hosted LinkedIn Profile API -- direct HTTP, no browser (Track A).

    POST /profile
    { "profile_url": "https://www.linkedin.com/in/example/" }

Returns the structured JSON extracted by extractor.py. Known, documented scope (see
../docs/FINDINGS.md): base profile fields and Experience are reliably covered. Education,
Skills, Projects, Honors & Awards, and Certifications-when-populated are NOT currently
extractable via direct HTTP -- confirmed to require client-fingerprint headers only a
real (undisguised) browser instance can legitimately generate, which is out of scope for
a headless, no-browser service. Those fields come back as empty lists, not fabricated or
silently guessed.

Auth: a single li_at session cookie, read from the LI_AT environment variable only --
never accepted from a request, never logged, never returned in any response. See
../docs/FINDINGS.md "session revocation" for why this session can go invalid under load
and needs periodic manual refresh; SESSION_INVALID (503) surfaces that clearly instead of
crashing.
"""

import logging
import os
import threading
import time
import uuid
from urllib.parse import urlparse

from cachetools import TTLCache
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator

import extractor

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("linkedin-profile-api")

app = FastAPI(title="LinkedIn Profile API", version="1.0")

CACHE_TTL_SECONDS = int(os.environ.get("CACHE_TTL_SECONDS", "3600"))
CACHE_MAX_SIZE = int(os.environ.get("CACHE_MAX_SIZE", "500"))
_cache = TTLCache(maxsize=CACHE_MAX_SIZE, ttl=CACHE_TTL_SECONDS)

# Simple in-memory per-IP rate limiter. Known limitation (see README): resets on
# restart and doesn't share state across multiple instances -- fine for a single-process
# deployment, not a substitute for a real rate-limiting layer (e.g. at a reverse proxy)
# under real production load.
RATE_LIMIT_PER_MINUTE = int(os.environ.get("RATE_LIMIT_PER_MINUTE", "10"))
_rate_buckets: dict[str, list[float]] = {}


def _check_rate_limit(client_ip: str) -> None:
    now = time.time()
    window_start = now - 60
    hits = [t for t in _rate_buckets.get(client_ip, []) if t > window_start]
    if len(hits) >= RATE_LIMIT_PER_MINUTE:
        raise HTTPException(status_code=429, detail="Rate limit exceeded. Try again in a moment.")
    hits.append(now)
    _rate_buckets[client_ip] = hits


class ProfileRequest(BaseModel):
    profile_url: str

    @field_validator("profile_url")
    @classmethod
    def validate_linkedin_url(cls, v: str) -> str:
        parsed = urlparse(v)
        if parsed.scheme not in ("http", "https") or "linkedin.com" not in parsed.netloc:
            raise ValueError("profile_url must be a linkedin.com profile URL")
        parts = [p for p in parsed.path.split("/") if p]
        if len(parts) < 2 or parts[0] != "in":
            raise ValueError("profile_url must look like https://www.linkedin.com/in/{slug}/")
        return v


def _extract_and_cache(profile_url: str, on_progress=None):
    """Runs the extraction and translates exceptions into a (status_code, detail) pair
    instead of raising HTTPException -- so this same logic works both synchronously
    (POST /profile) and from a background thread (POST /profile/async), which has no
    request/response cycle to attach an HTTPException to."""
    li_at = os.environ.get("LI_AT")
    if not li_at:
        return None, (503, "Server misconfigured: LI_AT environment variable not set.")
    try:
        result = extractor.extract_profile(profile_url, li_at, on_progress=on_progress)
    except extractor.LinkedInSessionRevokedError:
        log.error("LinkedIn session revoked -- LI_AT needs to be refreshed")
        return None, (503, "LinkedIn session is no longer valid and needs to be refreshed by the operator. Try again shortly.")
    except ValueError as e:
        return None, (400, str(e))
    except Exception:
        log.exception("unexpected error extracting %s", profile_url)
        return None, (502, "Failed to fetch or parse the LinkedIn profile.")
    _cache[profile_url] = result
    return result, None


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/profile")
def get_profile(req: ProfileRequest, request: Request):
    """Synchronous, single-call endpoint -- matches the challenge's required contract
    exactly. A cold (uncached) fetch now covers Education/Skills/Projects/Honors &
    Awards/Certifications/Languages/Recommendations too (see docs/FINDINGS.md), which
    means ~9 paced requests to LinkedIn under the hood; this can take a while. Clients
    that don't want to hold one long request open (this project's own frontend included)
    should use POST /profile/async + GET /profile/status/{job_id} instead."""
    client_ip = request.client.host if request.client else "unknown"
    _check_rate_limit(client_ip)

    if req.profile_url in _cache:
        log.info("cache hit for %s", req.profile_url)
        return _cache[req.profile_url]

    log.info("fetching %s", req.profile_url)
    result, error = _extract_and_cache(req.profile_url)
    if error:
        status_code, detail = error
        raise HTTPException(status_code=status_code, detail=detail)
    return result


# In-memory async job store. Same "single-process only, resets on restart" caveat as
# the cache/rate-limiter above -- fine for this deployment, not a substitute for a real
# queue under multi-instance load. TTL just bounds memory for jobs nobody ever polls to
# completion; it's not a request timeout.
JOB_TTL_SECONDS = int(os.environ.get("JOB_TTL_SECONDS", "600"))
_jobs: TTLCache = TTLCache(maxsize=1000, ttl=JOB_TTL_SECONDS)
_jobs_lock = threading.Lock()


def _append_log(job_id: str, message: str):
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is None:
            return
        job.setdefault("log", []).append({"t": time.time(), "message": message})


def _run_job(job_id: str, profile_url: str):
    result, error = _extract_and_cache(profile_url, on_progress=lambda msg: _append_log(job_id, msg))
    with _jobs_lock:
        if job_id not in _jobs:
            return  # evicted/expired before finishing
        log = _jobs[job_id].get("log", [])
        if error:
            status_code, detail = error
            _jobs[job_id] = {"status": "error", "status_code": status_code, "detail": detail, "log": log}
        else:
            _jobs[job_id] = {"status": "done", "result": result, "log": log}


@app.post("/profile/async", status_code=202)
def start_profile_job(req: ProfileRequest, request: Request):
    """Starts extraction in the background and returns a job id immediately, instead of
    blocking the request for however long a cold fetch takes. Poll
    GET /profile/status/{job_id} for the result."""
    client_ip = request.client.host if request.client else "unknown"
    _check_rate_limit(client_ip)

    job_id = str(uuid.uuid4())
    if req.profile_url in _cache:
        with _jobs_lock:
            _jobs[job_id] = {"status": "done", "result": _cache[req.profile_url], "log": [{"t": time.time(), "message": "Served from cache."}]}
        return {"job_id": job_id, "status": "done"}

    with _jobs_lock:
        _jobs[job_id] = {"status": "pending", "log": []}
    threading.Thread(target=_run_job, args=(job_id, req.profile_url), daemon=True).start()
    return {"job_id": job_id, "status": "pending"}


@app.get("/profile/status/{job_id}")
def get_job_status(job_id: str):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown or expired job_id.")
    if job["status"] == "error":
        raise HTTPException(status_code=job["status_code"], detail=job["detail"])
    return job


@app.exception_handler(Exception)
async def unhandled_exception_handler(request, exc):
    log.exception("unhandled exception")
    return JSONResponse(status_code=500, content={"detail": "Internal server error."})


# Mounted last so it doesn't shadow the API routes above -- serves static/index.html at
# "/". The frontend only ever asks for a profile_url; it has no field for li_at at all,
# since auth is entirely server-side (LI_AT env var, see _get_li_at above).
app.mount("/", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static"), html=True), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
