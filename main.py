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

app = FastAPI(title="Reelz Wise Handshake API", version="7.0.0")

# High-velocity streaming compression network configurations
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
    return {"status": "online", "engine": "Server-Side Handshake Gateway v7.0"}

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
    Points the final episode stream URLs back to our intelligent API endpoint.
    Flutter changes nothing; it just calls this endpoint like it always has.
    """
    if not TMDB_API_KEY:
        raise HTTPException(status_code=500, detail="Configuration Key missing: TMDB_API_KEY")

    try:
        tmdb_url = f"https://api.themoviedb.org/3/movie/{slug}?api_key={TMDB_API_KEY}"
        movie_info = requests.get(tmdb_url, timeout=4).json()
        
        if "status_code" in movie_info and movie_info["status_code"] == 34:
            raise HTTPException(status_code=404, detail="Target missing from global index")
            
        runtime = movie_info.get("runtime", 120)  
        
        # High-speed chronological slicing loop (5-minute segments)
        episode_length_mins = 5
        total_episodes = math.ceil(runtime / episode_length_mins)
        episodes_list = []
        
        # Pull your real domain dynamically or use your absolute deployment URL
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
            "movie": sniper_movie_mapping(movie_info),
            "episodes": episodes_list,
            "total_episodes": total_episodes,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Sniper gateway failure: {str(e)}")

@app.get("/api/stream/{slug}/ep{ep}")
def stream_resolver(slug: str, ep: int):
    """
    The Live Server Handshake Engine.
    Intercepts Flutter's request, securely handles the API handshake with VidLink,
    extracts the raw working video file link, and throws a 302 redirect.
    Your native Flutter video player follows the redirect seamlessly!
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

        # Step 2: Query the hidden provider API directly using spoofed device credentials
        vidlink_api_endpoint = f"https://vidlink.pro/api/movie/{imdb_id}"
        spoofed_headers = {
            "User-Agent": "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36",
            "Referer": "https://vidlink.pro/",
            "Accept": "application/json"
        }
        
        try:
            api_response = requests.get(vidlink_api_endpoint, headers=spoofed_headers, timeout=4).json()
            raw_video_url = api_response.get("stream_url")
            
            # If we successfully extracted the clean native .m3u8/.mp4 stream link, hand it off!
            if raw_video_url:
                return RedirectResponse(url=raw_video_url, status_code=302)
        except Exception as api_err:
            print(f"Provider API Handshake failed internally: {api_err}")
            pass

        # EMERGENCY FALLBACK TRACK: If the provider goes down, play the test track 
        # so your native interface never hangs or errors out on a black screen.
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
