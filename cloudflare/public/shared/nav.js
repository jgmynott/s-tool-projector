/* s-tool shared nav helpers — dropdown + themed Clerk modal + sign-out.
 * Loaded on every page with a user pill. Centralises the logic so we
 * fix UX bugs in ONE file, not eight.
 *
 * Self-initialises on script load (defer guarantees post-parse). Pages
 * MAY call STNav.init({ signInRedirect: '/foo' }) for a custom redirect,
 * but the default — location.pathname — covers every existing page. Init
 * is idempotent so calling it twice is a safe no-op.
 *
 * Loading flow (eliminates the prior "Sign in" flash on signed-in users):
 *   1. The HTML ships with a neutral .nav-pill-skel placeholder in #navEnd.
 *   2. nav.js runs (defer, post-parse). It reads the __client_uat cookie:
 *      - cookie indicates a session → keep the skel; wait for Clerk.
 *      - no session cookie → swap the skel to a real "Sign in" button.
 *   3. Clerk finishes loading. refreshPill() paints either the user pill
 *      (signed-in) or the Sign in button (signed-out, if Clerk decided
 *      the cookie was stale).
 *
 * On lazy pages (/how, /faq) Clerk isn't bundled by default. The Sign in
 * button delegates to lazySignIn() so the SDK only downloads when needed;
 * a session cookie also auto-triggers the lazy load via clerk-lazy.js.
 */
