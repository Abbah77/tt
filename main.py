"""
ReelzLite Backend  –  main.py
==============================
Serves public-domain movies from archive.org as 5-minute episodes,
matching the Dart app's ApiService contract exactly:

  GET /feed?cursor=<int>&limit=<int>   → FeedResponse
  GET /movie/<slug>                    → MovieDetail  (with episodes)
  GET /search?q=<str>&limit=<int>      → { "data": [MovieCard] }
  GET /                                → health check

Episode strategy (no ffmpeg needed)
------------------------------------
archive.org serves MP4 files that support HTTP Range and time-fragment
URIs via the media fragment standard:
    https://archive.org/download/<id>/file.mp4#t=0,300
Most video players (including Flutter's video_player / chewie) honour
the #t= fragment to auto-seek and stop.  We compute N episodes of
EPISODE_SECONDS each, each carrying:
  • stream_url  – direct MP4 URL with #t=start,end fragment
  • start_time  – seconds into the source file
  • end_time    – seconds into the source file
  • duration    – seconds (capped at EPISODE_SECONDS)

The Flutter side just passes startAt=start_time when it opens the
player and stops / auto-advances at end_time.

Duration estimation
-------------------
archive.org metadata often carries a "length" or "runtime" field.
We parse those; if absent we estimate from file size (≈1.5 Mbit/s
for typical SD MP4, a conservative estimate that avoids showing
episodes beyond EOF).
"""

from __future__ import annotations

import asyncio
import re
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

# ── constants ─────────────────────────────────────────────────────────────────

EPISODE_SECONDS = 300          # 5 minutes per episode
BITRATE_ESTIMATE = 1_500_000  # bits/s fallback for size→duration
MIN_DURATION = 300             # discard clips shorter than 5 min

ARCHIVE_SEARCH = "https://archive.org/advancedsearch.php"
ARCHIVE_META   = "https://archive.org/metadata"
ARCHIVE_DOWN   = "https://archive.org/download"
ARCHIVE_IMG    = "https://archive.org/services/img"

# Collections most likely to have full-length public-domain movies
FEED_QUERIES = [
    'collection:feature_films AND mediatype:movies',
    'collection:PublicDomainMovies AND mediatype:movies',
    'collection:moviesandfilms AND mediatype:movies',
    'mediatype:movies AND subject:"public domain" AND format:mp4',
]

SEARCH_QUERY_TMPL = (
    'mediatype:movies AND ({q}) AND (collection:feature_films OR '
    'collection:PublicDomainMovies OR collection:moviesandfilms)'
)

# ── app setup ─────────────────────────────────────────────────────────────────

