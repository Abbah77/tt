"""
Reelz Backend — FastAPI + Supabase PostgreSQL
"""

import hashlib
import hmac
import json
import logging
import os
import asyncio
from datetime import datetime, timedelta, timezone
from typing import List, Optional

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

# How long a cached feed page is considered "fresh" before we trigger a
# background refresh. Stale entries are still served instantly — the user
# never pays the network latency after the very first load for a page.
FEED_CACHE_TTL_SECONDS = 10 * 60  # 10 minutes

app = FastAPI(title="Reelz API", version="1.4.0")
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

                -- Cache key is now "slug:page" so paginated requests don't
                -- collide on a single cached blob per slug — each page of
                -- the feed (explore or community) is cached independently,
                -- which is what makes infinite scroll actually infinite
                -- instead of capped at whatever the first scrape returned.
                CREATE TABLE IF NOT EXISTS feed_cache (
                    cache_key  TEXT PRIMARY KEY,
                    videos     JSONB NOT NULL,
                    has_more   BOOLEAN NOT NULL DEFAULT true,
                    cached_at  TIMESTAMPTZ NOT NULL DEFAULT now()
                );
            """)
            # Migration path: earlier deployments created feed_cache with a
            # "slug" primary key and no "has_more" column. Add what's
            # missing rather than dropping/recreating, so existing cached
            # rows survive the upgrade instead of forcing a cold cache.
            cur.execute("""
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name = 'feed_cache' AND column_name = 'cache_key'
                    ) AND EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name = 'feed_cache' AND column_name = 'slug'
                    ) THEN
                        ALTER TABLE feed_cache RENAME COLUMN slug TO cache_key;
                    END IF;

                    IF NOT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name = 'feed_cache' AND column_name = 'has_more'
                    ) THEN
                        ALTER TABLE feed_cache ADD COLUMN has_more BOOLEAN NOT NULL DEFAULT true;
                    END IF;
                END $$;
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

# ── ENDPOINT 5: GET /shorts/feed ─────────────────────────────────────────────
#
# ═════════════════════════════════════════════════════════════════════════════
# PRIMARY SOURCE: seocloud.biz JSON BFF API  (discovered via DevTools inspection
# of the real ifunny.club mobile traffic — see screenshot/header dump).
#
# This replaces the old "scrape rendered HTML / parse Next.js RSC streaming
# payload" approach as the primary path. It's the same data the official site
# itself calls, as plain JSON, with simple static headers and NO signature /
# auth token — confirmed against a real captured 200 OK request:
#
#   GET https://api.seocloud.biz/wf/feed-seo-bff/post/explore?page=N&perPage=20
#   GET https://api.seocloud.biz/wf/feed-seo-bff/community/{seoKey}?page=N&perPage=20
#
#   Required headers (all static, no per-request computation):
#     Accept: application/json
#     Origin: https://ifunny.club
#     Referer: https://ifunny.club/
#     X-Client-Info: {"package_name":"movieboxbuzz","timezone":"Africa/Lagos"}
#     X-Request-Lang: en
#     User-Agent: <any modern mobile UA>
#
# Response shape (data.items[]) gives us, per post:
#   - media.video[0].url   → direct .mp4 (this is our hlsUrl/fallbackUrl)
#   - media.cover.url      → poster/thumbnail
#   - stat.likeCount / commentCount / shareCount → real engagement numbers
#   - user.nickname, group.name → author/community display fields
#   - data.pager.hasMore / nextPage → REAL pagination cursor, unlike the old
#     scraper which only ever returned a fixed slice of ~30 items per slug.
#     This is what makes "infinite scroll" actually infinite instead of
#     capped — loadMore() now asks the API for the next page directly
#     instead of re-scraping and hoping for different (shuffled) results.
#
# PURE API BUILD — no scraper fallback. If this endpoint errors, times out,
# changes shape, or 4xx/5xxs, the request fails loudly (502) instead of
# silently degrading. This is intentional: the point of this build is to
# find out, definitively, whether the API alone is reliable enough to
# depend on. Watch your server logs/error rates — if 502s show up, that's
# your answer, and you'd reintroduce the RSC-JSON/HTML scraper as a
# fallback for whichever slug/page is failing.
# ═════════════════════════════════════════════════════════════════════════════

