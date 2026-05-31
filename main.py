import os
import math
import logging
import urllib.parse
import requests
from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import RedirectResponse
from dotenv import load_dotenv

# Setup Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()

app = FastAPI(title="Reelz Server-Side Gateway", version="7.1.0")

# Middleware
app.add_middleware(GZipMiddleware, minimum_size=256)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Configs
TMDB_API_KEY = os.getenv("TMDB_API_KEY")
PRODUCTION_DOMAIN = os.getenv("PRODUCTION_DOMAIN", "https://onrender.com")
FALLBACK_URL = "https://googleapis.com"

def get_tmdb(endpoint: str, params: dict = None):
    params = params or {}
    params["api_key"] = TMDB_API_KEY
    url = f"https://themoviedb.org{endpoint}"
    response = requests.get(url, params=params, timeout=5)
    response.raise_for_status()
    return response.json()

def map_movie(m: dict) -> dict:
    poster = m.get("poster_path")
    return {
        "id": m.get("id"),
        "title": m.get("title") or m.get("original_title") or "Untitled",
        "thumbnail_url": f"https://tmdb.org{poster}" if poster else "https://unsplash.com",
    }

@app.get("/")
def root():
    return {"status": "online", "version": "7.1.0"}

@app.get("/feed")
def feed(page: int = Query(1, ge=1)):
    try:
        data = get_tmdb("discover/movie", {"sort_by": "popularity.desc", "page": page})
        rows = data.get("results", [])
        return {
            "data": [map_movie(r) for r in rows],
            "next_page": page + 1 if rows else None
        }
    except Exception as e:
        logger.error(f"Feed error: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch feed")

@app.get("/movie/{slug}")
def movie(slug: str):
    try:
        info = get_tmdb(f"movie/{slug}")
        runtime = info.get("runtime") or 120
        episodes = math.ceil(runtime / 5)
        return {
            "movie": map_movie(info),
            "episodes": [{"id": i, "url": f"{PRODUCTION_DOMAIN}/api/stream/{slug}/ep{i}"} for i in range(1, episodes + 1)]
        }
    except Exception as e:
        logger.error(f"Movie detail error: {e}")
        raise HTTPException(status_code=404, detail="Movie not found")

@app.get("/api/stream/{slug}/ep{ep}")
def stream_resolver(slug: str, ep: int):
    try:
        # 1. Get IMDb ID
        tmdb_data = get_tmdb(f"movie/{slug}")
        imdb_id = tmdb_data.get("imdb_id")
        if not imdb_id:
            ext = get_tmdb(f"movie/{slug}/external_ids")
            imdb_id = ext.get("imdb_id") or f"tt{slug}"

        # 2. Query VidLink
        vidlink_url = f"https://vidlink.pro{imdb_id}"
        
        # FIX: Stopped requests from chasing the stream asset internally 
        res = requests.get(vidlink_url, timeout=5, allow_redirects=False)
        
        # 3. Handle VidLink's structural response layouts
        # Match case A: API replies with direct redirect status code
        if res.status_code in [301, 302, 307, 308]:
            redirect_target = res.headers.get("Location")
            if redirect_target:
                return RedirectResponse(url=redirect_target, status_code=302)

        # Match case B: API replies with standard status code and text/json payload
        if res.status_code == 200:
            try:
                json_data = res.json()
                stream_url = json_data.get("stream_url")
                if stream_url: 
                    return RedirectResponse(url=stream_url, status_code=302)
            except Exception:
                # Fallback if 200 payload happens to be an unparsed text block of the stream URL
                if res.text and res.text.startswith("http"):
                    return RedirectResponse(url=res.text.strip(), status_code=302)
            
        return RedirectResponse(url=FALLBACK_URL, status_code=302)
    except Exception as e:
        logger.error(f"Stream resolve error: {e}")
        return RedirectResponse(url=FALLBACK_URL, status_code=302)
