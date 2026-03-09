#!/usr/bin/env python3
"""
Siegeworks Job Compiler — Catalog Fetch Script
================================================
Runs inside GitHub Actions every 6 hours.

What it does:
  1. Loads the existing catalog from data/listings.json
  2. Prunes listings older than 7 days (by fetched_at timestamp)
  3. HEAD-verifies remaining listing URLs (removes confirmed 404s)
  4. Fetches new listings from free APIs
  5. Deduplicates by URL and writes the fresh catalog

Free API sources (no keys required):
  - Remotive   — remote tech/marketing jobs
  - Jobicy     — remote jobs across categories

Optional (requires free registration, set as GitHub Secrets):
  - Adzuna     — broad US job coverage including on-site roles
                 Register at developer.adzuna.com
                 Set ADZUNA_APP_ID and ADZUNA_APP_KEY in repo secrets
"""

import json
import os
import sys
import httpx
from datetime import datetime, timedelta, timezone
from pathlib import Path

CATALOG_PATH = Path("data/listings.json")
MAX_AGE_DAYS = 7
REQUEST_TIMEOUT = 12
VERIFY_TIMEOUT  = 8


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def load_existing() -> dict:
    if CATALOG_PATH.exists():
        try:
            return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"  Warning: could not parse existing catalog — {e}")
    return {"jobs": [], "updated_at": None, "total": 0}


def prune_stale(jobs: list) -> list:
    """Remove listings older than MAX_AGE_DAYS based on fetched_at."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)
    kept, removed = [], 0
    for j in jobs:
        fa = j.get("fetched_at")
        if not fa:
            kept.append(j)  # keep if no timestamp (conservative)
            continue
        try:
            ts = datetime.fromisoformat(fa.replace("Z", "+00:00"))
            if ts > cutoff:
                kept.append(j)
            else:
                removed += 1
        except Exception:
            kept.append(j)  # keep if unparseable
    if removed:
        print(f"  Pruned {removed} listing(s) older than {MAX_AGE_DAYS} days")
    return kept


def verify_urls(jobs: list) -> list:
    """
    HEAD-check each listing URL.
    Conservative: keep the listing if the check fails or is ambiguous.
    Only remove on definitive 404.
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
                    print(f"  Removed (404): {j.get('title','?')} at {j.get('company','?')}")
                else:
                    kept.append(j)
            except Exception:
                kept.append(j)  # network error → conservative keep

    if removed:
        print(f"  URL verification removed {removed} dead listing(s)")
    return kept


def normalise(job: dict) -> dict:
    """Ensure all required fields are present and typed correctly."""
    return {
        "id":          job.get("id", ""),
        "title":       (job.get("title") or "").strip(),
        "company":     (job.get("company") or "").strip(),
        "location":    (job.get("location") or "").strip(),
        "remote_type": (job.get("remote_type") or "See listing").strip(),
        "salary":      job.get("salary"),
        "posted":      (job.get("posted") or "")[:10],   # YYYY-MM-DD or truncated
        "url":         (job.get("url") or "").strip(),
        "source":      (job.get("source") or "").strip(),
        "description": (job.get("description") or "")[:600],
        "requirements": job.get("requirements") if isinstance(job.get("requirements"), list) else [],
        "ats_board":   job.get("ats_board"),
        "fetched_at":  job.get("fetched_at") or utcnow_iso(),
    }


# ── Fetchers ──────────────────────────────────────────────────────────────────

