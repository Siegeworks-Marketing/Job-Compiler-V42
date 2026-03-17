/**
 * Siegeworks Job Compiler — Vercel API Proxy v4 (Gemini Edition)
 *
 * Routes:
 *   POST /api/proxy       → Google Gemini 2.0 Flash (server-side key, no user key needed)
 *   POST /api/verify-url  → Multi-strategy ATS verification
 *
 * AI Provider: Google Gemini 2.0 Flash
 *   - Free tier: 15 req/min, 1M tokens/day, no credit card needed
 *   - Built-in Google Search grounding (replaces Anthropic web-search tool)
 *   - This proxy translates Anthropic message format → Gemini → back to Anthropic
 *     so index.html needs only minimal changes
 *
 * Required Vercel environment variable:
 *   GEMINI_API_KEY  → your Google AI Studio key (free at aistudio.google.com)
 *
 * Optional Vercel environment variables:
 *   PROXY_SECRET    → shared secret to block unauthorized requests
 *   ALLOWED_ORIGIN  → your GitHub Pages URL for tight CORS
 *     e.g. https://siegeworks-marketing.github.io
 *
 * Verification strategies by platform:
 *   Workday        → CXS JSON API (public, returns job data or 404)
 *   Greenhouse     → boards-api.greenhouse.io/v1/boards/{co}/jobs/{id}
 *   Lever          → api.lever.co/v0/postings/{co}/{id}
 *   All others     → Full body read up to 200KB + 40+ closed-signal patterns
 *
 * Security:
 *   PROXY_SECRET   → request filter
 *   ALLOWED_ORIGIN → tight CORS
 *   Rate limiting  → 30 req/min AI calls, 60 req/min verify-url
 *   ATS domain allowlist on verify-url (SSRF prevention)
 *   Private IP blocklist (SSRF prevention)
 */

// ── Rate limiter ──────────────────────────────────────────────────────────────
const rateLimits = new Map();
function rateLimit(ip, bucket, maxPerMin) {
  const key = `${bucket}:${ip}`;
  const now = Date.now();
  const e = rateLimits.get(key) || { count: 0, window: now };
  if (now - e.window > 60_000) { e.count = 0; e.window = now; }
  e.count++;
  rateLimits.set(key, e);
  if (rateLimits.size > 5000)
    for (const [k, v] of rateLimits) if (now - v.window > 120_000) rateLimits.delete(k);
  return e.count > maxPerMin;
}

// ── Body reader ───────────────────────────────────────────────────────────────
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

function visibleText(html) {
  return html
    .replace(/<script[^>]*>[\s\S]*?<\/script>/gi, " ")
    .replace(/<style[^>]*>[\s\S]*?<\/style>/gi, " ")
    .replace(/<[^>]+>/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

// ── Closed-job signal list ────────────────────────────────────────────────────
const CLOSED_SIGNALS = [
  "job no longer available", "position has been filled",
  "no longer accepting applications", "this job has expired",
  "job listing has been removed", "position is no longer",
  "job posting has expired", "application is closed",
  "this position has been filled", "posting has been deactivated",
  "job has been filled", "this position is no longer available",
  "sorry, this job", "page not found", "this job is no longer",
  "no longer available",
  "the page you are looking for doesn't exist",
  "the page you are looking for does not exist",
  "we're sorry, the page you", "we couldn't find that page",
  "this job requisition is no longer", "requisition is no longer available",
  "job is no longer open",
  "this job is no longer accepting applications",
  "job application is closed", "this role is no longer",
  "this job posting has been archived", "this position is no longer open",
  "position has been closed",
  "this requisition is no longer active", "this job opening is no longer",
  "this job offer is no longer available", "job has expired",
  "the job you are trying to apply for is no longer available",
  "this position has been filled or is no longer available",
  "this job is closed", "application period has ended",
  "this position is no longer accepting applications",
  "this job has been closed",
  "404", "doesn't exist", "does not exist", "not found",
];

// ── ATS verification ──────────────────────────────────────────────────────────
async function checkWorkday(jobUrl) {
  const m = jobUrl.match(
    /https?:\/\/([^.]+)\.(wd\d+)\.myworkdayjobs\.com\/([^/]+)\/job\/[^/]+\/.*?(_R-[\d-]+\d)$/i
  );
  if (!m) return null;
  const [, tenant, instance, board, rawId] = m;
  const jobId = rawId.replace(/^_/, "");
  const apiUrl = `https://${tenant}.${instance}.myworkdayjobs.com/wday/cxs/${tenant}/${board}/jobs`;
  try {
    const ctrl = new AbortController();
    setTimeout(() => ctrl.abort(), 8000);
    const r = await fetch(apiUrl, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json",
        "User-Agent": "Mozilla/5.0 (compatible; SiegeworksJobCompiler/1.0)" },
      body: JSON.stringify({ limit: 1, offset: 0, searchText: jobId }),
      signal: ctrl.signal,
    });
    if (!r.ok) return { open: false, signal: `Workday API ${r.status}`, method: "workday-api" };
    const data = await r.json();
    const total = data?.total ?? data?.jobPostings?.length ?? -1;
    if (total === 0) return { open: false, signal: "Workday API: 0 results", method: "workday-api" };
    if (total > 0)   return { open: true,  signal: `Workday API: ${total} result(s)`, method: "workday-api" };
    return null;
  } catch { return null; }
}

