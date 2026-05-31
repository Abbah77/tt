import os
import math
import urllib.parse
import requests
import yt_dlp
from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from dotenv import load_dotenv

# Load local environment variables automatically during boot stage
load_dotenv()

app = FastAPI(title="Reelz Wise Handshake API", version="9.0.0")

# High-velocity streaming compression network configurations
app.add_middleware(GZipMiddleware, minimum_size=256)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Core Configurations fetched straight from environment variables
TMDB_API_KEY = os.getenv("TMDB_API_KEY")
PRODUCTION_DOMAIN = os.getenv("PRODUCTION_DOMAIN", "https://onrender.com")
PROXY_URL = os.getenv("PROXY_URL")  # Populated via your Webshare credential string

def sniper_movie_mapping(m: dict) -> dict:
    """Transforms raw TMDB metadata into your exact TikTok-feed structure"""
    movie_id = m.get("id")
    poster = m.get("poster_path")
    return {
        "id": movie_id,
        "title": m.get("title") or m.get("original_title") or "Untitled",
        "slug": str(movie_id),
        "thumbnail_url": f"https://tmdb.org{poster}" if poster else "https://unsplash.com",
    }

@app.get("/")
def root():
    return {"status": "online", "engine": "Reelz Independent Extraction Gateway v9.0"}

@app.get("/feed")
def feed(page: int = Query(1, ge=1)):
    """High-velocity discovery trends array feeding the main loop"""
    if not TMDB_API_KEY:
        raise HTTPException(status_code=500, detail="Configuration Key missing: TMDB_API_KEY")
        
    url = f"https://themoviedb.org{TMDB_API_KEY}&sort_by=popularity.desc&page={page}"
    try:
        response = requests.get(url, timeout=5)
        response.raise_for_status()
        rows = response.json().get("results", [])
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
    """
    if not TMDB_API_KEY:
        raise HTTPException(status_code=500, detail="Configuration Key missing: TMDB_API_KEY")

    try:
        tmdb_url = f"https://themoviedb.org{slug}?api_key={TMDB_API_KEY}"
        response = requests.get(tmdb_url, timeout=5)
        movie_info = response.json()
        
        if "status_code" in movie_info and movie_info["status_code"] == 34:
            raise HTTPException(status_code=404, detail="Target missing from global index")
            
        runtime = movie_info.get("runtime", 120)  
        if not runtime or runtime == 0:
            runtime = 120
        
        # High-speed chronological slicing loop (5-minute segments)
        episode_length_mins = 5
        total_episodes = math.ceil(runtime / episode_length_mins)
        episodes_list = []
        
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
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Sniper gateway failure: {str(e)}")

@app.get("/api/stream/{slug}/ep{ep}")
def stream_resolver(slug: str, ep: int):
    """
    The Live Independent Extraction Engine.
    Resolves targets using internal yt-dlp search indexes via a resident proxy.
    """
    fallback_video = "https://googleapis.com"
    
    try:
        # Step 1: Use TMDB metadata to construct a highly specific title query
        lookup_url = f"https://themoviedb.org{slug}?api_key={TMDB_API_KEY}"
        tmdb_res = requests.get(lookup_url, timeout=4)
        if tmdb_res.status_code != 200:
            print(f"[-] TMDB Metadata Lookup rejected for slug: {slug}")
            return RedirectResponse(url=fallback_video, status_code=302)
            
        tmdb_data = tmdb_res.json()
        title = tmdb_data.get("title")
        release_date = tmdb_data.get("release_date", "")
        year = release_date.split("-")[0] if "-" in release_date else ""

        if not title:
            return RedirectResponse(url=fallback_video, status_code=302)

        # Build clean search query token
        search_query = f"ytsearch1:{title} {year} full movie"
        print(f"[+] Launching independent extraction vector for: {search_query}")

        # Step 2: Configure broad-extraction compatibility options
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'skip_download': True,  # Ensures server resource load stays at zero
            'http_headers': {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
                'Accept': '*/*',
            }
        }

        # Apply your Webshare datacenter/residential tracking node if configured
        if PROXY_URL:
            ydl_opts['proxy'] = PROXY_URL

        raw_stream_url = None

        # Step 3: Run safe inner extraction thread
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(search_query, download=False)
            
            # Unpack the nested list entries generated by search queries
            if info and 'entries' in info and len(info['entries']) > 0:
                target_entry = info['entries'][0]
                
                # Sift out adaptive manifests (.m3u8), direct paths (.mp4), or flat structures
                raw_stream_url = target_entry.get('url') or target_entry.get('manifest_url')
                
                # Look deep inside format arrays if primary parameter resolution yields empty values
                if not raw_stream_url and 'formats' in target_entry and len(target_entry['formats']) > 0:
                    raw_stream_url = target_entry['formats'][-1].get('url')
            
            # Direct target index mapping fallback
            elif info and 'url' in info:
                raw_stream_url = info.get('url')

        # Step 4: Issue an efficient 302 Redirect payload right back to the client player
        if raw_stream_url:
            print(f"[+] Match Resolved. Issuing 302 Redirect to stream target: {raw_stream_url[:60]}...")
            return RedirectResponse(url=raw_stream_url, status_code=302)
            
        print("[-] Resolution complete but zero playable targets returned. Utilizing safe fallback video.")
        return RedirectResponse(url=fallback_video, status_code=302)

    except Exception as e:
        print(f"[!] Critical System Engine Extraction Failure: {e}")
        return RedirectResponse(url=fallback_video, status_code=302)

@app.get("/search")
def search(q: str = Query(..., min_length=1), page: int = Query(1, ge=1)):
    """Instant query text vector mapping global results"""
    if not TMDB_API_KEY:
        raise HTTPException(status_code=500, detail="Configuration Key missing: TMDB_API_KEY")

    url = f"https://themoviedb.org{TMDB_API_KEY}&query={urllib.parse.quote(q)}&page={page}"
    try:
        response = requests.get(url, timeout=5).json()
        rows = response.get("results", [])
        return {"data": [sniper_movie_mapping(r) for r in rows]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
