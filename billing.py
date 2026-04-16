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
    obj = event["data"]["object"]

    if etype == "checkout.session.completed":
        clerk_user_id = obj.get("client_reference_id")
        customer_id = obj.get("customer")
        sub_id = obj.get("subscription")
        if clerk_user_id:
            if customer_id:
                set_stripe_customer(conn, clerk_user_id, customer_id)
            _sync_subscription(conn, clerk_user_id, sub_id)
        return {"ok": True, "type": etype}

    if etype in (
        "customer.subscription.created",
        "customer.subscription.updated",
        "customer.subscription.deleted",
    ):
        customer_id = obj.get("customer")
        user = get_user_by_customer(conn, customer_id) if customer_id else None
        if not user:
            log.info("Webhook %s: no matching user for customer %s", etype, customer_id)
            return {"ok": True, "type": etype, "skipped": "no_user"}
        status = obj.get("status")
        # Derive tier from subscription metadata (set at checkout) or the
        # price id on the subscription's first item. Fall back to free when
        # neither matches — prevents a bad webhook from granting unintended
        # tier access.
        tier = "free"
        if status in ACTIVE_STATUSES:
            meta_tier = (obj.get("metadata") or {}).get("tier")
            if meta_tier in ("pro", "strategist"):
                tier = meta_tier
            else:
                items = (obj.get("items") or {}).get("data") or []
                price_id = items[0].get("price", {}).get("id") if items else None
                for t_name, (t_price, _) in TIER_PRICES.items():
                    if price_id and price_id == t_price:
                        tier = t_name
                        break
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
    tier = "pro" if sub.status in ACTIVE_STATUSES else "free"
    set_subscription(
        conn,
        clerk_user_id,
        subscription_id=sub.id,
        status=sub.status,
        tier=tier,
    )
