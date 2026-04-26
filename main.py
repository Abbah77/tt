from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from supabase import create_client
from dotenv import load_dotenv
import os

load_dotenv()

app = FastAPI(title="Reelz API", version="1.0.0")

# CORS — allow all origins (lock down in production)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Supabase
supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
CDN = os.getenv("CDN_BASE", "https://reelz.dpdns.org")

# ── Helpers ──────────────────────────────────────

def cdn_url(r2_key: str) -> str:
    if not r2_key:
        return None
    return f"{CDN}/{r2_key}"

def make_movie_response(movie: dict) -> dict:
    return {
        "id": movie["id"],
        "title": movie["title"],
        "slug": movie["slug"],
        "thumbnail_url": cdn_url(movie.get("thumbnail")),
        "trailer_url": cdn_url(movie.get("trailer")),
        "created_at": str(movie.get("created_at", "")),
    }

def make_episode_response(ep: dict) -> dict:
    return {
        "id": ep["id"],
        "episode_number": ep["episode_number"],
        "url": cdn_url(ep["r2_key"]),
    }


# ═══════════════════════════════════════════════════
# ROOT
# ═══════════════════════════════════════════════════

@app.get("/")
def root():
    return {"name": "Reelz API", "version": "1.0.0", "status": "online"}


# ═══════════════════════════════════════════════════
# FEED — CURSOR PAGINATION
# ═══════════════════════════════════════════════════

@app.get("/feed")
def feed(
    cursor: str = Query(None, description="Movie ID to start after (for pagination)"),
    limit: int = Query(10, ge=1, le=50),
):
    """
    TikTok-style feed of trailers.
    Returns movies with trailers + cursor for infinite scroll.
    """
    query = supabase.table("movies") \
        .select("id,title,slug,thumbnail,trailer,created_at") \
        .not_.is_("trailer", "null") \
        .order("created_at", desc=True) \
        .limit(limit + 1)  # fetch 1 extra to know if there's a next page

    if cursor:
        # Cursor = last seen movie ID, fetch older ones
        query = query.lt("id", cursor)

    result = query.execute()

    if not result.data:
        return {"data": [], "next_cursor": None, "has_more": False}

    movies = result.data
    has_more = len(movies) > limit

    if has_more:
        movies = movies[:limit]  # trim the extra

    data = [make_movie_response(m) for m in movies]
    next_cursor = data[-1]["id"] if has_more else None

    return {
        "data": data,
        "next_cursor": next_cursor,
        "has_more": has_more,
    }


# ═══════════════════════════════════════════════════
# MOVIE DETAILS + EPISODES
# ═══════════════════════════════════════════════════

@app.get("/movie/{slug}")
def movie_detail(slug: str):
    """
    Get movie info + all episodes sorted by episode_number.
    App uses this when user clicks "Watch" on a trailer.
    """
    # Fetch movie
    movie_result = supabase.table("movies") \
        .select("*") \
        .eq("slug", slug) \
        .execute()

    if not movie_result.data:
        raise HTTPException(status_code=404, detail="Movie not found")

    movie = movie_result.data[0]

    # Fetch all episodes for this movie
    episodes_result = supabase.table("episodes") \
        .select("*") \
        .eq("movie_id", movie["id"]) \
        .order("episode_number", ascending=True) \
        .execute()

    episodes = [make_episode_response(ep) for ep in episodes_result.data]

    return {
        "movie": make_movie_response(movie),
        "episodes": episodes,
        "total_episodes": len(episodes),
    }


# ═══════════════════════════════════════════════════
# EPISODE REDIRECT (INSTANT STREAM)
# ═══════════════════════════════════════════════════

@app.get("/stream/{slug}/ep{episode_number}")
def stream_episode(slug: str, episode_number: int):
    """
    Redirect to CDN for instant streaming.
    CDN handles Range requests, chunked transfer, caching.
    """
    # Verify movie + episode exist
    movie = supabase.table("movies").select("id").eq("slug", slug).execute()
    if not movie.data:
        raise HTTPException(status_code=404, detail="Movie not found")

    episode = supabase.table("episodes") \
        .select("r2_key") \
        .eq("movie_id", movie.data[0]["id"]) \
        .eq("episode_number", episode_number) \
        .execute()

    if not episode.data:
        raise HTTPException(status_code=404, detail="Episode not found")

    # 302 redirect to CDN (browser streams directly from R2 CDN)
    return RedirectResponse(
        url=cdn_url(episode.data[0]["r2_key"]),
        status_code=302
    )


# ═══════════════════════════════════════════════════
# SEARCH
# ═══════════════════════════════════════════════════

@app.get("/search")
def search(
    q: str = Query(..., min_length=1, max_length=100),
    limit: int = Query(10, ge=1, le=50),
):
    """Search movies by title."""
    result = supabase.table("movies") \
        .select("id,title,slug,thumbnail,trailer,created_at") \
        .ilike("title", f"%{q}%") \
        .limit(limit) \
        .execute()

    data = [make_movie_response(m) for m in result.data]
    return {"data": data, "total": len(data)}


# ═══════════════════════════════════════════════════
# HEALTH
# ═══════════════════════════════════════════════════

@app.get("/health")
def health():
    return {"status": "ok"}
