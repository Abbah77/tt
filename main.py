import os
import math
import urllib.parse
import requests
from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from dotenv import load_dotenv

# Load environment variables from .env file immediately on boot
load_dotenv()

app = FastAPI(title="Reelz Native Scraper API", version="10.0.0")

# High-velocity response compression network layers
app.add_middleware(GZipMiddleware, minimum_size=256)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Core Configurations
TMDB_API_KEY = os.getenv("TMDB_API_KEY")

def sniper_movie_mapping(m):
    """Transforms raw TMDB metadata into your exact TikTok-feed structure"""
    movie_id = m.get("id")
    poster = m.get("poster_path")
    return {
        "id": movie_id,
        "title": m.get("title") or m.get("original_title") or "Untitled",
        "slug": str(movie_id),
        "thumbnail_url": f"https://image.tmdb.org/t/p/w500{poster}" if poster else "https://images.unsplash.com/photo-1440404653325-ab127d49abc1?q=80&w=500",
    }

@app.get("/")
def root():
    return {"status": "online", "engine": "Direct Multi-Source Scraper Engine v10.0"}

@app.get("/feed")
def feed(page: int = Query(1, ge=1)):
    """High-velocity discovery trends array feeding the main loop"""
    if not TMDB_API_KEY:
        raise HTTPException(status_code=500, detail="Configuration Key missing: TMDB_API_KEY")
        
    url = f"https://api.themoviedb.org/3/discover/movie?api_key={TMDB_API_KEY}&sort_by=popularity.desc&page={page}"
    try:
        response = requests.get(url, timeout=4).json()
        rows = response.get("results", [])
        return JSONResponse(content={
            "data": [sniper_movie_mapping(r) for r in rows],
            "next_page": page + 1 if len(rows) > 0 else None,
            "has_more": len(rows) > 0
        })
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/movie/{slug}")
def movie(slug: str):
    """
    The Core Sniper Matrix.
    Calculates episodic splits and passes absolute proxy URLs.
    """
    if not TMDB_API_KEY:
        raise HTTPException(status_code=500, detail="Configuration Key missing: TMDB_API_KEY")

    try:
        tmdb_url = f"https://api.themoviedb.org/3/movie/{slug}?api_key={TMDB_API_KEY}"
        movie_info = requests.get(tmdb_url, timeout=4).json()
        
        if "status_code" in movie_info and movie_info["status_code"] == 34:
            raise HTTPException(status_code=404, detail="Target missing from global index")
            
        runtime = movie_info.get("runtime", 120)  
        episode_length_mins = 5
        total_episodes = math.ceil(runtime / episode_length_mins)
        episodes_list = []
        
        PRODUCTION_DOMAIN = "https://tt-b577.onrender.com"
        
        for i in range(1, total_episodes + 1):
            start_seconds = (i - 1) * episode_length_mins * 60
            episodes_list.append({
                "id": i,
                "episode_number": i,
                "url": f"{PRODUCTION_DOMAIN}/api/stream/{slug}/ep{i}", 
                "seek_seconds": start_seconds
            })

        return {
            "id": movie_info.get("id"),
            "title": movie_info.get("title") or movie_info.get("original_title") or "Untitled",
            "thumbnail_url": f"https://image.tmdb.org/t/p/w500{movie_info.get('poster_path')}" if movie_info.get('poster_path') else "https://images.unsplash.com/photo-1440404653325-ab127d49abc1?q=80&w=500",
            "episodes": episodes_list,
            "total_episodes": total_episodes,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Sniper gateway failure: {str(e)}")

@app.get("/api/stream/{slug}/ep{ep}")
def stream_resolver(slug: str, ep: int):
    """
    The Extraction Core.
    Queries an open-source stream scraper to extract raw, unblocked media files 
    and redirects the native Flutter player right to the direct .m3u8 play target.
    """
    try:
        # Step 1: TMDB ID -> IMDb ID conversion
        lookup_url = f"https://api.themoviedb.org/3/movie/{slug}?api_key={TMDB_API_KEY}"
        tmdb_data = requests.get(lookup_url, timeout=3).json()
        imdb_id = tmdb_data.get("imdb_id")
        
        if not imdb_id or not str(imdb_id).startswith("tt"):
            ext_url = f"https://api.themoviedb.org/3/movie/{slug}/external_ids?api_key={TMDB_API_KEY}"
            ext_data = requests.get(ext_url, timeout=3).json()
            imdb_id = ext_data.get("imdb_id") or f"tt{slug}"

        # Step 2: Query an open source streamer scraper pipeline
        # Pulls direct media sources without proxy/HTML wrapper elements
        scraper_api_url = f"https://vidsrc-api-one.vercel.app/api/movie/{imdb_id}"
        
        headers = {"User-Agent": "Mozilla/5.0 (Linux; Android 10) AppleWebKit/537.36"}
        response = requests.get(scraper_api_url, headers=headers, timeout=5).json()
        
        # Look for direct stream array tracks
        sources = response.get("sources", [])
        if sources and len(sources) > 0:
            direct_m3u8_url = sources[0].get("url")
            if direct_m3u8_url:
                # Redirects your native player straight to the raw video path target
                return RedirectResponse(url=direct_m3u8_url, status_code=302)

        # Emergency Fallback Loop
        fallback_video = "https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/BigBuckBunny.mp4"
        return RedirectResponse(url=fallback_video, status_code=302)

    except Exception as e:
        fallback_video = "https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/BigBuckBunny.mp4"
        return RedirectResponse(url=fallback_video, status_code=302)

@app.get("/search")
def search(q: str = Query(..., min_length=1), page: int = Query(1, ge=1)):
    """Instant query text vector mapping global results"""
    if not TMDB_API_KEY:
        raise HTTPException(status_code=500, detail="Configuration Key missing: TMDB_API_KEY")

    url = f"https://api.themoviedb.org/3/search/movie?api_key={TMDB_API_KEY}&query={urllib.parse.quote(q)}&page={page}"
    try:
        response = requests.get(url, timeout=4).json()
        rows = response.get("results", [])
        return {"data": [sniper_movie_mapping(r) for r in rows]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
