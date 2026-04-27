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

export default {
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
