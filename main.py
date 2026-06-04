import os
import logging
import time
import requests
from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import RedirectResponse, JSONResponse
from dotenv import load_dotenv
from typing import Optional, List, Dict, Any

# ── Optional Supabase ─────────────────────────────────────────────────────────
try:
    from supabase import create_client, Client as SupabaseClient
    _supabase_available = True
except ImportError:
    _supabase_available = False

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
load_dotenv()

app = FastAPI(title="Reelz Gateway - Heiermuer Edition", version="12.0.0")
app.add_middleware(GZipMiddleware, minimum_size=256)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Config ────────────────────────────────────────────────────────────────────
HEIERMUER_API = "https://json.heimuer.tv/api.php/provide/vod/"
HEIERMUER_PLAYER = "https://player.heimuer.tv/index.html?url="
SHORT_DRAMA_FALLBACK = "http://74.120.175.78/JK/XYQTVBox/dj.json"

DOMAIN         = os.getenv("PRODUCTION_DOMAIN", "https://tt-b577.onrender.com")
SUPABASE_URL   = os.getenv("SUPABASE_URL")
SUPABASE_KEY   = os.getenv("SUPABASE_SERVICE_KEY")
FALLBACK_THUMB = "https://images.unsplash.com/photo-1440404653325-ab127d49abc1?q=80&w=500"

# ── In-memory caches ──────────────────────────────────────────────────────────
_drama_cache: Dict[str, Any] = {}  # Cache drama list
_stream_cache: Dict[str, tuple[str, float]] = {}  # Cache m3u8 links
CACHE_TTL = 3600  # 1 hour (m3u8 links expire quickly!)

# ── Supabase client (unchanged) ───────────────────────────────────────────────
_sb: "SupabaseClient | None" = None

def get_supabase() -> "SupabaseClient | None":
    global _sb
    if _sb is not None:
        return _sb
    if not _supabase_available:
        logger.warning("supabase-py not installed; DB features disabled")
        return None
    if not SUPABASE_URL or not SUPABASE_KEY:
        logger.warning("SUPABASE_URL / SUPABASE_SERVICE_KEY not set; DB disabled")
        return None
    try:
        _sb = create_client(SUPABASE_URL, SUPABASE_KEY)
        logger.info("Supabase connected")
        return _sb
    except Exception as e:
        logger.error(f"Supabase init failed: {e}")
        return None


# ── Heiermuer.tv Content Fetcher ──────────────────────────────────────────────

def fetch_heimuer_catalog(ac: str = "list", page: int = 1, limit: int = 20) -> List[Dict]:
    """Fetch catalog from Heiermuer API"""
    try:
        params = {
            "ac": ac,
            "pg": page,
            "t": "short_drama"  # Filter for short dramas
        }
        
        response = requests.get(
            HEIERMUER_API,
            params=params,
            timeout=10,
            headers={"User-Agent": "Mozilla/5.0"}
        )
        response.raise_for_status()
        data = response.json()
        
        # Heiermuer returns: {"code": 1, "msg": "success", "list": [...]}
        if data.get("code") != 1:
            logger.warning(f"Heiermuer API error: {data.get('msg')}")
            return []
        
        dramas = []
        for item in data.get("list", [])[:limit]:
            # Extract m3u8 from vod_play_url
            m3u8_url = extract_m3u8_from_play_url(item.get("vod_play_url", ""))
            
            dramas.append({
                "id": item.get("vod_id"),
                "slug": str(item.get("vod_id")),
                "title": item.get("vod_name", "Untitled"),
                "thumbnail_url": item.get("vod_pic", FALLBACK_THUMB),
                "description": item.get("vod_content", ""),
                "year": item.get("vod_year", ""),
                "rating": item.get("vod_rating", 0),
                "m3u8_url": m3u8_url,  # This is your video stream!
                "episodes": extract_episodes(item.get("vod_play_url", ""))
            })
        
        return dramas
        
    except Exception as e:
        logger.error(f"Failed to fetch Heiermuer catalog: {e}")
        return []


def extract_m3u8_from_play_url(play_url: str) -> Optional[str]:
    """Extract the first m3u8 URL from vod_play_url field"""
    if not play_url:
        return None
    
    # Heiermuer format: "episode1$m3u8_url#episode2$m3u8_url"
    if "$" in play_url:
        parts = play_url.split("#")
        for part in parts:
            if "$" in part:
                url = part.split("$")[1]
                if ".m3u8" in url:
                    return url
    
    # If it's just a direct m3u8 URL
    if ".m3u8" in play_url:
        return play_url
    
    return None


