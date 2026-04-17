"""
Stripe billing glue.

Two concerns:
  1. Create a Checkout Session for the authed user, tied to our Clerk user id
     via client_reference_id.
  2. Consume Stripe webhooks to keep user tier in sync with subscription
     lifecycle events.

Webhook signature verification uses STRIPE_WEBHOOK_SECRET. The webhook handler
is idempotent — Stripe retries on 5xx, so every branch returns 200 on
already-processed events.
"""

from __future__ import annotations

import logging
import os
import sqlite3

import stripe
from fastapi import HTTPException

from users_db import (
    get_user,
    get_user_by_customer,
    set_stripe_customer,
    set_subscription,
    upsert_user,
)

log = logging.getLogger("billing")


def _as_dict(obj) -> dict:
    """Coerce a Stripe StripeObject (or anything vaguely dict-like) into a
    plain dict. Older versions of stripe-python supported `.get()` directly
    on StripeObject; newer ones don't, and accessing them like dicts raises.
    Converting once at the boundary keeps the rest of the module simple.
    """
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    for method in ("to_dict_recursive", "to_dict"):
        fn = getattr(obj, method, None)
        if callable(fn):
            try:
                return fn()
            except Exception:
                pass
    try:
        return dict(obj)
    except Exception:
        return {}

STRIPE_SK = os.getenv("STRIPE_SK", "").strip()
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "").strip()
STRIPE_PRICE_ID_PRO = os.getenv("STRIPE_PRICE_ID_PRO", "").strip()
STRIPE_PRICE_ID_STRATEGIST = os.getenv("STRIPE_PRICE_ID_STRATEGIST", "").strip()

# Map tier name → price id + canonical tier label written to users.tier
# when the subscription becomes active. Keep this in sync with
# users_db.quota_for_user and the landing page pricing copy.
TIER_PRICES = {
    "pro": (STRIPE_PRICE_ID_PRO, "pro"),
    "strategist": (STRIPE_PRICE_ID_STRATEGIST, "strategist"),
}

if STRIPE_SK:
    stripe.api_key = STRIPE_SK


def _require_configured(tier: str = "pro") -> str:
    """Resolve the Stripe price id for the requested tier. Raises if unset."""
    price_id, _ = TIER_PRICES.get(tier, ("", ""))
    if not STRIPE_SK or not price_id:
        raise HTTPException(
            status_code=503,
            detail=f"Billing not configured for tier '{tier}'",
        )
    return price_id


def create_checkout_session(
    conn: sqlite3.Connection,
    *,
    clerk_user_id: str,
    email: str | None,
    success_url: str,
    cancel_url: str,
    tier: str = "pro",
) -> str:
    """Create a Checkout Session for the requested tier. Returns redirect URL."""
    price_id = _require_configured(tier)

    user = upsert_user(conn, clerk_user_id, email=email)
    customer_id = user.get("stripe_customer_id")
    if not customer_id:
        customer = stripe.Customer.create(
            email=email or None,
            metadata={"clerk_user_id": clerk_user_id},
        )
        customer_id = customer.id
        set_stripe_customer(conn, clerk_user_id, customer_id)

    session = stripe.checkout.Session.create(
        mode="subscription",
        customer=customer_id,
        client_reference_id=clerk_user_id,
        line_items=[{"price": price_id, "quantity": 1}],
        success_url=success_url,
        cancel_url=cancel_url,
        allow_promotion_codes=True,
        # Stamp the requested tier into the session metadata so the webhook
        # knows which tier to assign when the subscription activates.
        metadata={"tier": tier},
        subscription_data={"metadata": {"tier": tier}},
    )
    return session.url


def create_portal_session(
    conn: sqlite3.Connection,
    *,
    clerk_user_id: str,
    return_url: str,
) -> str:
    """Return a Stripe Customer Portal URL for subscription management."""
    _require_configured()
    user = get_user(conn, clerk_user_id)
    if not user or not user.get("stripe_customer_id"):
        raise HTTPException(status_code=404, detail="No Stripe customer on file")
    portal = stripe.billing_portal.Session.create(
        customer=user["stripe_customer_id"],
        return_url=return_url,
    )
    return portal.url


# ── Webhook ──

ACTIVE_STATUSES = {"active", "trialing"}


