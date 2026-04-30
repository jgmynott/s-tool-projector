// s-tool.io worker.
// - Serves /app and /picks from the bundled Assets binding.
// - Reverse-proxies /api/* to the Railway-hosted FastAPI backend.
// - Injects security headers (CSP only on HTML responses).
// Auth + billing were removed 2026-04-30 — site is open access.

const RAILWAY_API = "https://api-production-9fce.up.railway.app";

// Strict-ish CSP. ApexCharts + Google Fonts are the only third parties.
const CSP = [
  "default-src 'self'",
  "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net",
  "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
  "img-src 'self' data: blob: https:",
  "font-src 'self' data: https://fonts.gstatic.com",
  "connect-src 'self' https://api-production-9fce.up.railway.app https://s-tool.io https://www.s-tool.io",
  "worker-src 'self' blob:",
  "frame-ancestors 'none'",
  "base-uri 'self'",
  "form-action 'self'",
].join("; ");

function applySecurityHeaders(response, { html }) {
  const headers = new Headers(response.headers);
  headers.set("Strict-Transport-Security", "max-age=31536000; includeSubDomains; preload");
  headers.set("X-Content-Type-Options", "nosniff");
  headers.set("X-Frame-Options", "DENY");
  headers.set("Referrer-Policy", "strict-origin-when-cross-origin");
  headers.set("Permissions-Policy", "geolocation=(), microphone=(), camera=()");
  if (html) headers.set("Content-Security-Policy", CSP);
  return new Response(response.body, { status: response.status, statusText: response.statusText, headers });
}

// Cache-Control override — env.ASSETS.fetch() returns "public, max-age=0,
// must-revalidate" on every static response and the _headers file does
// NOT propagate through the Worker assets binding the same way it does
// for pure Pages projects (verified live 2026-04-27). Setting it here
// in the Worker is the only reliable way to get real browser caching,
// which is what makes repeat in-app navigation feel instant. SWR keeps
// the page warm for 24h after max-age expires — eliminates white flash.
function applyCacheControl(response, { html, pathname }) {
  const headers = new Headers(response.headers);
  if (pathname.startsWith("/shared/") || pathname.startsWith("/img/")) {
    // Long browser cache for assets; we bust them via ?v= query params on
    // the script src in HTML, so a deploy that changes JS/CSS surfaces
    // immediately as a brand-new URL.
    headers.set("Cache-Control", "public, max-age=3600, stale-while-revalidate=86400");
  } else if (html) {
    // Short TTL on HTML so a deploy reaches users within ~30s on next
    // navigation. SWR=120 trades a tiny staleness window for one
    // additional snappy visit. Previously max-age=300 + SWR=86400 left
    // users on stale HTML for up to a full day after a deploy and
    // forced manual hard-refresh on every iteration cycle.
    headers.set("Cache-Control", "public, max-age=30, stale-while-revalidate=120");
  }
  return new Response(response.body, { status: response.status, statusText: response.statusText, headers });
}

