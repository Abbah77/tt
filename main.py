from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import RedirectResponse, JSONResponse
from supabase import create_client
import os

app = FastAPI(title="Reelz API", version="2.0.0")

app.add_middleware(GZipMiddleware, minimum_size=500)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
CDN = os.getenv("CDN_BASE", "https://reelz.dpdns.org")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

def cdn(key):
    return f"{CDN}/{key}" if key else None

def movie_json(m):
    return {
        "id": m["id"],
        "title": m["title"],
        "slug": m["slug"],
        "thumbnail_url": cdn(m.get("thumbnail")),
        "trailer_url": cdn(m.get("trailer")),
        "created_at": str(m.get("created_at", "")),
    }

def ep_json(e):
    return {
        "id": e["id"],
        "episode_number": e["episode_number"],
        "url": cdn(e["r2_key"]),
    }

@app.get("/")
def root():
    return {"status": "online", "name": "Reelz API", "version": "2.0.0"}

@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/feed")
def feed(
    cursor: int = Query(None),
    limit: int = Query(10, ge=1, le=50),
):
    q = supabase.table("movies")\
        .select("id,title,slug,thumbnail,trailer,created_at")\
        .not_.is_("thumbnail", "null")\
        .order("created_at", desc=True)\
        .limit(limit + 1)

    if cursor:
        q = q.lt("id", cursor)

    rows = q.execute().data
    has_more = len(rows) > limit
    if has_more:
        rows = rows[:limit]

    response = JSONResponse(content={
        "data": [movie_json(r) for r in rows],
        "next_cursor": rows[-1]["id"] if has_more else None,
        "has_more": has_more,
    })
    response.headers["Cache-Control"] = "public, max-age=30"
    return response

@app.get("/movie/{slug}")
def movie(slug: str):
    mv = supabase.table("movies")\
        .select("*")\
        .eq("slug", slug)\
        .execute()

    if not mv.data:
        raise HTTPException(status_code=404, detail="Movie not found")

    m = mv.data[0]

    eps = supabase.table("episodes")\
        .select("*")\
        .eq("movie_id", m["id"])\
        .order("episode_number")\
        .execute()

    return {
        "movie": movie_json(m),
        "episodes": [ep_json(e) for e in eps.data],
        "total_episodes": len(eps.data),
    }

@app.get("/stream/{slug}/ep{ep}")
def stream(slug: str, ep: int):
    mv = supabase.table("movies")\
        .select("id")\
        .eq("slug", slug)\
        .execute()

    if not mv.data:
        raise HTTPException(status_code=404, detail="Movie not found")

    ep_data = supabase.table("episodes")\
        .select("r2_key")\
        .eq("movie_id", mv.data[0]["id"])\
        .eq("episode_number", ep)\
        .execute()

    if not ep_data.data:
        raise HTTPException(status_code=404, detail="Episode not found")

    return RedirectResponse(
        cdn(ep_data.data[0]["r2_key"]),
        status_code=302,
    )

@app.get("/search")
def search(
    q: str = Query(..., min_length=1),
    limit: int = Query(10, ge=1, le=50),
):
    rows = supabase.table("movies")\
        .select("id,title,slug,thumbnail,trailer")\
        .ilike("title", f"%{q}%")\
        .limit(limit)\
        .execute()

    return {"data": [movie_json(r) for r in rows.data]}
