import os
import math
import urllib.parse
import requests
from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from dotenv import load_dotenv
from supabase import create_client, Client

# Load environment variables from .env file immediately on boot
load_dotenv()

app = FastAPI(title="Reelz Sniper API", version="3.0.0")

# High-velocity response compression network layers
app.add_middleware(GZipMiddleware, minimum_size=256)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Configuration Variables
TMDB_API_KEY = os.getenv("1eef1496d59aa06f62e201ddce2741b4")
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
    Injects the direct trailer stream vector as Key 3 so your frontend 
    can autoplay it immediately when scrolling.
    """
    movie_id = m.get("id")
    poster = m.get("poster_path")
    
    return {
        "id": movie_id,
        "title": m.get("title") or m.get("original_title") or "Untitled",
        "slug": str(movie_id),
        "thumbnail_url": f"https://image.tmdb.org/t/p/w500{poster}" if poster else "https://images.unsplash.com/photo-1440404653325-ab127d49abc1?q=80&w=500",
        "trailer_url": f"{STREAM_BASE}/trailer/{movie_id}", # Key 3: Instant TikTok background engine
    }

@app.get("/")
def root():
    return {"status": "online", "engine": "High-Speed Sniper v3"}

@app.get("/feed")
def feed(page: int = Query(1, ge=1)):
    """High-velocity discovery array feeding your main application loop"""
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
    Simulates your old pre-split bucket architecture by using pure chronology math.
    """
    if not TMDB_API_KEY:
        raise HTTPException(status_code=500, detail="TMDB API Key missing from environment configurations.")

    try:
        # Pull core structural properties directly from worldwide asset registers
        tmdb_url = f"https://api.themoviedb.org/3/movie/{slug}?api_key={TMDB_API_KEY}"
        movie_info = requests.get(tmdb_url, timeout=4).json()
        
        if "status_code" in movie_info and movie_info["status_code"] == 34:
            raise HTTPException(status_code=404, detail="Target asset missing from global registry")
            
        runtime = movie_info.get("runtime", 120)  # Safe execution fallback
        master_stream = f"{STREAM_BASE}/movie/{slug}" # Key 4: Master file layout target

        # HIGH-SPEED CHRONOLOGICAL SLICING MATRIX
        # Instantly segments any full-length film into virtual 5-minute chunks in server memory
        episode_length_mins = 5
        total_episodes = math.ceil(runtime / episode_length_mins)
        episodes_list = []
        
        for i in range(1, total_episodes + 1):
            start_seconds = (i - 1) * episode_length_mins * 60
            episodes_list.append({
                "id": i,
                "episode_number": i,
                "url": master_stream,        # Raw stream endpoint for extraction
                "seek_seconds": start_seconds # Precise playback anchor offset
            })

        return {
            "movie": sniper_movie_mapping(movie_info),
            "episodes": episodes_list,
            "total_episodes": total_episodes,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Sniper mapping failure: {str(e)}")

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