// Cron-trigger → trader dispatcher.
// GitHub Actions cron has documented 0–60 min slippage. We had a 60-min
// slip on 2026-04-27 close that left daytrade positions held overnight,
// triggered the breaker the next morning, and lost a full day of trading.
// Cloudflare Workers cron is sub-minute reliable, so we use the Worker as
// the primary scheduler. It just calls workflow_dispatch on trader.yml at
// the exact UTC minute we want — trader.py still runs in GH Actions where
// it already lives. The GH Actions schedule in trader.yml is kept as a
// fallback (concurrency: cancel-in-progress=false handles the rare double
// fire as a queued no-op).
//
// Required secrets (wrangler secret put):
//   GITHUB_PAT     fine-grained token, repo scope, contents:write+actions:write
//   GITHUB_REPO    "owner/repo" e.g. "jgmynott/s-tool-projector"
//
// Trigger → window mapping. Each cron expression maps to one window. We
// fire BOTH a DST and ST variant for open + close so the right ET time
// fires year-round; the off-season fire hits trader.py's market-closed
// guard or its late-close journal pull and is a clean no-op. Rotate cron
// covers 14:00–19:30 UTC every 30 min — captures both seasons.
// Each cron maps to (workflow file, optional inputs). Cron expressions
// here MUST exactly match wrangler.toml — a typo silently falls through
// to no-op and the scheduler quietly stops.
function targetForCron(cron) {
  // Morning digest — 08:00 ET, fires both DST (12 UTC) and ST (13 UTC)
  // so it lands at the same local time year-round. Off-season fire is
  // a duplicate push during the changeover week; acceptable.
  if (cron === "0 12 * * 1-5")        return { workflow: "morning-digest.yml", inputs: {} };
  if (cron === "0 13 * * 1-5")        return { workflow: "morning-digest.yml", inputs: {} };
  // EOD trades report — 16:30 ET (post-close, give Alpaca 30 min to
  // settle the close print). DST: 20:30 UTC; ST: 21:30 UTC.
  if (cron === "30 20 * * 1-5")       return { workflow: "eod-report.yml", inputs: {} };
  if (cron === "30 21 * * 1-5")       return { workflow: "eod-report.yml", inputs: {} };
  // Trader pre_open — 13:25 UTC (DST) or 14:25 UTC (ST). Sweeps any
  // daytrade held-over from yesterday so the 09:30 open print can't
  // gap them down through the breaker. Submits sells while market is
  // still closed; Alpaca queues them for the open print.
  if (cron === "25 13,14 * * 1-5")    return { workflow: "trader.yml",  inputs: { mode: "live", window: "pre_open" } };
  // Trader open  — 13:30 UTC (DST) or 14:30 UTC (ST)
  if (cron === "30 13,14 * * 1-5")    return { workflow: "trader.yml",  inputs: { mode: "live", window: "open"  } };
  // Trader rotate — every 30 min 14:00–19:30 UTC
  if (cron === "0,30 14-19 * * 1-5")  return { workflow: "trader.yml",  inputs: { mode: "live", window: "rotate" } };
  // Trader close — 19:55 UTC (DST) or 20:55 UTC (ST)
  if (cron === "55 19,20 * * 1-5")    return { workflow: "trader.yml",  inputs: { mode: "live", window: "close" } };
  // Scalper — every 5 min during RTH. No window input; scalper.yml's
  // dispatch block defaults to mode=live when not provided via input.
  if (cron === "*/5 14-19 * * 1-5")   return { workflow: "scalper.yml", inputs: { mode: "live" } };
  return null;
}

async function dispatchWorkflow(env, target) {
  const repo = env.GITHUB_REPO;
  const pat = env.GITHUB_PAT;
  if (!repo || !pat) {
    return { ok: false, error: "GITHUB_REPO/GITHUB_PAT secrets not set on Worker" };
  }
  const url = `https://api.github.com/repos/${repo}/actions/workflows/${target.workflow}/dispatches`;
  const resp = await fetch(url, {
    method: "POST",
    headers: {
      "Accept": "application/vnd.github+json",
      "Authorization": `Bearer ${pat}`,
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "s-tool-cf-cron",
    },
    body: JSON.stringify({
      ref: "main",
      inputs: target.inputs || {},
    }),
  });
  if (resp.status === 204) return { ok: true };
  const body = await resp.text();
  return { ok: false, status: resp.status, body: body.slice(0, 300) };
}

async function notifyOnFailure(env, message) {
  const topic = env.NTFY_TOPIC;
  if (!topic) return;
  try {
    await fetch(`https://ntfy.sh/${topic}`, {
      method: "POST",
      headers: { "Title": "S-Tool CF cron failure", "Priority": "high", "Tags": "warning" },
      body: message.slice(0, 500),
    });
  } catch (_) { /* best effort */ }
}

// =============================================================
// /status route — phone-pull-refresh health view.
// =============================================================
//
// Aggregates the signals you'd otherwise have to check across Railway,
// GitHub, and the live API. Everything fetches in parallel so the page
// renders in <2s even if one upstream is slow.
//
// Each row is one ✅/❌ check. Reading order = dependency order: the
// trader can't fire if the data is stale, so data-status lives above
// trader-liveness, and you can stop reading the moment you hit a ❌.

async function gh(env, path) {
  // GitHub API needs a UA; PAT optional (rate-limit better with).
  const headers = { "User-Agent": "s-tool-status", "Accept": "application/vnd.github+json" };
  if (env.GITHUB_PAT) headers["Authorization"] = `Bearer ${env.GITHUB_PAT}`;
  const r = await fetch(`https://api.github.com${path}`, { headers });
  if (!r.ok) return null;
  return await r.json();
}

async function checkApiHealth() {
  try {
    const r = await fetch(`${RAILWAY_API}/api/health`, { signal: AbortSignal.timeout(5000) });
    return { ok: r.ok, detail: r.ok ? "200" : `HTTP ${r.status}` };
  } catch (e) {
    return { ok: false, detail: `unreachable: ${e.message || e}` };
  }
}

