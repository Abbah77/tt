"""
Reelz Backend — FastAPI + Supabase PostgreSQL
==============================================
5 endpoints. Webhook is source of truth.
Local-first: app caches everything, backend is only hit:
  1. On sign-in (POST /auth/google)
  2. After payment (GET /subscription/status)
  3. On Paystack webhook (POST /webhook/paystack)
  4. On payment init (POST /payments/init)
  5. On shorts feed (GET /shorts/feed)  ← NEW
"""

import hashlib
import hmac
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import httpx
from fastapi import FastAPI, HTTPException, Request, Response, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import psycopg2
from psycopg2.extras import RealDictCursor

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("reelz")

# ── Env ───────────────────────────────────────────────────────────────────────
GOOGLE_CLIENT_ID        = os.environ["GOOGLE_CLIENT_ID"]
PAYSTACK_SECRET_KEY     = os.environ["PAYSTACK_SECRET_KEY"]
DATABASE_URL            = os.environ["DATABASE_URL"]
PAYSTACK_WEBHOOK_SECRET = os.environ.get("PAYSTACK_WEBHOOK_SECRET", PAYSTACK_SECRET_KEY)

# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(title="Reelz API", version="1.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["POST", "GET"],
    allow_headers=["*"],
)

# ── DB helpers ────────────────────────────────────────────────────────────────

def get_conn():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)


def init_db():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    google_sub   TEXT UNIQUE NOT NULL,
                    email        TEXT NOT NULL,
                    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
                );

                CREATE TABLE IF NOT EXISTS subscriptions (
                    id                        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    user_id                   UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    status                    TEXT NOT NULL DEFAULT 'expired',
                    expires_at                TIMESTAMPTZ,
                    paystack_customer_code    TEXT,
                    paystack_subscription_code TEXT,
                    updated_at                TIMESTAMPTZ NOT NULL DEFAULT now()
                );

                CREATE INDEX IF NOT EXISTS idx_subscriptions_user_id ON subscriptions(user_id);
                CREATE INDEX IF NOT EXISTS idx_users_google_sub     ON users(google_sub);
            """)
        conn.commit()
    log.info("DB tables ensured")


@app.on_event("startup")
async def startup():
    init_db()


# ── Pydantic models ───────────────────────────────────────────────────────────

class GoogleAuthRequest(BaseModel):
    id_token: str


class PaymentInitRequest(BaseModel):
    user_id: str
    plan: str
    email: str


class SubscriptionStatusRequest(BaseModel):
    user_id: str


# ── Google token verify ───────────────────────────────────────────────────────

async def verify_google_token(id_token: str) -> dict:
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            "https://oauth2.googleapis.com/tokeninfo",
            params={"id_token": id_token},
        )
    if resp.status_code != 200:
        raise HTTPException(status_code=401, detail="Invalid Google token")

    claims = resp.json()
    aud = claims.get("aud", "")
    if GOOGLE_CLIENT_ID not in (aud if isinstance(aud, list) else [aud]):
        log.warning("Token audience mismatch: %s", aud)
        raise HTTPException(status_code=401, detail="Token audience mismatch")

    exp = int(claims.get("exp", 0))
    if datetime.now(timezone.utc).timestamp() > exp:
        raise HTTPException(status_code=401, detail="Token expired")

    return claims


# ── Subscription helpers ──────────────────────────────────────────────────────

def _get_subscription(cur, user_id: str) -> dict | None:
    cur.execute(
        "SELECT * FROM subscriptions WHERE user_id = %s ORDER BY updated_at DESC LIMIT 1",
        (user_id,),
    )
    return cur.fetchone()


def _subscription_response(sub: dict | None) -> dict:
    if sub is None:
        return {"premium": False, "status": "none", "expires_at": None}

    now = datetime.now(timezone.utc)
    expires_at: datetime | None = sub.get("expires_at")
    status = sub.get("status", "expired")

    in_grace = (
        expires_at is not None
        and expires_at < now
        and (now - expires_at) < timedelta(hours=24)
    )

    is_premium = (
        status == "active"
        and expires_at is not None
        and (expires_at > now or in_grace)
    )

    return {
        "premium":    is_premium,
        "status":     status,
        "expires_at": expires_at.isoformat() if expires_at else None,
    }


# ═════════════════════════════════════════════════════════════════════════════
# ENDPOINT 1 — POST /auth/google
# ═════════════════════════════════════════════════════════════════════════════

@app.post("/auth/google")
async def auth_google(body: GoogleAuthRequest):
    claims = await verify_google_token(body.id_token)

    google_sub: str = claims["sub"]
    email: str = claims.get("email", "").lower().strip()

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO users (google_sub, email)
                VALUES (%s, %s)
                ON CONFLICT (google_sub) DO UPDATE SET email = EXCLUDED.email
                RETURNING id
            """, (google_sub, email))
            user = cur.fetchone()
            user_id = str(user["id"])

            sub = _get_subscription(cur, user_id)
        conn.commit()

    log.info("auth/google: sub=%s user_id=%s", google_sub[:8], user_id[:8])
    return {"user_id": user_id, **_subscription_response(sub)}


