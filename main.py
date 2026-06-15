from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import httpx
import asyncio

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

client = httpx.AsyncClient(timeout=20)


# ── helpers ───────────────────────────────────────────────────────────────────

async def search_archive(query: str, rows: int = 30) -> list:
    params = {
        "q": query,
        "fl[]": ["identifier", "title", "description", "year", "subject", "thumb"],
        "rows": rows,
        "page": 1,
        "output": "json",
        "mediatype": "movies",
    }
    r = await client.get(ARCHIVE_SEARCH, params=params)
    r.raise_for_status()
    return r.json().get("response", {}).get("docs", [])


async def get_meta(identifier: str) -> dict:
    r = await client.get(f"{ARCHIVE_META}/{identifier}")
    r.raise_for_status()
    return r.json()


def pick_video(files: list, identifier: str) -> str | None:
    """Pick best video file: prefer mp4 > ogv > avi"""
    for ext in ("mp4", "ogv", "avi", "mkv"):
        for f in files:
            name = f.get("name", "")
            if name.lower().endswith(f".{ext}") and "_512kb" not in name:
                return f"{ARCHIVE_DOWN}/{identifier}/{name}"
    return None


def thumb(identifier: str, doc: dict = None) -> str:
    if doc and doc.get("thumb"):
        return doc["thumb"]
    return f"https://archive.org/services/img/{identifier}"


def doc_to_item(doc: dict, media_type: str) -> dict:
    ident = doc.get("identifier", "")
    return {
        "id":          ident,
        "title":       doc.get("title", ident),
        "thumbnail":   thumb(ident, doc),
        "poster":      thumb(ident, doc),
        "year":        str(doc.get("year", "")),
        "type":        media_type,
        "description": doc.get("description", "") or "",
        "genre":       ", ".join(doc.get("subject", [])[:3]) if isinstance(doc.get("subject"), list) else str(doc.get("subject", "")),
    }


# ── routes ────────────────────────────────────────────────────────────────────

@app.get("/movies")
async def get_movies():
    docs = await search_archive(
        'mediatype:movies subject:"feature film" OR subject:"public domain" -collection:test_collection',
        rows=40,
    )
    return [doc_to_item(d, "movie") for d in docs if d.get("identifier")]


@app.get("/tv")
async def get_tv():
    docs = await search_archive(
        'mediatype:movies subject:"television" OR subject:"tv series" OR subject:"public domain television"',
        rows=40,
    )
    return [doc_to_item(d, "tv") for d in docs if d.get("identifier")]


@app.get("/movie/{id}")
async def stream_movie(id: str):
    meta = await get_meta(id)
    files = meta.get("files", [])
    url = pick_video(files, id)
    if not url:
        raise HTTPException(404, "No video found for this item")
    return {
        "url":     url,
        "type":    "mp4",
        "headers": {},
        "referer": "",
    }


@app.get("/tv/{id}")
async def tv_detail(id: str):
    meta = await get_meta(id)
    files = meta.get("files", [])
    videos = sorted(
        [f["name"] for f in files if f.get("name", "").lower().endswith((".mp4", ".ogv", ".avi"))],
    )
    episode_count = max(len(videos), 1)
    return {
        "id":        id,
        "title":     meta.get("metadata", {}).get("title", id),
        "thumbnail": thumb(id),
        "poster":    thumb(id),
        "seasons":   [{"season": 1, "episode_count": episode_count}],
    }


@app.get("/tv/{id}/{season}/{episode}")
async def stream_tv(id: str, season: int, episode: int):
    meta = await get_meta(id)
    files = meta.get("files", [])
    videos = sorted(
        [f["name"] for f in files if f.get("name", "").lower().endswith((".mp4", ".ogv", ".avi"))],
    )
    if not videos:
        raise HTTPException(404, "No video files found")
    idx = max(0, min(episode - 1, len(videos) - 1))
    url = f"{ARCHIVE_DOWN}/{id}/{videos[idx]}"
    return {
        "url":     url,
        "type":    "mp4",
        "headers": {},
        "referer": "",
    }


@app.get("/")
async def root():
    return {"status": "ok", "service": "ReelzLite Backend"}
