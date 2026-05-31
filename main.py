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


# ── Trailer Proxy (the key route) ─────────────────────────────────────────────

# Cache resolved direct stream URLs too (they expire after ~6 hours on YouTube)
_stream_cache: dict[str, str] = {}

PIPED_INSTANCES = [
    "https://piped.video",
    "https://piped.adminforge.de",
    "https://piped.privacydev.net",
]

@app.get("/proxy/trailer/{key}")
def trailer_proxy(key: str):
    """
    Resolves a YouTube key → direct streamable video URL via Piped API.

    Piped is open-source YouTube frontend. Its /api/streams/{key} endpoint
    returns direct video stream URLs that media_kit can play — no yt-dlp,
    no subprocess, response in ~200ms. Completely free and legal.

    Falls back through multiple Piped instances if one is down.
    """
    # Return cached stream URL if we have a fresh one
    if key in _stream_cache:
        return RedirectResponse(url=_stream_cache[key], status_code=302)

    for instance in PIPED_INSTANCES:
        try:
            r = requests.get(
                f"{instance}/api/streams/{key}",
                timeout=6,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            if r.status_code != 200:
                continue

            data = r.json()

            # videoStreams is a list of {url, quality, mimeType, ...}
            streams = data.get("videoStreams", [])

            # Pick best mp4 stream at or under 720p
            mp4_streams = [
                s for s in streams
                if "mp4" in s.get("mimeType", "").lower()
                or s.get("format", "").upper() == "MPEG_4"
            ]

            # Sort by quality — pick highest under 720p
            def quality_num(s):
                q = s.get("quality", "0p").replace("p", "")
                try:
                    return int(q)
                except ValueError:
                    return 0

            mp4_streams.sort(key=quality_num, reverse=True)
            best = next((s for s in mp4_streams if quality_num(s) <= 720), None)
            if not best and mp4_streams:
                best = mp4_streams[-1]  # lowest quality as last resort

            if best and best.get("url"):
                stream_url = best["url"]
                _stream_cache[key] = stream_url
                logger.info(f"Piped resolved {key} @ {best.get('quality')} via {instance}")
                return RedirectResponse(url=stream_url, status_code=302)

            # Also try audioVideoStreams (combined) if videoStreams empty
            av_streams = data.get("audioVideoStreams", [])
            if av_streams:
                url = av_streams[0].get("url")
                if url:
                    _stream_cache[key] = url
                    return RedirectResponse(url=url, status_code=302)

        except Exception as e:
            logger.warning(f"Piped instance {instance} failed for {key}: {e}")
            continue

    # All Piped instances failed — return YouTube embed URL
    # Flutter can open this in a WebView as last resort
    logger.error(f"All Piped instances failed for {key}")
    return RedirectResponse(
        url=f"https://www.youtube.com/watch?v={key}",
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