(function (global) {
  'use strict';
  // v2 brand — forest/cream palette. Clerk modal reads as the warm-white
  // panel with forest primary, mint highlight on focus rings.
  const CLERK_DARK = {
    appearance: {
      variables: {
        colorPrimary: '#0d4a2c',
        colorBackground: '#faf7f0',
        colorText: '#1a1a1a',
        colorTextSecondary: '#6b6b6b',
        colorInputBackground: '#ffffff',
        colorInputText: '#1a1a1a',
        colorNeutral: '#6b6b6b',
        borderRadius: '0',
        fontFamily: 'Geist, system-ui, sans-serif',
      },
      elements: {
        card: { boxShadow: '0 24px 64px -16px rgba(13,74,44,0.28)', border: '1px solid #c4c0b8' },
        modalBackdrop: { background: 'rgba(13,74,44,0.32)' },
      },
    },
  };

  let config = { signInRedirect: null };
  let initialized = false;
  let clerkReady = null;

  // Cookie heuristic: Clerk sets __client_uat=<unix-timestamp> when a
  // session exists; the timestamp is "0" when explicitly signed out, so
  // require a non-zero digit. This lets us decide the initial pill state
  // synchronously, before Clerk's ~320KB SDK has finished downloading,
  // so signed-in users no longer see "Sign in" flash for 300ms on every
  // page they land on.
  function looksSignedIn() {
    return /(?:^|;\s*)__client_uat=[^;]*[1-9]/.test(document.cookie || '');
  }

  async function authHeaders() {
    try {
      const t = await global.Clerk?.session?.getToken?.();
      return t ? { Authorization: `Bearer ${t}` } : {};
    } catch { return {}; }
  }

  function toggleMenu(evt) {
    evt?.stopPropagation?.();
    document.querySelector('.user-pill-wrap')?.classList.toggle('open');
  }
  function closeMenu() {
    document.querySelector('.user-pill-wrap')?.classList.remove('open');
  }

  async function signOut() {
    try { await global.Clerk?.signOut?.(); } catch (_) {}
    location.href = '/';
  }

  function openProfile() {
    global.Clerk?.openUserProfile?.(CLERK_DARK);
  }

  async function openBillingPortal() {
    try {
      const r = await fetch('/api/billing/portal', {
        method: 'POST', headers: await authHeaders(),
      });
      if (!r.ok) {
        const body = await r.json().catch(() => ({}));
        alert(body.detail || 'Billing portal unavailable — only active subscribers have one.');
        return;
      }
      const { portal_url } = await r.json();
      location.href = portal_url;
    } catch (e) {
      alert(`Could not open billing: ${e.message || e}`);
    }
  }

  // Render the initial pill state (skel for cookie-says-signed-in, sign-in
  // button otherwise). Runs synchronously on script load so the visual
  // settles before paint on prerendered pages and within ~50ms on cold
  // pages. Idempotent — if the HTML already shipped the right placeholder
  // we just reuse it.
  function paintInitialPill() {
    const end = document.getElementById('navEnd');
    if (!end) return;
    // If the page hasn't been migrated to ship a skel placeholder yet,
    // honor whatever it ships. We still want to replace inline-onclick
    // sign-in buttons (they reference window.Clerk directly which fails
    // silently on lazy pages and uses default un-themed Clerk modal on
    // eager pages).
    const existingSignIn = end.querySelector('.signin-btn[onclick]');
    if (existingSignIn) {
      existingSignIn.setAttribute('onclick', 'STNav.openSignIn()');
    }
    if (looksSignedIn()) {
      // Don't overwrite if the skel is already present.
      if (!end.querySelector('.nav-pill-skel') && !end.querySelector('.user-pill')) {
        end.innerHTML = '<div class="nav-pill-skel" aria-hidden="true"></div>';
      }
    } else if (!end.querySelector('.signin-btn')) {
      end.innerHTML = '<button class="signin-btn" onclick="STNav.openSignIn()">Sign in</button>';
    }
  }

  async function refreshPill() {
    const end = document.getElementById('navEnd');
    if (!end) return;
    if (!global.Clerk?.session) {
      end.innerHTML = `<button class="signin-btn" onclick="STNav.openSignIn()">Sign in</button>`;
      return;
    }
    try {
      const r = await fetch('/api/me', { headers: await authHeaders() });
      if (!r.ok) return;
      const d = await r.json();
      // Paywall removed 2026-04-29 — no tier distinction, no upgrade
      // path, no subscription management surfaced from the user pill.
      const fullEmail = d.email || '';
      const shortEmail = fullEmail.split('@')[0] || 'account';
      const subRow = '';
      // On desktop the pill shows: avatar · email · tier badge · caret.
      // On mobile (<640px) the .pill-email and .pill-caret are hidden via
      // CSS so the pill collapses to avatar + tier, leaving room for the
      // nav links. The menu then carries the full email + every
      // navigation destination so mobile users lose nothing.
      // pill-avatar shows the first letter of the email in mint-on-forest
      // for the new brand. CSS in /shared/nav.css handles the visual.
      const initial = (shortEmail[0] || 'S').toUpperCase();
      end.innerHTML = `
        <div class="user-pill-wrap">
          <button class="user-pill" onclick="STNav.toggleMenu(event)" aria-haspopup="true" aria-label="Account menu for ${fullEmail || shortEmail}">
            <span class="pill-avatar">${initial}</span>
            <span class="pill-email">${shortEmail}</span>
            <svg class="pill-caret" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"></polyline></svg>
          </button>
          <div class="user-menu" role="menu">
            <div class="menu-email" title="${fullEmail}">${fullEmail || 'Signed in'}</div>
            <button class="menu-item" onclick="STNav.openProfile(); STNav.closeMenu();">Manage account</button>
            ${subRow}
            <button class="menu-item" onclick="location.href='/'; STNav.closeMenu();">Home</button>
            <button class="menu-item" onclick="location.href='/app'; STNav.closeMenu();">App</button>
            <button class="menu-item" onclick="location.href='/picks'; STNav.closeMenu();">Picks</button>
            <button class="menu-item" onclick="location.href='/how'; STNav.closeMenu();">How it works</button>
            <button class="menu-item" onclick="location.href='/faq'; STNav.closeMenu();">FAQ</button>
            <button class="menu-item danger" onclick="STNav.signOut(); STNav.closeMenu();">Sign out</button>
          </div>
        </div>`;
    } catch (_) {}
  }

  function openSignIn() {
    const redirect = config.signInRedirect || location.pathname || '/';
    if (global.Clerk?.openSignIn) {
      global.Clerk.openSignIn({ afterSignInUrl: redirect, ...CLERK_DARK });
      return;
    }
    // Lazy page (clerk-lazy.js loaded but Clerk SDK not yet downloaded).
    // The lazy helper triggers the SDK fetch then opens Clerk's modal.
    if (typeof global.lazySignIn === 'function') {
      global.lazySignIn(redirect);
      return;
    }
    // Fallback: pricing page is always reachable and explains the gate.
    location.href = '/pricing';
  }

  // Returns a singleton promise that resolves when Clerk is loaded. On
  // eager pages this is fast (the SDK is already downloading via the
  // <script async> tag in <head>). On lazy pages we wait for clerk-lazy.js
  // or a Sign-in click to start the load. Polling cadence is fast (40ms)
  // so hot-cached SDKs paint nearly instantly.
  function ensureClerkReady() {
    if (clerkReady) return clerkReady;
    clerkReady = new Promise((resolve) => {
      const tick = () => {
        if (global.Clerk?.load) {
          global.Clerk.load({ appearance: CLERK_DARK.appearance })
            .then(() => resolve(global.Clerk))
            .catch(() => resolve(null));
        } else {
          setTimeout(tick, 40);
        }
      };
      tick();
    });
    return clerkReady;
  }

  // init(): idempotent — calling it twice is a safe no-op (subsequent
  // calls update config but don't re-run setup). Auto-fires on script
  // load so pages that forget to call it (track-record, how, backtest)
  // still get a working user pill.
  function init(opts = {}) {
    config = { signInRedirect: location.pathname || '/', ...config, ...opts };
    if (initialized) return;
    initialized = true;
    document.addEventListener('click', (e) => {
      if (!e.target.closest?.('.user-pill-wrap')) closeMenu();
    });
    paintInitialPill();
    ensureClerkReady().then((clerk) => {
      if (!clerk) return;
      refreshPill();
      try { clerk.addListener(refreshPill); } catch (_) {}
    });
  }

  // Self-init on script load. Defer guarantees the body — and therefore
  // #navEnd — has already been parsed by the time we run.
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => init(), { once: true });
  } else {
    init();
  }

  global.STNav = {
    init, refreshPill, toggleMenu, closeMenu, signOut,
    openProfile, openBilling: openBillingPortal, openSignIn,
    ready: ensureClerkReady,
  };
})(window);
