/**
 * Siegeworks Job Compiler — Vercel API Proxy v3
 *
 * Routes:
 *   POST /api/proxy       → Anthropic API forward
 *   POST /api/verify-url  → Multi-strategy ATS verification
 *
 * Verification strategies by platform:
 *   Workday       → CXS JSON API (public, returns job data or 404)
 *   Greenhouse    → boards-api.greenhouse.io/v1/boards/{co}/jobs/{id}
 *   Lever         → api.lever.co/v0/postings/{co}/{id}
 *   SmartRecruiters → Full body read (SSR, closed text is in HTML)
 *   All others    → Full body read up to 200KB + closed signal scan
 *
 * Security:
 *   PROXY_SECRET  → request filter (set in Vercel env vars)
 *   ALLOWED_ORIGIN → set to your GitHub Pages URL for tight CORS
 *   Rate limiting → 30 req/min Anthropic, 60 req/min verify
 *   Model allowlist, payload size cap, API key format validation
 */

const rateLimits = new Map();
function rateLimit(ip, bucket, maxPerMin) {
  const key = `${bucket}:${ip}`;
  const now = Date.now();
  const e = rateLimits.get(key) || { count:0, window:now };
  if (now - e.window > 60_000) { e.count = 0; e.window = now; }
  e.count++;
  rateLimits.set(key, e);
  if (rateLimits.size > 5000) {
    for (const [k,v] of rateLimits) if (now - v.window > 120_000) rateLimits.delete(k);
  }
  return e.count > maxPerMin;
}

// ── Read full response body up to maxBytes ──────────────────────────────────
async function readBody(response, maxBytes = 204800) {
  const reader = response.body?.getReader();
  if (!reader) return "";
  const chunks = [];
  let total = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done || !value) break;
      chunks.push(value);
      total += value.length;
      if (total >= maxBytes) break;
    }
  } catch {}
  reader.cancel().catch(() => {});
  const merged = new Uint8Array(total);
  let offset = 0;
  for (const c of chunks) { merged.set(c, offset); offset += c.length; }
  return new TextDecoder().decode(merged).toLowerCase();
}

// ── Strip HTML/JS to get visible text (approx) ─────────────────────────────
function visibleText(html) {
  return html
    .replace(/<script[^>]*>[\s\S]*?<\/script>/gi, " ")
    .replace(/<style[^>]*>[\s\S]*?<\/style>/gi, " ")
    .replace(/<[^>]+>/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

// ── Closed signal list (platform-specific + generic) ───────────────────────
const CLOSED_SIGNALS = [
  // Generic
  "job no longer available",
  "position has been filled",
  "no longer accepting applications",
  "this job has expired",
  "job listing has been removed",
  "position is no longer",
  "job posting has expired",
  "application is closed",
  "this position has been filled",
  "posting has been deactivated",
  "job has been filled",
  "this position is no longer available",
  "sorry, this job",
  "page not found",
  "this job is no longer",
  "no longer available",
  // Workday-specific (soft 200 with error page)
  "the page you are looking for doesn't exist",
  "the page you are looking for does not exist",
  "we're sorry, the page you",
  "we couldn't find that page",
  "this job requisition is no longer",
  "requisition is no longer available",
  "job is no longer open",
  // Greenhouse
  "this job is no longer accepting applications",
  "job application is closed",
  "this role is no longer",
  // Lever
  "this job posting has been archived",
  "this position is no longer open",
  "position has been closed",
  // iCIMS
  "this requisition is no longer active",
  "this job opening is no longer",
  // SmartRecruiters
  "this job offer is no longer available",
  "job has expired",
  // Taleo
  "the job you are trying to apply for is no longer available",
  // BambooHR
  "this position has been filled or is no longer available",
  // Ashby
  "this job is closed",
  "application period has ended",
  // Workable
  "this position is no longer accepting applications",
  // Recruitee
  "this job has been closed",
  // Jobvite
  "this position has been filled",
  // Generic 404-style in SPAs
  "404",
  "doesn't exist",
  "does not exist",
  "not found",
];

// ── Workday CXS JSON API check ──────────────────────────────────────────────
async function checkWorkday(jobUrl) {
  // Parse: https://{tenant}.{instance}.myworkdayjobs.com/{board}/job/{loc}/{slug}_{jobId}
  const m = jobUrl.match(
    /https?:\/\/([^.]+)\.(wd\d+)\.myworkdayjobs\.com\/([^/]+)\/job\/[^/]+\/.*?(_R-[\d-]+\d)$/i
  );
  if (!m) return null; // Can't parse — skip API check

  const [, tenant, instance, board, rawId] = m;
  const jobId = rawId.replace(/^_/, ""); // e.g. "R-801367"

  // Workday public search API — search by job requisition ID
  const apiUrl = `https://${tenant}.${instance}.myworkdayjobs.com/wday/cxs/${tenant}/${board}/jobs`;
  try {
    const ctrl = new AbortController();
    setTimeout(() => ctrl.abort(), 8000);
    const r = await fetch(apiUrl, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (compatible; SiegeworksJobCompiler/1.0)",
      },
      body: JSON.stringify({ limit: 1, offset: 0, searchText: jobId }),
      signal: ctrl.signal,
    });
    if (!r.ok) return { open: false, signal: `Workday API ${r.status}`, method: "workday-api" };
    const data = await r.json();
    const total = data?.total ?? data?.jobPostings?.length ?? -1;
    if (total === 0) return { open: false, signal: "Workday API: 0 results for job ID", method: "workday-api" };
    if (total > 0)  return { open: true,  signal: `Workday API: ${total} result(s)`, method: "workday-api" };
    return null; // Unexpected shape — fall through
  } catch (e) {
    if (e.name === "AbortError") return null;
    return null; // API blocked or network error — fall through to other checks
  }
}