async function checkGreenhouse(jobUrl) {
  const m = jobUrl.match(/greenhouse\.io\/([^/]+)\/jobs\/(\d+)/i);
  if (!m) return null;
  const [, company, jobId] = m;
  try {
    const ctrl = new AbortController();
    setTimeout(() => ctrl.abort(), 7000);
    const r = await fetch(
      `https://boards-api.greenhouse.io/v1/boards/${company}/jobs/${jobId}`,
      { signal: ctrl.signal, headers: { Accept: "application/json" } }
    );
    if (r.status === 404) return { open: false, signal: "Greenhouse 404 — job closed", method: "greenhouse-api" };
    if (r.ok)             return { open: true,  signal: "Greenhouse 200 — job exists", method: "greenhouse-api" };
    return null;
  } catch { return null; }
}

async function checkLever(jobUrl) {
  const m = jobUrl.match(/jobs\.lever\.co\/([^/]+)\/([a-f0-9-]{36})/i);
  if (!m) return null;
  const [, company, jobId] = m;
  try {
    const ctrl = new AbortController();
    setTimeout(() => ctrl.abort(), 7000);
    const r = await fetch(
      `https://api.lever.co/v0/postings/${company}/${jobId}`,
      { signal: ctrl.signal, headers: { Accept: "application/json" } }
    );
    if (r.status === 404) return { open: false, signal: "Lever 404 — posting closed", method: "lever-api" };
    if (r.ok) {
      const d = await r.json();
      if (d?.state === "closed") return { open: false, signal: "Lever: state=closed", method: "lever-api" };
      return { open: true, signal: "Lever: posting open", method: "lever-api" };
    }
    return null;
  } catch { return null; }
}

async function checkBodyScan(jobUrl) {
  try {
    const ctrl = new AbortController();
    setTimeout(() => ctrl.abort(), 12000);
    const resp = await fetch(jobUrl, {
      redirect: "follow", signal: ctrl.signal,
      headers: {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        Accept: "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
      },
    });
    if (resp.status === 404) return { open: false, signal: "HTTP 404", method: "body-scan" };
    if (resp.status >= 400) return { open: false, signal: `HTTP ${resp.status}`, method: "body-scan" };
    const finalUrl = resp.url || jobUrl;
    if (finalUrl !== jobUrl) {
      try {
        const orig = new URL(jobUrl), final = new URL(finalUrl);
        if (orig.hostname === final.hostname &&
            /^\/(jobs|careers)?\/?$/.test(final.pathname))
          return { open: false, signal: "Redirected to careers homepage", method: "body-scan" };
      } catch {}
    }
    const rawHtml = await readBody(resp, 204800);
    const visible = visibleText(rawHtml);
    if (visible.length < 500)
      return { open: null, signal: "SPA — content JS-rendered, cannot verify", method: "body-scan", spa: true };
    const found = CLOSED_SIGNALS.find(s => visible.includes(s));
    if (found) return { open: false, signal: `Closed signal: "${found}"`, method: "body-scan" };
    const hasApply = visible.includes("apply") || visible.includes("submit application");
    if (hasApply) return { open: true, signal: "Apply text found", method: "body-scan" };
    return { open: null, signal: "Page accessible, status ambiguous", method: "body-scan" };
  } catch (e) {
    if (e.name === "AbortError") return { open: null, signal: "Timeout", method: "body-scan" };
    return { open: null, signal: "Fetch error", method: "body-scan" };
  }
}

