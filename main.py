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
PRODUCTION_DOMAIN = os.getenv("PRODUCTION_DOMAIN", "https://tt-b577.onrender.com")
FALLBACK_URL = "https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/BigBuckBunny.mp4"

def get_tmdb(endpoint: str, params: dict = None):
    params = params or {}
    params["api_key"] = TMDB_API_KEY
    url = f"https://api.themoviedb.org/3/{endpoint}"
    response = requests.get(url, params=params, timeout=5)
    response.raise_for_status()
    return response.json()

def map_movie(m: dict) -> dict:
    poster = m.get("poster_path")
    return {
        "id": m.get("id"),
        "title": m.get("title") or m.get("original_title") or "Untitled",
        "thumbnail_url": f"https://image.tmdb.org/t/p/w500{poster}" if poster else "https://images.unsplash.com/photo-1440404653325-ab127d49abc1?q=80&w=500",
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
        # Get IMDb ID
        tmdb_data = get_tmdb(f"movie/{slug}")
        imdb_id = tmdb_data.get("imdb_id")
        if not imdb_id:
            ext = get_tmdb(f"movie/{slug}/external_ids")
            imdb_id = ext.get("imdb_id") or f"tt{slug}"

        # Fetch from VidLink
        res = requests.get(f"https://vidlink.pro/api/movie/{imdb_id}", timeout=5)
        if res.status_code == 200:
            url = res.json().get("stream_url")
            if url: return RedirectResponse(url=url)
            
        return RedirectResponse(url=FALLBACK_URL)
    except Exception as e:
        logger.error(f"Stream resolve error: {e}")
        return RedirectResponse(url=FALLBACK_URL)