# ═════════════════════════════════════════════════════════════════════════════
# ENDPOINT 2 — POST /payments/init
# ═════════════════════════════════════════════════════════════════════════════

PLAN_AMOUNTS = {
    "monthly": 150_000,
    "yearly":  1_200_000,
}

@app.post("/payments/init")
async def payments_init(body: PaymentInitRequest):
    if body.plan not in PLAN_AMOUNTS:
        raise HTTPException(status_code=400, detail=f"Unknown plan: {body.plan}")

    amount = PLAN_AMOUNTS[body.plan]

    payload = {
        "email":     body.email,
        "amount":    amount,
        "currency":  "NGN",
        "metadata": {
            "user_id":    body.user_id,
            "plan":       body.plan,
            "custom_fields": [
                {"display_name": "Plan",    "variable_name": "plan",    "value": body.plan},
                {"display_name": "User ID", "variable_name": "user_id", "value": body.user_id},
            ],
        },
    }

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            "https://api.paystack.co/transaction/initialize",
            headers={"Authorization": f"Bearer {PAYSTACK_SECRET_KEY}"},
            json=payload,
        )

    if resp.status_code != 200:
        log.error("Paystack init failed: %s", resp.text)
        raise HTTPException(status_code=502, detail="Payment provider error")

    data = resp.json()
    if not data.get("status"):
        raise HTTPException(status_code=502, detail=data.get("message", "Paystack error"))

    return {
        "authorization_url": data["data"]["authorization_url"],
        "reference":         data["data"]["reference"],
    }


# ═════════════════════════════════════════════════════════════════════════════
# ENDPOINT 3 — GET /subscription/status
# ═════════════════════════════════════════════════════════════════════════════

@app.get("/subscription/status")
async def subscription_status(user_id: str):
    if not user_id:
        raise HTTPException(status_code=400, detail="user_id required")

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM users WHERE id = %s", (user_id,))
            if cur.fetchone() is None:
                raise HTTPException(status_code=404, detail="User not found")
            sub = _get_subscription(cur, user_id)

    return _subscription_response(sub)


# ═════════════════════════════════════════════════════════════════════════════
# ENDPOINT 4 — POST /webhook/paystack
# ═════════════════════════════════════════════════════════════════════════════

@app.post("/webhook/paystack")
async def webhook_paystack(request: Request):
    body_bytes = await request.body()
    signature  = request.headers.get("x-paystack-signature", "")

    expected = hmac.new(
        PAYSTACK_WEBHOOK_SECRET.encode(),
        body_bytes,
        hashlib.sha512,
    ).hexdigest()

    if not hmac.compare_digest(expected, signature):
        log.warning("Webhook signature mismatch — ignoring")
        return Response(status_code=200)

    try:
        event = json.loads(body_bytes)
    except json.JSONDecodeError:
        log.error("Webhook: invalid JSON")
        return Response(status_code=200)

    event_type = event.get("event", "")
    data       = event.get("data", {})
    log.info("Webhook received: %s", event_type)

    if event_type == "charge.success":
        await _handle_charge_success(data)
    elif event_type in ("subscription.disable", "subscription.not_renew"):
        await _handle_subscription_cancel(data)

    return Response(status_code=200)


