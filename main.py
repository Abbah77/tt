import os
import logging
import time
import requests
from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from dotenv import load_dotenv
from typing import Optional, List, Dict, Any

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
load_dotenv()

app = FastAPI(title="Reelz Gateway - Heiermuer Edition", version="12.0.0")
app.add_middleware(GZipMiddleware, minimum_size=256)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Config ────────────────────────────────────────────────────────────────────
HEIERMUER_API = "https://json.heimuer.tv/api.php/provide/vod/"
SHORT_DRAMA_FALLBACK = "http://74.120.175.78/JK/XYQTVBox/dj.json"
FALLBACK_THUMB = "https://images.unsplash.com/photo-1440404653325-ab127d49abc1?q=80&w=500"

# Cache
_drama_cache: Dict[str, Any] = {}
CACHE_TTL = 300  # 5 minutes


# ── Heiermuer.tv Content Fetcher with Error Handling ──────────────────────────

def fetch_heimuer_catalog(page: int = 1, limit: int = 20) -> List[Dict]:
    """Fetch catalog from Heiermuer API with full error handling"""
    try:
        params = {
            "ac": "list",
            "pg": page,
            "t": "short_drama"  # Try to filter for short dramas
        }
        
        logger.info(f"Calling Heiermuer API: {HEIERMUER_API} with params {params}")
        
        response = requests.get(
            HEIERMUER_API,
            params=params,
            timeout=15,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "application/json"
            }
        )
        
        logger.info(f"Heiermuer response status: {response.status_code}")
        
        if response.status_code != 200:
            logger.error(f"Heiermuer returned {response.status_code}")
            return []
        
        # Try to parse JSON
        try:
            data = response.json()
        except Exception as json_err:
            logger.error(f"Failed to parse JSON: {json_err}")
            logger.error(f"Response text preview: {response.text[:500]}")
            return []
        
        logger.info(f"Response keys: {data.keys() if isinstance(data, dict) else 'not a dict'}")
        
        # Check response structure
        if isinstance(data, dict):
            # Some APIs return {"code": 1, "list": [...]}
            if data.get("code") == 1:
                items = data.get("list", [])
            elif "list" in data:
                items = data.get("list", [])
            elif "data" in data:
                items = data.get("data", [])
            else:
                # Maybe the whole response is the list?
                items = []
                logger.warning(f"Unknown response structure: {list(data.keys())}")
        elif isinstance(data, list):
            items = data
        else:
            logger.error(f"Unexpected response type: {type(data)}")
            return []
        
        if not items:
            logger.warning("No items found in response")
            return []
        
        dramas = []
        for item in items[:limit]:
            # Safely extract fields
            vod_id = item.get("vod_id") or item.get("id")
            vod_name = item.get("vod_name") or item.get("name") or item.get("title", "Untitled")
            vod_pic = item.get("vod_pic") or item.get("pic") or item.get("thumbnail") or FALLBACK_THUMB
            vod_play_url = item.get("vod_play_url") or item.get("play_url") or item.get("url", "")
            
            # Extract m3u8 from play URL
            m3u8_url = extract_m3u8_from_play_url(vod_play_url)
            
            dramas.append({
                "id": str(vod_id) if vod_id else f"item_{len(dramas)}",
                "slug": str(vod_id) if vod_id else f"item_{len(dramas)}",
                "title": vod_name,
                "thumbnail_url": vod_pic if vod_pic.startswith("http") else FALLBACK_THUMB,
                "description": item.get("vod_content") or item.get("description", ""),
                "year": item.get("vod_year") or item.get("year", ""),
                "rating": item.get("vod_rating") or item.get("rating", 0),
                "m3u8_url": m3u8_url,
                "episodes": extract_episodes(vod_play_url)
            })
        
        logger.info(f"Successfully parsed {len(dramas)} dramas")
        return dramas
        
    except requests.exceptions.Timeout:
        logger.error("Heiermuer API timeout")
        return []
    except requests.exceptions.ConnectionError:
        logger.error("Heiermuer API connection error")
        return []
    except Exception as e:
        logger.error(f"Unexpected error in fetch_heimuer_catalog: {e}")
        return []