def handle_webhook(
    conn: sqlite3.Connection,
    *,
    payload: bytes,
    signature: str,
) -> dict:
    """Verify + process a Stripe webhook. Returns a small diagnostic dict."""
    if not STRIPE_WEBHOOK_SECRET:
        raise HTTPException(status_code=503, detail="Webhook secret not configured")
    try:
        event = stripe.Webhook.construct_event(
            payload, signature, STRIPE_WEBHOOK_SECRET
        )
    except (ValueError, stripe.error.SignatureVerificationError) as e:
        log.warning("Webhook verification failed: %s", e)
        raise HTTPException(status_code=400, detail="Invalid signature")

    etype = event["type"]
    obj = _as_dict(event["data"]["object"])
    log.info("webhook received: type=%s obj_id=%s", etype, obj.get("id"))

    if etype == "checkout.session.completed":
        clerk_user_id = obj.get("client_reference_id")
        customer_id = obj.get("customer")
        sub_id = obj.get("subscription")
        log.info("checkout.session.completed: clerk=%s customer=%s sub=%s",
                 clerk_user_id, customer_id, sub_id)
        if clerk_user_id:
            if customer_id:
                set_stripe_customer(conn, clerk_user_id, customer_id)
            _sync_subscription(conn, clerk_user_id, sub_id)
            from users_db import get_user
            fresh = get_user(conn, clerk_user_id)
            log.info("checkout.session.completed result: tier=%s status=%s",
                     fresh.get("tier") if fresh else "?",
                     fresh.get("subscription_status") if fresh else "?")
        return {"ok": True, "type": etype}

    # Defensive fallback: if Stripe isn't configured to send
    # checkout.session.completed or customer.subscription.* events, we can
    # still derive the tier from invoice.paid (fires on every successful
    # subscription payment and carries both customer + subscription ids).
    if etype == "invoice.paid":
        customer_id = obj.get("customer")
        sub_id = obj.get("subscription")
        user = get_user_by_customer(conn, customer_id) if customer_id else None
        if not user or not sub_id:
            log.info("invoice.paid: no user/sub (customer=%s sub=%s)", customer_id, sub_id)
            return {"ok": True, "type": etype, "skipped": "no_user_or_sub"}
        _sync_subscription(conn, user["clerk_user_id"], sub_id)
        return {"ok": True, "type": etype, "resynced": True}

    if etype in (
        "customer.subscription.created",
        "customer.subscription.updated",
        "customer.subscription.deleted",
    ):
        customer_id = obj.get("customer")
        user = get_user_by_customer(conn, customer_id) if customer_id else None
        log.info("%s: customer=%s matched_user=%s", etype, customer_id,
                 user.get("clerk_user_id") if user else None)
        if not user:
            log.info("Webhook %s: no matching user for customer %s", etype, customer_id)
            return {"ok": True, "type": etype, "skipped": "no_user"}
        status = obj.get("status")
        tier = _tier_from_subscription(obj)
        log.info("%s: deriving tier=%s status=%s for clerk=%s",
                 etype, tier, status, user.get("clerk_user_id"))
        set_subscription(
            conn,
            user["clerk_user_id"],
            subscription_id=obj.get("id"),
            status=status,
            tier=tier,
        )
        return {"ok": True, "type": etype, "tier": tier}

    log.info("Webhook ignored: %s", etype)
    return {"ok": True, "type": etype, "ignored": True}


def _tier_from_subscription(sub) -> str:
    """Derive the canonical tier label from a Stripe Subscription object.

    SECURITY: We derive tier from the actual PRICE ID first — metadata is
    user-settable via the Customer Portal (item swap) and could be used
    to self-upgrade if trusted. Metadata is only consulted as a tiebreaker
    when the price id doesn't match our known TIER_PRICES (e.g. during a
    tier rename window).  Non-active subs → 'free'.
    """
    s = _as_dict(sub)
    if s.get("status") not in ACTIVE_STATUSES:
        return "free"
    items = _as_dict(s.get("items")).get("data") or []
    price_id = None
    if items:
        first = _as_dict(items[0])
        price_id = _as_dict(first.get("price")).get("id")
    # Primary: price-id match (authoritative — set by our backend at checkout)
    for t_name, (t_price, _) in TIER_PRICES.items():
        if price_id and price_id == t_price:
            return t_name
    # Secondary: metadata tier, only if the price id didn't match AND the
    # claim is a known tier. Logged so we can detect drift.
    meta_tier = (_as_dict(s.get("metadata")) or {}).get("tier")
    if meta_tier in ("pro", "strategist"):
        log.warning("sub %s: price_id %s matched no TIER_PRICES, falling back to metadata tier=%s",
                    s.get("id"), price_id, meta_tier)
        return meta_tier
    return "free"


def _sync_subscription(
    conn: sqlite3.Connection,
    clerk_user_id: str,
    subscription_id: str | None,
) -> None:
    if not subscription_id:
        return
    try:
        sub = stripe.Subscription.retrieve(subscription_id)
    except stripe.error.StripeError as e:
        log.warning("Subscription fetch failed for %s: %s", subscription_id, e)
        return
    tier = _tier_from_subscription(sub)
    set_subscription(
        conn,
        clerk_user_id,
        subscription_id=sub.id,
        status=sub.status,
        tier=tier,
    )


def resync_by_customer(conn: sqlite3.Connection, clerk_user_id: str) -> dict:
    """Force-resync tier by looking up the user's active subscription(s) on
    Stripe's side. Useful when the local DB never recorded a subscription_id
    because Stripe didn't send `checkout.session.completed` / sub.created
    events. Returns {tier, subscription_status} of the updated record.
    """
    from users_db import get_user  # local import to avoid cycles
    user = get_user(conn, clerk_user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    customer_id = user.get("stripe_customer_id")
    if not customer_id:
        raise HTTPException(status_code=404, detail="No Stripe customer on file")
    try:
        subs = stripe.Subscription.list(customer=customer_id, status="all", limit=10)
    except stripe.error.StripeError as e:
        log.warning("Subscription list failed for %s: %s", customer_id, e)
        raise HTTPException(status_code=502, detail=f"Stripe lookup failed: {e}")
    # Prefer an active/trialing sub; else the most recent.
    active = [s for s in subs.data if s.status in ACTIVE_STATUSES]
    chosen = active[0] if active else (subs.data[0] if subs.data else None)
    if not chosen:
        set_subscription(conn, clerk_user_id,
                         subscription_id=None, status=None, tier="free")
        return {"tier": "free", "subscription_status": None}
    tier = _tier_from_subscription(chosen)
    set_subscription(
        conn, clerk_user_id,
        subscription_id=chosen.id,
        status=chosen.status,
        tier=tier,
    )
    return {"tier": tier, "subscription_status": chosen.status}
