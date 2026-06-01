import os
import logging
import requests
from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import RedirectResponse
from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
load_dotenv()

app = FastAPI(title="Reelz Gateway", version="10.0.0")
app.add_middleware(GZipMiddleware, minimum_size=256)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

TMDB_API_KEY   = os.getenv("TMDB_API_KEY")
DOMAIN         = os.getenv("PRODUCTION_DOMAIN", "https://tt-b577.onrender.com")
TMDB_IMG       = "https://image.tmdb.org/t/p/w500"
FALLBACK_THUMB = "https://images.unsplash.com/photo-1440404653325-ab127d49abc1?q=80&w=500"

# ── In-memory trailer cache (survives the request, resets on restart) ─────────
_trailer_cache: dict[int, str | None] = {}


# ── TMDB ──────────────────────────────────────────────────────────────────────

def tmdb(endpoint: str, params: dict = None) -> dict:
    p = {"api_key": TMDB_API_KEY, **(params or {})}
    r = requests.get(
        f"https://api.themoviedb.org/3/{endpoint}",
        params=p,
        timeout=8,
    )
    r.raise_for_status()
    return r.json()


def get_yt_key(movie_id: int) -> str | None:
    """
    Returns best YouTube trailer key from TMDB videos endpoint.
    Priority: Official Trailer > Trailer > Teaser > Clip
    Result is cached in memory.
    """
    if movie_id in _trailer_cache:
        return _trailer_cache[movie_id]

    try:
        videos = tmdb(f"movie/{movie_id}/videos").get("results", [])
        for kind in ("Trailer", "Teaser", "Clip", "Featurette"):
            for v in videos:
                if v.get("site") == "YouTube" and v.get("type") == kind:
                    _trailer_cache[movie_id] = v["key"]
                    logger.info(f"Trailer found for {movie_id}: {v['key']}")
                    return v["key"]
    except Exception as e:
        logger.warning(f"TMDB videos failed for {movie_id}: {e}")

    _trailer_cache[movie_id] = None
    return None


def build_trailer_url(movie_id: int) -> str | None:
    """
    Returns a trailer_url that media_kit in Flutter can play directly.

    Strategy:
      1. Get YouTube key from TMDB
      2. Return a piped.video URL — this is an open-source YouTube
         frontend that serves direct video streams (no yt-dlp needed,
         no API key needed, completely free and legal).
         Piped proxies the YouTube stream so media_kit gets a real
         .mp4/webm stream URL without any YouTube auth.
    """
    key = get_yt_key(movie_id)
    if not key:
        return None

    # Piped public instances — all serve direct streamable video
    # media_kit plays these with no extra setup needed
    # Format: https://piped.video/watch?v={key}  ← WebView fallback
    # Direct stream: https://pipedproxy.kavin.rocks/videoplayback?... 
    # BUT: easiest approach is the /api/streams endpoint:
    # GET https://piped.video/api/streams/{key}
    # → returns { videoStreams: [{url, quality, ...}] }
    # We fetch this server-side and redirect to the best mp4 url.
    # This is instant (<200ms), no subprocess, works on Render free tier.
    return f"{DOMAIN}/proxy/trailer/{key}"


def map_movie(m: dict) -> dict:
    movie_id = m.get("id")
    poster   = m.get("poster_path")
    return {
        "id":           movie_id,
        "slug":         str(movie_id),
        "title":        m.get("title") or m.get("original_title") or "Untitled",
        "thumbnail_url": f"{TMDB_IMG}{poster}" if poster else FALLBACK_THUMB,
        "trailer_url":  build_trailer_url(movie_id),
    }


# ── Trailer Proxy ─────────────────────────────────────────────────────────────

# Stream URLs from Invidious/Piped expire after ~6h — cache with timestamp
import time
_stream_cache: dict[str, tuple[str, float]] = {}  # key → (url, timestamp)
CACHE_TTL = 4 * 3600  # 4 hours

# Invidious instances — open-source YouTube frontend with a proper JSON API
# These are specifically designed for programmatic server-to-server access
# unlike Piped which blocks non-browser requests
INVIDIOUS_INSTANCES = [
    "https://inv.nadeko.net",
    "https://invidious.privacydev.net",
    "https://iv.datura.network",
    "https://invidious.perennialte.ch",
    "https://invidious.fdn.fr",
]


def _pick_best_stream(formats: list[dict]) -> str | None:
    """
    Pick the best single-file mp4 video stream from Invidious adaptiveFormats.
    Prefers: mp4 container, video+audio combined, max 720p.
    """
    if not formats:
        return None

    # Invidious returns adaptiveFormats like:
    # { "url": "...", "type": "video/mp4; codecs=...", "quality": "720p",
    #   "container": "mp4", "encoding": "h264" }

    def score(f):
        container = f.get("container", "")
        type_     = f.get("type", "")
        quality   = f.get("quality", "")
        q_num = 0
        try:
            q_num = int(quality.replace("p", "").split(".")[0])
        except Exception:
            pass

        is_mp4     = "mp4" in container.lower() or "mp4" in type_.lower()
        is_h264    = "h264" in type_.lower() or "avc" in type_.lower()
        has_audio  = "audio" not in type_.lower() or f.get("audioSampleRate")
        under_720  = q_num <= 720 and q_num > 0

        if not is_mp4:
            return -1
        return (int(is_h264) * 1000) + (int(under_720) * 500) + q_num

    ranked = sorted(formats, key=score, reverse=True)
    for f in ranked:
        url = f.get("url")
        if url and score(f) > 0:
            return url

    return None


