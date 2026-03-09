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
  5. Builds a dynamic ATS allowlist from aggregator results
  6. Fetches directly from Greenhouse/Lever for allowlisted companies
  7. Deduplicates by URL and title+company hash, then writes the fresh catalog

Free API sources (no keys required):
  - Remotive    — remote tech/marketing jobs
  - Jobicy      — remote jobs across categories
  - Arbeitnow   — broad remote listings, EU + US
  - The Muse    — company culture + listings, strong for marketing/creative roles
  - Greenhouse  — direct ATS (dynamic allowlist, derived from aggregator results)
  - Lever       — direct ATS (dynamic allowlist, derived from aggregator results)

Optional (requires free registration, set as GitHub Secrets):
  - Adzuna      — broad US job coverage including on-site roles
                  Register at developer.adzuna.com
                  Set ADZUNA_APP_ID and ADZUNA_APP_KEY in repo secrets
  - USAJobs     — US federal government listings (high integrity)
                  Register at developer.usajobs.gov
                  Set USAJOBS_API_KEY and USAJOBS_USER_AGENT in repo secrets

NOTE ON ATS ALLOWLIST:
  The dynamic allowlist controls which company domains the GitHub Action
  makes outbound requests to — it is NOT a trust bypass. Every listing
  sourced from Greenhouse/Lever still passes through normalise(),
  required-field validation, URL verification, and deduplication,
  exactly like any other source.