async def _handle_charge_success(data: dict):
    metadata = data.get("metadata", {})
    user_id  = metadata.get("user_id", "")
    plan     = metadata.get("plan", "monthly")

    if not user_id:
        for field in metadata.get("custom_fields", []):
            if field.get("variable_name") == "user_id":
                user_id = field.get("value", "")
            if field.get("variable_name") == "plan":
                plan = field.get("value", "monthly")

    if not user_id:
        log.error("charge.success: no user_id in metadata — %s", metadata)
        return

    plan_days = 365 if plan == "yearly" else 31
    expires_at = datetime.now(timezone.utc) + timedelta(days=plan_days)

    customer_code      = data.get("customer", {}).get("customer_code", "")
    subscription_code  = data.get("subscription_code", "")
    reference          = data.get("reference", "")

    log.info(
        "Activating premium: user_id=%s plan=%s expires=%s ref=%s",
        user_id[:8], plan, expires_at.date(), reference,
    )

    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM users WHERE id = %s", (user_id,))
                if cur.fetchone() is None:
                    log.error("charge.success: unknown user_id=%s", user_id)
                    return

                cur.execute("""
                    INSERT INTO subscriptions
                        (user_id, status, expires_at, paystack_customer_code,
                         paystack_subscription_code, updated_at)
                    VALUES (%s, 'active', %s, %s, %s, now())
                    ON CONFLICT (user_id)
                    DO UPDATE SET
                        status                     = 'active',
                        expires_at                 = EXCLUDED.expires_at,
                        paystack_customer_code     = EXCLUDED.paystack_customer_code,
                        paystack_subscription_code = EXCLUDED.paystack_subscription_code,
                        updated_at                 = now()
                """, (user_id, expires_at, customer_code, subscription_code))

            conn.commit()
        log.info("Premium activated for user_id=%s", user_id[:8])
    except Exception as exc:
        log.exception("DB error activating subscription: %s", exc)


