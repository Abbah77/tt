import os
import math
import logging
import requests
import subprocess
from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import RedirectResponse, JSONResponse
from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
load_dotenv()

app = FastAPI(title="Reelz Gateway", version="9.0.0")
app.add_middleware(GZipMiddleware, minimum_size=256)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

TMDB_API_KEY     = os.getenv("TMDB_API_KEY")
DOMAIN           = os.getenv("PRODUCTION_DOMAIN", "https://tt-b577.onrender.com")
TMDB_IMG         = "https://image.tmdb.org/t/p/w500"
FALLBACK_THUMB   = "https://images.unsplash.com/photo-1440404653325-ab127d49abc1?q=80&w=500"
FALLBACK_VIDEO   = "https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/BigBuckBunny.mp4"

# Simple in-memory cache so we don't hammer TMDB on every feed scroll
_trailer_cache: dict[int, str | None] = {}


# ── TMDB ─────────────────────────────────────────────────────────────────────

def tmdb(endpoint: str, params: dict = None) -> dict:
    p = {"api_key": TMDB_API_KEY, **(params or {})}
    r = requests.get(f"https://api.themoviedb.org/3/{endpoint}", params=p, timeout=8)
    r.raise_for_status()
    return r.json()


def get_yt_trailer_key(movie_id: int) -> str | None:
    """Returns the best YouTube trailer key from TMDB, cached."""
    if movie_id in _trailer_cache:
        return _trailer_cache[movie_id]

    try:
        videos = tmdb(f"movie/{movie_id}/videos").get("results", [])
        # Priority: Official Trailer > Trailer > Teaser, all on YouTube
        for type_ in ("Trailer", "Teaser", "Clip"):
            for v in videos:
                if v.get("site") == "YouTube" and v.get("type") == type_:
                    _trailer_cache[movie_id] = v["key"]
                    return v["key"]
    except Exception as e:
        logger.warning(f"TMDB videos failed for {movie_id}: {e}")

    _trailer_cache[movie_id] = None
    return None


def map_movie(m: dict) -> dict:
    movie_id = m.get("id")
    poster   = m.get("poster_path")
    yt_key   = get_yt_trailer_key(movie_id)

    return {
        "id":            movie_id,
        "slug":          str(movie_id),
        "title":         m.get("title") or m.get("original_title") or "Untitled",
        "thumbnail_url": f"{TMDB_IMG}{poster}" if poster else FALLBACK_THUMB,
        # Trailer URL — resolved to direct mp4 via /proxy/yt/{key}
        # Flutter media_kit plays this directly
        "trailer_url":   f"{DOMAIN}/proxy/yt/{yt_key}" if yt_key else None,
        # For the Watch button — opens YouTube in a WebView/browser
        "youtube_key":   yt_key,
    }


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/")
def root():
    return {"status": "online", "version": "9.0.0"}


@app.get("/feed")
def feed(cursor: int = Query(None), limit: int = Query(10, le=20)):
    """
    Cursor-paginated feed matching Flutter FeedController exactly.
    Returns: { data, next_cursor, has_more }
    Each item has trailer_url that media_kit can play directly.
    """
    try:
        # Map cursor → TMDB page (20 results per page)
        page   = ((cursor or 0) // 20) + 1
        offset = (cursor or 0) % 20

        data       = tmdb("discover/movie", {"sort_by": "popularity.desc", "page": page})
        results    = data.get("results", [])
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


@app.get("/search")
def search(q: str = Query(..., min_length=1), limit: int = Query(20, le=40)):
    """Search movies. Returns same MovieCard shape as feed."""
    try:
        results = tmdb("search/movie", {"query": q, "include_adult": False}).get("results", [])[:limit]
        return {"data": [map_movie(r) for r in results]}
    except Exception as e:
        logger.error(f"Search error: {e}")
        raise HTTPException(status_code=500, detail="Search failed")


@app.get("/movie/{slug}")
def movie_detail(slug: str):
    """
    Movie detail + episode list.
    Episode URLs point to YouTube chapters of the trailer (best we can do legally)
    OR you can replace with your own hosted content later.
    Returns shape Flutter MovieDetail expects exactly.
    """
    try:
        info     = tmdb(f"movie/{slug}")
        runtime  = info.get("runtime") or 90
        yt_key   = get_yt_trailer_key(int(slug))

        # Split into episodes — each episode is a timestamped segment of the trailer
        # (or a real episode URL if you have your own content)
        total_eps = max(1, math.ceil(runtime / 45))

        episodes = []
        for i in range(1, total_eps + 1):
            # Each episode links to the same trailer but could be
            # replaced with real episode URLs (your own CDN, etc.)
            ep_url = f"{DOMAIN}/proxy/yt/{yt_key}" if yt_key else FALLBACK_VIDEO
            episodes.append({
                "id":             i,
                "episode_number": i,
                "url":            ep_url,
            })

        return {
            "movie":          map_movie(info),
            "episodes":       episodes,
            "total_episodes": total_eps,
        }
    except Exception as e:
        logger.error(f"Movie detail error {slug}: {e}")
        raise HTTPException(status_code=404, detail="Movie not found")


@app.get("/proxy/yt/{key}")
def youtube_proxy(key: str):
    """
    THE KEY ROUTE — resolves a YouTube key to a direct MP4 stream URL.

    Uses yt-dlp (must be installed: pip install yt-dlp).
    media_kit in Flutter plays this URL directly — no WebView needed.

    Install on Render: add 'yt-dlp' to requirements.txt
    """
    try:
        result = subprocess.run(
            [
                "yt-dlp",
                "--no-playlist",
                # Best single mp4 file under 720p that media_kit can handle
                "-f", "best[ext=mp4][height<=720]/best[ext=mp4]/best",
                "--get-url",
                f"https://www.youtube.com/watch?v={key}",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        url = result.stdout.strip().split("\n")[0]
        if url and url.startswith("http"):
            logger.info(f"yt-dlp resolved {key} → {url[:60]}...")
            return RedirectResponse(url=url, status_code=302)

        logger.warning(f"yt-dlp returned no URL for {key}: {result.stderr[:200]}")
        return RedirectResponse(url=FALLBACK_VIDEO)

    except FileNotFoundError:
        # yt-dlp not installed
        logger.error("yt-dlp not found — install it: pip install yt-dlp")
        return RedirectResponse(url=FALLBACK_VIDEO)
    except subprocess.TimeoutExpired:
        logger.error(f"yt-dlp timeout for {key}")
        return RedirectResponse(url=FALLBACK_VIDEO)
    except Exception as e:
        logger.error(f"youtube_proxy error for {key}: {e}")
        return RedirectResponse(url=FALLBACK_VIDEO)


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    """Check if yt-dlp is available."""
    try:
        r = subprocess.run(["yt-dlp", "--version"], capture_output=True, text=True, timeout=5)
        yt_dlp_version = r.stdout.strip()
    except Exception:
        yt_dlp_version = "NOT INSTALLED"

    return {
        "status":        "online",
        "yt_dlp":        yt_dlp_version,
        "tmdb_key_set":  bool(TMDB_API_KEY),
    }
