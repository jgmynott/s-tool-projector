/* s-tool shared nav helpers — dropdown + themed Clerk modal + sign-out.
 * Loaded on every page that renders the user pill. Centralises the logic
 * so we fix UX bugs in ONE file, not six.
 *
 * Pages call:
 *   STNav.init({ signInRedirect: '/picks' })
 * after Clerk has loaded. The script:
 *   - wires document click-outside to close the dropdown
 *   - polls /api/me, paints the pill OR a sign-in button into #navEnd
 *   - opens Clerk's profile / Stripe portal with dark theming
 */
(function (global) {
  const CLERK_DARK = {
    appearance: {
      variables: {
        colorPrimary: '#5FAAC5',
        colorBackground: '#111512',
        colorText: '#EDECE6',
        colorTextSecondary: '#9BA1B9',
        colorInputBackground: '#1F232D',
        colorInputText: '#EDECE6',
        colorNeutral: '#6B7382',
        borderRadius: '10px',
        fontFamily: 'Inter, sans-serif',
      },
      elements: {
        card: { boxShadow: '0 20px 60px rgba(0,0,0,0.55)' },
        modalBackdrop: { background: 'rgba(11,16,6,0.72)' },
      },
    },
  };

  let config = { signInRedirect: '/' };

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
      const tier = (d.tier || 'free').toLowerCase();
      const badge = tier === 'strategist' ? 'Strategist' : tier === 'pro' ? 'Pro' : 'Free';
      const fullEmail = d.email || '';
      const shortEmail = fullEmail.split('@')[0] || 'account';
      const hasSub = tier === 'pro' || tier === 'strategist';
      const subRow = hasSub
        ? `<button class="menu-item" onclick="STNav.openBilling(); STNav.closeMenu();">Manage subscription</button>`
        : `<button class="menu-item" onclick="location.href='/pricing'; STNav.closeMenu();">Upgrade</button>`;
      // On desktop the pill shows: avatar · email · tier badge · caret.
      // On mobile (<640px) the .pill-email and .pill-caret are hidden via
      // CSS so the pill collapses to avatar + tier, leaving room for the
      // nav links. The menu then carries the full email + every
      // navigation destination so mobile users lose nothing.
      end.innerHTML = `
        <div class="user-pill-wrap">
          <button class="user-pill" onclick="STNav.toggleMenu(event)" aria-haspopup="true" aria-label="Account menu for ${fullEmail || shortEmail}">
            <span class="pill-avatar" style="width:26px;height:26px;border-radius:50%;background:linear-gradient(135deg,#022834,#5FAAC5);display:inline-block;flex-shrink:0;"></span>
            <span class="pill-email">${shortEmail}</span>
            <span class="tier-badge">${badge}</span>
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
            <button class="menu-item" onclick="location.href='/pricing'; STNav.closeMenu();">Pricing</button>
            <button class="menu-item" onclick="location.href='/faq'; STNav.closeMenu();">FAQ</button>
            <button class="menu-item danger" onclick="STNav.signOut(); STNav.closeMenu();">Sign out</button>
          </div>
        </div>`;
    } catch (_) {}
  }

  function openSignIn() {
    global.Clerk?.openSignIn?.({
      afterSignInUrl: config.signInRedirect,
      ...CLERK_DARK,
    });
  }

  // Single Clerk-loaded promise — re-used by every consumer (nav.js init,
  // picks-app.js, track-record-app.js) so we never hit Clerk.load() twice
  // with different appearance configs.
  let _clerkReady;
  function ready() {
    if (_clerkReady) return _clerkReady;
    _clerkReady = new Promise((resolve) => {
      const tick = () => {
        if (!global.Clerk?.load) return setTimeout(tick, 60);
        global.Clerk.load({ appearance: CLERK_DARK.appearance })
          .then(() => resolve(global.Clerk))
          .catch(() => resolve(global.Clerk));
      };
      tick();
    });
    return _clerkReady;
  }

  function init(opts = {}) {
    config = { ...config, ...opts };
    document.addEventListener('click', (e) => {
      if (!e.target.closest?.('.user-pill-wrap')) closeMenu();
    });
    ready().then((clerk) => {
      refreshPill();
      try { clerk?.addListener?.(refreshPill); } catch (_) {}
    });
  }

  global.STNav = {
    init, ready, refreshPill, toggleMenu, closeMenu, signOut,
    openProfile, openBilling: openBillingPortal, openSignIn,
  };
})(window);