def extract_episodes(play_url: str) -> List[Dict]:
    """Extract all episodes from vod_play_url"""
    episodes = []
    if not play_url:
        return [{"id": 1, "episode_number": 1, "url": ""}]
    
    if "$" in play_url:
        parts = play_url.split("#")
        for idx, part in enumerate(parts, 1):
            if "$" in part:
                title, url = part.split("$", 1)
                episodes.append({
                    "id": idx,
                    "episode_number": idx,
                    "title": title,
                    "url": url if ".m3u8" in url else None
                })
    else:
        episodes.append({
            "id": 1,
            "episode_number": 1,
            "title": "Episode 1",
            "url": play_url if ".m3u8" in play_url else None
        })
    
    return episodes


def fetch_short_drama_fallback() -> List[Dict]:
    """Fallback to TVBox short drama interface if Heiermuer fails"""
    try:
        response = requests.get(SHORT_DRAMA_FALLBACK, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        dramas = []
        # TVBox format varies, adapt based on actual response
        if isinstance(data, list):
            for item in data[:20]:
                dramas.append({
                    "id": item.get("vod_id", hash(item.get("vod_name"))),
                    "slug": str(item.get("vod_id", "")),
                    "title": item.get("vod_name", "Untitled"),
                    "thumbnail_url": item.get("vod_pic", FALLBACK_THUMB),
                    "description": item.get("vod_content", ""),
                    "m3u8_url": item.get("vod_play_url", ""),
                    "episodes": [{"id": 1, "episode_number": 1, "url": item.get("vod_play_url", "")}]
                })
        return dramas
    except Exception as e:
        logger.error(f"Fallback failed: {e}")
        return []


# ── New Endpoints for Heiermuer Content ───────────────────────────────────────

@app.get("/api/dramas")
def get_dramas(
    page: int = Query(1, ge=1),
    limit: int = Query(20, le=50),
    use_fallback: bool = Query(False)
):
    """Get drama catalog with title, thumbnail, and m3u8 URLs"""
    try:
        # Try Heiermuer first
        dramas = fetch_heimuer_catalog(page=page, limit=limit)
        
        # If failed and fallback enabled, try TVBox source
        if not dramas and use_fallback:
            logger.info("Using fallback source")
            dramas = fetch_short_drama_fallback()
        
        if not dramas:
            raise HTTPException(status_code=503, detail="No content available from any source")
        
        return {
            "data": dramas,
            "page": page,
            "has_more": len(dramas) == limit,
            "source": "heimuer" if not use_fallback else "fallback"
        }
        
    except Exception as e:
        logger.error(f"Dramas endpoint error: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch dramas")


@app.get("/api/drama/{drama_id}")
def get_drama_detail(drama_id: str):
    """Get detailed drama info including all episodes with m3u8 URLs"""
    try:
        # Fetch specific drama
        params = {"ac": "detail", "ids": drama_id}
        response = requests.get(HEIERMUER_API, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        if data.get("code") != 1:
            raise HTTPException(status_code=404, detail="Drama not found")
        
        item = data.get("list", [{}])[0]
        
        return {
            "id": item.get("vod_id"),
            "title": item.get("vod_name"),
            "thumbnail_url": item.get("vod_pic", FALLBACK_THUMB),
            "description": item.get("vod_content"),
            "year": item.get("vod_year"),
            "rating": item.get("vod_rating"),
            "episodes": extract_episodes(item.get("vod_play_url", "")),
            "total_episodes": len(extract_episodes(item.get("vod_play_url", "")))
        }
        
    except Exception as e:
        logger.error(f"Drama detail error: {e}")
        raise HTTPException(status_code=404, detail="Drama not found")


@app.get("/api/stream/{drama_id}/{episode_id}")
def get_stream_url(drama_id: str, episode_id: int):
    """Get fresh m3u8 URL for a specific episode (no caching recommended)"""
    cache_key = f"{drama_id}_{episode_id}"
    
    # Check cache but with short TTL (Heiermuer links expire)
    if cache_key in _stream_cache:
        url, timestamp = _stream_cache[cache_key]
        if time.time() - timestamp < 300:  # 5 minutes cache only!
            return {"url": url, "expires_in": 300 - int(time.time() - timestamp)}
    
    try:
        # Fetch drama to get fresh m3u8
        params = {"ac": "detail", "ids": drama_id}
        response = requests.get(HEIERMUER_API, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        if data.get("code") != 1:
            raise HTTPException(status_code=404, detail="Episode not found")
        
        item = data.get("list", [{}])[0]
        episodes = extract_episodes(item.get("vod_play_url", ""))
        
        if episode_id <= len(episodes):
            url = episodes[episode_id - 1].get("url")
            if url:
                # Cache for 5 minutes only
                _stream_cache[cache_key] = (url, time.time())
                return {"url": url, "expires_in": 300}
        
        raise HTTPException(status_code=404, detail="Stream URL not found")
        
    except Exception as e:
        logger.error(f"Stream error: {e}")
        raise HTTPException(status_code=500, detail="Failed to get stream URL")


@app.get("/api/categories")
def get_categories():
    """Get available categories/genres"""
    try:
        params = {"ac": "list", "t": "short_drama"}
        response = requests.get(HEIERMUER_API, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        # Extract unique categories from response
        categories = set()
        for item in data.get("list", []):
            if item.get("vod_class"):
                categories.add(item.get("vod_class"))
        
        return {"categories": list(categories)[:20]}
        
    except Exception as e:
        logger.error(f"Categories error: {e}")
        return {"categories": ["Action", "Romance", "Comedy", "Drama", "Thriller"]}


# ── Keep your existing TMDB endpoints as backup (rename to avoid conflict) ────
# Original /feed becomes /tmdb/feed, etc.

@app.get("/tmdb/feed")
def tmdb_feed(cursor: int = Query(None), limit: int = Query(10, le=20)):
    """Legacy TMDB feed - kept for compatibility"""
    from tmdb_backup import feed_handler
    return feed_handler(cursor, limit)


# ── User likes / saves (unchanged from your original) ─────────────────────────
@app.get("/user/{uid}/interactions")
def get_interactions(uid: str):
    sb = get_supabase()
    if not sb:
        return {"liked": [], "saved": []}
    try:
        rows = (
            sb.table("user_interactions")
            .select("tmdb_id, kind")
            .eq("google_user_id", uid)
            .execute()
        ).data
        liked = [r["tmdb_id"] for r in rows if r["kind"] == "like"]
        saved = [r["tmdb_id"] for r in rows if r["kind"] == "save"]
        return {"liked": liked, "saved": saved}
    except Exception as e:
        logger.error(f"get_interactions error: {e}")
        return {"liked": [], "saved": []}


@app.post("/user/{uid}/like/{content_id}")
def toggle_like(uid: str, content_id: int):
    return _toggle(uid, content_id, "like")


@app.post("/user/{uid}/save/{content_id}")
def toggle_save(uid: str, content_id: int):
    return _toggle(uid, content_id, "save")


def _toggle(uid: str, content_id: int, kind: str) -> dict:
    sb = get_supabase()
    if not sb:
        return {"status": "no_db"}
    try:
        existing = (
            sb.table("user_interactions")
            .select("id")
            .eq("google_user_id", uid)
            .eq("tmdb_id", content_id)
            .eq("kind", kind)
            .execute()
        ).data
        if existing:
            sb.table("user_interactions").delete().eq("id", existing[0]["id"]).execute()
            return {"status": "removed"}
        else:
            sb.table("user_interactions").insert(
                {"google_user_id": uid, "tmdb_id": content_id, "kind": kind}
            ).execute()
            return {"status": "added"}
    except Exception as e:
        logger.error(f"toggle {kind} error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ── Health Check ──────────────────────────────────────────────────────────────
@app.get("/health")
def health():
    # Test Heiermuer connection
    test_dramas = fetch_heimuer_catalog(limit=1)
    sb_ok = get_supabase() is not None
    
    return {
        "status": "online",
        "version": "12.0.0",
        "content_source": "Heiermuer.tv",
        "content_available": len(test_dramas) > 0,
        "supabase_ready": sb_ok,
        "sample_drama": test_dramas[0] if test_dramas else None,
        "warning": "m3u8 links expire in 5 minutes - always fetch fresh!"
    }


@app.get("/")
def root():
    return {
        "status": "online",
        "version": "12.0.0",
        "endpoints": {
            "/api/dramas": "Get drama catalog",
            "/api/drama/{id}": "Get drama details + episode m3u8s",
            "/api/stream/{id}/{episode}": "Get fresh m3u8 URL",
            "/api/categories": "Get categories"
        }
    }