SEOCLOUD_BASE = "https://api.seocloud.biz/wf/feed-seo-bff"

SEOCLOUD_HEADERS = {
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://ifunny.club",
    "Referer": "https://ifunny.club/",
    "User-Agent": "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Mobile Safari/537.36",
    "X-Client-Info": json.dumps({"package_name": "movieboxbuzz", "timezone": "Africa/Lagos"}),
    "X-Request-Lang": "en",
}

PER_PAGE = 20

def _seocloud_post_to_video(post: dict) -> Optional[dict]:
    """Normalize one seocloud.biz post object into our internal video dict."""
    media = post.get("media") or {}
    videos = media.get("video") or []
    if not videos:
        return None
    video_url = (videos[0] or {}).get("url", "")
    if not video_url:
        return None

    cover = post.get("cover") or media.get("cover") or {}
    thumb_url = cover.get("url", "") if cover else ""

    stat = post.get("stat") or {}
    user = post.get("user") or {}
    group = post.get("group") or {}

    return {
        "id": post.get("postId", ""),
        "mp4": video_url,
        "thumb": thumb_url,
        "title": (post.get("content", "") or "").strip(),
        "author": user.get("nickname", "") or "",
        "community": group.get("name", "") or "",
        "ups": int(stat.get("likeCount", 0) or 0),
        "comments": int(stat.get("commentCount", 0) or 0),
        "duration": int((videos[0] or {}).get("duration", 0) or 0),
        "width": int((videos[0] or {}).get("width", 0) or 0),
        "height": int((videos[0] or {}).get("height", 0) or 0),
        # Lets /shorts/feed report exactly which path served this video —
        # see the "source" field on the response and the X-Feed-Source
        # header. This is how you verify the API is actually being used
        # instead of guessing from playback alone.
        "source": "api",
    }

async def fetch_seocloud_page(path: str, page: int) -> tuple[list[dict], bool]:
    """
    Calls one page of either /post/explore or /community/{seoKey} on the
    seocloud BFF. Returns (videos, has_more). Raises on any failure — this
    is the pure-API build, so a failure here surfaces as a real error
    rather than being silently masked.
    """
    url = f"{SEOCLOUD_BASE}{path}"
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url, params={"page": page, "perPage": PER_PAGE}, headers=SEOCLOUD_HEADERS)
    resp.raise_for_status()
    body = resp.json()
    if body.get("code", 0) != 0:
        raise ValueError(f"seocloud API returned code={body.get('code')} message={body.get('message')}")

    data = body.get("data") or {}
    items = data.get("items") or []
    pager = data.get("pager") or {}

    videos: list[dict] = []
    seen_mp4 = set()
    for item in items:
        if item.get("itemType") != "POST":
            continue
        v = _seocloud_post_to_video(item)
        if v and v["mp4"] not in seen_mp4:
            seen_mp4.add(v["mp4"])
            videos.append(v)

    has_more = bool(pager.get("hasMore", False))
    return videos, has_more

async def fetch_explore_page(page: int) -> tuple[list[dict], bool]:
    # Confirmed from live traffic capture: GET .../post/explore?page=3
    # returns 200 with real data, but our own page=0 (the very first
    # request, on cold cache) reproducibly 404s — see the traceback from
    # 2026-06-26 in server logs. /community/{seoKey} has no such issue at
    # page=0, so this looks specific to /post/explore being 1-indexed
    # rather than 0-indexed.
    #
    # We apply a consistent +1 offset to EVERY explore page rather than
    # only retrying page=0 on 404: a retry-only approach would desync
    # subsequent pages (our page=1 would re-fetch their page=1 — the same
    # content the retry already returned — instead of advancing to their
    # page=2), silently duplicating videos across "pages" as the user
    # scrolls. A flat offset keeps our page→their page mapping 1:1 and
    # predictable. If seocloud fixes page=0 on their end later, this
    # offset would need to be removed — watch for that by occasionally
    # testing without it.
    return await fetch_seocloud_page("/post/explore", page + 1)

async def fetch_community_page(seo_key: str, page: int) -> tuple[list[dict], bool]:
    return await fetch_seocloud_page(f"/community/{seo_key}", page)


