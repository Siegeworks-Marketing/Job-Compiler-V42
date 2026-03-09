/**
 * Siegeworks Job Compiler — Vercel API Proxy
 * Routes:
 *   POST /api/proxy       → Anthropic API forward (requires x-user-api-key)
 *   POST /api/verify-url  → HEAD-checks job URLs server-side (no API key needed)
 *
 * Security:
 * - PROXY_SECRET validates all requests (set in Vercel env vars)
 * - Per-IP rate limiting: 30 req/min (Anthropic), 60 req/min (verify-url)
 * - Payload size cap: 256 KB
 * - API key format validation + model allowlist
 * - ALLOWED_ORIGIN: set to your GitHub Pages URL for tightest CORS
 */

const rateLimits = new Map();

function rateLimit(ip, bucket, maxPerMin) {
  const key = `${bucket}:${ip}`;
  const now = Date.now();
  const entry = rateLimits.get(key) || { count: 0, window: now };
  if (now - entry.window > 60_000) { entry.count = 0; entry.window = now; }
  entry.count++;
  rateLimits.set(key, entry);
  if (rateLimits.size > 5000) {
    for (const [k, v] of rateLimits) {
      if (now - v.window > 120_000) rateLimits.delete(k);
    }
  }
  return entry.count > maxPerMin;
}

export default async function handler(req, res) {
  const origin  = req.headers["origin"] || "";
  const allowed = process.env.ALLOWED_ORIGIN || "*";
  const cors    = allowed === "*" ? "*" : (origin === allowed ? origin : "null");
  res.setHeader("Access-Control-Allow-Origin", cors);
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

  // ── Route: verify-url ───────────────────────────────────────────────────────
  if (url.includes("verify-url")) {
    if (rateLimit(clientIP, "verify", 60))
      return res.status(429).json({ error: "Rate limited — wait 60s" });

    const rawUrls = Array.isArray(req.body?.urls) ? req.body.urls.slice(0, 20) : [];
    if (!rawUrls.length) return res.status(400).json({ results: [] });

    const safeUrls = rawUrls.filter(u =>
      u && typeof u.url === "string" &&
      u.url.startsWith("https://") &&
      !/localhost|127\.0\.0|0\.0\.0\.0/.test(u.url)
    );

    const closedSignals = [
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
      // Workday-specific (soft 404 — returns HTTP 200)
      "the page you are looking for doesn't exist",
      "the page you are looking for does not exist",
      "we're sorry, the page you are looking for",
      "we couldn't find that page",
      "this job requisition is no longer",
      "requisition is no longer available",
      // Greenhouse-specific
      "this job is no longer accepting applications",
      "job application is closed",
      // Lever-specific
      "this job posting has been archived",
      "this position is no longer open",
      // iCIMS-specific
      "this requisition is no longer active",
      // SmartRecruiters
      "this job offer is no longer available",
      // Taleo
      "the job you are trying to apply for is no longer available",
      // BambooHR
      "this position has been filled or is no longer available",
    ];

    const results = await Promise.all(safeUrls.map(async ({ url: jobUrl, title }) => {
      try {
        const ctrl = new AbortController();
        const timer = setTimeout(() => ctrl.abort(), 9000);
        const resp = await fetch(jobUrl, {
          redirect: "follow", signal: ctrl.signal,
          headers: {
            "User-Agent": "Mozilla/5.0 (compatible; SiegeworksJobCompiler/1.0)",
            "Accept": "text/html,application/xhtml+xml",
          },
        });
        clearTimeout(timer);

        let bodySnippet = "";
        try {
          const reader = resp.body?.getReader();
          if (reader) {
            const { value } = await reader.read();
            if (value) bodySnippet = new TextDecoder().decode(value).toLowerCase().slice(0, 8000);
            reader.cancel();
          }
        } catch {}

        const isClosed = closedSignals.some(s => bodySnippet.includes(s));
        const redirectedAway = resp.url !== jobUrl &&
          (/\/jobs\/?$|\/careers\/?$/.test(resp.url)) && !resp.url.includes(jobUrl.split("/").pop());

        const open = resp.status !== 404 && !isClosed && !redirectedAway;
        return { url: jobUrl, open, status: resp.status,
          signal: !open ? (resp.status===404 ? "404" : isClosed ? "closed text" : "redirect away") : "ok" };
      } catch (e) {
        if (e.name === "AbortError") return { url: jobUrl, open: true, signal: "timeout→assumed open", status: 0 };
        return { url: jobUrl, open: true, signal: "fetch blocked→assumed open", status: 0 };
      }
    }));

    return res.status(200).json({ results });
  }

  // ── Route: Anthropic proxy ──────────────────────────────────────────────────
  if (rateLimit(clientIP, "anthropic", 30))
    return res.status(429).json({ error: "Rate limited — 30 req/min max. Wait 60s." });

  const userApiKey = (req.headers["x-user-api-key"] || "").trim();
  if (!userApiKey)                         return res.status(400).json({ error: "Missing x-user-api-key" });
  if (!userApiKey.startsWith("sk-ant-"))   return res.status(400).json({ error: "Invalid API key format" });
  if (userApiKey.length < 20 || userApiKey.length > 250)
    return res.status(400).json({ error: "API key length invalid" });

  const body = req.body || {};
  const ALLOWED_MODELS = ["claude-sonnet-4-6","claude-haiku-4-5-20251001"];
  if (body.model && !ALLOWED_MODELS.includes(body.model))
    return res.status(400).json({ error: "Model not permitted via this proxy" });
  if (body.max_tokens && (typeof body.max_tokens !== "number" || body.max_tokens > 16000))
    return res.status(400).json({ error: "max_tokens out of range" });

  try {
    const response = await fetch("https://api.anthropic.com/v1/messages", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "x-api-key": userApiKey,
        "anthropic-version": "2023-06-01",
        "anthropic-beta": "web-search-2025-03-05",
      },
      body: bodyStr,
    });
    const data = await response.json();
    return res.status(response.status).json(data);
  } catch (e) {
    return res.status(502).json({ error: "Upstream error: " + e.message });
  }
}