"""

import json
import os
import re
import sys
import hashlib
import httpx
from datetime import datetime, timedelta, timezone
from pathlib import Path

CATALOG_PATH  = Path("data/listings.json")
ATS_LIST_PATH = Path("data/ats_allowlist.json")  # persisted between runs
MAX_AGE_DAYS  = 7
REQUEST_TIMEOUT = 12
VERIFY_TIMEOUT  = 8

# Minimum times a company must appear in aggregator results
# before it earns an ATS lookup slot this cycle
ATS_MIN_APPEARANCES = 2
ATS_MAX_COMPANIES   = 40  # cap outbound ATS requests per run


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def strip_html(text: str) -> str:
    """Remove HTML tags from description strings."""
    return re.sub(r"<[^>]+>", " ", text or "").strip()


def company_to_slug(name: str) -> str:
    """
    Convert a company display name to a likely ATS slug.
    e.g. 'Acme Corp.' -> 'acme-corp'
    """
    slug = name.lower().strip()
    slug = re.sub(r"[^a-z0-9\s-]", "", slug)   # strip punctuation
    slug = re.sub(r"\s+", "-", slug)            # spaces to hyphens
    slug = re.sub(r"-+", "-", slug).strip("-")  # collapse double-hyphens
    return slug


def make_dedup_key(title: str, company: str) -> str:
    """Secondary dedupe key: hash of normalized title + company."""
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
    """Load persisted ATS allowlist. Returns {slug: {platform, appearances, last_seen}}."""
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
        encoding="utf-8"
    )


def prune_stale(jobs: list) -> list:
    """Remove listings older than MAX_AGE_DAYS based on fetched_at."""
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
    HEAD-check each listing URL.
    Conservative: keep if check fails or is ambiguous.
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
                kept.append(j)

    if removed:
        print(f"  URL verification removed {removed} dead listing(s)")
    return kept


def normalise(job: dict) -> dict:
    """Ensure all required fields are present, typed correctly, and HTML-stripped."""
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
    """Require title, company, and a valid URL before a listing enters the catalog."""
    return bool(
        job.get("title")
        and job.get("company")
        and job.get("url")
        and job["url"].startswith("http")
    )


# ── Fetchers ──────────────────────────────────────────────────────────────────

def fetch_remotive() -> list:
    """Remotive.com — free, no key. Remote tech/marketing roles."""
    try:
        r = httpx.get("https://remotive.com/api/remote-jobs?limit=100",
                      timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        jobs, now = [], utcnow_iso()
        for j in r.json().get("jobs", []):
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
                "description": j.get("description", "")[:600],
                "fetched_at":  now,
            }))
        jobs = [j for j in jobs if is_valid(j)]
        print(f"  Remotive: {len(jobs)} listings fetched")
        return jobs
    except Exception as e:
        print(f"  Remotive error: {e}", file=sys.stderr)
        return []


def fetch_jobicy() -> list:
    """Jobicy.com — free, no key. Remote jobs across categories."""
    try:
        r = httpx.get("https://jobicy.com/api/v2/remote-jobs?count=50",
                      timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        jobs, now = [], utcnow_iso()
        for j in r.json().get("jobs", []):
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
        jobs = [j for j in jobs if is_valid(j)]
        print(f"  Jobicy: {len(jobs)} listings fetched")
        return jobs
    except Exception as e:
        print(f"  Jobicy error: {e}", file=sys.stderr)
        return []


def fetch_arbeitnow() -> list:
    """
    Arbeitnow — free, no key required. Strong remote + EU listings,
    expanding US coverage. Identical risk profile to Remotive/Jobicy.
    """
    try:
        r = httpx.get("https://www.arbeitnow.com/api/job-board-api",
                      timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        jobs, now = [], utcnow_iso()
        for j in r.json().get("data", []):
            jobs.append(normalise({
                "id":          f"arbeitnow_{j.get('slug', '')}",
                "title":       j.get("title", ""),
                "company":     j.get("company_name", ""),
                "location":    j.get("location") or "Remote",
                "remote_type": "Remote" if j.get("remote") else "On-site",
                "salary":      None,
                "posted":      datetime.fromtimestamp(
                                   j["created_at"], tz=timezone.utc
                               ).strftime("%Y-%m-%d") if j.get("created_at") else "",
                "url":         j.get("url", ""),
                "source":      "Arbeitnow",
                "description": (j.get("description") or "")[:600],
                "fetched_at":  now,
            }))
        jobs = [j for j in jobs if is_valid(j)]
        print(f"  Arbeitnow: {len(jobs)} listings fetched")
        return jobs
    except Exception as e:
        print(f"  Arbeitnow error: {e}", file=sys.stderr)
        return []


def fetch_themuse() -> list:
    """
    The Muse — free, no key required (key optional for higher rate limits).
    Strong for marketing, creative, and company-culture-rich listings.
    """
    try:
        r = httpx.get(
            "https://www.themuse.com/api/public/jobs",
            params={"page": 0, "descending": "true"},
            timeout=REQUEST_TIMEOUT,
        )
        r.raise_for_status()
        jobs, now = [], utcnow_iso()
        for j in r.json().get("results", []):
            company = j.get("company", {}).get("name", "")
            locations = j.get("locations", [])
            location = locations[0].get("name", "") if locations else "See listing"
            levels = j.get("levels", [])
            jobs.append(normalise({
                "id":          f"themuse_{j.get('id', '')}",
                "title":       j.get("name", ""),
                "company":     company,
                "location":    location,
                "remote_type": "See listing",
                "salary":      None,
                "posted":      (j.get("publication_date") or "")[:10],
                "url":         j.get("refs", {}).get("landing_page", ""),
                "source":      "The Muse",
                "description": (j.get("contents") or "")[:600],
                "requirements": [lvl.get("name", "") for lvl in levels],
                "fetched_at":  now,
            }))
        jobs = [j for j in jobs if is_valid(j)]
        print(f"  The Muse: {len(jobs)} listings fetched")
        return jobs
    except Exception as e:
        print(f"  The Muse error: {e}", file=sys.stderr)
        return []


def fetch_adzuna(app_id: str, app_key: str) -> list:
    """
    Adzuna — free tier, requires registration at developer.adzuna.com.
    Set ADZUNA_APP_ID and ADZUNA_APP_KEY in GitHub repo secrets.
    """
    try:
        r = httpx.get(
            "https://api.adzuna.com/v1/api/jobs/us/search/1",
            params={
                "app_id":           app_id,
                "app_key":          app_key,
                "results_per_page": 50,
                "max_days_old":     7,
                "content-type":     "application/json",
            },
            timeout=REQUEST_TIMEOUT,
        )
        r.raise_for_status()
        jobs, now = [], utcnow_iso()
        for j in r.json().get("results", []):
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
        jobs = [j for j in jobs if is_valid(j)]
        print(f"  Adzuna: {len(jobs)} listings fetched")
        return jobs
    except Exception as e:
        print(f"  Adzuna error: {e}", file=sys.stderr)
        return []


def fetch_usajobs(api_key: str, user_agent: str) -> list:
    """
    USAJobs — free, requires registration at developer.usajobs.gov.
    Federal listings only; high integrity, underserved by most tools.
    Set USAJOBS_API_KEY and USAJOBS_USER_AGENT in GitHub repo secrets.
    """
    try:
        r = httpx.get(
            "https://data.usajobs.gov/api/search",
            params={"ResultsPerPage": 50, "DatePosted": 7},
            headers={
                "Authorization-Key": api_key,
                "User-Agent":        user_agent,
                "Host":              "data.usajobs.gov",
            },
            timeout=REQUEST_TIMEOUT,
        )
        r.raise_for_status()
        jobs, now = [], utcnow_iso()
        items = (r.json()
                  .get("SearchResult", {})
                  .get("SearchResultItems", []))
        for item in items:
            j = item.get("MatchedObjectDescriptor", {})
            positions = j.get("PositionLocation", [{}])
            location  = positions[0].get("LocationName", "See listing") if positions else "See listing"
            salary    = j.get("PositionRemuneration", [{}])
            min_pay   = salary[0].get("MinimumRange") if salary else None
            jobs.append(normalise({
                "id":          f"usajobs_{j.get('PositionID', '')}",
                "title":       j.get("PositionTitle", ""),
                "company":     j.get("OrganizationName", "U.S. Federal Government"),
                "location":    location,
                "remote_type": "See listing",
                "salary":      min_pay,
                "posted":      (j.get("PublicationStartDate") or "")[:10],
                "url":         j.get("PositionURI", ""),
                "source":      "USAJobs",
                "description": (j.get("UserArea", {})
                                  .get("Details", {})
                                  .get("JobSummary") or "")[:600],
                "fetched_at":  now,
            }))
        jobs = [j for j in jobs if is_valid(j)]
        print(f"  USAJobs: {len(jobs)} listings fetched")
        return jobs
    except Exception as e:
        print(f"  USAJobs error: {e}", file=sys.stderr)
        return []


# ── Dynamic ATS Allowlist ─────────────────────────────────────────────────────

def build_dynamic_ats_allowlist(
    aggregator_jobs: list,
    existing_allowlist: dict,
) -> dict:
    """
    Derives ATS slugs from companies already appearing in aggregator results.
    A company must appear ATS_MIN_APPEARANCES times across sources to earn
    a lookup slot. This ensures the allowlist reflects who is genuinely and
    actively hiring this cycle — not a static trusted list.

    The allowlist is updated incrementally:
      - New companies that hit the threshold are added
      - Companies not seen in the current cycle have their appearances decremented
      - Companies that fall to 0 appearances are pruned

    Structure: { slug: { platform, company_name, appearances, last_seen } }
    """
    # Count appearances per company across all aggregator results
    from collections import Counter
    appearance_counts: Counter = Counter()
    slug_to_name: dict = {}

    for j in aggregator_jobs:
        company = j.get("company", "").strip()
        if not company:
            continue
        slug = company_to_slug(company)
        if slug:
            appearance_counts[slug] += 1
            slug_to_name[slug] = company  # last-seen name wins

    # Update existing allowlist
    updated = dict(existing_allowlist)

    # Decrement companies not seen this cycle (gradual pruning)
    for slug in list(updated.keys()):
        if slug not in appearance_counts:
            updated[slug]["appearances"] = updated[slug].get("appearances", 1) - 1
            if updated[slug]["appearances"] <= 0:
                del updated[slug]
                print(f"  ATS allowlist: pruned '{slug}' (no longer appearing in aggregators)")

    # Add or reinforce companies meeting the threshold
    for slug, count in appearance_counts.items():
        if count >= ATS_MIN_APPEARANCES:
            if slug in updated:
                updated[slug]["appearances"] = min(updated[slug]["appearances"] + 1, 10)
                updated[slug]["last_seen"]   = utcnow_iso()
            else:
                updated[slug] = {
                    "company_name": slug_to_name[slug],
                    "appearances":  count,
                    "last_seen":    utcnow_iso(),
                }

    # Cap at ATS_MAX_COMPANIES, prioritizing highest appearance count
    if len(updated) > ATS_MAX_COMPANIES:
        top = sorted(updated.items(), key=lambda x: x[1]["appearances"], reverse=True)
        updated = dict(top[:ATS_MAX_COMPANIES])

    print(f"  ATS allowlist: {len(updated)} companies active this cycle")
    return updated


def fetch_greenhouse_ats(slug: str, company_name: str) -> list:
    """
    Fetch listings directly from a company's Greenhouse ATS board.
    Outbound request is gated by the dynamic allowlist — only slugs
    derived from verified aggregator appearances are contacted.
    All results still pass through normalise() and is_valid().
    """
    try:
        r = httpx.get(
            f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
            params={"content": "true"},
            timeout=REQUEST_TIMEOUT,
        )
        if r.status_code == 404:
            return []  # slug invalid, will be pruned naturally
        r.raise_for_status()
        jobs, now = [], utcnow_iso()
        for j in r.json().get("jobs", []):
            loc_obj  = j.get("location", {})
            location = loc_obj.get("name", "See listing") if isinstance(loc_obj, dict) else "See listing"
            jobs.append(normalise({
                "id":          f"greenhouse_{j.get('id', '')}",
                "title":       j.get("title", ""),
                "company":     company_name,
                "location":    location,
                "remote_type": "See listing",
                "salary":      None,
                "posted":      (j.get("updated_at") or "")[:10],
                "url":         j.get("absolute_url", ""),
                "source":      "Greenhouse ATS",
                "ats_board":   slug,
                "description": (j.get("content") or "")[:600],
                "fetched_at":  now,
            }))
        return [j for j in jobs if is_valid(j)]
    except Exception:
        return []


def fetch_lever_ats(slug: str, company_name: str) -> list:
    """
    Fetch listings directly from a company's Lever ATS board.
    Same allowlist gating as Greenhouse — only verified slugs are contacted.
    """
    try:
        r = httpx.get(
            f"https://api.lever.co/v0/postings/{slug}",
            params={"mode": "json"},
            timeout=REQUEST_TIMEOUT,
        )
        if r.status_code == 404:
            return []
        r.raise_for_status()
        jobs, now = [], utcnow_iso()
        for j in r.json():
            categories = j.get("categories", {})
            location   = categories.get("location") or categories.get("team") or "See listing"
            jobs.append(normalise({
                "id":          f"lever_{j.get('id', '')}",
                "title":       j.get("text", ""),
                "company":     company_name,
                "location":    location,
                "remote_type": "Remote" if "remote" in location.lower() else "See listing",
                "salary":      None,
                "posted":      datetime.fromtimestamp(
                                   j["createdAt"] / 1000, tz=timezone.utc
                               ).strftime("%Y-%m-%d") if j.get("createdAt") else "",
                "url":         j.get("hostedUrl", ""),
                "source":      "Lever ATS",
                "ats_board":   slug,
                "description": (j.get("descriptionPlain") or "")[:600],
                "fetched_at":  now,
            }))
        return [j for j in jobs if is_valid(j)]
    except Exception:
        return []


def fetch_all_ats(allowlist: dict) -> list:
    """
    Attempt Greenhouse then Lever for each allowlisted slug.
    Each company gets one successful fetch — if Greenhouse returns results,
    Lever is skipped for that slug to avoid duplicates.
    """
    all_jobs, total = [], 0
    for slug, meta in allowlist.items():
        company_name = meta.get("company_name", slug)
        jobs = fetch_greenhouse_ats(slug, company_name)
        if not jobs:
            jobs = fetch_lever_ats(slug, company_name)
        if jobs:
            total += len(jobs)
            all_jobs.extend(jobs)
    if total:
        print(f"  ATS (Greenhouse/Lever): {total} listings fetched across {len(allowlist)} companies")
    return all_jobs


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("=" * 50)
    print("Siegeworks Job Compiler — Catalog Fetch")
    print("=" * 50)

    # 1. Load existing catalog
    print("\n[1/6] Loading existing catalog…")
    existing = load_existing()
    old_jobs = existing.get("jobs", [])
    print(f"  Found {len(old_jobs)} existing listings")

    # 2. Prune stale listings
    print(f"\n[2/6] Pruning listings older than {MAX_AGE_DAYS} days…")
    fresh = prune_stale(old_jobs)
    print(f"  {len(fresh)} listings remain after date prune")

    # 3. Verify remaining URLs
    print(f"\n[3/6] Verifying {len(fresh)} listing URL(s)…")
    verified = verify_urls(fresh)
    print(f"  {len(verified)} listings remain after URL verification")

    # 4. Fetch from aggregator APIs
    print("\n[4/6] Fetching from aggregator APIs…")
    aggregator_jobs: list = []
    aggregator_jobs += fetch_remotive()
    aggregator_jobs += fetch_jobicy()
    aggregator_jobs += fetch_arbeitnow()
    aggregator_jobs += fetch_themuse()

    adzuna_id  = os.environ.get("ADZUNA_APP_ID",  "").strip()
    adzuna_key = os.environ.get("ADZUNA_APP_KEY", "").strip()
    if adzuna_id and adzuna_key:
        aggregator_jobs += fetch_adzuna(adzuna_id, adzuna_key)
    else:
        print("  Adzuna: skipped (ADZUNA_APP_ID / ADZUNA_APP_KEY not set)")

    usajobs_key = os.environ.get("USAJOBS_API_KEY",      "").strip()
    usajobs_ua  = os.environ.get("USAJOBS_USER_AGENT",   "").strip()
    if usajobs_key and usajobs_ua:
        aggregator_jobs += fetch_usajobs(usajobs_key, usajobs_ua)
    else:
        print("  USAJobs: skipped (USAJOBS_API_KEY / USAJOBS_USER_AGENT not set)")

    # 5. Build dynamic ATS allowlist and fetch ATS listings
    print("\n[5/6] Building dynamic ATS allowlist and fetching direct ATS listings…")
    existing_allowlist = load_ats_allowlist()
    allowlist = build_dynamic_ats_allowlist(aggregator_jobs, existing_allowlist)
    save_ats_allowlist(allowlist)
    ats_jobs = fetch_all_ats(allowlist)

    new_jobs = aggregator_jobs + ats_jobs

    # 6. Deduplicate by URL, then by title+company hash; write catalog
    print("\n[6/6] Deduplicating and writing catalog…")
    existing_urls     = {j.get("url")                            for j in verified if j.get("url")}
    existing_dk_keys  = {make_dedup_key(j["title"], j["company"]) for j in verified if j.get("title") and j.get("company")}

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
    catalog  = {
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
