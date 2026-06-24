"""
Reelz Backend — FastAPI + Supabase PostgreSQL
Optimized: direct ifunny.club JSON API with cursor pagination, no scraping/DOM parsing
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
from urllib.parse import urljoin, urlparse, urlencode

import httpx
from fastapi import FastAPI, HTTPException, Request, Response, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import psycopg2
from psycopg2.extras import RealDictCursor

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("reelz")

GOOGLE_CLIENT_ID        = os.environ["GOOGLE_CLIENT_ID"]
PAYSTACK_SECRET_KEY     = os.environ["PAYSTACK_SECRET_KEY"]
DATABASE_URL            = os.environ["DATABASE_URL"]
PAYSTACK_WEBHOOK_SECRET = os.environ.get("PAYSTACK_WEBHOOK_SECRET", PAYSTACK_SECRET_KEY)

# Cache TTL — stale-while-revalidate keeps every request instant after first load
FEED_CACHE_TTL_SECONDS = 8 * 60  # 8 minutes

# How many videos to fetch per API page from ifunny.club
PAGE_SIZE = 30

app = FastAPI(title="Reelz API", version="2.0.0")
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

                CREATE TABLE IF NOT EXISTS feed_cache (
                    slug       TEXT PRIMARY KEY,
                    videos     JSONB NOT NULL,
                    cursor     TEXT,
                    cached_at  TIMESTAMPTZ NOT NULL DEFAULT now()
                );
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

# ── ENDPOINT 5: GET /shorts/feed — Direct JSON API + Postgres cache ──────────
#
# ifunny.club exposes a clean JSON API at:
#   GET /api/v1/feeds/community/{slug}?limit=30&next={cursor}
#
# Each response contains:
#   { "items": [...posts], "paging": { "cursors": { "next": "..." } } }
#
# Each post has: postId, media.video[0].url, cover.url, stat, user, group, content
# This is orders of magnitude faster than HTML scraping — pure JSON, no parsing.
#
# Stale-while-revalidate: first request pays the API cost once, every
# subsequent request within TTL is instant from Postgres cache.

_IFUNNY_HEADERS = {
    "User-Agent": "iFunny/8.16.4 (Android; en_US; Pixel 7; Build/TQ3A.230805.001)",
    "Accept": "application/json",
    "Accept-Language": "en-US",
    "X-App-Version": "8.16.4",
    "X-Platform": "android",
}

async def fetch_community_api(base_url: str, slug: str, cursor: Optional[str] = None) -> tuple[list[dict], Optional[str]]:
    """
    Fetch one page from ifunny.club's JSON API.
    Returns (videos, next_cursor).
    """
    params: dict = {"limit": PAGE_SIZE}
    if cursor:
        params["next"] = cursor

    url = f"{base_url.rstrip('/')}/api/v1/feeds/community/{slug}?{urlencode(params)}"
    log.info(f"API fetch: {url}")

    try:
        async with httpx.AsyncClient(timeout=12, follow_redirects=True, headers=_IFUNNY_HEADERS) as client:
            resp = await client.get(url)

        if resp.status_code != 200:
            log.warning(f"API returned {resp.status_code} for {slug}")
            return [], None

        data = resp.json()

        # Handle both flat and nested response shapes
        items = data.get("items") or data.get("data", {}).get("items") or []
        paging = data.get("paging") or data.get("data", {}).get("paging") or {}
        next_cursor = (paging.get("cursors") or {}).get("next") or paging.get("next")

        videos = []
        seen: set[str] = set()

        for item in items:
            # Items can be wrapped in a "post" key or be the post directly
            post = item.get("post") or item if isinstance(item, dict) else {}
            v = _post_to_video(post)
            if v and v["mp4"] not in seen:
                seen.add(v["mp4"])
                videos.append(v)

        log.info(f"✓ {slug} → {len(videos)} videos, next_cursor={'yes' if next_cursor else 'no'}")
        return videos, next_cursor

    except Exception as e:
        log.exception(f"✗ API error for {slug}: {e}")
        return [], None


def _post_to_video(post: dict) -> Optional[dict]:
    """Normalize one ifunny post into our internal video shape."""
    if not post:
        return None

    # Resolve video URL — check multiple known locations
    media    = post.get("media") or {}
    video_list = media.get("video") or []
    if not video_list and isinstance(media, dict):
        # Some responses put it at media.url directly for video posts
        direct = media.get("url", "")
        if direct and ".mp4" in direct:
            video_list = [{"url": direct}]

    if not video_list:
        return None

    video_url = (video_list[0] or {}).get("url", "").strip()
    # Strip any whitespace/invisible chars embedded in the URL
    video_url = re.sub(r'\s+', '', video_url)
    if not video_url:
        return None

    # Thumbnail — cover takes priority, then media.thumbnail
    cover     = post.get("cover") or media.get("cover") or media.get("thumbnail") or {}
    thumb_url = re.sub(r'\s+', '', (cover.get("url") or "").strip())

    stat  = post.get("stat")  or {}
    user  = post.get("user")  or {}
    group = post.get("group") or post.get("community") or {}
    vid0  = video_list[0] or {}

    return {
        "id":        post.get("postId") or post.get("id") or "",
        "mp4":       video_url,
        "thumb":     thumb_url,
        "title":     (post.get("content") or post.get("title") or "").strip(),
        "author":    (user.get("nickname") or user.get("name") or "").strip(),
        "community": (group.get("name") or group.get("title") or "").strip(),
        "ups":       int(stat.get("likeCount") or stat.get("likes") or 0),
        "comments":  int(stat.get("commentCount") or stat.get("comments") or 0),
        "duration":  int(vid0.get("duration") or 0),
        "width":     int(vid0.get("width")    or 0),
        "height":    int(vid0.get("height")   or 0),
    }


def _to_short_video(v: dict) -> dict:
    return {
        "id":          v.get("id", ""),
        "title":       v.get("title", ""),
        "author":      v.get("author", ""),
        "community":   v.get("community", ""),
        "hlsUrl":      "",
        "audioUrl":    None,
        "fallbackUrl": v.get("mp4", ""),
        "thumbnail":   v.get("thumb", ""),
        "ups":         v.get("ups", 0),
        "comments":    v.get("comments", 0),
        "duration":    v.get("duration", 0),
        "hasAudio":    True,
        "width":       v.get("width", 0),
        "height":      v.get("height", 0),
    }


# ── Feed cache (Postgres, stale-while-revalidate) ─────────────────────────────

_refresh_in_progress: set[str] = set()

def _read_cache(cur, slug: str) -> Optional[dict]:
    cur.execute("SELECT videos, cursor, cached_at FROM feed_cache WHERE slug = %s", (slug,))
    return cur.fetchone()

def _write_cache(slug: str, videos: list[dict], cursor: Optional[str] = None):
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO feed_cache (slug, videos, cursor, cached_at)
                    VALUES (%s, %s, %s, now())
                    ON CONFLICT (slug) DO UPDATE SET
                        videos    = EXCLUDED.videos,
                        cursor    = EXCLUDED.cursor,
                        cached_at = EXCLUDED.cached_at
                """, (slug, json.dumps(videos), cursor))
            conn.commit()
    except Exception:
        log.exception(f"Failed writing feed cache for slug={slug}")