def _to_short_video(v: dict) -> dict:
    return {
        "id": v.get("id", ""),
        "title": v.get("title", ""),
        "author": v.get("author", ""),
        "community": v.get("community", ""),
        "hlsUrl": "",
        "audioUrl": None,
        "fallbackUrl": v.get("mp4", ""),
        "thumbnail": v.get("thumb", ""),
        "ups": v.get("ups", 0),
        "duration": v.get("duration", 0),
        "hasAudio": True,
        "width": v.get("width", 0),
        # Always "api" in this pure-API build — kept on the response so the
        # client-side code that already reads this field doesn't need to
        # change if the scraper fallback is reintroduced later.
        "source": v.get("source", "api"),
        "height": v.get("height", 0),
    }

# ── Source dispatch: seocloud BFF only, no scraper fallback ─────────────────
#
# `slug` here is the community seoKey (e.g. "lol-loop-GfRk3IGcil2"), or the
# special value "__explore__" for the For You / explore feed which has no
# community context.
#
# This is the "pure API" build — if api.seocloud.biz fails, this raises and
# the request fails loudly (502), rather than silently degrading to the
# HTML/RSC scraper. That's intentional: the whole point of this version is
# to find out definitively whether the API alone is reliable enough to
# depend on, without a scraper masking failures. If you start seeing 502s
# in your logs/app, that's your answer — swap back to the version with the
# scraper fallback.

EXPLORE_SLUG = "__explore__"

async def fetch_page_for_slug(base_url: str, slug: str, page: int) -> tuple[list[dict], bool]:
    """
    Returns (videos, has_more) for one page of one slug, via the seocloud
    JSON BFF only. `base_url` is accepted for call-site compatibility with
    the scraper-fallback version but is unused here.
    """
    if slug == EXPLORE_SLUG:
        return await fetch_explore_page(page)
    return await fetch_community_page(slug, page)

# ── Feed cache (Postgres-backed, stale-while-revalidate, per page) ──────────
#
# Read path per (slug, page):
#   1. Cache row missing entirely      -> fetch synchronously, write cache, return fresh
#   2. Cache row present and fresh     -> return cached immediately
#   3. Cache row present but stale     -> return cached immediately AND
#                                          kick off a background refresh for next time
#
# Caching is now keyed by "slug:page" instead of just "slug" — this is the
# core fix that makes infinite scroll genuinely infinite: each page the
# client asks for (as it scrolls) is fetched and cached independently, so
# loadMore() can keep advancing the page cursor instead of re-requesting
# the same fixed batch over and over.

_refresh_in_progress: set[str] = set()

def _cache_key(slug: str, page: int) -> str:
    return f"{slug}:{page}"

def _read_cache(cur, cache_key: str) -> Optional[dict]:
    cur.execute("SELECT videos, has_more, cached_at FROM feed_cache WHERE cache_key = %s", (cache_key,))
    return cur.fetchone()

