from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from supabase import create_client
import os
from dotenv import load_dotenv

load_dotenv()

app = FastAPI()

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
PRELOAD = 3

@app.get("/")
def root():
    return {"status": "ok", "message": "API is running 🔥"}

@app.get("/feed")
def get_feed(page: int = Query(default=1, ge=1)):
    start = (page - 1) * CHUNK_SIZE
    end = start + CHUNK_SIZE - 1

    result = supabase.table("videos")\
        .select("*")\
        .range(start, end)\
        .execute()

    next_start = end + 1
    next_end = next_start + (CHUNK_SIZE * PRELOAD) - 1

    preload = supabase.table("videos")\
        .select("id, video_url, thumbnail_url")\
        .range(next_start, next_end)\
        .execute()

    return {
        "page": page,
        "chunk_size": CHUNK_SIZE,
        "videos": result.data,
        "preload_urls": [
            {
                "id": v["id"],
                "video_url": v["video_url"],
                "thumbnail_url": v["thumbnail_url"]
            }
            for v in preload.data
        ],
        "next_page": page + 1
    }

@app.get("/video/{video_id}")
def get_video(video_id: str):
    result = supabase.table("videos")\
        .select("*")\
        .eq("id", video_id)\
        .single()\
        .execute()

    if not result.data:
        return {"error": "Video not found"}

    return result.data
