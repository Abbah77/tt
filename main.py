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
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
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

# How long a cached feed slug is considered "fresh" before we trigger a
# background refresh. Stale entries are still served instantly — the user
# never pays the scrape latency after the very first load for a slug.
FEED_CACHE_TTL_SECONDS = 10 * 60  # 10 minutes

app = FastAPI(title="Reelz API", version="1.3.0")
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

# ── ENDPOINT 5: GET /shorts/feed — HTTP Scraper + Postgres cache ────────────

def clean_url(url: str, base_url: str) -> str:
    """Clean and normalize URLs."""
    if not url:
        return ''

    # Remove whitespace
    url = url.strip()

    # Handle relative URLs
    if url.startswith('//'):
        return 'https:' + url
    elif url.startswith('/'):
        return urljoin(base_url, url)
    elif not url.startswith('http'):
        return urljoin(base_url, url)

    return url

async def scrape_community_http(base_url: str, slug: str) -> list[dict]:
    """
    Scrape ifunny.club for videos using HTTP requests.
    """
    url = f"{base_url}/community/{slug}"
    log.info(f"Scraping: {url}")

    try:
        async with httpx.AsyncClient(
            timeout=30,
            follow_redirects=True,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "gzip, deflate, br",
                "Connection": "keep-alive",
                "Upgrade-Insecure-Requests": "1",
                "Referer": base_url + "/",
                "Origin": base_url,
            }
        ) as client:
            resp = await client.get(url)

            if resp.status_code != 200:
                log.warning(f"Failed to fetch {url}: {resp.status_code}")
                return []

            html = resp.text
            soup = BeautifulSoup(html, 'html.parser')

            videos = []
            seen_urls = set()

            # Find all video elements
            video_elements = soup.find_all('video')
            log.info(f"Found {len(video_elements)} video elements")

            for video in video_elements:
                # Get video source from various attributes
                src = video.get('src', '')

                # Check for source tags
                if not src:
                    source = video.find('source')
                    if source:
                        src = source.get('src', '')

                # Check data attributes
                if not src:
                    for attr in ['data-src', 'data-video', 'data-url', 'data-href']:
                        if video.get(attr):
                            src = video.get(attr)
                            break

                # Get poster/thumbnail
                poster = video.get('poster', '')
                if not poster:
                    for attr in ['data-poster', 'data-thumb', 'data-thumbnail']:
                        if video.get(attr):
                            poster = video.get(attr)
                            break

                # Try to get video ID from parent links
                video_id = f"vid_{len(videos)}"
                parent = video.parent
                for _ in range(6):
                    if not parent:
                        break
                    links = parent.find_all('a', href=True)
                    for link in links:
                        href = link.get('href', '')
                        match = re.search(r'/(video|post|gif|content)/([A-Za-z0-9_-]+)', href)
                        if match:
                            video_id = match.group(2)
                            break
                    if video_id != f"vid_{len(videos)}":
                        break
                    parent = parent.parent

                # Clean URL
                if src:
                    src = clean_url(src, base_url)

                    if '.mp4' in src.lower() or '/video/' in src.lower():
                        if src not in seen_urls:
                            seen_urls.add(src)

                            # Clean poster URL
                            if poster:
                                poster = clean_url(poster, base_url)

                            # Try to find a title
                            title = ''
                            parent = video.parent
                            for _ in range(4):
                                if not parent:
                                    break
                                # Check for heading
                                heading = parent.find(['h1', 'h2', 'h3', 'h4', 'h5', 'h6'])
                                if heading:
                                    title = heading.get_text(strip=True)
                                    break
                                # Check for title attributes
                                title_attrs = parent.find_all(attrs={'title': True})
                                if title_attrs:
                                    title = title_attrs[0].get('title', '')
                                    break
                                parent = parent.parent

                            videos.append({
                                'id': video_id,
                                'mp4': src,
                                'thumb': poster if poster else '',
                                'title': title if title else f"Video {len(videos) + 1}",
                                'author': slug.split('-')[0].capitalize(),
                                'community': slug.split('-')[0].capitalize()
                            })

            # If no videos found, search HTML for MP4 URLs
            if not videos:
                log.info("No video tags found, searching HTML for MP4 URLs...")

                # Find all MP4 URLs in HTML
                mp4_pattern = r'https?://[^\s"\'<>]+\.mp4[^\s"\'<>]*'
                mp4_urls = re.findall(mp4_pattern, html)

                for idx, mp4_url in enumerate(mp4_urls[:30]):
                    if mp4_url not in seen_urls:
                        seen_urls.add(mp4_url)

                        # Try to find thumbnail nearby
                        thumb = ''
                        context = html[max(0, html.find(mp4_url) - 500):html.find(mp4_url) + 500]
                        thumb_pattern = r'https?://[^\s"\'<>]+\.(?:jpg|png|jpeg|webp|gif)'
                        thumbs = re.findall(thumb_pattern, context)
                        if thumbs:
                            thumb = thumbs[0]

                        videos.append({
                            'id': f"mp4_{idx}",
                            'mp4': mp4_url,
                            'thumb': thumb,
                            'title': f"Video {idx + 1}",
                            'author': slug.split('-')[0].capitalize(),
                            'community': slug.split('-')[0].capitalize()
                        })

            log.info(f"✓ {slug} → {len(videos)} videos found")
            return videos[:30]

    except Exception as e:
        log.exception(f"✗ Failed to scrape {slug}: {e}")
        return []

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
        "ups": 0,
        "duration": 0,
        "hasAudio": True,
        "width": 0,
        "height": 0,
    }