def extract_m3u8_from_play_url(play_url: str) -> Optional[str]:
    """Extract the first m3u8 URL from vod_play_url field"""
    if not play_url:
        return None
    
    # Handle different formats
    if ".m3u8" in play_url:
        # Direct URL
        return play_url
    
    # Format: "episode1$m3u8_url#episode2$m3u8_url"
    if "$" in play_url:
        parts = play_url.split("#")
        for part in parts:
            if "$" in part and ".m3u8" in part:
                url = part.split("$")[1] if len(part.split("$")) > 1 else part
                if ".m3u8" in url:
                    return url
    
    return None


def extract_episodes(play_url: str) -> List[Dict]:
    """Extract all episodes from vod_play_url"""
    episodes = []
    if not play_url:
        return [{"id": 1, "episode_number": 1, "title": "Episode 1", "url": ""}]
    
    if "$" in play_url:
        parts = play_url.split("#")
        for idx, part in enumerate(parts, 1):
            if "$" in part:
                parts_split = part.split("$", 1)
                title = parts_split[0] if len(parts_split) > 0 else f"Episode {idx}"
                url = parts_split[1] if len(parts_split) > 1 else ""
                episodes.append({
                    "id": idx,
                    "episode_number": idx,
                    "title": title,
                    "url": url if ".m3u8" in url else None
                })
    elif ".m3u8" in play_url:
        episodes.append({
            "id": 1,
            "episode_number": 1,
            "title": "Episode 1",
            "url": play_url
        })
    
    return episodes if episodes else [{"id": 1, "episode_number": 1, "title": "Episode 1", "url": None}]


