"""
Reelz Backend — FastAPI + Supabase PostgreSQL
"""

import hashlib
import hmac
import json
import logging
import os
import re
import random
import asyncio
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import httpx
from fastapi import FastAPI, HTTPException, Request, Response, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import psycopg2
from psycopg2.extras import RealDictCursor
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("reelz")

GOOGLE_CLIENT_ID        = os.environ["GOOGLE_CLIENT_ID"]
PAYSTACK_SECRET_KEY     = os.environ["PAYSTACK_SECRET_KEY"]
DATABASE_URL            = os.environ["DATABASE_URL"]
PAYSTACK_WEBHOOK_SECRET = os.environ.get("PAYSTACK_WEBHOOK_SECRET", PAYSTACK_SECRET_KEY)

app = FastAPI(title="Reelz API", version="1.2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["POST", "GET"],
    allow_headers=["*"],
)

# ── DB ────────────────────────────────────────────────────────────────────────

def get_conn():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)

def init_db():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    google_sub TEXT UNIQUE NOT NULL,
                    email      TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                );
                CREATE TABLE IF NOT EXISTS subscriptions (
                    id                         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    user_id                    UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    status                     TEXT NOT NULL DEFAULT 'expired',
                    expires_at                 TIMESTAMPTZ,
                    paystack_customer_code     TEXT,
                    paystack_subscription_code TEXT,
                    updated_at                 TIMESTAMPTZ NOT NULL DEFAULT now()
                );
                CREATE INDEX IF NOT EXISTS idx_subscriptions_user_id ON subscriptions(user_id);
                CREATE INDEX IF NOT EXISTS idx_users_google_sub      ON users(google_sub);
            """)
        conn.commit()
    log.info("DB tables ensured")

@app.on_event("startup")
async def startup():
    init_db()

# ── Models ────────────────────────────────────────────────────────────────────

class GoogleAuthRequest(BaseModel):
    id_token: str

class PaymentInitRequest(BaseModel):
    user_id: str
    plan: str
    email: str

# ── Google verify ─────────────────────────────────────────────────────────────

async def verify_google_token(id_token: str) -> dict:
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get("https://oauth2.googleapis.com/tokeninfo", params={"id_token": id_token})
    if resp.status_code != 200:
        raise HTTPException(status_code=401, detail="Invalid Google token")
    claims = resp.json()
    aud = claims.get("aud", "")
    if GOOGLE_CLIENT_ID not in (aud if isinstance(aud, list) else [aud]):
        raise HTTPException(status_code=401, detail="Token audience mismatch")
    if datetime.now(timezone.utc).timestamp() > int(claims.get("exp", 0)):
        raise HTTPException(status_code=401, detail="Token expired")
    return claims

# ── Subscription helpers ──────────────────────────────────────────────────────

def _get_subscription(cur, user_id: str):
    cur.execute("SELECT * FROM subscriptions WHERE user_id = %s ORDER BY updated_at DESC LIMIT 1", (user_id,))
    return cur.fetchone()

def _subscription_response(sub) -> dict:
    if sub is None:
        return {"premium": False, "status": "none", "expires_at": None}
    now = datetime.now(timezone.utc)
    expires_at = sub.get("expires_at")
    status = sub.get("status", "expired")
    in_grace = expires_at and expires_at < now and (now - expires_at) < timedelta(hours=24)
    is_premium = status == "active" and expires_at and (expires_at > now or in_grace)
    return {"premium": bool(is_premium), "status": status, "expires_at": expires_at.isoformat() if expires_at else None}

# ── ENDPOINT 1: POST /auth/google ─────────────────────────────────────────────

@app.post("/auth/google")
async def auth_google(body: GoogleAuthRequest):
    claims = await verify_google_token(body.id_token)
    google_sub = claims["sub"]
    email = claims.get("email", "").lower().strip()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO users (google_sub, email) VALUES (%s, %s)
                ON CONFLICT (google_sub) DO UPDATE SET email = EXCLUDED.email
                RETURNING id
            """, (google_sub, email))
            user_id = str(cur.fetchone()["id"])
            sub = _get_subscription(cur, user_id)
        conn.commit()
    return {"user_id": user_id, **_subscription_response(sub)}

# ── ENDPOINT 2: POST /payments/init ──────────────────────────────────────────

PLAN_AMOUNTS = {"monthly": 150_000, "yearly": 1_200_000}