def fetch_remotive() -> list:
    """Remotive.com — free API, no key required. Remote tech/marketing roles."""
    try:
        r = httpx.get("https://remotive.com/api/remote-jobs?limit=100",
                      timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        jobs = []
        now  = utcnow_iso()
        for j in r.json().get("jobs", []):
            if not j.get("title") or not j.get("company_name"):
                continue
            jobs.append(normalise({
                "id":          f"remotive_{j['id']}",
                "title":       j.get("title", ""),
                "company":     j.get("company_name", ""),
                "location":    j.get("candidate_required_location") or "Remote",
                "remote_type": "Remote",
                "salary":      j.get("salary"),
                "posted":      (j.get("publication_date") or "")[:10],
                "url":         j.get("url", ""),
                "source":      "Remotive",
                "description": j.get("description", "")[:600] if j.get("description") else "",
                "fetched_at":  now,
            }))
        print(f"  Remotive: {len(jobs)} listings fetched")
        return jobs
    except Exception as e:
        print(f"  Remotive error: {e}", file=sys.stderr)
        return []


def fetch_jobicy() -> list:
    """Jobicy.com — free API, no key required. Remote jobs across categories."""
    try:
        r = httpx.get("https://jobicy.com/api/v2/remote-jobs?count=50",
                      timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        jobs = []
        now  = utcnow_iso()
        for j in r.json().get("jobs", []):
            if not j.get("jobTitle") or not j.get("companyName"):
                continue
            jobs.append(normalise({
                "id":          f"jobicy_{j['id']}",
                "title":       j.get("jobTitle", ""),
                "company":     j.get("companyName", ""),
                "location":    j.get("jobGeo") or "Remote",
                "remote_type": "Remote",
                "salary":      j.get("annualSalaryMin"),
                "posted":      (j.get("pubDate") or "")[:10],
                "url":         j.get("url", ""),
                "source":      "Jobicy",
                "description": (j.get("jobExcerpt") or "")[:600],
                "fetched_at":  now,
            }))
        print(f"  Jobicy: {len(jobs)} listings fetched")
        return jobs
    except Exception as e:
        print(f"  Jobicy error: {e}", file=sys.stderr)
        return []


def fetch_adzuna(app_id: str, app_key: str) -> list:
    """
    Adzuna — free tier, requires registration at developer.adzuna.com.
    Set ADZUNA_APP_ID and ADZUNA_APP_KEY in GitHub repo secrets.
    Fetches broad US listings posted within the last 7 days.
    """
    try:
        url = "https://api.adzuna.com/v1/api/jobs/us/search/1"
        params = {
            "app_id":          app_id,
            "app_key":         app_key,
            "results_per_page": 50,
            "max_days_old":    7,
            "content-type":    "application/json",
        }
        r = httpx.get(url, params=params, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        jobs = []
        now  = utcnow_iso()
        for j in r.json().get("results", []):
            if not j.get("title") or not j.get("company", {}).get("display_name"):
                continue
            jobs.append(normalise({
                "id":          f"adzuna_{j['id']}",
                "title":       j.get("title", ""),
                "company":     j.get("company", {}).get("display_name", ""),
                "location":    j.get("location", {}).get("display_name", ""),
                "remote_type": "See listing",
                "salary":      j.get("salary_min"),
                "posted":      (j.get("created") or "")[:10],
                "url":         j.get("redirect_url", ""),
                "source":      "Adzuna",
                "description": (j.get("description") or "")[:600],
                "fetched_at":  now,
            }))
        print(f"  Adzuna: {len(jobs)} listings fetched")
        return jobs
    except Exception as e:
        print(f"  Adzuna error: {e}", file=sys.stderr)
        return []


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("=" * 50)
    print("Siegeworks Job Compiler — Catalog Fetch")
    print("=" * 50)

    # 1. Load existing catalog
    print("\n[1/5] Loading existing catalog…")
    existing  = load_existing()
    old_jobs  = existing.get("jobs", [])
    print(f"  Found {len(old_jobs)} existing listings")

    # 2. Prune stale listings
    print(f"\n[2/5] Pruning listings older than {MAX_AGE_DAYS} days…")
    fresh = prune_stale(old_jobs)
    print(f"  {len(fresh)} listings remain after date prune")

    # 3. Verify remaining URLs
    print(f"\n[3/5] Verifying {len(fresh)} listing URL(s)…")
    verified = verify_urls(fresh)
    print(f"  {len(verified)} listings remain after URL verification")

    # 4. Fetch new listings
    print("\n[4/5] Fetching new listings from APIs…")
    new_jobs: list = []
    new_jobs += fetch_remotive()
    new_jobs += fetch_jobicy()

    adzuna_id  = os.environ.get("ADZUNA_APP_ID",  "").strip()
    adzuna_key = os.environ.get("ADZUNA_APP_KEY", "").strip()
    if adzuna_id and adzuna_key:
        new_jobs += fetch_adzuna(adzuna_id, adzuna_key)
    else:
        print("  Adzuna: skipped (ADZUNA_APP_ID / ADZUNA_APP_KEY not set)")

    # 5. Deduplicate by URL, merge, write
    print("\n[5/5] Deduplicating and writing catalog…")
    existing_urls = {j.get("url") for j in verified if j.get("url")}
    unique_new    = [j for j in new_jobs
                     if j.get("url") and j["url"] not in existing_urls
                     and j.get("title") and j.get("company")]
    print(f"  {len(unique_new)} new unique listing(s) added")

    all_jobs = verified + unique_new

    catalog = {
        "jobs":       all_jobs,
        "updated_at": utcnow_iso(),
        "total":      len(all_jobs),
    }

    CATALOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CATALOG_PATH.write_text(
        json.dumps(catalog, indent=2, ensure_ascii=False),
        encoding="utf-8"
    )

    print(f"\n✓ Catalog written: {len(all_jobs)} listings → {CATALOG_PATH}")
    print("=" * 50)


if __name__ == "__main__":
    main()