async function verifyOne(jobUrl) {
  const url = jobUrl.toLowerCase();
  if (url.includes("myworkdayjobs.com")) {
    const r = await checkWorkday(jobUrl);
    return r !== null ? r : checkBodyScan(jobUrl);
  }
  if (url.includes("greenhouse.io")) {
    const r = await checkGreenhouse(jobUrl);
    return r !== null ? r : checkBodyScan(jobUrl);
  }
  if (url.includes("lever.co")) {
    const r = await checkLever(jobUrl);
    return r !== null ? r : checkBodyScan(jobUrl);
  }
  return checkBodyScan(jobUrl);
}

// ── SSRF protection ───────────────────────────────────────────────────────────
const ATS_ALLOWLIST = [
  "myworkdayjobs.com","greenhouse.io","lever.co","smartrecruiters.com",
  "icims.com","taleo.net","bamboohr.com","ashbyhq.com","workable.com",
  "recruitee.com","jobvite.com","rippling.com","applytojob.com",
  "linkedin.com","indeed.com","builtin.com","careers.microsoft.com",
  "amazon.jobs","jobs.google.com","jobs.apple.com","careers.google.com",
  "meta.com","jobs.lever.co",
];
function isAllowedAtsUrl(urlStr) {
  try {
    const { hostname } = new URL(urlStr);
    return ATS_ALLOWLIST.some(d => hostname === d || hostname.endsWith("." + d));
  } catch { return false; }
}
const PRIVATE_IP_RE = /^https?:\/\/(localhost|127\.|0\.0\.0\.0|10\.|172\.(1[6-9]|2\d|3[01])\.|192\.168\.|169\.254\.|::1|\[::1\])/i;

// ── Gemini translation layer ──────────────────────────────────────────────────
// The frontend sends Anthropic-format payloads and expects Anthropic-format
// responses. This layer converts transparently so index.html barely changes.

function toGeminiContents(messages) {
  // Anthropic: [{role:"user"|"assistant", content: string | [{type,text},...]}]
  // Gemini:    [{role:"user"|"model",     parts: [{text},...]}]
  return messages.map(m => ({
    role: m.role === "assistant" ? "model" : "user",
    parts: Array.isArray(m.content)
      ? m.content
          .filter(b => b.type === "text" || (b.type === "tool_result"))
          .map(b => ({ text: b.type === "text" ? b.text : JSON.stringify(b.content) }))
      : [{ text: String(m.content || "") }],
  }));
}

function toGeminiSystemInstruction(systemPrompt) {
  if (!systemPrompt) return undefined;
  return { parts: [{ text: systemPrompt }] };
}