def _write_cache(cache_key: str, videos: list[dict], has_more: bool):
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO feed_cache (cache_key, videos, has_more, cached_at)
                    VALUES (%s, %s, %s, now())
                    ON CONFLICT (cache_key) DO UPDATE SET
                        videos = EXCLUDED.videos, has_more = EXCLUDED.has_more, cached_at = EXCLUDED.cached_at
                """, (cache_key, json.dumps(videos), has_more))
            conn.commit()
    except Exception:
        log.exception(f"Failed writing feed cache for key={cache_key}")

async def _fetch_and_cache(base_url: str, slug: str, page: int) -> tuple[list[dict], bool]:
    videos, has_more = await fetch_page_for_slug(base_url, slug, page)
    cache_key = _cache_key(slug, page)
    _write_cache(cache_key, videos, has_more)
    return videos, has_more

def _schedule_background_refresh(base_url: str, slug: str, page: int):
    cache_key = _cache_key(slug, page)
    if cache_key in _refresh_in_progress:
        return
    _refresh_in_progress.add(cache_key)

    async def _run():
        try:
            await _fetch_and_cache(base_url, slug, page)
            log.info(f"↻ background refresh complete for key={cache_key}")
        finally:
            _refresh_in_progress.discard(cache_key)

    asyncio.create_task(_run())

async def get_videos_for_slug_page(base_url: str, slug: str, page: int) -> tuple[list[dict], bool]:
    cache_key = _cache_key(slug, page)
    with get_conn() as conn:
        with conn.cursor() as cur:
            row = _read_cache(cur, cache_key)

    if row is None:
        log.info(f"cache MISS key={cache_key} — fetching synchronously")
        return await _fetch_and_cache(base_url, slug, page)

    age = (datetime.now(timezone.utc) - row["cached_at"]).total_seconds()
    videos = row["videos"]
    has_more = row["has_more"]

    if age > FEED_CACHE_TTL_SECONDS:
        log.info(f"cache STALE key={cache_key} age={int(age)}s — serving stale + refreshing in background")
        _schedule_background_refresh(base_url, slug, page)
    else:
        log.info(f"cache HIT key={cache_key} age={int(age)}s")

    return videos, has_more

@app.get("/shorts/feed")
async def shorts_feed(
    subs: str = Query(..., description='"+"-separated community seoKeys, or "explore" for the For-You feed'),
    base_url: str = Query("https://ifunny.club"),
    page: int = Query(0, ge=0, description="Page cursor for pagination — increment this to load more"),
):
    raw_slugs = [s.strip() for s in subs.replace("%2B", "+").split("+") if s.strip()]
    if not raw_slugs:
        raise HTTPException(status_code=400, detail="subs param is required")

    # "explore" is the magic value the app sends for the For-You feed (no
    # specific community context) — maps onto the seocloud /post/explore
    # endpoint rather than a community seoKey.
    slugs = [EXPLORE_SLUG if s.lower() == "explore" else s for s in raw_slugs][:5]

    tasks = [get_videos_for_slug_page(base_url, slug, page) for slug in slugs]
    results = await asyncio.gather(*tasks)

    merged, seen_urls = [], set()
    any_has_more = False
    for group_videos, group_has_more in results:
        any_has_more = any_has_more or group_has_more
        for v in group_videos:
            if v["mp4"] not in seen_urls:
                seen_urls.add(v["mp4"])
                merged.append(v)

    # Quick visibility into which path actually served this batch, without
    # needing to inspect individual video objects. Check this log line (or
    # the "sourceCounts" field below) any time you want to confirm the
    # seocloud API is doing the work vs the HTML/RSC scraper fallback.
    api_count = sum(1 for v in merged if v.get("source") == "api")
    scraper_count = sum(1 for v in merged if v.get("source") == "scraper")
    log.info(
        f"shorts/feed page={page} slugs={slugs} → {len(merged)} total "
        f"(api={api_count}, scraper={scraper_count}), hasMore={any_has_more}"
    )

    return {
        "videos": [_to_short_video(v) for v in merged],
        "count": len(merged),
        "hasMore": any_has_more,
        "nextPage": page + 1,
        # Per-source breakdown for this response. If scraper > 0, the
        # seocloud BFF call failed for at least one slug/page in this
        # request and the HTML/RSC fallback covered for it — check the
        # server logs for "falling back to scraper" around this timestamp
        # to see which slug and why.
        "sourceCounts": {"api": api_count, "scraper": scraper_count},
    }

@app.post("/shorts/cache/clear")
async def clear_feed_cache(slug: Optional[str] = Query(None, description="Clear one slug (all its pages), or omit to clear all")):
    """
    Manually invalidate the feed cache. Useful right after a backend fix so
    a previously-cached (possibly broken) batch of URLs doesn't keep being
    served for the rest of its TTL window.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            if slug:
                cur.execute("DELETE FROM feed_cache WHERE cache_key LIKE %s", (f"{slug}:%",))
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
        "message": "Reelz API is running",
        "endpoints": {
            "/auth/google": "POST - Google authentication",
            "/payments/init": "POST - Initialize payment",
            "/subscription/status": "GET - Check subscription status",
            "/webhook/paystack": "POST - Paystack webhook",
            "/shorts/feed": "GET - Get video feed (?subs=explore|seoKey1+seoKey2&page=0)",
            "/health": "GET - Health check"
        }
    }
