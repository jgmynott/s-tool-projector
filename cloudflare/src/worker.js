// s-tool.io worker.
// - Serves static UI (frontend.html as index.html) from the bundled Assets binding.
// - Reverse-proxies /api/* to the Railway-hosted FastAPI backend.
// - Injects security headers on every response: HSTS + XFO + XCTO always;
//   CSP only on HTML responses (JSON/assets don't need it and adding it
//   can break Stripe/Clerk's own redirects).

const RAILWAY_API = "https://api-production-9fce.up.railway.app";

// Strict-ish CSP that still allows the known third parties we embed:
//   - Clerk frontend SDK: *.clerk.accounts.dev + clerk.io
//   - Stripe Checkout + Elements: js.stripe.com + hooks.stripe.com
//   - ApexCharts + fonts from jsDelivr + Google Fonts
// If we later move off one of these, trim the directive.
const CSP = [
  "default-src 'self'",
  "script-src 'self' 'unsafe-inline' https://*.clerk.accounts.dev https://*.clerk.com https://js.stripe.com https://cdn.jsdelivr.net",
  "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
  "img-src 'self' data: blob: https:",
  "font-src 'self' data: https://fonts.gstatic.com",
  "connect-src 'self' https://*.clerk.accounts.dev https://*.clerk.com https://api.stripe.com https://api-production-9fce.up.railway.app https://s-tool.io https://www.s-tool.io",
  "frame-src https://js.stripe.com https://hooks.stripe.com https://*.clerk.accounts.dev",
  "worker-src 'self' blob:",
  "frame-ancestors 'none'",
  "base-uri 'self'",
  "form-action 'self' https://*.stripe.com",
].join("; ");

function applySecurityHeaders(response, { html }) {
  const headers = new Headers(response.headers);
  headers.set("Strict-Transport-Security", "max-age=31536000; includeSubDomains; preload");
  headers.set("X-Content-Type-Options", "nosniff");
  headers.set("X-Frame-Options", "DENY");
  headers.set("Referrer-Policy", "strict-origin-when-cross-origin");
  headers.set("Permissions-Policy", "geolocation=(), microphone=(), camera=(), payment=(self \"https://js.stripe.com\")");
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
    headers.set("Cache-Control", "public, max-age=3600, stale-while-revalidate=86400");
  } else if (html) {
    headers.set("Cache-Control", "public, max-age=300, stale-while-revalidate=86400");
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