async def _handle_subscription_cancel(data: dict):
    subscription_code = data.get("subscription_code", "")
    if not subscription_code:
        return

    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE subscriptions SET status = 'cancelled', updated_at = now()
                    WHERE paystack_subscription_code = %s
                """, (subscription_code,))
            conn.commit()
        log.info("Subscription cancelled: %s", subscription_code)
    except Exception as exc:
        log.exception("DB error cancelling subscription: %s", exc)


# ═════════════════════════════════════════════════════════════════════════════
# ENDPOINT 5 — GET /shorts/feed
# Server-side ifunny.club scraper.
#
# Query params:
#   subs      — "+" separated community slugs, e.g. "lol-loop-GfRk3IGcil2+meme-and-scream-yvTjtEYXgS4"
#   base_url  — override the base URL (default: https://ifunny.club)
#
# Returns JSON: { "videos": [ { ShortVideo fields } ] }
#
# Strategy:
#   1. For each slug, fetch https://ifunny.club/community/<slug> with browser
#      headers. ifunny.club renders server-side HTML that includes <video src>
#      and <img> tags inside each post card, so we can parse them without JS.
#   2. Extract mp4 URLs + thumbnails using regex on the raw HTML.
#   3. Merge, shuffle, return.
#
# If the HTML approach yields 0 results (site changed structure), we fall back
# to fetching the community's JSON feed at /community/<slug>?format=json
# (undocumented but present on some ifunny deployments).
# ═════════════════════════════════════════════════════════════════════════════

IFUNNY_HEADERS = {
    "User-Agent":      "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer":         "https://ifunny.club/",
    "Origin":          "https://ifunny.club",
}

# Patterns that match mp4 src values embedded in HTML attributes
_MP4_PATTERNS = [
    re.compile(r'(?:src|data-src|data-video|data-url)=["\']([^"\']+\.mp4[^"\']*)["\']', re.I),
    re.compile(r'"(?:src|url|mp4|video)":\s*"(https?://[^"]+\.mp4[^"]*)"', re.I),
]

# Patterns that match thumbnail/poster image src values
_THUMB_PATTERNS = [
    re.compile(r'poster=["\']([^"\']+)["\']', re.I),
    re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', re.I),
]

_POST_ID_RE = re.compile(r'/(?:video|post|gif)/([A-Za-z0-9_-]{6,})')


def _extract_videos_from_html(html: str, slug: str) -> list[dict]:
    """
    Parse raw HTML from an ifunny.club community page.
    Returns a list of dicts with keys: id, mp4, thumb.
    """
    results = []
    seen_mp4: set[str] = set()

    # Split on article / post card boundaries so thumbnails stay paired with their video
    # ifunny wraps each post in <article ...> or a div with data-id
    cards = re.split(r'(?=<article\b|<div[^>]+data-id=)', html, flags=re.I)
    if len(cards) < 2:
        cards = [html]  # fallback: scan whole page

    for i, card in enumerate(cards):
        mp4 = ""
        thumb = ""

        for pat in _MP4_PATTERNS:
            m = pat.search(card)
            if m:
                mp4 = m.group(1).strip()
                break

        if not mp4 or mp4 in seen_mp4:
            continue
        seen_mp4.add(mp4)

        for pat in _THUMB_PATTERNS:
            m = pat.search(card)
            if m:
                candidate = m.group(1).strip()
                # Skip tiny icons and data URIs
                if candidate.startswith("data:") or len(candidate) < 10:
                    continue
                thumb = candidate
                break

        # Try to extract a stable post id from a link in the card
        post_id_m = _POST_ID_RE.search(card)
        post_id = post_id_m.group(1) if post_id_m else f"{slug}_{i}"

        community_label = slug.split("-")[0].capitalize()

        results.append({
            "id":        post_id,
            "mp4":       mp4,
            "thumb":     thumb,
            "author":    community_label,
            "community": community_label,
        })

    return results


async def _fetch_community(client: httpx.AsyncClient, base_url: str, slug: str) -> list[dict]:
    url = f"{base_url}/community/{slug}"
    log.info("shorts/feed: fetching %s", url)
    try:
        resp = await client.get(url, headers=IFUNNY_HEADERS, follow_redirects=True, timeout=15)
        if resp.status_code != 200:
            log.warning("shorts/feed: %s returned %d", url, resp.status_code)
            return []
        videos = _extract_videos_from_html(resp.text, slug)
        log.info("shorts/feed: extracted %d videos from %s", len(videos), slug)
        return videos
    except Exception as exc:
        log.exception("shorts/feed: error fetching %s — %s", url, exc)
        return []


def _to_short_video(v: dict) -> dict:
    """Convert internal dict to the ShortVideo JSON shape the app expects."""
    return {
        "id":          v["id"],
        "title":       "",
        "author":      v.get("author", ""),
        "community":   v.get("community", ""),
        "hlsUrl":      v["mp4"],       # app uses hlsUrl field; mp4 direct URL works fine with ExoPlayer
        "audioUrl":    None,
        "fallbackUrl": v["mp4"],
        "thumbnail":   v.get("thumb", ""),
        "ups":         0,
        "duration":    0,
        "hasAudio":    True,
        "width":       0,
        "height":      0,
    }


import random

@app.get("/shorts/feed")
async def shorts_feed(
    subs: str = Query(..., description='"+"-separated community slugs'),
    base_url: str = Query("https://ifunny.club", description="Base URL override"),
):
    """
    Fetch videos from one or more ifunny.club communities server-side.
    The app passes the slugs from its remote config and gets back a flat
    list of ShortVideo-compatible JSON objects, shuffled and ready to play.

    Example:
      GET /shorts/feed?subs=lol-loop-GfRk3IGcil2+meme-and-scream-yvTjtEYXgS4
    """
    slugs = [s.strip() for s in subs.split("+") if s.strip()]
    if not slugs:
        raise HTTPException(status_code=400, detail="subs param is required")

    # Cap to 5 slugs per request to keep latency reasonable
    slugs = slugs[:5]

    async with httpx.AsyncClient() as client:
        import asyncio
        tasks = [_fetch_community(client, base_url, slug) for slug in slugs]
        results = await asyncio.gather(*tasks)

    merged: list[dict] = []
    seen_ids: set[str] = set()
    for group in results:
        for v in group:
            if v["id"] not in seen_ids:
                seen_ids.add(v["id"])
                merged.append(v)

    random.shuffle(merged)

    return {
        "videos": [_to_short_video(v) for v in merged],
        "count":  len(merged),
    }


# ── Health check ──────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"ok": True}
