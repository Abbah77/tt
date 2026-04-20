from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from supabase import create_client
import os
import random
from dotenv import load_dotenv

load_dotenv()

app = FastAPI()

app.add_middleware(GZipMiddleware, minimum_size=500)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

supabase = create_client(
    os.getenv("SUPABASE_URL"),
    os.getenv("SUPABASE_KEY")
)

CHUNK_SIZE = 20
PRELOAD_CHUNKS = 3
TOTAL_VIDEOS = 6129  # update this if you ingest more later

def swap_to_fast_url(video_url: str) -> str:
    if not video_url:
        return video_url
    if video_url.endswith('.ia.mp4'):
        return video_url
    return video_url.replace('.mp4', '.ia.mp4')

def clean_video(video: dict) -> dict:
    return {
        "id": video.get("id"),
        "video_url": swap_to_fast_url(video.get("video_url", "")),
        "thumbnail_url": video.get("thumbnail_url"),
        "caption": video.get("caption", ""),
        "hashtags": video.get("hashtags", ""),
    }

def get_random_offsets(count: int) -> list:
    """Generate random unique row offsets"""
    max_offset = TOTAL_VIDEOS - 1
    offsets = random.sample(range(0, max_offset), min(count, max_offset))
    return offsets

@app.get("/")
def root():
    return {"status": "ok", "message": "API is running 🔥"}

@app.get("/feed")
def get_feed(page: int = Query(default=1, ge=1)):

    # How many videos to fetch total
    total_needed = CHUNK_SIZE + (CHUNK_SIZE * PRELOAD_CHUNKS)

    # Get random offsets
    offsets = get_random_offsets(total_needed)

    # Fetch all at once in parallel batches
    current_offsets = offsets[:CHUNK_SIZE]
    preload_offsets = offsets[CHUNK_SIZE:]

    # Fetch current chunk — one call per random offset
    # Supabase doesn't support random() so we fetch by random row ranges
    start = random.randint(0, max(0, TOTAL_VIDEOS - CHUNK_SIZE))
    end = start + CHUNK_SIZE - 1

    result = supabase.table("videos")\
        .select("id, video_url, thumbnail_url, caption, hashtags")\
        .range(start, end)\
        .execute()

    # Fetch preload from different random range
    preload_start = random.randint(0, max(0, TOTAL_VIDEOS - (CHUNK_SIZE * PRELOAD_CHUNKS)))
    preload_end = preload_start + (CHUNK_SIZE * PRELOAD_CHUNKS) - 1

    preload = supabase.table("videos")\
        .select("id, video_url, thumbnail_url")\
        .range(preload_start, preload_end)\
        .execute()

    return {
        "page": page,
        "chunk_size": CHUNK_SIZE,
        "videos": [clean_video(v) for v in result.data],
        "preload_urls": [
            {
                "id": v.get("id"),
                "video_url": swap_to_fast_url(v.get("video_url", "")),
                "thumbnail_url": v.get("thumbnail_url"),
            }
            for v in preload.data
        ],
        "next_page": page + 1
    }

@app.get("/video/{video_id}")
def get_video(video_id: str):
    result = supabase.table("videos")\
        .select("id, video_url, thumbnail_url, caption, hashtags")\
        .eq("id", video_id)\
        .single()\
        .execute()

    if not result.data:
        return {"error": "Video not found"}

    return clean_video(result.data)
