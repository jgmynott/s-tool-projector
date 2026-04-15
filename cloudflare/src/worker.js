// s-tool.io worker.
// - Serves static UI (frontend.html as index.html) from the bundled Assets binding.
// - Reverse-proxies /api/* to the Railway-hosted FastAPI backend.

const RAILWAY_API = "https://api-production-9fce.up.railway.app";

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (url.pathname.startsWith("/api/")) {
      const upstream = RAILWAY_API + url.pathname + url.search;
      // Don't mutate Host header — let fetch set it from the target URL.
      // Strip incoming Host/CF-* headers so Railway sees the request cleanly.
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
      return fetch(upstream, init);
    }

    return env.ASSETS.fetch(request);
  },
};