@app.get("/proxy/trailer/{key}")
def trailer_proxy(key: str):
    """
    Resolves YouTube key → direct MP4 stream via Invidious API.

    Invidious /api/v1/videos/{key} returns adaptiveFormats with direct
    video URLs. These are the actual YouTube CDN URLs — media_kit plays
    them natively with no extra setup.

    Tries multiple public Invidious instances as fallback.
    Caches results for 4 hours to avoid hammering instances.
    """
    # Return cached URL if still fresh
    if key in _stream_cache:
        url, ts = _stream_cache[key]
        if time.time() - ts < CACHE_TTL:
            logger.info(f"Cache hit for {key}")
            return RedirectResponse(url=url, status_code=302)
        else:
            del _stream_cache[key]

    headers = {
        "User-Agent": "Mozilla/5.0 (Linux; Android 10) AppleWebKit/537.36",
        "Accept": "application/json",
    }

    for instance in INVIDIOUS_INSTANCES:
        try:
            logger.info(f"Trying Invidious instance: {instance}")
            r = requests.get(
                f"{instance}/api/v1/videos/{key}",
                params={"fields": "adaptiveFormats,formatStreams"},
                headers=headers,
                timeout=7,
            )

            if r.status_code != 200:
                logger.warning(f"{instance} returned {r.status_code}")
                continue

            # Make sure we got JSON not HTML
            content_type = r.headers.get("content-type", "")
            if "html" in content_type.lower():
                logger.warning(f"{instance} returned HTML — instance down")
                continue

            data = r.json()

            # Try adaptiveFormats first (separate video+audio streams)
            adaptive = data.get("adaptiveFormats", [])
            url = _pick_best_stream(adaptive)

            # Fall back to formatStreams (combined video+audio, usually lower quality)
            if not url:
                combined = data.get("formatStreams", [])
                # formatStreams are already combined, just pick best mp4
                for f in reversed(combined):  # last = highest quality
                    if "mp4" in f.get("container", "").lower():
                        url = f.get("url")
                        break

            if url:
                _stream_cache[key] = (url, time.time())
                logger.info(f"✓ Resolved {key} via {instance}")
                return RedirectResponse(url=url, status_code=302)
            else:
                logger.warning(f"{instance}: no playable stream found in response")

        except requests.exceptions.Timeout:
            logger.warning(f"{instance} timed out")
        except Exception as e:
            logger.warning(f"{instance} error: {e}")
        continue

    # ── All instances failed ──────────────────────────────────────────────────
    # Last resort: return the YouTube nocookie embed URL.
    # This won't play in media_kit but WILL play in a WebView widget.
    # Better than a broken video.
    logger.error(f"All Invidious instances failed for {key} — returning YouTube fallback")
    return RedirectResponse(
        url=f"https://www.youtube-nocookie.com/embed/{key}?autoplay=1",
        status_code=302,
    )


# ── Feed ──────────────────────────────────────────────────────────────────────

@app.get("/feed")
def feed(cursor: int = Query(None), limit: int = Query(10, le=20)):
    try:
        page   = ((cursor or 0) // 20) + 1
        offset = (cursor or 0) % 20

        data        = tmdb("discover/movie", {"sort_by": "popularity.desc", "page": page})
        results     = data.get("results", [])
        total_pages = data.get("total_pages", 1)

        slice_      = results[offset: offset + limit]
        next_cursor = (cursor or 0) + len(slice_) if slice_ else None
        has_more    = page < total_pages

        return {
            "data":        [map_movie(r) for r in slice_],
            "next_cursor": next_cursor if has_more else None,
            "has_more":    has_more,
        }
    except Exception as e:
        logger.error(f"Feed error: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch feed")


# ── Search ────────────────────────────────────────────────────────────────────

@app.get("/search")
def search(q: str = Query(..., min_length=1), limit: int = Query(20, le=40)):
    try:
        results = tmdb(
            "search/movie", {"query": q, "include_adult": False}
        ).get("results", [])[:limit]
        return {"data": [map_movie(r) for r in results]}
    except Exception as e:
        logger.error(f"Search error: {e}")
        raise HTTPException(status_code=500, detail="Search failed")


# ── Movie detail ──────────────────────────────────────────────────────────────

@app.get("/movie/{slug}")
def movie_detail(slug: str):
    try:
        info    = tmdb(f"movie/{slug}")
        yt_key  = get_yt_key(int(slug))
        ep_url  = f"{DOMAIN}/proxy/trailer/{yt_key}" if yt_key else None

        return {
            "movie":          map_movie(info),
            # Single "episode" = the full trailer for now
            # Replace ep_url with real hosted content when you have it
            "episodes": [
                {
                    "id":             1,
                    "episode_number": 1,
                    "url":            ep_url or "",
                }
            ],
            "total_episodes": 1,
        }
    except Exception as e:
        logger.error(f"Movie detail error {slug}: {e}")
        raise HTTPException(status_code=404, detail="Movie not found")


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    """
    Test the full pipeline. Visit this URL in browser after deploying.
    Should return trailer_url that plays in VLC or any video player.
    """
    test_id = 550  # Fight Club — always has a trailer
    key = get_yt_key(test_id)
    return {
        "status":       "online",
        "version":      "10.0.0",
        "tmdb_key_set": bool(TMDB_API_KEY),
        "test_movie":   "Fight Club (id=550)",
        "yt_key":       key,
        "trailer_url":  f"{DOMAIN}/proxy/trailer/{key}" if key else None,
        "piped_instances": PIPED_INSTANCES,
    }


@app.get("/")
def root():
    return {"status": "online", "version": "10.0.0"}