@app.post("/payments/init")
async def payments_init(body: PaymentInitRequest):
    if body.plan not in PLAN_AMOUNTS:
        raise HTTPException(status_code=400, detail=f"Unknown plan: {body.plan}")
    payload = {
        "email": body.email, "amount": PLAN_AMOUNTS[body.plan], "currency": "NGN",
        "metadata": {"user_id": body.user_id, "plan": body.plan,
            "custom_fields": [
                {"display_name": "Plan",    "variable_name": "plan",    "value": body.plan},
                {"display_name": "User ID", "variable_name": "user_id", "value": body.user_id},
            ]},
    }
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post("https://api.paystack.co/transaction/initialize",
            headers={"Authorization": f"Bearer {PAYSTACK_SECRET_KEY}"}, json=payload)
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="Payment provider error")
    data = resp.json()
    if not data.get("status"):
        raise HTTPException(status_code=502, detail=data.get("message", "Paystack error"))
    return {"authorization_url": data["data"]["authorization_url"], "reference": data["data"]["reference"]}

# ── ENDPOINT 3: GET /subscription/status ─────────────────────────────────────

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

# ── ENDPOINT 4: POST /webhook/paystack ───────────────────────────────────────

@app.post("/webhook/paystack")
async def webhook_paystack(request: Request):
    body_bytes = await request.body()
    signature  = request.headers.get("x-paystack-signature", "")
    expected   = hmac.new(PAYSTACK_WEBHOOK_SECRET.encode(), body_bytes, hashlib.sha512).hexdigest()
    if not hmac.compare_digest(expected, signature):
        return Response(status_code=200)
    try:
        event = json.loads(body_bytes)
    except json.JSONDecodeError:
        return Response(status_code=200)
    event_type = event.get("event", "")
    data       = event.get("data", {})
    if event_type == "charge.success":
        await _handle_charge_success(data)
    elif event_type in ("subscription.disable", "subscription.not_renew"):
        await _handle_subscription_cancel(data)
    return Response(status_code=200)

async def _handle_charge_success(data: dict):
    metadata = data.get("metadata", {})
    user_id  = metadata.get("user_id", "")
    plan     = metadata.get("plan", "monthly")
    for field in metadata.get("custom_fields", []):
        if field.get("variable_name") == "user_id": user_id = field.get("value", "")
        if field.get("variable_name") == "plan":    plan    = field.get("value", "monthly")
    if not user_id:
        return
    expires_at        = datetime.now(timezone.utc) + timedelta(days=365 if plan == "yearly" else 31)
    customer_code     = data.get("customer", {}).get("customer_code", "")
    subscription_code = data.get("subscription_code", "")
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM users WHERE id = %s", (user_id,))
                if cur.fetchone() is None: return
                cur.execute("""
                    INSERT INTO subscriptions (user_id, status, expires_at, paystack_customer_code, paystack_subscription_code, updated_at)
                    VALUES (%s, 'active', %s, %s, %s, now())
                    ON CONFLICT (user_id) DO UPDATE SET
                        status = 'active', expires_at = EXCLUDED.expires_at,
                        paystack_customer_code = EXCLUDED.paystack_customer_code,
                        paystack_subscription_code = EXCLUDED.paystack_subscription_code,
                        updated_at = now()
                """, (user_id, expires_at, customer_code, subscription_code))
            conn.commit()
    except Exception as exc:
        log.exception("DB error activating subscription: %s", exc)

async def _handle_subscription_cancel(data: dict):
    subscription_code = data.get("subscription_code", "")
    if not subscription_code: return
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE subscriptions SET status='cancelled', updated_at=now() WHERE paystack_subscription_code=%s", (subscription_code,))
            conn.commit()
    except Exception as exc:
        log.exception("DB error cancelling subscription: %s", exc)

# ── ENDPOINT 5: GET /shorts/feed — Playwright scraper ────────────────────────
#
# ifunny.club is JS-rendered. We use a headless Chromium (Playwright) to:
#   1. Navigate to the community page
#   2. Wait for <video> elements to appear in the DOM
#   3. Extract src + poster attributes
#   4. Return as ShortVideo-compatible JSON
#
# Playwright is run with a shared browser instance (reused across requests).
# ─────────────────────────────────────────────────────────────────────────────

_playwright_instance = None
_browser = None