async def _fetch_and_cache(base_url: str, slug: str, cursor: Optional[str] = None) -> list[dict]:
    videos, next_cursor = await fetch_community_api(base_url, slug, cursor)
    if videos:
        _write_cache(slug, videos, next_cursor)
    return videos

def _schedule_background_refresh(base_url: str, slug: str, cursor: Optional[str]):
    if slug in _refresh_in_progress:
        return
    _refresh_in_progress.add(slug)

    async def _run():
        try:
            await _fetch_and_cache(base_url, slug, cursor)
            log.info(f"↻ background refresh complete for slug={slug}")
        finally:
            _refresh_in_progress.discard(slug)

    asyncio.create_task(_run())

async def get_videos_for_slug(base_url: str, slug: str, page_cursor: Optional[str] = None) -> tuple[list[dict], Optional[str]]:
    """
    Returns (videos, next_cursor).
    - If page_cursor is given (pagination request): always fetches live from API.
    - Otherwise: stale-while-revalidate from cache.
    """
    if page_cursor:
        # Explicit pagination — fetch fresh page, don't touch cache
        return await fetch_community_api(base_url, slug, page_cursor)

    with get_conn() as conn:
        with conn.cursor() as cur:
            row = _read_cache(cur, slug)

    if row is None:
        log.info(f"cache MISS slug={slug} — fetching from API")
        videos, next_cursor = await fetch_community_api(base_url, slug)
        if videos:
            _write_cache(slug, videos, next_cursor)
        return videos, next_cursor

    age = (datetime.now(timezone.utc) - row["cached_at"]).total_seconds()
    videos     = row["videos"]
    cached_cur = row.get("cursor")

    if age > FEED_CACHE_TTL_SECONDS:
        log.info(f"cache STALE slug={slug} age={int(age)}s — serving stale, refreshing in bg")
        _schedule_background_refresh(base_url, slug, None)
    else:
        log.info(f"cache HIT slug={slug} age={int(age)}s")

    return videos, cached_cur