// ── Greenhouse public API check ─────────────────────────────────────────────
async function checkGreenhouse(jobUrl) {
  // Patterns: boards.greenhouse.io/company/jobs/id  OR  job-boards.greenhouse.io/company/jobs/id
  const m = jobUrl.match(/greenhouse\.io\/([^/]+)\/jobs\/(\d+)/i);
  if (!m) return null;
  const [, company, jobId] = m;
  try {
    const ctrl = new AbortController();
    setTimeout(() => ctrl.abort(), 7000);
    const r = await fetch(
      `https://boards-api.greenhouse.io/v1/boards/${company}/jobs/${jobId}`,
      { signal: ctrl.signal, headers: { "Accept": "application/json" } }
    );
    if (r.status === 404) return { open: false, signal: "Greenhouse API 404 — job closed", method: "greenhouse-api" };
    if (r.ok)             return { open: true,  signal: "Greenhouse API 200 — job exists", method: "greenhouse-api" };
    return null;
  } catch { return null; }
}

// ── Lever public API check ──────────────────────────────────────────────────
async function checkLever(jobUrl) {
  // jobs.lever.co/company/uuid
  const m = jobUrl.match(/jobs\.lever\.co\/([^/]+)\/([a-f0-9-]{36})/i);
  if (!m) return null;
  const [, company, jobId] = m;
  try {
    const ctrl = new AbortController();
    setTimeout(() => ctrl.abort(), 7000);
    const r = await fetch(
      `https://api.lever.co/v0/postings/${company}/${jobId}`,
      { signal: ctrl.signal, headers: { "Accept": "application/json" } }
    );
    if (r.status === 404) return { open: false, signal: "Lever API 404 — posting closed", method: "lever-api" };
    if (r.ok) {
      const d = await r.json();
      if (d?.state === "closed") return { open: false, signal: "Lever API: state=closed", method: "lever-api" };
      return { open: true, signal: "Lever API: posting exists and open", method: "lever-api" };
    }
    return null;
  } catch { return null; }
}

