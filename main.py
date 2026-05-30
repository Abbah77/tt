import os
import math
import urllib.parse
import requests
from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from dotenv import load_dotenv
from supabase import create_client, Client

# Load environment variables from .env file immediately on boot
load_dotenv()

app = FastAPI(title="Reelz Sniper API", version="3.6.0")

# High-velocity response compression network layers
app.add_middleware(GZipMiddleware, minimum_size=256)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Configuration Variables
TMDB_API_KEY = os.getenv("TMDB_API_KEY")
STREAM_BASE = "https://vidlink.pro"

# Optional: Initialize Supabase client if credentials exist in your environment
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase: Client = None

if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    except Exception as e:
        print(f"Warning: Supabase client initialization failed: {e}")

def sniper_movie_mapping(m):
    """
    Transforms global metadata into your instant TikTok-feed layout.
    """
    movie_id = m.get("id")
    poster = m.get("poster_path")
    
    return {
        "id": movie_id,
        "title": m.get("title") or m.get("original_title") or "Untitled",
        "slug": str(movie_id),
        "thumbnail_url": f"https://image.tmdb.org/t/p/w500{poster}" if poster else "https://images.unsplash.com/photo-1440404653325-ab127d49abc1?q=80&w=500",
        "trailer_url": f"{STREAM_BASE}/trailer/{movie_id}", 
    }

@app.get("/")
def root():
    return {"status": "online", "engine": "High-Speed Sniper v3.6 - IMDb ID Bridge Active"}

@app.get("/feed")
def feed(page: int = Query(1, ge=1)):
    """Discovery feed array using standard TMDB data"""
    if not TMDB_API_KEY:
        raise HTTPException(status_code=500, detail="TMDB API Key missing from environment configurations.")
        
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
        raise HTTPException(status_code=500, detail=f"Feed sniper failure: {str(e)}")

@app.get("/movie/{slug}")
def movie(slug: str):
    """
    The Core Sniper Calculation Engine.
    Keeps everything running on your 5-minute custom episodic split math.
    """
    if not TMDB_API_KEY:
        raise HTTPException(status_code=500, detail="TMDB API Key missing from environment configurations.")

    try:
        tmdb_url = f"https://api.themoviedb.org/3/movie/{slug}?api_key={TMDB_API_KEY}"
        movie_info = requests.get(tmdb_url, timeout=4).json()
        
        if "status_code" in movie_info and movie_info["status_code"] == 34:
            raise HTTPException(status_code=404, detail="Target asset missing from global registry")
            
        runtime = movie_info.get("runtime", 120)  
        
        # HIGH-SPEED CHRONOLOGICAL SLICING MATRIX
        episode_length_mins = 5
        total_episodes = math.ceil(runtime / episode_length_mins)
        episodes_list = []
        
        for i in range(1, total_episodes + 1):
            start_seconds = (i - 1) * episode_length_mins * 60
            episodes_list.append({
                "id": i,
                "episode_number": i,
                "url": f"/api/stream/{slug}/ep{i}", 
                "seek_seconds": start_seconds
            })

        return {
            "movie": sniper_movie_mapping(movie_info),
            "episodes": episodes_list,
            "total_episodes": total_episodes,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Sniper mapping failure: {str(e)}")

@app.get("/api/stream/{slug}/ep{ep}")
def stream_resolver(slug: str, ep: int):
    """
    The IMDb ID Bridge Gateway.
    Takes your TMDB numerical ID, fetches the 'ttXXXXXX' IMDb counterpart,
    and forwards it directly to VidLink so the video plays instantly.
    """
    try:
        # 1. Ask TMDB for the external IDs mapping link
        id_lookup_url = f"https://api.themoviedb.org/3/movie/{slug}?api_key={TMDB_API_KEY}"
        tmdb_data = requests.get(id_lookup_url, timeout=3).json()
        
        # Pull the standard alphanumeric IMDb tracking identifier
        imdb_id = tmdb_data.get("imdb_id")
        
        # If the lookup fails, try fallback string method
        if not imdb_id or not str(imdb_id).startswith("tt"):
            # Fall back to checking embedded sub-dictionary configurations
            external_url = f"https://api.themoviedb.org/3/movie/{slug}/external_ids?api_key={TMDB_API_KEY}"
            ext_data = requests.get(external_url, timeout=3).json()
            imdb_id = ext_data.get("imdb_id")

        # 2. Forward the valid IMDb ID directly to VidLink's parser engine
        if imdb_id:
            vidlink_api = f"{STREAM_BASE}/api/movie/{imdb_id}"
            try:
                api_response = requests.get(vidlink_api, timeout=3).json()
                if api_response.get("stream_url"):
                    return RedirectResponse(url=api_response["stream_url"], status_code=302)
            except:
                pass

        # EMERGENCY FALLBACK: If video doesn't exist on provider servers, play test loop 
        # so your components never hang on a dead black layout frame.
        fallback_video = "https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/BigBuckBunny.mp4"
        return RedirectResponse(url=fallback_video, status_code=302)

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/search")
def search(q: str = Query(..., min_length=1), page: int = Query(1, ge=1)):
    """Instant text query search vector querying global asset networks"""
    if not TMDB_API_KEY:
        raise HTTPException(status_code=500, detail="TMDB API Key missing from environment configurations.")

    url = f"https://api.themoviedb.org/3/search/movie?api_key={TMDB_API_KEY}&query={urllib.parse.quote(q)}&page={page}"
    try:
        response = requests.get(url, timeout=4).json()
        rows = response.get("results", [])
        return {"data": [sniper_movie_mapping(r) for r in rows]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