# ── GET /shorts/feed ──────────────────────────────────────────────────────────

@app.get("/shorts/feed")
async def shorts_feed(
    subs: str = Query(..., description='"+"-separated community slugs'),
    base_url: str = Query("https://ifunny.club"),
    cursor: Optional[str] = Query(None, description="Pagination cursor for next page"),
):
    slugs = [s.strip() for s in subs.replace("%2B", "+").split("+") if s.strip()]
    if not slugs:
        raise HTTPException(status_code=400, detail="subs param is required")

    slugs = slugs[:5]

    # Fetch all slugs in parallel
    tasks = [get_videos_for_slug(base_url, slug, cursor) for slug in slugs]
    results = await asyncio.gather(*tasks)

    merged: list[dict] = []
    seen_urls: set[str] = set()
    next_cursors: list[str] = []

    for videos, next_cur in results:
        for v in videos:
            if v["mp4"] not in seen_urls:
                seen_urls.add(v["mp4"])
                merged.append(v)
        if next_cur:
            next_cursors.append(next_cur)

    # Shuffle only on first load (no cursor) for feed variety
    if not cursor:
        random.shuffle(merged)

    log.info(f"shorts/feed → {len(merged)} videos cursor={'yes' if cursor else 'no'}")

    return {
        "videos":      [_to_short_video(v) for v in merged],
        "count":       len(merged),
        "next_cursor": next_cursors[0] if next_cursors else None,
    }


# ── POST /shorts/cache/clear ──────────────────────────────────────────────────

@app.post("/shorts/cache/clear")
async def clear_feed_cache(slug: Optional[str] = Query(None)):
    with get_conn() as conn:
        with conn.cursor() as cur:
            if slug:
                cur.execute("DELETE FROM feed_cache WHERE slug = %s", (slug,))
            else:
                cur.execute("DELETE FROM feed_cache")
            cleared = cur.rowcount
        conn.commit()
    return {"cleared_rows": cleared}


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"ok": True}

@app.get("/")
async def root():
    return {
        "status": "ok",
        "message": "Reelz API v2 — direct JSON feed",
        "endpoints": {
            "/auth/google":        "POST - Google authentication",
            "/payments/init":      "POST - Initialize payment",
            "/subscription/status":"GET  - Check subscription status",
            "/webhook/paystack":   "POST - Paystack webhook",
            "/shorts/feed":        "GET  - Video feed (cursor pagination)",
            "/shorts/cache/clear": "POST - Invalidate feed cache",
            "/health":             "GET  - Health check",
        }
    }
