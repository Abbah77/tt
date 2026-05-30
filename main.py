import os
import math
import urllib.parse
import requests
from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from dotenv import load_dotenv

# Load environment variables from .env file immediately on boot
load_dotenv()

app = FastAPI(title="Reelz Native Proxy API", version="8.0.0")

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
    return {"status": "online", "engine": "Direct Stream Proxy Engine v8.0"}

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
    Pipes streaming URLs directly back to our server proxy.
    Flutter reads this as a normal data link, completely native.
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
                # Appends a pseudo-extension (.mp4) so the native Flutter video player
                # recognizes it instantly as a valid playable media target
                "url": f"{PRODUCTION_DOMAIN}/api/stream/{slug}/ep{i}/video.mp4", 
                "seek_seconds": start_seconds
            })

        return {
            "movie": sniper_movie_mapping(movie_info),
            "episodes": episodes_list,
            "total_episodes": total_episodes,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Sniper gateway failure: {str(e)}")

@app.get("/api/stream/{slug}/ep{ep}/video.mp4")
def stream_proxy(slug: str, ep: int):
    """
    The Live Stream Proxy Pipeline.
    Intercepts Flutter's request, resolves the source target, 
    and pipes raw stream bytes directly back to the app without redirects.
    """
    fallback_video = "https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/BigBuckBunny.mp4"
    video_source_url = fallback_video

    try:
        # Step 1: TMDB ID -> IMDb ID conversion
        lookup_url = f"https://api.themoviedb.org/3/movie/{slug}?api_key={TMDB_API_KEY}"
        tmdb_data = requests.get(lookup_url, timeout=3).json()
        imdb_id = tmdb_data.get("imdb_id")
        
        if not imdb_id or not str(imdb_id).startswith("tt"):
            ext_url = f"https://api.themoviedb.org/3/movie/{slug}/external_ids?api_key={TMDB_API_KEY}"
            ext_data = requests.get(ext_url, timeout=3).json()
            imdb_id = ext_data.get("imdb_id") or f"tt{slug}"

        # Step 2: Grab the direct file location from the streaming network provider
        vidlink_api_endpoint = f"https://vidlink.pro/api/movie/{imdb_id}"
        spoofed_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://vidlink.pro/",
        }
        
        api_response = requests.get(vidlink_api_endpoint, headers=spoofed_headers, timeout=4).json()
        raw_video_url = api_response.get("stream_url")
        
        if raw_video_url:
            video_source_url = raw_video_url

    except Exception as e:
        print(f"Proxy link mapping falling back: {e}")
        pass

    # Step 3: Stream bytes directly down the pipe into Flutter
    def video_stream_generator():
        with requests.get(video_source_url, stream=True, timeout=10) as r:
            r.raise_for_status()
            for chunk in r.iter_content(chunk_size=1024 * 64): # High-speed 64KB chunks
                yield chunk

    return StreamingResponse(video_stream_generator(), media_type="video/mp4")

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