async def get_browser():
    global _playwright_instance, _browser
    if _browser is None or not _browser.is_connected():
        _playwright_instance = await async_playwright().start()
        _browser = await _playwright_instance.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--no-first-run",
                "--no-zygote",
                "--single-process",
            ],
        )
        log.info("Playwright browser launched")
    return _browser

@app.on_event("shutdown")
async def shutdown():
    global _browser, _playwright_instance
    if _browser:
        await _browser.close()
    if _playwright_instance:
        await _playwright_instance.stop()


async def scrape_community(base_url: str, slug: str) -> list[dict]:
    """
    Open the ifunny.club community page in a headless browser,
    wait for video elements to render, then extract mp4 + poster.
    """
    url = f"{base_url}/community/{slug}"
    log.info("Scraping: %s", url)
    try:
        browser = await get_browser()
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
            viewport={"width": 390, "height": 844},
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": f"{base_url}/",
            },
        )
        page = await context.new_page()

        # Block images/fonts/css to speed up load — we only need the DOM/JS
        await page.route("**/*.{png,jpg,jpeg,gif,svg,woff,woff2,ttf,css}", lambda r: r.abort())

        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)

        # Wait for at least one <video> to appear (up to 15s)
        try:
            await page.wait_for_selector("video", timeout=15_000)
        except PlaywrightTimeout:
            log.warning("No <video> elements found on %s", url)
            await context.close()
            return []

        # Scroll down a bit to trigger lazy-load
        for _ in range(3):
            await page.evaluate("window.scrollBy(0, window.innerHeight)")
            await asyncio.sleep(1)

        # Extract all video elements
        videos = await page.evaluate("""
            () => {
                const results = [];
                document.querySelectorAll('video').forEach((v, i) => {
                    const src = v.src || v.querySelector('source')?.src || '';
                    const poster = v.poster || '';
                    // Walk up to find a post container with a link for the id
                    let id = '';
                    let el = v;
                    for (let d = 0; d < 8; d++) {
                        el = el.parentElement;
                        if (!el) break;
                        const a = el.querySelector('a[href*="/video/"], a[href*="/gif/"], a[href*="/post/"]');
                        if (a) {
                            const m = a.href.match(/\\/(video|gif|post)\\/([A-Za-z0-9_-]+)/);
                            if (m) { id = m[2]; break; }
                        }
                    }
                    if (src && src.includes('.mp4')) {
                        results.push({ src, poster, id: id || ('v_' + i) });
                    }
                });
                return results;
            }
        """)

        await context.close()

        community_label = slug.split("-")[0].capitalize()
        result = []
        seen = set()
        for v in videos:
            if v["src"] in seen:
                continue
            seen.add(v["src"])
            result.append({
                "id":        v["id"],
                "mp4":       v["src"],
                "thumb":     v["poster"],
                "author":    community_label,
                "community": community_label,
            })

        log.info("✓ %s → %d videos", slug, len(result))
        return result

    except Exception as e:
        log.exception("✗ scrape_community(%s) failed: %s", slug, e)
        return []


def _to_short_video(v: dict) -> dict:
    return {
        "id":          v["id"],
        "title":       "",
        "author":      v.get("author", ""),
        "community":   v.get("community", ""),
        "hlsUrl":      v["mp4"],
        "audioUrl":    None,
        "fallbackUrl": v["mp4"],
        "thumbnail":   v.get("thumb", ""),
        "ups":         0,
        "duration":    0,
        "hasAudio":    True,
        "width":       0,
        "height":      0,
    }


@app.get("/shorts/feed")
async def shorts_feed(
    subs: str = Query(..., description='"+"-separated community slugs'),
    base_url: str = Query("https://ifunny.club"),
):
    slugs = [s.strip() for s in subs.replace("%2B", "+").split("+") if s.strip()]
    if not slugs:
        raise HTTPException(status_code=400, detail="subs param is required")

    # Max 5 slugs, scraped concurrently
    slugs = slugs[:5]
    tasks = [scrape_community(base_url, slug) for slug in slugs]
    results = await asyncio.gather(*tasks)

    merged, seen_ids = [], set()
    for group in results:
        for v in group:
            if v["id"] not in seen_ids:
                seen_ids.add(v["id"])
                merged.append(v)

    random.shuffle(merged)
    log.info("shorts/feed → %d total videos", len(merged))

    return {"videos": [_to_short_video(v) for v in merged], "count": len(merged)}


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"ok": True}