function fromGeminiToAnthropic(geminiData) {
  // Extract text from Gemini response and wrap in Anthropic content shape.
  // Also surfaces any Google Search grounding queries as pseudo tool_use blocks
  // so the frontend's onLog callback (which looks for web_search tool_use) fires.
  const candidate = geminiData?.candidates?.[0];
  if (!candidate) throw new Error("Gemini returned no candidates");

  const content = [];

  // Surface search queries as pseudo tool_use so frontend logging works
  const groundingMeta = candidate.groundingMetadata;
  if (groundingMeta?.searchEntryPoint || groundingMeta?.webSearchQueries) {
    const queries = groundingMeta.webSearchQueries || [];
    queries.forEach((q, i) => {
      content.push({
        type: "tool_use",
        id: `gs_${i}`,
        name: "web_search",
        input: { query: q },
      });
    });
  }

  // Main text
  const text = (candidate.content?.parts || [])
    .map(p => p.text || "")
    .join("")
    .trim();
  if (text) content.push({ type: "text", text });

  const finishReason = candidate.finishReason;
  const stop_reason =
    finishReason === "STOP"        ? "end_turn"  :
    finishReason === "MAX_TOKENS"  ? "max_tokens":
    finishReason === "TOOL_USE"    ? "tool_use"  : "end_turn";

  return { content, stop_reason, model: "gemini-2.0-flash" };
}

// ── Main handler ──────────────────────────────────────────────────────────────
export default async function handler(req, res) {
  const origin  = req.headers["origin"] || "";
  const allowed = process.env.ALLOWED_ORIGIN || "*";
  const cors    = allowed === "*" ? "*" : (origin === allowed ? origin : "null");
  res.setHeader("Access-Control-Allow-Origin",  cors);
  res.setHeader("Access-Control-Allow-Methods", "POST, OPTIONS");
  res.setHeader("Access-Control-Allow-Headers", "Content-Type, x-proxy-secret");
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

  // ── Route: POST /api/verify-url ───────────────────────────────────────────
  if (url.includes("verify-url")) {
    if (rateLimit(clientIP, "verify", 60))
      return res.status(429).json({ error: "Rate limited — wait 60s" });

    const rawUrls = Array.isArray(req.body?.urls) ? req.body.urls.slice(0, 25) : [];
    if (!rawUrls.length) return res.status(400).json({ results: [] });

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
          return { url: jobUrl, open: null, signal: "Error: " + e.message.slice(0, 60), method: "error" };
        }
      })
    );
    return res.status(200).json({ results });
  }

  // ── Route: POST /api/proxy → Gemini Flash ────────────────────────────────
  if (rateLimit(clientIP, "ai", 120))
    return res.status(429).json({ error: "Rate limited (120 req/min). Wait 120s." });

  const geminiKey = process.env.GEMINI_API_KEY;
  if (!geminiKey)
    return res.status(500).json({ error: "Server misconfigured — GEMINI_API_KEY not set" });

  const body = req.body || {};

  // Convert Anthropic payload to Gemini format
  const contents           = toGeminiContents(body.messages || []);
  const systemInstruction  = toGeminiSystemInstruction(body.system);
  const maxOutputTokens    = Math.min(body.max_tokens || 8000, 8192);

  // Enable Google Search grounding (replaces Anthropic web_search tool)
  const useSearch = (body.tools || []).some(t => t.name === "web_search" || t.type?.includes("web_search"));
  const tools = useSearch
    ? [{ google_search: {} }]
    : undefined;

  const geminiPayload = {
    contents,
    ...(systemInstruction ? { system_instruction: systemInstruction } : {}),
    generationConfig: { maxOutputTokens, temperature: 0.7 },
    ...(tools ? { tools } : {}),
  };

  try {
    const model = "gemini-2.0-flash";
    const geminiUrl = `https://generativelanguage.googleapis.com/v1beta/models/${model}:generateContent?key=${geminiKey}`;

    const resp = await fetch(geminiUrl, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(geminiPayload),
    });

    const text = await resp.text();
    let geminiData;
    try { geminiData = JSON.parse(text); }
    catch { return res.status(resp.status).send(text); }

    if (!resp.ok) {
      const msg = geminiData?.error?.message || `Gemini API error ${resp.status}`;
      return res.status(resp.status).json({ type: "error", error: { message: msg } });
    }

    // Convert Gemini response back to Anthropic format
    const anthropicResponse = fromGeminiToAnthropic(geminiData);
    return res.status(200).json(anthropicResponse);

  } catch (e) {
    return res.status(502).json({ type: "error", error: { message: "Upstream error: " + e.message } });
  }
}