async function checkDataFreshness() {
  try {
    const r = await fetch(`${RAILWAY_API}/api/data-status`, { signal: AbortSignal.timeout(5000) });
    if (!r.ok) return { ok: false, detail: `data-status ${r.status}` };
    const d = await r.json();
    const ph = (d.feeds || {}).picks_history || {};
    const latest = ph.latest_pick_date;
    if (!latest) return { ok: false, detail: "no latest_pick_date" };
    const today = new Date().toISOString().slice(0, 10);
    return { ok: latest === today, detail: `picks: ${latest} (today=${today})` };
  } catch (e) {
    return { ok: false, detail: `error: ${e.message || e}` };
  }
}

async function checkPortfolio() {
  try {
    const r = await fetch(`${RAILWAY_API}/api/portfolio`, { signal: AbortSignal.timeout(5000) });
    if (!r.ok) return { ok: false, detail: `portfolio ${r.status}` };
    const d = await r.json();
    const eq = (d.account || {}).equity;
    const dcp = (d.account || {}).day_change_pct;
    if (eq == null) return { ok: false, detail: "no equity" };
    const pct = dcp == null ? "—" : `${(dcp * 100).toFixed(2)}%`;
    return { ok: true, detail: `$${Math.round(eq).toLocaleString()} · ${pct}` };
  } catch (e) {
    return { ok: false, detail: `error: ${e.message || e}` };
  }
}

function isMarketHours() {
  // Returns true if we're inside RTH (loose: any weekday 13:00–20:30 UTC).
  // Used to decide whether trader/scalper silence is alarming or expected.
  const d = new Date();
  const dow = d.getUTCDay();
  if (dow === 0 || dow === 6) return false;
  const utcMin = d.getUTCHours() * 60 + d.getUTCMinutes();
  return utcMin >= 13 * 60 && utcMin <= 20 * 60 + 30;
}

async function checkWorkflowRecent(env, workflowFile, maxMinutes, label) {
  try {
    const repo = env.GITHUB_REPO || "jgmynott/s-tool-projector";
    const data = await gh(env, `/repos/${repo}/actions/workflows/${workflowFile}/runs?per_page=5`);
    if (!data || !data.workflow_runs || data.workflow_runs.length === 0) {
      return { ok: false, detail: `${label}: no runs` };
    }
    const latest = data.workflow_runs[0];
    const ageMin = (Date.now() - new Date(latest.created_at).getTime()) / 60000;
    const ageStr = ageMin < 60 ? `${Math.round(ageMin)}m` : `${(ageMin / 60).toFixed(1)}h`;
    if (!isMarketHours()) {
      // After-hours: silence is expected. Just report.
      return { ok: true, detail: `${label}: ${ageStr} ago, ${latest.conclusion || latest.status} (off-hours)` };
    }
    if (ageMin > maxMinutes) {
      return { ok: false, detail: `${label}: ${ageStr} ago (>${maxMinutes}m stale)` };
    }
    if (latest.conclusion === "failure") {
      return { ok: false, detail: `${label}: ${ageStr} ago, FAILED` };
    }
    return { ok: true, detail: `${label}: ${ageStr} ago, ${latest.conclusion || latest.status}` };
  } catch (e) {
    return { ok: false, detail: `${label}: error ${e.message || e}` };
  }
}

