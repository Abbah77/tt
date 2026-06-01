import os
import logging
import time
import requests
from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import RedirectResponse
from dotenv import load_dotenv

# ── Optional Supabase ─────────────────────────────────────────────────────────
try:
    from supabase import create_client, Client as SupabaseClient
    _supabase_available = True
except ImportError:
    _supabase_available = False

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
load_dotenv()

app = FastAPI(title="Reelz Gateway", version="11.0.0")
app.add_middleware(GZipMiddleware, minimum_size=256)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Config ────────────────────────────────────────────────────────────────────
TMDB_API_KEY   = os.getenv("TMDB_API_KEY")
DOMAIN         = os.getenv("PRODUCTION_DOMAIN", "https://tt-b577.onrender.com")
SUPABASE_URL   = os.getenv("SUPABASE_URL")
SUPABASE_KEY   = os.getenv("SUPABASE_SERVICE_KEY")   # service role key
TMDB_IMG       = "https://image.tmdb.org/t/p/w500"
FALLBACK_THUMB = "https://images.unsplash.com/photo-1440404653325-ab127d49abc1?q=80&w=500"

# ── Supabase client ───────────────────────────────────────────────────────────
_sb: "SupabaseClient | None" = None

def get_supabase() -> "SupabaseClient | None":
    global _sb
    if _sb is not None:
        return _sb
    if not _supabase_available:
        logger.warning("supabase-py not installed; DB features disabled")
        return None
    if not SUPABASE_URL or not SUPABASE_KEY:
        logger.warning("SUPABASE_URL / SUPABASE_SERVICE_KEY not set; DB disabled")
        return None
    try:
        _sb = create_client(SUPABASE_URL, SUPABASE_KEY)
        logger.info("Supabase connected")
        return _sb
    except Exception as e:
        logger.error(f"Supabase init failed: {e}")
        return None


# ── In-memory caches ──────────────────────────────────────────────────────────
_trailer_cache: dict[int, str | None] = {}
_stream_cache: dict[str, tuple[str, float]] = {}
CACHE_TTL = 4 * 3600  # 4 hours

# ── TMDB ──────────────────────────────────────────────────────────────────────

def tmdb(endpoint: str, params: dict = None) -> dict:
    p = {"api_key": TMDB_API_KEY, **(params or {})}
    r = requests.get(
        f"https://api.themoviedb.org/3/{endpoint}",
        params=p, timeout=8,
    )
    r.raise_for_status()
    return r.json()


def get_yt_key(movie_id: int) -> str | None:
    if movie_id in _trailer_cache:
        return _trailer_cache[movie_id]
    try:
        videos = tmdb(f"movie/{movie_id}/videos").get("results", [])
        for kind in ("Trailer", "Teaser", "Clip", "Featurette"):
            for v in videos:
                if v.get("site") == "YouTube" and v.get("type") == kind:
                    _trailer_cache[movie_id] = v["key"]
                    return v["key"]
    except Exception as e:
        logger.warning(f"TMDB videos failed for {movie_id}: {e}")
    _trailer_cache[movie_id] = None
    return None


def build_trailer_url(movie_id: int) -> str | None:
    key = get_yt_key(movie_id)
    if not key:
        return None
    return f"{DOMAIN}/proxy/trailer/{key}"


def map_movie(m: dict) -> dict:
    movie_id = m.get("id")
    poster   = m.get("poster_path")
    return {
        "id":            movie_id,
        "slug":          str(movie_id),
        "title":         m.get("title") or m.get("original_title") or "Untitled",
        "thumbnail_url": f"{TMDB_IMG}{poster}" if poster else FALLBACK_THUMB,
        "trailer_url":   build_trailer_url(movie_id),
    }


# ── Invidious trailer proxy ───────────────────────────────────────────────────

INVIDIOUS_INSTANCES = [
    "https://inv.nadeko.net",
    "https://invidious.privacydev.net",
    "https://iv.datura.network",
    "https://invidious.perennialte.ch",
    "https://invidious.fdn.fr",
]


def _pick_best_stream(formats: list[dict]) -> str | None:
    if not formats:
        return None

    def score(f):
        container = f.get("container", "")
        type_     = f.get("type", "")
        quality   = f.get("quality", "")
        q_num = 0
        try:
            q_num = int(quality.replace("p", "").split(".")[0])
        except Exception:
            pass
        is_mp4    = "mp4" in container.lower() or "mp4" in type_.lower()
        is_h264   = "h264" in type_.lower() or "avc" in type_.lower()
        under_720 = q_num <= 720 and q_num > 0
        if not is_mp4:
            return -1
        return (int(is_h264) * 1000) + (int(under_720) * 500) + q_num

    ranked = sorted(formats, key=score, reverse=True)
    for f in ranked:
        url = f.get("url")
        if url and score(f) > 0:
            return url
    return None


@app.get("/proxy/trailer/{key}")
def trailer_proxy(key: str):
    if key in _stream_cache:
        url, ts = _stream_cache[key]
        if time.time() - ts < CACHE_TTL:
            return RedirectResponse(url=url, status_code=302)
        del _stream_cache[key]

    headers = {
        "User-Agent": "Mozilla/5.0 (Linux; Android 10) AppleWebKit/537.36",
        "Accept": "application/json",
    }

    for instance in INVIDIOUS_INSTANCES:
        try:
            r = requests.get(
                f"{instance}/api/v1/videos/{key}",
                params={"fields": "adaptiveFormats,formatStreams"},
                headers=headers,
                timeout=7,
            )
            if r.status_code != 200:
                continue
            if "html" in r.headers.get("content-type", "").lower():
                continue

            data = r.json()
            url = _pick_best_stream(data.get("adaptiveFormats", []))
            if not url:
                for f in reversed(data.get("formatStreams", [])):
                    if "mp4" in f.get("container", "").lower():
                        url = f.get("url")
                        break

            if url:
                _stream_cache[key] = (url, time.time())
                return RedirectResponse(url=url, status_code=302)

        except requests.exceptions.Timeout:
            continue
        except Exception as e:
            logger.warning(f"{instance} error: {e}")
            continue

    return RedirectResponse(
        url=f"https://www.youtube-nocookie.com/embed/{key}?autoplay=1",
        status_code=302,
    )