// ── Full-body text scan (SSR pages: SmartRecruiters, Ashby, etc.) ───────────
async function checkBodyScan(jobUrl) {
  try {
    const ctrl = new AbortController();
    setTimeout(() => ctrl.abort(), 12000);
    const resp = await fetch(jobUrl, {
      redirect: "follow",
      signal: ctrl.signal,
      headers: {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
      },
    });

    if (resp.status === 404) return { open: false, signal: "HTTP 404", method: "body-scan" };
    if (resp.status >= 400) return { open: false, signal: `HTTP ${resp.status}`, method: "body-scan" };

    // Check for redirect to generic careers/jobs page (job removed)
    const finalUrl = resp.url || jobUrl;
    if (finalUrl !== jobUrl) {
      const orig = new URL(jobUrl);
      const final = new URL(finalUrl);
      // If redirected to a completely different path (e.g. /jobs or /careers root)
      if (orig.hostname === final.hostname &&
          (final.pathname === "/jobs" || final.pathname === "/careers" ||
           final.pathname === "/" || final.pathname.match(/^\/(jobs|careers)\/?$/))) {
        return { open: false, signal: "Redirected to careers homepage — listing removed", method: "body-scan" };
      }
    }

    const rawHtml = await readBody(resp, 204800); // 200KB
    const visible = visibleText(rawHtml);

    // If the body has very little visible text, it's a SPA — cannot confirm status
    if (visible.length < 500) {
      return { open: null, signal: "SPA detected — content JS-rendered, cannot verify", method: "body-scan", spa: true };
    }

    // Check for closed signals in visible text
    const found = CLOSED_SIGNALS.find(s => visible.includes(s));
    if (found) return { open: false, signal: `Closed signal: "${found}"`, method: "body-scan" };

    // Check for any apply button or job-related text as positive signal
    const hasApply = visible.includes("apply") || visible.includes("submit application") || visible.includes("apply now");
    if (hasApply) return { open: true, signal: "Apply button/text found in page", method: "body-scan" };

    // No positive signal, no negative signal — uncertain but accessible
    return { open: null, signal: "Page accessible, status ambiguous", method: "body-scan" };
  } catch (e) {
    if (e.name === "AbortError") return { open: null, signal: "Timeout — assumed uncertain", method: "body-scan" };
    return { open: null, signal: "Fetch error — assumed uncertain", method: "body-scan" };
  }
}

// ── Master verification function ─────────────────────────────────────────────
async function verifyOne(jobUrl) {
  const url = jobUrl.toLowerCase();

  // Route to the best strategy for this ATS platform
  if (url.includes("myworkdayjobs.com")) {
    const apiResult = await checkWorkday(jobUrl);
    if (apiResult !== null) return apiResult;
    // API parse failed — fall back to body scan
    return checkBodyScan(jobUrl);
  }

  if (url.includes("greenhouse.io")) {
    const apiResult = await checkGreenhouse(jobUrl);
    if (apiResult !== null) return apiResult;
    return checkBodyScan(jobUrl);
  }

  if (url.includes("jobs.lever.co") || url.includes("lever.co")) {
    const apiResult = await checkLever(jobUrl);
    if (apiResult !== null) return apiResult;
    return checkBodyScan(jobUrl);
  }

  // All others: full body scan
  return checkBodyScan(jobUrl);
}