# ── Feed cache (Postgres-backed, stale-while-revalidate) ──────────────────────
#
# Read path:
#   1. Cache row missing entirely      -> scrape synchronously, write cache, return fresh
#   2. Cache row present and fresh     -> return cached immediately
#   3. Cache row present but stale     -> return cached immediately AND
#                                          kick off a background refresh for next time
#
# This means a real scrape only ever blocks a user request the very first
# time a slug is ever requested. Every request after that is instant.

# Tracks slugs currently being refreshed in the background so we don't
# fire duplicate scrapes if multiple requests land while one is in flight.
_refresh_in_progress: set[str] = set()

def _read_cache(cur, slug: str) -> Optional[dict]:
    cur.execute("SELECT videos, cached_at FROM feed_cache WHERE slug = %s", (slug,))
    return cur.fetchone()

def _write_cache(slug: str, videos: list[dict]):
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO feed_cache (slug, videos, cached_at)
                    VALUES (%s, %s, now())
                    ON CONFLICT (slug) DO UPDATE SET
                        videos = EXCLUDED.videos, cached_at = EXCLUDED.cached_at
                """, (slug, json.dumps(videos)))
            conn.commit()
    except Exception:
        log.exception(f"Failed writing feed cache for slug={slug}")

async def _scrape_and_cache(base_url: str, slug: str) -> list[dict]:
    videos = await scrape_community_http(base_url, slug)
    # Shuffle once at write-time so repeated reads within the TTL window
    # stay in a stable order (no re-shuffling on every loadMore/refresh).
    random.shuffle(videos)
    _write_cache(slug, videos)
    return videos

def _schedule_background_refresh(base_url: str, slug: str):
    if slug in _refresh_in_progress:
        return
    _refresh_in_progress.add(slug)

    async def _run():
        try:
            await _scrape_and_cache(base_url, slug)
            log.info(f"↻ background refresh complete for slug={slug}")
        finally:
            _refresh_in_progress.discard(slug)

    asyncio.create_task(_run())

async def get_videos_for_slug(base_url: str, slug: str) -> list[dict]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            row = _read_cache(cur, slug)

    if row is None:
        # Never cached before — this request pays the scrape cost once.
        log.info(f"cache MISS slug={slug} — scraping synchronously")
        return await _scrape_and_cache(base_url, slug)

    age = (datetime.now(timezone.utc) - row["cached_at"]).total_seconds()
    videos = row["videos"]

    if age > FEED_CACHE_TTL_SECONDS:
        log.info(f"cache STALE slug={slug} age={int(age)}s — serving stale + refreshing in background")
        _schedule_background_refresh(base_url, slug)
    else:
        log.info(f"cache HIT slug={slug} age={int(age)}s")

    return videos

@app.get("/shorts/feed")
async def shorts_feed(
    subs: str = Query(..., description='"+"-separated community slugs'),
    base_url: str = Query("https://ifunny.club"),
):
    slugs = [s.strip() for s in subs.replace("%2B", "+").split("+") if s.strip()]
    if not slugs:
        raise HTTPException(status_code=400, detail="subs param is required")

    slugs = slugs[:5]

    tasks = [get_videos_for_slug(base_url, slug) for slug in slugs]
    results = await asyncio.gather(*tasks)

    merged, seen_urls = [], set()
    for group in results:
        for v in group:
            if v["mp4"] not in seen_urls:
                seen_urls.add(v["mp4"])
                merged.append(v)

    log.info(f"shorts/feed → {len(merged)} total videos")

    return {"videos": [_to_short_video(v) for v in merged], "count": len(merged)}

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
            "/shorts/feed": "GET - Get video feed",
            "/health": "GET - Health check"
        }
    }