# ── Feed ──────────────────────────────────────────────────────────────────────

@app.get("/feed")
def feed(cursor: int = Query(None), limit: int = Query(10, le=20)):
    try:
        page   = ((cursor or 0) // 20) + 1
        offset = (cursor or 0) % 20
        data        = tmdb("discover/movie", {"sort_by": "popularity.desc", "page": page})
        results     = data.get("results", [])
        total_pages = data.get("total_pages", 1)
        slice_      = results[offset: offset + limit]
        next_cursor = (cursor or 0) + len(slice_) if slice_ else None
        has_more    = page < total_pages
        return {
            "data":        [map_movie(r) for r in slice_],
            "next_cursor": next_cursor if has_more else None,
            "has_more":    has_more,
        }
    except Exception as e:
        logger.error(f"Feed error: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch feed")


# ── Search ────────────────────────────────────────────────────────────────────

@app.get("/search")
def search(q: str = Query(..., min_length=1), limit: int = Query(20, le=40)):
    try:
        results = tmdb("search/movie", {"query": q, "include_adult": False}
                       ).get("results", [])[:limit]
        return {"data": [map_movie(r) for r in results]}
    except Exception as e:
        logger.error(f"Search error: {e}")
        raise HTTPException(status_code=500, detail="Search failed")


# ── Movie detail ──────────────────────────────────────────────────────────────

@app.get("/movie/{slug}")
def movie_detail(slug: str):
    try:
        info   = tmdb(f"movie/{slug}")
        yt_key = get_yt_key(int(slug))
        ep_url = f"{DOMAIN}/proxy/trailer/{yt_key}" if yt_key else None
        return {
            "movie":          map_movie(info),
            "episodes": [{"id": 1, "episode_number": 1, "url": ep_url or ""}],
            "total_episodes": 1,
        }
    except Exception as e:
        logger.error(f"Movie detail error {slug}: {e}")
        raise HTTPException(status_code=404, detail="Movie not found")


# ── User likes / saves (Supabase) ─────────────────────────────────────────────
# All endpoints are protected by google_user_id (verified by client).
# We store only TMDB IDs — no media, no file blobs, just integers.
#
# Supabase schema (run once in SQL editor):
#
# create table if not exists user_interactions (
#   id            bigserial primary key,
#   google_user_id text not null,
#   tmdb_id        integer not null,
#   kind           text not null check (kind in ('like', 'save')),
#   created_at     timestamptz default now(),
#   unique (google_user_id, tmdb_id, kind)
# );
# create index on user_interactions (google_user_id, kind);


@app.get("/user/{uid}/interactions")
def get_interactions(uid: str):
    """Return all liked + saved TMDB IDs for a user."""
    sb = get_supabase()
    if not sb:
        return {"liked": [], "saved": []}
    try:
        rows = (
            sb.table("user_interactions")
            .select("tmdb_id, kind")
            .eq("google_user_id", uid)
            .execute()
        ).data
        liked = [r["tmdb_id"] for r in rows if r["kind"] == "like"]
        saved = [r["tmdb_id"] for r in rows if r["kind"] == "save"]
        return {"liked": liked, "saved": saved}
    except Exception as e:
        logger.error(f"get_interactions error: {e}")
        return {"liked": [], "saved": []}


@app.post("/user/{uid}/like/{tmdb_id}")
def toggle_like(uid: str, tmdb_id: int):
    return _toggle(uid, tmdb_id, "like")


@app.post("/user/{uid}/save/{tmdb_id}")
def toggle_save(uid: str, tmdb_id: int):
    return _toggle(uid, tmdb_id, "save")


def _toggle(uid: str, tmdb_id: int, kind: str) -> dict:
    sb = get_supabase()
    if not sb:
        return {"status": "no_db"}
    try:
        existing = (
            sb.table("user_interactions")
            .select("id")
            .eq("google_user_id", uid)
            .eq("tmdb_id", tmdb_id)
            .eq("kind", kind)
            .execute()
        ).data
        if existing:
            sb.table("user_interactions").delete().eq(
                "id", existing[0]["id"]
            ).execute()
            return {"status": "removed"}
        else:
            sb.table("user_interactions").insert(
                {"google_user_id": uid, "tmdb_id": tmdb_id, "kind": kind}
            ).execute()
            return {"status": "added"}
    except Exception as e:
        logger.error(f"toggle {kind} error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    test_id = 550  # Fight Club
    key = get_yt_key(test_id)
    sb_ok = get_supabase() is not None
    return {
        "status":          "online",
        "version":         "11.0.0",
        "tmdb_key_set":    bool(TMDB_API_KEY),
        "supabase_ready":  sb_ok,
        "test_movie":      "Fight Club (id=550)",
        "yt_key":          key,
        "trailer_url":     f"{DOMAIN}/proxy/trailer/{key}" if key else None,
        "invidious_instances": INVIDIOUS_INSTANCES,
    }


@app.get("/")
def root():
    return {"status": "online", "version": "11.0.0"}
