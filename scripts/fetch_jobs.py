#!/usr/bin/env python3
"""
Siegeworks Job Compiler — Catalog Fetch Script
================================================
Runs inside GitHub Actions every 6 hours.

What it does:
  1. Loads the existing catalog from data/listings.json
  2. Prunes listings older than 7 days (by fetched_at timestamp)
  3. HEAD-verifies remaining listing URLs (removes confirmed 404s)
  4. Fetches new listings from free aggregator APIs
  5. Builds a weighted dynamic ATS allowlist from aggregator results
     (cross-source appearances score higher; narrow sources score less)
  6. Probes each allowlist slug against Greenhouse, Lever, and Ashby
     before committing, so dead slugs don't consume run quota
  7. Fetches directly from confirmed ATS boards
  8. Deduplicates by URL and title+company hash, then writes the fresh catalog

Free API sources (no keys required):
  - Remotive      — remote tech/marketing jobs
  - Jobicy        — remote jobs across categories
  - Arbeitnow     — broad remote listings, EU + US
  - The Muse      — company culture + listings, strong for marketing/creative
  - Remote OK     — real-time remote job board, strong signal for tech hiring
  - Himalayas     — curated remote company listings, growing fast
  - Greenhouse    — direct ATS (dynamic allowlist)
  - Lever         — direct ATS (dynamic allowlist)
  - Ashby         — direct ATS (dynamic allowlist) — top-3 ATS for funded startups

Optional (requires free registration, set as GitHub Secrets):
  - Adzuna        — broad US job coverage including on-site roles
                    Register at developer.adzuna.com
                    Set ADZUNA_APP_ID and ADZUNA_APP_KEY in repo secrets
  - USAJobs       — US federal government listings (high integrity)
                    Register at developer.usajobs.gov
                    Set USAJOBS_API_KEY and USAJOBS_USER_AGENT in repo secrets

NOTE ON ATS ALLOWLIST:
  The dynamic allowlist controls which company domains the GitHub Action
  makes outbound requests to — it is NOT a trust bypass. Every listing
  sourced from Greenhouse/Lever/Ashby still passes through normalise(),
  required-field validation, URL verification, and deduplication,
  exactly like any other source.

SOURCE WEIGHTS:
  Each aggregator source is weighted by its breadth of company coverage.
  Sources with wider reach (on-site + remote, many categories) score higher
  per appearance when building the ATS allowlist. This means a company must
  generate stronger cross-source signals to earn an ATS lookup slot.

  Narrow remote-only sources:   weight 1
  Broader multi-category:       weight 2
  Wide US coverage (on-site +): weight 3
"""

import json
import os
import re
import sys
import hashlib
import httpx
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

CATALOG_PATH  = Path("data/listings.json")
ATS_LIST_PATH = Path("data/ats_allowlist.json")
MAX_AGE_DAYS  = 7
REQUEST_TIMEOUT = 12
VERIFY_TIMEOUT  = 8

# Weighted appearance threshold before a company earns an ATS lookup.
ATS_MIN_SCORE     = 2
ATS_MAX_COMPANIES = 40

# How much each source contributes per company appearance to the allowlist score.
SOURCE_WEIGHTS: dict = {
    "Remotive":   1,
    "Jobicy":     1,
    "Arbeitnow":  1,
    "Remote OK":  1,
    "Himalayas":  1,
    "The Muse":   2,
    "Adzuna":     3,
    "USAJobs":    2,
}
DEFAULT_SOURCE_WEIGHT = 1


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", " ", text or "").strip()


def company_to_slug(name: str) -> str:
    slug = name.lower().strip()
    slug = re.sub(r"[^a-z0-9\s-]", "", slug)
    slug = re.sub(r"\s+", "-", slug)
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug


def make_dedup_key(title: str, company: str) -> str:
    raw = f"{title.lower().strip()}|{company.lower().strip()}"
    return hashlib.md5(raw.encode()).hexdigest()


def load_existing() -> dict:
    if CATALOG_PATH.exists():
        try:
            return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"  Warning: could not parse existing catalog — {e}")
    return {"jobs": [], "updated_at": None, "total": 0}