// ── Main handler ──────────────────────────────────────────────────────────────
export default async function handler(req, res) {
  const origin  = req.headers["origin"] || "";
  const allowed = process.env.ALLOWED_ORIGIN || "*";
  const cors    = allowed === "*" ? "*" : (origin === allowed ? origin : "null");
  res.setHeader("Access-Control-Allow-Origin",  cors);
  res.setHeader("Access-Control-Allow-Methods", "POST, OPTIONS");
  res.setHeader("Access-Control-Allow-Headers", "Content-Type, x-user-api-key, x-proxy-secret");
  res.setHeader("X-Frame-Options", "DENY");

  if (req.method === "OPTIONS") return res.status(200).end();
  if (req.method !== "POST")   return res.status(405).json({ error: "Method not allowed" });

  const bodyStr = JSON.stringify(req.body || {});
  if (bodyStr.length > 262144) return res.status(413).json({ error: "Payload too large" });

  const proxySecret = process.env.PROXY_SECRET;
  if (proxySecret && req.headers["x-proxy-secret"] !== proxySecret)
    return res.status(403).json({ error: "Forbidden" });

  const clientIP = (req.headers["x-forwarded-for"] || "unknown").split(",")[0].trim();
  const url = req.url || "";

  // ── Route: POST /api/verify-url ─────────────────────────────────────────────
  if (url.includes("verify-url")) {
    if (rateLimit(clientIP, "verify", 60))
      return res.status(429).json({ error: "Rate limited — wait 60s" });

    const rawUrls = Array.isArray(req.body?.urls) ? req.body.urls.slice(0, 25) : [];
    if (!rawUrls.length) return res.status(400).json({ results: [] });

    // ATS domain allowlist — verify-url will ONLY fetch known job board domains.
    // This prevents use as a SSRF probe or DDoS amplifier.
    const ATS_DOMAIN_ALLOWLIST = [
      "myworkdayjobs.com", "greenhouse.io", "lever.co", "smartrecruiters.com",
      "icims.com", "taleo.net", "bamboohr.com", "ashbyhq.com", "workable.com",
      "recruitee.com", "jobvite.com", "rippling.com", "applytojob.com",
      "linkedin.com", "indeed.com", "builtin.com", "careers.microsoft.com",
      "amazon.jobs", "jobs.google.com", "jobs.apple.com", "hire.withgoogle.com",
      "careers.google.com", "meta.com", "jobs.lever.co",
    ];
    function isAllowedAtsUrl(urlStr) {
      try {
        const { hostname } = new URL(urlStr);
        return ATS_DOMAIN_ALLOWLIST.some(d => hostname === d || hostname.endsWith("." + d));
      } catch { return false; }
    }

    // Block private/link-local IP ranges to prevent SSRF
    const PRIVATE_IP_RE = /^https?:\/\/(localhost|127\.|0\.0\.0\.0|10\.|172\.(1[6-9]|2\d|3[01])\.|192\.168\.|169\.254\.|::1|\[::1\])/i;

    const safeUrls = rawUrls.filter(u =>
      u && typeof u.url === "string" &&
      u.url.startsWith("https://") &&
      !PRIVATE_IP_RE.test(u.url) &&
      isAllowedAtsUrl(u.url)
    );

    const results = await Promise.all(
      safeUrls.map(async ({ url: jobUrl }) => {
        try {
          const result = await verifyOne(jobUrl);
          return { url: jobUrl, ...result };
        } catch (e) {
          return { url: jobUrl, open: null, signal: "Unhandled error: " + e.message.slice(0, 60), method: "error" };
        }
      })
    );

    return res.status(200).json({ results });
  }

  // ── Route: POST /api/proxy → Anthropic ─────────────────────────────────────
  if (rateLimit(clientIP, "anthropic", 30))
    return res.status(429).json({ error: "Rate limited (30 req/min). Wait 60s." });

  const userApiKey = (req.headers["x-user-api-key"] || "").trim();
  if (!userApiKey)                        return res.status(400).json({ error: "Missing x-user-api-key" });
  if (!userApiKey.startsWith("sk-ant-"))  return res.status(400).json({ error: "Invalid API key format" });
  if (userApiKey.length < 20 || userApiKey.length > 250)
    return res.status(400).json({ error: "API key length invalid" });

  const body = req.body || {};
  const ALLOWED_MODELS = ["claude-sonnet-4-6", "claude-haiku-4-5-20251001"];
  if (body.model && !ALLOWED_MODELS.includes(body.model))
    return res.status(400).json({ error: "Model not permitted via this proxy" });
  if (body.max_tokens && (typeof body.max_tokens !== "number" || body.max_tokens > 16000))
    return res.status(400).json({ error: "max_tokens out of range" });

  try {
    const resp = await fetch("https://api.anthropic.com/v1/messages", {
      method: "POST",
      headers: {
        "Content-Type":      "application/json",
        "x-api-key":         userApiKey,
        "anthropic-version": "2023-06-01",
        "anthropic-beta":    "web-search-2025-03-05",
      },
      body: bodyStr,
    });
    // Read full response text before parsing to avoid 502 on non-JSON Anthropic errors
    const text = await resp.text();
    let data;
    try { data = JSON.parse(text); }
    catch { return res.status(resp.status).send(text); }
    return res.status(resp.status).json(data);
  } catch (e) {
    return res.status(502).json({ error: "Upstream error: " + e.message });
  }
}
