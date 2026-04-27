// Lazy Clerk loader — used by marketing pages that don't need the
// 320KB SDK on first paint. Visitors who never click Sign in pay zero
// cost; visitors who click pay the SDK download + parse exactly once.
// Pages call: lazySignIn('/how') etc.
(function () {
  let promise;
  function ensureClerk() {
    if (promise) return promise;
    promise = new Promise(function (resolve, reject) {
      const s = document.createElement('script');
      s.src = 'https://fluent-mole-71.clerk.accounts.dev/npm/@clerk/clerk-js@5/dist/clerk.browser.js';
      s.crossOrigin = 'anonymous';
      s.dataset.clerkPublishableKey = 'pk_test_Zmx1ZW50LW1vbGUtNzEuY2xlcmsuYWNjb3VudHMuZGV2JA';
      s.async = true;
      s.onload = function () {
        (function wait() {
          if (window.Clerk && window.Clerk.load) {
            window.Clerk.load().then(function () { resolve(window.Clerk); });
          } else { setTimeout(wait, 50); }
        })();
      };
      s.onerror = reject;
      document.head.appendChild(s);
    });
    return promise;
  }
  window.lazySignIn = function (redirect) {
    ensureClerk().then(function (c) {
      c.openSignIn({ afterSignInUrl: redirect || location.pathname });
    });
  };
  // If the user happens to already be signed in, the cookie is set.
  // We auto-load Clerk in that case so the user pill renders without
  // requiring a click. Cookie names per Clerk's session implementation.
  const isSignedInCookie = /(?:^|;\s*)__client_uat=/.test(document.cookie || '');
  if (isSignedInCookie) ensureClerk();
})();
