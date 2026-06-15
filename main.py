from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import httpx

app = FastAPI(title="ReelzLite Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

ARCHIVE_SEARCH = "https://archive.org/advancedsearch.php"
ARCHIVE_META   = "https://archive.org/metadata"
ARCHIVE_DOWN   = "https://archive.org/download"

# Hardcoded known-good public domain collections from archive.org
MOVIE_COLLECTIONS = [
    "feature_films",
    "PublicDomainMovies",
    "moviesandfilms",
]

TV_COLLECTIONS = [
    "classic_tv",
    "netlabels",
    "opensource",
]

client = httpx.AsyncClient(timeout=30)


# ── helpers ───────────────────────────────────────────────────────────────────

async def search_archive(q: str, rows: int = 30) -> list:
    try:
        r = await client.get(ARCHIVE_SEARCH, params={
            "q": q,
            "fl[]": ["identifier", "title", "description", "year", "subject", "thumb"],
            "rows": rows,
            "page": 1,
            "output": "json",
        })
        r.raise_for_status()
        docs = r.json().get("response", {}).get("docs", [])
        return [d for d in docs if d.get("identifier") and d.get("title")]
    except Exception:
        return []


async def get_meta(identifier: str) -> dict:
    r = await client.get(f"{ARCHIVE_META}/{identifier}")
    r.raise_for_status()
    return r.json()


def pick_video(files: list, identifier: str) -> str | None:
    # Prefer mp4, then ogv, then avi — skip sample/trailer files
    for ext in ("mp4", "ogv", "avi", "mkv"):
        for f in files:
            name: str = f.get("name", "")
            nl = name.lower()
            if nl.endswith(f".{ext}") and not any(x in nl for x in ("sample", "trailer", "512kb", "64kb")):
                return f"{ARCHIVE_DOWN}/{identifier}/{name}"
    return None


def thumb(identifier: str, doc: dict = None) -> str:
    if doc and doc.get("thumb"):
        t = doc["thumb"]
        if t.startswith("http"):
            return t
    return f"https://archive.org/services/img/{identifier}"


def doc_to_item(doc: dict, media_type: str) -> dict:
    ident = doc.get("identifier", "")
    subj  = doc.get("subject", "")
    genre = ", ".join(subj[:3]) if isinstance(subj, list) else str(subj or "")
    desc  = doc.get("description", "") or ""
    if isinstance(desc, list):
        desc = " ".join(desc)
    return {
        "id":          ident,
        "title":       doc.get("title", ident),
        "thumbnail":   thumb(ident, doc),
        "poster":      thumb(ident, doc),
        "year":        str(doc.get("year", "")),
        "type":        media_type,
        "description": desc[:300],
        "genre":       genre,
    }


# ── routes ────────────────────────────────────────────────────────────────────

@app.get("/movies")
async def get_movies():
    # Try multiple queries to guarantee results
    docs = await search_archive(
        'collection:feature_films AND mediatype:movies', rows=50
    )
    if not docs:
        docs = await search_archive(
            'collection:PublicDomainMovies AND mediatype:movies', rows=50
        )
    if not docs:
        docs = await search_archive(
            'mediatype:movies AND subject:"public domain" AND format:mp4', rows=50
        )
    return [doc_to_item(d, "movie") for d in docs]


@app.get("/tv")
async def get_tv():
    docs = await search_archive(
        'collection:classic_tv AND mediatype:movies', rows=50
    )
    if not docs:
        docs = await search_archive(
            'mediatype:movies AND subject:"television" AND subject:"public domain"', rows=50
        )
    if not docs:
        # Fall back to more movies if no TV found
        docs = await search_archive(
            'collection:moviesandfilms AND mediatype:movies', rows=50
        )
    return [doc_to_item(d, "tv") for d in docs]


@app.get("/movie/{id:path}")
async def stream_movie(id: str):
    try:
        meta  = await get_meta(id)
        files = meta.get("files", [])
        url   = pick_video(files, id)
        if not url:
            raise HTTPException(404, f"No playable video found for: {id}")
        return {"url": url, "type": "mp4", "headers": {}, "referer": ""}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/tv/{id}/1/1")
async def stream_tv_first(id: str):
    return await stream_tv(id, 1, 1)


@app.get("/tv/{id}")
async def tv_detail(id: str):
    try:
        meta  = await get_meta(id)
        files = meta.get("files", [])
        m     = meta.get("metadata", {})
        videos = sorted([
            f["name"] for f in files
            if f.get("name", "").lower().endswith((".mp4", ".ogv", ".avi", ".mkv"))
            and not any(x in f["name"].lower() for x in ("sample", "trailer", "64kb"))
        ])
        return {
            "id":        id,
            "title":     m.get("title", id),
            "thumbnail": thumb(id),
            "poster":    thumb(id),
            "seasons":   [{"season": 1, "episode_count": max(len(videos), 1)}],
        }
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/tv/{id}/{season}/{episode}")
async def stream_tv(id: str, season: int, episode: int):
    try:
        meta  = await get_meta(id)
        files = meta.get("files", [])
        videos = sorted([
            f["name"] for f in files
            if f.get("name", "").lower().endswith((".mp4", ".ogv", ".avi", ".mkv"))
            and not any(x in f["name"].lower() for x in ("sample", "trailer", "64kb"))
        ])
        if not videos:
            raise HTTPException(404, "No video files found")
        idx = max(0, min(episode - 1, len(videos) - 1))
        url = f"{ARCHIVE_DOWN}/{id}/{videos[idx]}"
        return {"url": url, "type": "mp4", "headers": {}, "referer": ""}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/")
async def root():
    return {"status": "ok", "service": "ReelzLite Backend"}
