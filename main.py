import os
import math
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

app = FastAPI(title="Reelz Gateway", version="8.0.0")

app.add_middleware(GZipMiddleware, minimum_size=256)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

TMDB_API_KEY = os.getenv("TMDB_API_KEY")
PRODUCTION_DOMAIN = os.getenv("PRODUCTION_DOMAIN", "https://tt-b577.onrender.com")
FALLBACK_VIDEO = "https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/BigBuckBunny.mp4"
TMDB_IMAGE = "https://image.tmdb.org/t/p/w500"
FALLBACK_THUMB = "https://images.unsplash.com/photo-1440404653325-ab127d49abc1?q=80&w=500"


# ── TMDB helpers ──────────────────────────────────────────────────────────────

def tmdb(endpoint: str, params: dict = None) -> dict:
    p = params or {}
    p["api_key"] = TMDB_API_KEY
    r = requests.get(f"https://api.themoviedb.org/3/{endpoint}", params=p, timeout=8)
    r.raise_for_status()
    return r.json()


def get_trailer_url(movie_id: int) -> str | None:
    """Return a YouTube embed URL for the first official trailer, or None."""
    try:
        data = tmdb(f"movie/{movie_id}/videos")
        videos = data.get("results", [])
        # Prefer official trailers on YouTube
        for v in videos:
            if v.get("site") == "YouTube" and v.get("type") in ("Trailer", "Teaser"):
                key = v["key"]
                # Return a streamable YouTube URL via yt-dlp-style embed
                # media_kit can play youtube-dl resolved URLs —
                # we expose a /proxy/yt/{key} route that redirects to the direct stream
                return f"{PRODUCTION_DOMAIN}/proxy/yt/{key}"
        return None
    except Exception as e:
        logger.warning(f"Trailer fetch failed for {movie_id}: {e}")
        return None


def get_imdb_id(movie_id: int) -> str | None:
    try:
        data = tmdb(f"movie/{movie_id}/external_ids")
        return data.get("imdb_id")
    except Exception:
        return None


def map_movie(m: dict, include_trailer: bool = False) -> dict:
    movie_id = m.get("id")
    poster = m.get("poster_path")
    result = {
        "id": movie_id,
        # ✅ slug is the TMDB id as string — used for /movie/{slug} calls
        "slug": str(movie_id),
        "title": m.get("title") or m.get("original_title") or "Untitled",
        "thumbnail_url": f"{TMDB_IMAGE}{poster}" if poster else FALLBACK_THUMB,
        "trailer_url": get_trailer_url(movie_id) if include_trailer else None,
    }
    return result


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/")
def root():
    return {"status": "online", "version": "8.0.0"}


@app.get("/feed")
def feed(cursor: int = Query(None), limit: int = Query(10, le=20)):
    """
    Cursor-based feed. cursor maps to TMDB page number.
    Returns: { data, next_cursor, has_more }
    """
    try:
        page = (cursor // 20) + 1 if cursor else 1
        data = tmdb("discover/movie", {"sort_by": "popularity.desc", "page": page})
        results = data.get("results", [])
        total_pages = data.get("total_pages", 1)

        # Slice to requested limit
        offset = (cursor % 20) if cursor else 0
        slice_ = results[offset: offset + limit]

        next_cursor = (cursor or 0) + len(slice_) if slice_ else None
        has_more = page < total_pages

        # ✅ include_trailer=True so feed cards autoplay trailers
        movies = [map_movie(r, include_trailer=True) for r in slice_]

        return {
            "data": movies,
            "next_cursor": next_cursor if has_more else None,
            "has_more": has_more,
        }
    except Exception as e:
        logger.error(f"Feed error: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch feed")


@app.get("/search")
def search(q: str = Query(..., min_length=1), limit: int = Query(20, le=40)):
    """
    Search movies via TMDB. Returns list of MovieCard objects.
    """
    try:
        data = tmdb("search/movie", {"query": q, "include_adult": False})
        results = data.get("results", [])[:limit]
        return {
            "data": [map_movie(r, include_trailer=False) for r in results]
        }
    except Exception as e:
        logger.error(f"Search error: {e}")
        raise HTTPException(status_code=500, detail="Search failed")


@app.get("/movie/{slug}")
def movie_detail(slug: str):
    """
    Movie detail + episode list. Episodes divide runtime into ~45-min chunks.
    Returns shape Flutter expects: { movie, episodes, total_episodes }
    """
    try:
        info = tmdb(f"movie/{slug}")
        runtime = info.get("runtime") or 90
        # Treat each ~45 min as one "episode" (minimum 1)
        total_eps = max(1, math.ceil(runtime / 45))

        episodes = [
            {
                "id": i,
                "episode_number": i,           # ✅ Flutter EpisodeModel needs this
                "url": f"{PRODUCTION_DOMAIN}/api/stream/{slug}/ep/{i}",
            }
            for i in range(1, total_eps + 1)
        ]

        return {
            "movie": map_movie(info, include_trailer=True),
            "episodes": episodes,
            "total_episodes": total_eps,       # ✅ Flutter MovieDetail needs this
        }
    except Exception as e:
        logger.error(f"Movie detail error for {slug}: {e}")
        raise HTTPException(status_code=404, detail="Movie not found")


@app.get("/api/stream/{slug}/ep/{ep}")
def stream_resolver(slug: str, ep: int):
    """
    Resolves a video stream for a given movie + episode number.
    Tries multiple free sources before falling back.
    """
    try:
        imdb_id = get_imdb_id(int(slug))

        # ── Source 1: vidsrc.to (reliable free source) ──
        if imdb_id:
            vidsrc_url = f"https://vidsrc.to/embed/movie/{imdb_id}"
            r = requests.head(vidsrc_url, timeout=4, allow_redirects=True)
            if r.status_code < 400:
                return RedirectResponse(url=vidsrc_url)

        # ── Source 2: vidsrc.me ──
        if imdb_id:
            vidsrc_me = f"https://vidsrc.me/embed/movie?imdb={imdb_id}"
            return RedirectResponse(url=vidsrc_me)

        # ── Fallback ──
        return RedirectResponse(url=FALLBACK_VIDEO)

    except Exception as e:
        logger.error(f"Stream resolve error slug={slug} ep={ep}: {e}")
        return RedirectResponse(url=FALLBACK_VIDEO)


@app.get("/proxy/yt/{key}")
def youtube_proxy(key: str):
    """
    Resolves a YouTube video key to a direct streamable URL using yt-dlp.
    media_kit can play these directly.
    """
    try:
        import subprocess, json
        result = subprocess.run(
            ["yt-dlp", "-f", "bestvideo[ext=mp4][height<=720]+bestaudio/best[ext=mp4]",
             "--get-url", f"https://www.youtube.com/watch?v={key}"],
            capture_output=True, text=True, timeout=10
        )
        url = result.stdout.strip().split("\n")[0]
        if url and url.startswith("http"):
            return RedirectResponse(url=url)
        # Fallback: return YouTube embed (works in WebView, not media_kit)
        return RedirectResponse(url=f"https://www.youtube.com/watch?v={key}")
    except Exception as e:
        logger.warning(f"yt-dlp proxy failed for {key}: {e}")
        return RedirectResponse(url=FALLBACK_VIDEO)