async function checkOpenIssues(env) {
  try {
    const repo = env.GITHUB_REPO || "jgmynott/s-tool-projector";
    const issues = await gh(env, `/repos/${repo}/issues?state=open&labels=watchdog,trader-failure&per_page=20`);
    if (!Array.isArray(issues)) return { ok: false, detail: "GH issues unreachable" };
    if (issues.length === 0) return { ok: true, detail: "no open watchdog/trader issues" };
    return { ok: false, detail: `${issues.length} open: ${issues.slice(0, 2).map(i => `#${i.number}`).join(", ")}` };
  } catch (e) {
    return { ok: false, detail: `error: ${e.message || e}` };
  }
}

async function renderStatus(env) {
  const [api, freshness, portfolio, trader, scalper, issues] = await Promise.all([
    checkApiHealth(),
    checkDataFreshness(),
    checkPortfolio(),
    checkWorkflowRecent(env, "trader.yml", 90, "trader"),
    checkWorkflowRecent(env, "scalper.yml", 15, "scalper"),
    checkOpenIssues(env),
  ]);
  const checks = [
    { name: "API",                ...api },
    { name: "Data freshness",     ...freshness },
    { name: "Portfolio",          ...portfolio },
    { name: "Trader liveness",    ...trader },
    { name: "Scalper liveness",   ...scalper },
    { name: "Open issues",        ...issues },
  ];
  const allOk = checks.every(c => c.ok);
  const overall = allOk ? "✅ all systems" : "❌ attention needed";
  const generated = new Date().toISOString().replace("T", " ").slice(0, 19) + "Z";
  const rows = checks.map(c => {
    const icon = c.ok ? "✅" : "❌";
    const cls = c.ok ? "ok" : "bad";
    return `<div class="row ${cls}"><span class="icon">${icon}</span><span class="name">${c.name}</span><span class="detail">${c.detail || ""}</span></div>`;
  }).join("");
  const html = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />
<meta name="theme-color" content="${allOk ? "#10b981" : "#ef4444"}" />
<title>S-Tool status — ${overall}</title>
<style>
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body { margin:0; padding:16px; background:#0b0e14; color:#e6e6e6; font:15px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }
h1 { font-size:18px; margin:0 0 4px; font-weight:600; }
.sub { color:#9aa0a6; font-size:12px; margin-bottom:18px; }
.row { display:flex; align-items:flex-start; gap:10px; padding:14px 12px; border-radius:10px; margin-bottom:8px; background:#141821; }
.row.bad { background:#1f1015; border:1px solid #4a1d24; }
.icon { width:22px; flex-shrink:0; }
.name { width:130px; font-weight:600; flex-shrink:0; color:#cfd2d9; }
.detail { color:#9aa0a6; font-size:13px; word-break:break-word; }
.row.bad .detail { color:#fca5a5; }
.foot { margin-top:18px; color:#6b7280; font-size:11px; text-align:center; }
.foot a { color:#9aa0a6; text-decoration:none; }
</style>
</head>
<body>
<h1>${overall}</h1>
<div class="sub">${generated} · pull to refresh</div>
${rows}
<div class="foot">
  <a href="https://github.com/${env.GITHUB_REPO || "jgmynott/s-tool-projector"}/actions">actions</a> ·
  <a href="/app/">projector</a> ·
  <a href="/picks/">picks</a>
</div>
</body>
</html>`;
  return new Response(html, {
    status: 200,
    headers: {
      "Content-Type": "text/html; charset=utf-8",
      "Cache-Control": "no-store",
      "X-Content-Type-Options": "nosniff",
      "X-Frame-Options": "DENY",
    },
  });
}

export default {
  async scheduled(event, env, ctx) {
    const target = targetForCron(event.cron);
    if (!target) {
      console.log(`unmapped cron: ${event.cron}`);
      return;
    }
    const result = await dispatchWorkflow(env, target);
    if (!result.ok) {
      const msg = `dispatch ${target.workflow} ${JSON.stringify(target.inputs)} failed: ${JSON.stringify(result)}`;
      console.error(msg);
      ctx.waitUntil(notifyOnFailure(env, msg));
    } else {
      console.log(`dispatched ${target.workflow} ${JSON.stringify(target.inputs)} via cron=${event.cron}`);
    }

  },

  async fetch(request, env) {
    const url = new URL(request.url);

    // Site is now just /app + /picks. Bounce the old marketing routes so
    // existing inbound links don't 404.
    const DROPPED = new Set(["/", "/index.html", "/how", "/how/", "/pricing", "/pricing/", "/faq", "/faq/", "/backtest", "/backtest/", "/track-record", "/track-record/", "/studio", "/studio/"]);
    if (DROPPED.has(url.pathname)) {
      return Response.redirect(new URL("/app/", url).toString(), 302);
    }

    // Phone-friendly health dashboard. Aggregates the same signals you'd
    // otherwise have to check across Railway, GitHub, and the live API.
    if (url.pathname === "/status" || url.pathname === "/status/") {
      return await renderStatus(env);
    }

    if (url.pathname.startsWith("/api/")) {
      const upstream = RAILWAY_API + url.pathname + url.search;
      const headers = new Headers(request.headers);
      headers.delete("host");
      for (const h of [...headers.keys()]) {
        if (h.startsWith("cf-") || h.startsWith("x-forwarded-")) headers.delete(h);
      }
      const init = {
        method: request.method,
        headers,
        body: ["GET", "HEAD"].includes(request.method) ? undefined : request.body,
        redirect: "manual",
      };
      const response = await fetch(upstream, init);
      return applySecurityHeaders(response, { html: false });
    }

    const assetResponse = await env.ASSETS.fetch(request);
    const isHtml = (assetResponse.headers.get("content-type") || "").includes("text/html");
    const withCache = applyCacheControl(assetResponse, { html: isHtml, pathname: url.pathname });
    return applySecurityHeaders(withCache, { html: isHtml });
  },
};