def load_ats_allowlist() -> dict:
    if ATS_LIST_PATH.exists():
        try:
            return json.loads(ATS_LIST_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_ats_allowlist(allowlist: dict) -> None:
    ATS_LIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    ATS_LIST_PATH.write_text(
        json.dumps(allowlist, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def prune_stale(jobs: list) -> list:
    cutoff = datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)
    kept, removed = [], 0
    for j in jobs:
        fa = j.get("fetched_at")
        if not fa:
            kept.append(j)
            continue
        try:
            ts = datetime.fromisoformat(fa.replace("Z", "+00:00"))
            if ts > cutoff:
                kept.append(j)
            else:
                removed += 1
        except Exception:
            kept.append(j)
    if removed:
        print(f"  Pruned {removed} listing(s) older than {MAX_AGE_DAYS} days")
    return kept


def verify_urls(jobs: list) -> list:
    """
    HEAD-check each listing URL; fall back to streaming GET on 405/501.
    Many ATS servers reject HEAD entirely — the fallback catches those.
    Conservative: keep on network error. Only remove on definitive 404.
    """
    if not jobs:
        return jobs
    kept, removed = [], 0
    with httpx.Client(follow_redirects=True, timeout=VERIFY_TIMEOUT) as client:
        for j in jobs:
            url = j.get("url")
            if not url or not url.startswith("http"):
                kept.append(j)
                continue
            try:
                r = client.head(url)
                if r.status_code == 404:
                    removed += 1
                    print(f"  Removed (404 HEAD): {j.get('title','?')} @ {j.get('company','?')}")
                    continue
                if r.status_code in (405, 501):
                    try:
                        with client.stream("GET", url) as gr:
                            if gr.status_code == 404:
                                removed += 1
                                print(f"  Removed (404 GET): {j.get('title','?')} @ {j.get('company','?')}")
                                continue
                    except Exception:
                        pass
                kept.append(j)
            except Exception:
                kept.append(j)
    if removed:
        print(f"  URL verification removed {removed} dead listing(s)")
    return kept


def normalise(job: dict) -> dict:
    return {
        "id":           job.get("id", ""),
        "title":        strip_html((job.get("title") or "")).strip(),
        "company":      strip_html((job.get("company") or "")).strip(),
        "location":     strip_html((job.get("location") or "")).strip(),
        "remote_type":  (job.get("remote_type") or "See listing").strip(),
        "salary":       job.get("salary"),
        "posted":       (job.get("posted") or "")[:10],
        "url":          (job.get("url") or "").strip(),
        "source":       (job.get("source") or "").strip(),
        "description":  strip_html((job.get("description") or ""))[:600],
        "requirements": job.get("requirements") if isinstance(job.get("requirements"), list) else [],
        "ats_board":    job.get("ats_board"),
        "fetched_at":   job.get("fetched_at") or utcnow_iso(),
    }


def is_valid(job: dict) -> bool:
    return bool(
        job.get("title")
        and job.get("company")
        and job.get("url")
        and job["url"].startswith("http")
    )


# ── Aggregator fetchers ───────────────────────────────────────────────────────

def fetch_remotive() -> list:
    try:
        r = httpx.get("https://remotive.com/api/remote-jobs?limit=100", timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        jobs, now = [], utcnow_iso()
        for j in r.json().get("jobs", []):
            jobs.append(normalise({
                "id": f"remotive_{j['id']}", "title": j.get("title", ""),
                "company": j.get("company_name", ""),
                "location": j.get("candidate_required_location") or "Remote",
                "remote_type": "Remote", "salary": j.get("salary"),
                "posted": (j.get("publication_date") or "")[:10],
                "url": j.get("url", ""), "source": "Remotive",
                "description": j.get("description", "")[:600], "fetched_at": now,
            }))
        jobs = [j for j in jobs if is_valid(j)]
        print(f"  Remotive: {len(jobs)} listings fetched")
        return jobs
    except Exception as e:
        print(f"  Remotive error: {e}", file=sys.stderr)
        return []


def fetch_jobicy() -> list:
    try:
        r = httpx.get("https://jobicy.com/api/v2/remote-jobs?count=50", timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        jobs, now = [], utcnow_iso()
        for j in r.json().get("jobs", []):
            jobs.append(normalise({
                "id": f"jobicy_{j['id']}", "title": j.get("jobTitle", ""),
                "company": j.get("companyName", ""),
                "location": j.get("jobGeo") or "Remote",
                "remote_type": "Remote", "salary": j.get("annualSalaryMin"),
                "posted": (j.get("pubDate") or "")[:10],
                "url": j.get("url", ""), "source": "Jobicy",
                "description": (j.get("jobExcerpt") or "")[:600], "fetched_at": now,
            }))
        jobs = [j for j in jobs if is_valid(j)]
        print(f"  Jobicy: {len(jobs)} listings fetched")
        return jobs
    except Exception as e:
        print(f"  Jobicy error: {e}", file=sys.stderr)
        return []


def fetch_arbeitnow() -> list:
    # Arbeitnow is a German-origin board — filter to English-language remote-only
    # listings to avoid flooding the catalog with European on-site roles.
    try:
        r = httpx.get("https://www.arbeitnow.com/api/job-board-api", timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        jobs, now = [], utcnow_iso()
        for j in r.json().get("data", []):
            # Only include remote listings — on-site Arbeitnow listings are almost
            # exclusively European and not useful for US job seekers.
            if not j.get("remote"):
                continue
            # Basic English-language filter: skip listings where the description
            # contains German/non-English indicator words.
            desc = (j.get("description") or "").lower()
            non_english_signals = ["wir suchen", "sie haben", "ihre aufgaben",
                                   "ihr profil", "wir bieten", "m/w/d", "vollzeit",
                                   "teilzeit", "stellenangebot", "berufserfahrung"]
            if any(sig in desc for sig in non_english_signals):
                continue
            jobs.append(normalise({
                "id": f"arbeitnow_{j.get('slug', '')}",
                "title": j.get("title", ""), "company": j.get("company_name", ""),
                "location": j.get("location") or "Remote",
                "remote_type": "Remote",
                "salary": None,
                "posted": datetime.fromtimestamp(j["created_at"], tz=timezone.utc).strftime("%Y-%m-%d") if j.get("created_at") else "",
                "url": j.get("url", ""), "source": "Arbeitnow",
                "description": (j.get("description") or "")[:600], "fetched_at": now,
            }))
        jobs = [j for j in jobs if is_valid(j)]
        print(f"  Arbeitnow: {len(jobs)} listings fetched (remote + English only)")
        return jobs
    except Exception as e:
        print(f"  Arbeitnow error: {e}", file=sys.stderr)
        return []


def fetch_themuse() -> list:
    try:
        r = httpx.get("https://www.themuse.com/api/public/jobs",
                      params={"page": 0, "descending": "true"}, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        jobs, now = [], utcnow_iso()
        for j in r.json().get("results", []):
            company   = j.get("company", {}).get("name", "")
            locations = j.get("locations", [])
            location  = locations[0].get("name", "") if locations else "See listing"
            levels    = j.get("levels", [])
            jobs.append(normalise({
                "id": f"themuse_{j.get('id', '')}", "title": j.get("name", ""),
                "company": company, "location": location, "remote_type": "See listing",
                "salary": None, "posted": (j.get("publication_date") or "")[:10],
                "url": j.get("refs", {}).get("landing_page", ""), "source": "The Muse",
                "description": (j.get("contents") or "")[:600],
                "requirements": [lvl.get("name", "") for lvl in levels], "fetched_at": now,
            }))
        jobs = [j for j in jobs if is_valid(j)]
        print(f"  The Muse: {len(jobs)} listings fetched")
        return jobs
    except Exception as e:
        print(f"  The Muse error: {e}", file=sys.stderr)
        return []


def fetch_remoteok() -> list:
    """
    Remote OK — free, no key. Real-time remote job board.
    Strong cross-validation signal: a company here AND in Remotive/Jobicy
    is genuinely actively hiring that week. First array element is metadata.
    """
    try:
        r = httpx.get(
            "https://remoteok.com/api",
            headers={"User-Agent": "SiegeworksJobCompiler/1.0 (catalog-fetch)"},
            timeout=REQUEST_TIMEOUT,
        )
        r.raise_for_status()
        raw_jobs = [j for j in r.json() if isinstance(j, dict) and j.get("id")]
        jobs, now = [], utcnow_iso()
        for j in raw_jobs:
            jobs.append(normalise({
                "id": f"remoteok_{j.get('id', '')}",
                "title": j.get("position", ""), "company": j.get("company", ""),
                "location": j.get("location") or "Remote", "remote_type": "Remote",
                "salary": j.get("salary_min"),
                "posted": (j.get("date") or "")[:10],
                "url": j.get("url", ""), "source": "Remote OK",
                "description": strip_html(j.get("description", ""))[:600], "fetched_at": now,
            }))
        jobs = [j for j in jobs if is_valid(j)]
        print(f"  Remote OK: {len(jobs)} listings fetched")
        return jobs
    except Exception as e:
        print(f"  Remote OK error: {e}", file=sys.stderr)
        return []


def fetch_himalayas() -> list:
    """
    Himalayas — free, no key. Curated remote company listings.
    Skews toward funded startups actively building teams — high ATS signal.
    """
    try:
        r = httpx.get("https://himalayas.app/jobs/api", timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        jobs, now = [], utcnow_iso()
        for j in r.json().get("jobs", []):
            company_obj = j.get("company", {}) or {}
            restrictions = j.get("locationRestrictions", [])
            location = restrictions[0] if restrictions else "Remote"
            jobs.append(normalise({
                "id": f"himalayas_{j.get('id', '')}",
                "title": j.get("title", ""),
                "company": company_obj.get("name", j.get("companyName", "")),
                "location": location, "remote_type": "Remote",
                "salary": j.get("salaryMin") if j.get("salaryCurrency") else None,
                "posted": (j.get("createdAt") or "")[:10],
                "url": j.get("applicationLink", ""), "source": "Himalayas",
                "description": (j.get("description") or "")[:600], "fetched_at": now,
            }))
        jobs = [j for j in jobs if is_valid(j)]
        print(f"  Himalayas: {len(jobs)} listings fetched")
        return jobs
    except Exception as e:
        print(f"  Himalayas error: {e}", file=sys.stderr)
        return []


def fetch_adzuna(app_id: str, app_key: str) -> list:
    try:
        r = httpx.get(
            "https://api.adzuna.com/v1/api/jobs/us/search/1",
            params={"app_id": app_id, "app_key": app_key,
                    "results_per_page": 50, "max_days_old": 7,
                    "content-type": "application/json"},
            timeout=REQUEST_TIMEOUT,
        )
        r.raise_for_status()
        jobs, now = [], utcnow_iso()
        for j in r.json().get("results", []):
            jobs.append(normalise({
                "id": f"adzuna_{j['id']}", "title": j.get("title", ""),
                "company": j.get("company", {}).get("display_name", ""),
                "location": j.get("location", {}).get("display_name", ""),
                "remote_type": "See listing", "salary": j.get("salary_min"),
                "posted": (j.get("created") or "")[:10],
                "url": j.get("redirect_url", ""), "source": "Adzuna",
                "description": (j.get("description") or "")[:600], "fetched_at": now,
            }))
        jobs = [j for j in jobs if is_valid(j)]
        print(f"  Adzuna: {len(jobs)} listings fetched")
        return jobs
    except Exception as e:
        print(f"  Adzuna error: {e}", file=sys.stderr)
        return []


def fetch_usajobs(api_key: str, user_agent: str) -> list:
    try:
        r = httpx.get(
            "https://data.usajobs.gov/api/search",
            params={"ResultsPerPage": 50, "DatePosted": 7},
            headers={"Authorization-Key": api_key, "User-Agent": user_agent,
                     "Host": "data.usajobs.gov"},
            timeout=REQUEST_TIMEOUT,
        )
        r.raise_for_status()
        jobs, now = [], utcnow_iso()
        items = r.json().get("SearchResult", {}).get("SearchResultItems", [])
        for item in items:
            j = item.get("MatchedObjectDescriptor", {})
            positions = j.get("PositionLocation", [{}])
            location  = positions[0].get("LocationName", "See listing") if positions else "See listing"
            salary    = j.get("PositionRemuneration", [{}])
            min_pay   = salary[0].get("MinimumRange") if salary else None
            jobs.append(normalise({
                "id": f"usajobs_{j.get('PositionID', '')}",
                "title": j.get("PositionTitle", ""),
                "company": j.get("OrganizationName", "U.S. Federal Government"),
                "location": location, "remote_type": "See listing", "salary": min_pay,
                "posted": (j.get("PublicationStartDate") or "")[:10],
                "url": j.get("PositionURI", ""), "source": "USAJobs",
                "description": (j.get("UserArea", {}).get("Details", {}).get("JobSummary") or "")[:600],
                "fetched_at": now,
            }))
        jobs = [j for j in jobs if is_valid(j)]
        print(f"  USAJobs: {len(jobs)} listings fetched")
        return jobs
    except Exception as e:
        print(f"  USAJobs error: {e}", file=sys.stderr)
        return []


# ── Dynamic ATS Allowlist ─────────────────────────────────────────────────────

def build_dynamic_ats_allowlist(aggregator_jobs: list, existing_allowlist: dict) -> dict:
    """
    Derives ATS slugs from companies appearing in aggregator results,
    weighted by source breadth (SOURCE_WEIGHTS at top of file).

    A company must accumulate at least ATS_MIN_SCORE weighted points to earn
    an ATS lookup slot. Cross-source appearances score higher:
      - Two narrow sources (Remotive + Jobicy): 1 + 1 = 2  → just qualifies
      - One wide source (Adzuna) + one narrow:  3 + 1 = 4  → strong signal
      - Adzuna alone:                           3           → does not qualify
    This prevents a single outlier source from granting ATS access.

    Allowlist structure: { slug: { company_name, score, last_seen } }
    Decay: companies not seen this cycle lose 1 score point and are pruned at 0.
    """
    score_tally: Counter = Counter()
    slug_to_name: dict   = {}

    for j in aggregator_jobs:
        company = j.get("company", "").strip()
        source  = j.get("source", "")
        if not company:
            continue
        slug = company_to_slug(company)
        if not slug:
            continue
        score_tally[slug] += SOURCE_WEIGHTS.get(source, DEFAULT_SOURCE_WEIGHT)
        slug_to_name[slug] = company

    updated = dict(existing_allowlist)

    # Decay companies not seen this cycle
    for slug in list(updated.keys()):
        if slug not in score_tally:
            updated[slug]["score"] = updated[slug].get("score", 1) - 1
            if updated[slug]["score"] <= 0:
                del updated[slug]
                print(f"  ATS allowlist: pruned '{slug}' (score decayed to 0)")

    # Add or reinforce companies meeting the weighted threshold
    for slug, score in score_tally.items():
        if score >= ATS_MIN_SCORE:
            if slug in updated:
                updated[slug]["score"]        = min(updated[slug]["score"] + 1, 15)
                updated[slug]["last_seen"]    = utcnow_iso()
                updated[slug]["company_name"] = slug_to_name[slug]
            else:
                updated[slug] = {
                    "company_name": slug_to_name[slug],
                    "score":        score,
                    "last_seen":    utcnow_iso(),
                }

    if len(updated) > ATS_MAX_COMPANIES:
        top     = sorted(updated.items(), key=lambda x: x[1]["score"], reverse=True)
        updated = dict(top[:ATS_MAX_COMPANIES])

    print(f"  ATS allowlist: {len(updated)} companies active "
          f"(threshold: weighted score >= {ATS_MIN_SCORE})")
    return updated


# ── ATS fetchers ──────────────────────────────────────────────────────────────

def probe_slug(slug: str, client: httpx.Client) -> str:
    """
    Lightweight probe: try Greenhouse, then Lever, then Ashby.
    Returns the first platform that confirms the slug is live, or empty string.

    This prevents wasted requests on guessed/dead slugs. A slug that returns
    no live platform is skipped — it will decay off the allowlist naturally.
    """
    checks = [
        ("greenhouse", f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"),
        ("ashby",      f"https://api.ashbyhq.com/posting-api/job-board/{slug}"),
        ("lever",      f"https://api.lever.co/v0/postings/{slug}?mode=json"),
    ]
    for platform, url in checks:
        try:
            r = client.get(url, timeout=5)
            if r.status_code == 404:
                continue
            if r.status_code == 200:
                data = r.json()
                # Greenhouse returns {"jobs": [...]}
                if platform == "greenhouse" and isinstance(data.get("jobs"), list):
                    return platform
                # Ashby returns {"jobPostings": [...]}
                if platform == "ashby" and isinstance(data.get("jobPostings"), list):
                    return platform
                # Lever returns a list directly
                if platform == "lever" and isinstance(data, list):
                    return platform
        except Exception:
            continue
    return ""


def fetch_greenhouse_ats(slug: str, company_name: str) -> list:
    try:
        r = httpx.get(
            f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
            params={"content": "true"}, timeout=REQUEST_TIMEOUT,
        )
        if r.status_code == 404:
            return []
        r.raise_for_status()
        jobs, now = [], utcnow_iso()
        for j in r.json().get("jobs", []):
            loc_obj  = j.get("location", {})
            location = loc_obj.get("name", "See listing") if isinstance(loc_obj, dict) else "See listing"
            jobs.append(normalise({
                "id": f"greenhouse_{j.get('id', '')}", "title": j.get("title", ""),
                "company": company_name, "location": location, "remote_type": "See listing",
                "salary": None, "posted": (j.get("updated_at") or "")[:10],
                "url": j.get("absolute_url", ""), "source": "Greenhouse ATS",
                "ats_board": slug, "description": (j.get("content") or "")[:600],
                "fetched_at": now,
            }))
        return [j for j in jobs if is_valid(j)]
    except Exception:
        return []


def fetch_lever_ats(slug: str, company_name: str) -> list:
    try:
        r = httpx.get(
            f"https://api.lever.co/v0/postings/{slug}",
            params={"mode": "json"}, timeout=REQUEST_TIMEOUT,
        )
        if r.status_code == 404:
            return []
        r.raise_for_status()
        jobs, now = [], utcnow_iso()
        for j in r.json():
            categories = j.get("categories", {})
            location   = categories.get("location") or categories.get("team") or "See listing"
            jobs.append(normalise({
                "id": f"lever_{j.get('id', '')}", "title": j.get("text", ""),
                "company": company_name, "location": location,
                "remote_type": "Remote" if "remote" in location.lower() else "See listing",
                "salary": None,
                "posted": datetime.fromtimestamp(j["createdAt"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d") if j.get("createdAt") else "",
                "url": j.get("hostedUrl", ""), "source": "Lever ATS",
                "ats_board": slug, "description": (j.get("descriptionPlain") or "")[:600],
                "fetched_at": now,
            }))
        return [j for j in jobs if is_valid(j)]
    except Exception:
        return []


def fetch_ashby_ats(slug: str, company_name: str) -> list:
    """
    Ashby — top-3 ATS for funded tech startups and scale-ups.
    Filters out unlisted/draft postings (isListed=False).
    All results pass through normalise() and is_valid() — no trust bypass.
    """
    try:
        r = httpx.get(
            f"https://api.ashbyhq.com/posting-api/job-board/{slug}",
            timeout=REQUEST_TIMEOUT,
        )
        if r.status_code == 404:
            return []
        r.raise_for_status()
        jobs, now = [], utcnow_iso()
        for j in r.json().get("jobPostings", []):
            if not j.get("isListed", True):
                continue
            location = j.get("locationName") or j.get("location") or "See listing"
            jobs.append(normalise({
                "id": f"ashby_{j.get('id', '')}", "title": j.get("title", ""),
                "company": company_name, "location": location,
                "remote_type": "Remote" if j.get("isRemote") else "See listing",
                "salary": j.get("compensationTierSummary") or None,
                "posted": (j.get("publishedDate") or "")[:10],
                "url": j.get("jobUrl", ""), "source": "Ashby ATS",
                "ats_board": slug,
                "description": (j.get("descriptionPlain") or j.get("description") or "")[:600],
                "fetched_at": now,
            }))
        return [j for j in jobs if is_valid(j)]
    except Exception:
        return []


def fetch_all_ats(allowlist: dict) -> list:
    """
    For each allowlisted slug:
      1. Probe all three ATS platforms to find which is live.
      2. Fetch from confirmed platform only — no blind sequential requests.
      3. If probe finds nothing, skip — slug decays off allowlist naturally.
    """
    all_jobs, confirmed, misses = [], 0, 0

    with httpx.Client(follow_redirects=True, timeout=6) as probe_client:
        for slug, meta in allowlist.items():
            company_name = meta.get("company_name", slug)
            platform = probe_slug(slug, probe_client)

            if platform == "greenhouse":
                jobs = fetch_greenhouse_ats(slug, company_name)
            elif platform == "lever":
                jobs = fetch_lever_ats(slug, company_name)
            elif platform == "ashby":
                jobs = fetch_ashby_ats(slug, company_name)
            else:
                misses += 1
                continue

            if jobs:
                confirmed += len(jobs)
                all_jobs.extend(jobs)

    if confirmed or misses:
        live = len(allowlist) - misses
        print(f"  ATS (Greenhouse/Lever/Ashby): {confirmed} listings "
              f"from {live} companies ({misses} slug(s) returned no live board)")
    return all_jobs


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("=" * 54)
    print("Siegeworks Job Compiler — Catalog Fetch")
    print("=" * 54)

    print("\n[1/6] Loading existing catalog…")
    existing = load_existing()
    old_jobs = existing.get("jobs", [])
    print(f"  Found {len(old_jobs)} existing listings")

    print(f"\n[2/6] Pruning listings older than {MAX_AGE_DAYS} days…")
    fresh = prune_stale(old_jobs)
    print(f"  {len(fresh)} listings remain after date prune")

    print(f"\n[3/6] Verifying {len(fresh)} listing URL(s)…")
    verified = verify_urls(fresh)
    print(f"  {len(verified)} listings remain after URL verification")

    print("\n[4/6] Fetching from aggregator APIs…")
    aggregator_jobs: list = []
    aggregator_jobs += fetch_remotive()
    aggregator_jobs += fetch_jobicy()
    aggregator_jobs += fetch_arbeitnow()
    aggregator_jobs += fetch_themuse()
    aggregator_jobs += fetch_remoteok()
    aggregator_jobs += fetch_himalayas()

    adzuna_id  = os.environ.get("ADZUNA_APP_ID",  "").strip()
    adzuna_key = os.environ.get("ADZUNA_APP_KEY", "").strip()
    if adzuna_id and adzuna_key:
        aggregator_jobs += fetch_adzuna(adzuna_id, adzuna_key)
    else:
        print("  Adzuna: skipped (ADZUNA_APP_ID / ADZUNA_APP_KEY not set)")

    usajobs_key = os.environ.get("USAJOBS_API_KEY",    "").strip()
    usajobs_ua  = os.environ.get("USAJOBS_USER_AGENT", "").strip()
    if usajobs_key and usajobs_ua:
        aggregator_jobs += fetch_usajobs(usajobs_key, usajobs_ua)
    else:
        print("  USAJobs: skipped (USAJOBS_API_KEY / USAJOBS_USER_AGENT not set)")

    print(f"  Total aggregator results this cycle: {len(aggregator_jobs)}")

    print("\n[5/6] Building weighted ATS allowlist and fetching direct ATS listings…")
    existing_allowlist = load_ats_allowlist()
    allowlist = build_dynamic_ats_allowlist(aggregator_jobs, existing_allowlist)
    save_ats_allowlist(allowlist)
    ats_jobs = fetch_all_ats(allowlist)

    new_jobs = aggregator_jobs + ats_jobs

    print("\n[6/6] Deduplicating and writing catalog…")
    existing_urls    = {j.get("url")                             for j in verified if j.get("url")}
    existing_dk_keys = {make_dedup_key(j["title"], j["company"]) for j in verified
                        if j.get("title") and j.get("company")}

    unique_new, seen_urls, seen_dk = [], set(), set()
    for j in new_jobs:
        url = j.get("url", "")
        dk  = make_dedup_key(j.get("title", ""), j.get("company", ""))
        if url in existing_urls or url in seen_urls:
            continue
        if dk in existing_dk_keys or dk in seen_dk:
            continue
        seen_urls.add(url)
        seen_dk.add(dk)
        unique_new.append(j)

    print(f"  {len(unique_new)} new unique listing(s) added")

    all_jobs = verified + unique_new
    catalog  = {"jobs": all_jobs, "updated_at": utcnow_iso(), "total": len(all_jobs)}

    CATALOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CATALOG_PATH.write_text(
        json.dumps(catalog, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print(f"\n✓ Catalog written: {len(all_jobs)} listings → {CATALOG_PATH}")
    print("=" * 54)


if __name__ == "__main__":
    main()