def fetch_short_drama_fallback() -> List[Dict]:
    """Fallback to TVBox short drama interface"""
    try:
        logger.info("Attempting fallback source...")
        response = requests.get(SHORT_DRAMA_FALLBACK, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        dramas = []
        if isinstance(data, list):
            for idx, item in enumerate(data[:20]):
                dramas.append({
                    "id": str(item.get("vod_id", idx)),
                    "slug": str(item.get("vod_id", idx)),
                    "title": item.get("vod_name", f"Drama {idx+1}"),
                    "thumbnail_url": item.get("vod_pic", FALLBACK_THUMB),
                    "description": item.get("vod_content", ""),
                    "m3u8_url": item.get("vod_play_url", ""),
                    "episodes": [{"id": 1, "episode_number": 1, "title": "Episode 1", "url": item.get("vod_play_url", "")}]
                })
        
        logger.info(f"Fallback returned {len(dramas)} dramas")
        return dramas
    except Exception as e:
        logger.error(f"Fallback failed: {e}")
        return []


# ── API Endpoints ──────────────────────────────────────────────────────────────

@app.get("/api/dramas")
def get_dramas(
    page: int = Query(1, ge=1),
    limit: int = Query(20, le=50),
    use_fallback: bool = Query(False)
):
    """Get drama catalog with title, thumbnail, and m3u8 URLs"""
    try:
        # Try Heiermuer first
        dramas = fetch_heimuer_catalog(page=page, limit=limit)
        
        # If failed and fallback enabled, try TVBox source
        if (not dramas or len(dramas) == 0) and use_fallback:
            logger.info("Heiermuer returned no results, trying fallback")
            dramas = fetch_short_drama_fallback()
        
        if not dramas or len(dramas) == 0:
            # Return mock data for testing
            logger.warning("No real content available, returning mock data for testing")
            return {
                "data": get_mock_dramas(),
                "page": page,
                "has_more": False,
                "source": "mock",
                "warning": "Using mock data. Heiermuer API may be down or changed."
            }
        
        return {
            "data": dramas,
            "page": page,
            "has_more": len(dramas) == limit,
            "source": "heimuer" if not use_fallback else "fallback"
        }
        
    except Exception as e:
        logger.error(f"Dramas endpoint error: {e}")
        # Return mock data instead of 500 error
        return {
            "data": get_mock_dramas(),
            "page": page,
            "has_more": False,
            "source": "mock",
            "error": str(e),
            "warning": "Using mock data due to API error"
        }


def get_mock_dramas() -> List[Dict]:
    """Return mock data for testing when real API fails"""
    return [
        {
            "id": "1",
            "slug": "1",
            "title": "Test Drama 1 - The Heist",
            "thumbnail_url": "https://image.tmdb.org/t/p/w500/8cdWjvZc6MjVQrnT6tFkEVcR9nU.jpg",
            "description": "A test drama for your app",
            "m3u8_url": "https://test-streams.mux.dev/x36xhzz/x36xhzz.m3u8",
            "episodes": [
                {"id": 1, "episode_number": 1, "title": "Episode 1", "url": "https://test-streams.mux.dev/x36xhzz/x36xhzz.m3u8"},
                {"id": 2, "episode_number": 2, "title": "Episode 2", "url": "https://test-streams.mux.dev/x36xhzz/x36xhzz.m3u8"}
            ]
        },
        {
            "id": "2",
            "slug": "2",
            "title": "Test Drama 2 - Lost in Tokyo",
            "thumbnail_url": "https://image.tmdb.org/t/p/w500/wwemzKWzjKYJFfCeiB57q3r4Bcm.png",
            "description": "Another test drama",
            "m3u8_url": "https://test-streams.mux.dev/x36xhzz/x36xhzz.m3u8",
            "episodes": [
                {"id": 1, "episode_number": 1, "title": "Episode 1", "url": "https://test-streams.mux.dev/x36xhzz/x36xhzz.m3u8"}
            ]
        }
    ]


@app.get("/api/drama/{drama_id}")
def get_drama_detail(drama_id: str):
    """Get detailed drama info including all episodes with m3u8 URLs"""
    try:
        params = {"ac": "detail", "ids": drama_id}
        response = requests.get(HEIERMUER_API, params=params, timeout=10)
        
        if response.status_code != 200:
            # Return mock detail
            return get_mock_drama_detail(drama_id)
        
        data = response.json()
        
        if isinstance(data, dict):
            items = data.get("list", [])
            if items:
                item = items[0]
                return {
                    "id": drama_id,
                    "title": item.get("vod_name", "Untitled"),
                    "thumbnail_url": item.get("vod_pic", FALLBACK_THUMB),
                    "description": item.get("vod_content", ""),
                    "year": item.get("vod_year", ""),
                    "rating": item.get("vod_rating", 0),
                    "episodes": extract_episodes(item.get("vod_play_url", "")),
                    "total_episodes": len(extract_episodes(item.get("vod_play_url", "")))
                }
        
        return get_mock_drama_detail(drama_id)
        
    except Exception as e:
        logger.error(f"Drama detail error: {e}")
        return get_mock_drama_detail(drama_id)


def get_mock_drama_detail(drama_id: str) -> Dict:
    """Return mock drama detail"""
    return {
        "id": drama_id,
        "title": f"Test Drama {drama_id}",
        "thumbnail_url": FALLBACK_THUMB,
        "description": "This is mock data while the real API is being configured.",
        "year": "2024",
        "rating": 8.5,
        "episodes": [
            {"id": 1, "episode_number": 1, "title": "Episode 1", "url": "https://test-streams.mux.dev/x36xhzz/x36xhzz.m3u8"},
            {"id": 2, "episode_number": 2, "title": "Episode 2", "url": "https://test-streams.mux.dev/x36xhzz/x36xhzz.m3u8"}
        ],
        "total_episodes": 2
    }


@app.get("/api/stream/{drama_id}/{episode_id}")
def get_stream_url(drama_id: str, episode_id: int):
    """Get fresh m3u8 URL for a specific episode"""
    # Return test stream URL
    return {
        "url": "https://test-streams.mux.dev/x36xhzz/x36xhzz.m3u8",
        "expires_in": 300,
        "warning": "Using test stream. Real streams will come from Heiermuer when available."
    }


@app.get("/health")
def health():
    """Health check endpoint"""
    # Test Heiermuer connection
    try:
        response = requests.get(HEIERMUER_API, params={"ac": "list", "pg": 1}, timeout=5)
        heimuer_status = "up" if response.status_code == 200 else f"down ({response.status_code})"
    except Exception as e:
        heimuer_status = f"down: {str(e)[:50]}"
    
    return {
        "status": "online",
        "version": "12.0.0",
        "content_source": "Heiermuer.tv (with mock fallback)",
        "heimuer_api": heimuer_status,
        "mock_data_enabled": True,
        "endpoints": {
            "/api/dramas": "Get drama catalog",
            "/api/drama/{id}": "Get drama details",
            "/api/stream/{id}/{ep}": "Get stream URL"
        }
    }


@app.get("/")
def root():
    return {
        "status": "online",
        "version": "12.0.0",
        "message": "Backend is running. Use /api/dramas to get content.",
        "docs": "/docs"
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