app = FastAPI(title="ReelzLite Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

client = httpx.AsyncClient(timeout=30)

# Simple in-process cache: identifier → metadata dict
_meta_cache: dict[str, dict] = {}


# ── archive.org helpers ───────────────────────────────────────────────────────

async def search_archive(q: str, rows: int = 50) -> list[dict]:
    """Call the archive.org Lucene search API."""
    try:
        r = await client.get(ARCHIVE_SEARCH, params={
            "q":     q,
            "fl[]": ["identifier", "title", "description", "year",
                     "subject", "runtime", "length"],
            "rows":  rows,
            "page":  1,
            "output": "json",
        })
        r.raise_for_status()
        docs = r.json().get("response", {}).get("docs", [])
        return [d for d in docs if d.get("identifier") and d.get("title")]
    except Exception:
        return []


async def get_meta(identifier: str) -> dict:
    """Fetch item metadata, with a simple in-process cache."""
    if identifier in _meta_cache:
        return _meta_cache[identifier]
    r = await client.get(f"{ARCHIVE_META}/{identifier}")
    r.raise_for_status()
    data = r.json()
    _meta_cache[identifier] = data
    return data


# ── duration helpers ──────────────────────────────────────────────────────────

_RUNTIME_RE = re.compile(
    r"(?:(\d+)\s*h(?:r|ours?)?)?\s*(?:(\d+)\s*m(?:in)?s?)?\s*(?:(\d+)\s*s(?:ec)?s?)?",
    re.IGNORECASE,
)


def _parse_runtime(raw: str | list | None) -> int | None:
    """
    Convert archive.org 'runtime' / 'length' strings to total seconds.
    Handles: "1:32:00", "92 min", "1h 32m", "5520" (bare seconds).
    """
    if raw is None:
        return None
    if isinstance(raw, list):
        raw = raw[0]
    raw = str(raw).strip()

    # HH:MM:SS or MM:SS
    parts = raw.split(":")
    if len(parts) in (2, 3):
        try:
            nums = [int(p) for p in parts]
            if len(nums) == 3:
                return nums[0] * 3600 + nums[1] * 60 + nums[2]
            return nums[0] * 60 + nums[1]
        except ValueError:
            pass

    # bare integer (seconds)
    if raw.isdigit():
        return int(raw)

    # "92 min" / "1h 32m 10s"
    m = _RUNTIME_RE.fullmatch(raw)
    if m:
        h, mi, s = (int(x) if x else 0 for x in m.groups())
        total = h * 3600 + mi * 60 + s
        if total > 0:
            return total

    return None


def _estimate_duration(files: list[dict], identifier: str) -> int | None:
    """
    Try to get duration from file metadata fields, then fall back
    to estimating from file size.
    """
    best_size = 0
    best_duration = None

    for f in files:
        name: str = (f.get("name") or "").lower()
        if not name.endswith((".mp4", ".ogv", ".avi", ".mkv")):
            continue
        if any(x in name for x in ("sample", "trailer", "64kb", "512kb")):
            continue

        # Length field on the file object
        d = _parse_runtime(f.get("length")) or _parse_runtime(f.get("runtime"))
        if d and d > best_duration or (best_duration is None and d):
            best_duration = d

        # Track largest video file for size-based fallback
        try:
            sz = int(f.get("size", 0))
            if sz > best_size:
                best_size = sz
        except (ValueError, TypeError):
            pass

    if best_duration and best_duration >= MIN_DURATION:
        return best_duration

    # Size-based estimate
    if best_size > 0:
        est = int((best_size * 8) / BITRATE_ESTIMATE)
        if est >= MIN_DURATION:
            return est

    return None


# ── video file selection ───────────────────────────────────────────────────────

def pick_video_file(files: list[dict]) -> dict | None:
    """
    Return the best video file dict from the archive.org files list.
    Prefers mp4 > ogv > avi > mkv; skips sample/trailer/low-quality copies.
    """
    SKIP = ("sample", "trailer", "64kb", "512kb", "256kb")
    for ext in ("mp4", "ogv", "avi", "mkv"):
        for f in files:
            name: str = (f.get("name") or "").lower()
            if name.endswith(f".{ext}") and not any(x in name for x in SKIP):
                return f
    return None


def thumb_url(identifier: str) -> str:
    return f"{ARCHIVE_IMG}/{identifier}"


# ── model builders ────────────────────────────────────────────────────────────

def build_movie_card(doc: dict, episode_count: int = 1) -> dict:
    """
    Build the MovieCard JSON the Dart app expects.
    Keys must exactly match MovieCard.fromJson() in models.dart:
      id, title, slug, thumbnail_url, trailer_url
    slug == archive.org identifier (used as /movie/<slug>).
    id   == stable hash of identifier (Dart expects int, not string).
    """
    ident  = doc.get("identifier", "")
    subj   = doc.get("subject", "")
    genre  = (", ".join(subj[:3]) if isinstance(subj, list) else str(subj or ""))[:60]
    desc   = doc.get("description", "") or ""
    if isinstance(desc, list):
        desc = " ".join(desc)

    # Dart MovieCard requires an int id — use a stable hash of the identifier
    card_id = abs(hash(ident)) % (10 ** 9)

    return {
        # ── Fields read by MovieCard.fromJson ──
        "id":            card_id,
        "title":         doc.get("title", ident),
        "slug":          ident,
        "thumbnail_url": thumb_url(ident),   # ← was "thumbnail" (wrong key)
        "trailer_url":   None,                # feed cards have no separate trailer
        # ── Extra fields (ignored by Dart but useful for debugging) ──
        "year":          str(doc.get("year", "")),
        "genre":         genre,
        "description":   desc[:300],
        "episode_count": episode_count,
    }


def build_episodes(identifier: str, video_file: dict, total_seconds: int) -> list[dict]:
    """
    Slice a movie into 5-minute episode dicts.
    Keys must exactly match EpisodeModel.fromJson() in models.dart:
      id, episode_number, url
    Each episode carries a #t=start,end fragment so media_kit seeks correctly.
    """
    fname    = video_file["name"]
    base_url = f"{ARCHIVE_DOWN}/{identifier}/{fname}"
    episodes = []
    ep_num   = 1
    start    = 0

    while start < total_seconds:
        end = min(start + EPISODE_SECONDS, total_seconds)

        episodes.append({
            # ── Fields read by EpisodeModel.fromJson ──
            "id":             ep_num,                       # ← Dart needs int id
            "episode_number": ep_num,                       # ← was "episode" (wrong key)
            "url":            f"{base_url}#t={start},{end}", # ← was "stream_url" (wrong key)
            # ── Extra fields for the player to use ──
            "start_time": start,
            "end_time":   end,
            "duration":   end - start,
            "title":      f"Episode {ep_num}",
        })

        start  += EPISODE_SECONDS
        ep_num += 1

    return episodes


# ── routes ────────────────────────────────────────────────────────────────────

@app.get("/")
async def root():
    return {"status": "ok", "service": "ReelzLite Backend"}


@app.get("/feed")
async def feed(
    cursor: Optional[int] = Query(None),
    limit:  int           = Query(10, ge=1, le=50),
):
    """
    Paginated feed of MovieCards.
    cursor = offset into the master list (None → 0).
    Returns: { data: [MovieCard], next_cursor: int|null, has_more: bool }
    """
    offset = cursor or 0

    # Fetch enough results to satisfy the page
    need   = offset + limit
    rows   = max(need + 10, 60)          # over-fetch slightly

    docs: list[dict] = []
    for q in FEED_QUERIES:
        if len(docs) >= need:
            break
        batch = await search_archive(q, rows=rows)
        # dedupe by identifier
        seen  = {d["identifier"] for d in docs}
        docs += [d for d in batch if d["identifier"] not in seen]

    page     = docs[offset: offset + limit]
    has_more = len(docs) > offset + limit
    nxt      = (offset + limit) if has_more else None

    # For card list we don't fetch full metadata — estimate episodes from
    # a quick heuristic (archive search has no duration field, so default 12).
    cards = [build_movie_card(d, episode_count=12) for d in page]

    return {
        "data":        cards,
        "next_cursor": nxt,
        "has_more":    has_more,
    }


@app.get("/search")
async def search(
    q:     str = Query(..., min_length=1),
    limit: int = Query(20, ge=1, le=50),
):
    """
    Free-text search against archive.org.
    Returns: { "data": [MovieCard] }
    """
    query = SEARCH_QUERY_TMPL.format(q=q)
    docs  = await search_archive(query, rows=limit + 10)
    cards = [build_movie_card(d, episode_count=12) for d in docs[:limit]]
    return {"data": cards}


@app.get("/movie/{slug:path}")
async def movie_detail(slug: str):
    """
    Full MovieDetail for one item, with real episode list.
    Returns: { movie: MovieCard, episodes: [Episode] }

    This is the expensive call — it fetches archive.org metadata to get
    the actual video URL and compute real episode boundaries.
    """
    try:
        meta  = await get_meta(slug)
    except httpx.HTTPStatusError as e:
        raise HTTPException(404, f"Not found on archive.org: {slug}") from e
    except Exception as e:
        raise HTTPException(502, f"archive.org error: {e}") from e

    files     = meta.get("files", [])
    m         = meta.get("metadata", {})
    video_f   = pick_video_file(files)

    if not video_f:
        raise HTTPException(404, f"No playable video file for: {slug}")

    # Duration: prefer metadata fields, then file-level, then size estimate
    total_secs = (
        _parse_runtime(m.get("runtime"))
        or _parse_runtime(m.get("length"))
        or _estimate_duration(files, slug)
    )

    if not total_secs or total_secs < MIN_DURATION:
        # Too short to bother splitting — serve as single episode
        total_secs = EPISODE_SECONDS

    episodes = build_episodes(slug, video_f, total_secs)

    subj  = m.get("subject", "")
    genre = (", ".join(subj[:3]) if isinstance(subj, list) else str(subj or ""))[:60]
    desc  = m.get("description", "") or ""
    if isinstance(desc, list):
        desc = " ".join(desc)

    card = {
        # ── Fields read by MovieCard.fromJson ──
        "id":            abs(hash(slug)) % (10 ** 9),
        "title":         m.get("title", slug),
        "slug":          slug,
        "thumbnail_url": thumb_url(slug),   # ← correct key
        "trailer_url":   None,
        # ── Extra fields ──
        "year":          str(m.get("year", "")),
        "genre":         genre,
        "description":   desc[:300],
        "episode_count": len(episodes),
    }

    return {
        "movie":          card,
        "episodes":       episodes,
        "total_episodes": len(episodes),   # ← Dart MovieDetail.fromJson reads this
    }
