import asyncio
import csv
import datetime
import json
import logging
import os
import platform

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app import (audible, audiobookshelf, auth, authors, book_search, convert, db, editions, health, indexers, library,
                 organize, reading_list, release_calendar, scraper, series_index, splitter, stats)
from app.monitor import (auto_download_book, classify_editions, find_missing_books, grab, match_job, run_monitor_loop,
                         schedule_search, schedule_searches, start_match_job, sync_series)
from app.qbittorrent import get_torrents, test_connection
from app.scraper import fetch_detail_info, search_audiobooks
from app.torznab import build_caps, build_rss

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

class SafeJSONResponse(JSONResponse):
    """JSON with non-ASCII characters escaped, so file names that aren't valid UTF-8 (kept
    as surrogates by Python) can be sent to the browser and back without crashing."""

    def render(self, content) -> bytes:
        return json.dumps(content, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode("ascii")


app = FastAPI(title="Bayarr", default_response_class=SafeJSONResponse)
app.middleware("http")(auth.auth_middleware)

@app.on_event("startup")
async def startup_event():
    # Start the background monitor loop, and the M4B conversion queue
    asyncio.create_task(run_monitor_loop())
    asyncio.create_task(convert.run_queue())

# Ensure static directory exists
os.makedirs("app/static", exist_ok=True)
app.mount("/static", StaticFiles(directory="app/static"), name="static")

@app.get("/", response_class=HTMLResponse)
async def root():
    """Root endpoint, serves the main UI."""
    try:
        with open("app/static/index.html", "r", encoding="utf-8") as f:
            page = f.read()
    except FileNotFoundError:
        return "UI not found. Please create app/static/index.html."
    # Tag the CSS and JS with their modification time, so browsers fetch the new
    # files after an update instead of reusing cached copies
    for asset in ("style.css", "script.js", "logo.svg"):
        version = int(os.path.getmtime(os.path.join("app/static", asset)))
        page = page.replace(f"/static/{asset}\"", f"/static/{asset}?v={version}\"")
    return HTMLResponse(page, headers={"Cache-Control": "no-cache"})

@app.get("/favicon.ico")
async def favicon():
    """The logo, for browsers and apps that ask for /favicon.ico."""
    return FileResponse("app/static/logo.svg", media_type="image/svg+xml")

@app.get("/api")
async def torznab_api(request: Request, t: str = "", q: str = "", author: str = "", title: str = "", offset: int = 0, limit: int = 100):
    """Main Torznab endpoint for indexer queries."""
    
    # Return Capabilities
    if t == "caps":
        xml = build_caps()
        return Response(content=xml, media_type="application/xml")
        
    # Handle Search Queries
    if t in ("search", "book"):
        logger.info(f"Received search request - query: '{q}', author: '{author}', title: '{title}', offset: {offset}, limit: {limit}")
        # Combine parameters into a generic search for audiobookbay
        query_parts = []
        if q:
            query_parts.append(q)
        if author:
            query_parts.append(author)
        if title:
            query_parts.append(title)
            
        search_query = " ".join(query_parts).strip()
        
        try:
            logger.info(f"Searching AudiobookBay for: '{search_query}'")
            results = await search_audiobooks(search_query, offset=offset, limit=limit)
            logger.info(f"Search returned {len(results)} results")
        except Exception as e:
            logger.error(f"Error during search: {e}", exc_info=True)
            results = []
            
        host_url = f"{request.url.scheme}://{request.url.netloc}"
        xml = build_rss(results, host_url, offset=offset)
        return Response(content=xml, media_type="application/xml")

    # Fallback for unsupported operations
    return Response(content="<?xml version=\"1.0\" encoding=\"UTF-8\"?><error code=\"201\" description=\"Incorrect parameter\"/>", media_type="application/xml")

@app.get("/api/download")
async def get_magnet(url: str, title: str = None):
    """Simulates downloading a torrent by fetching the detail page and returning the magnet."""
    logger.info(f"Download requested for URL: {url} with title: {title}")
    if not url:
        logger.warning("Download requested without URL parameter")
        raise HTTPException(status_code=400, detail="Missing url parameter")
        
    detail_info = await fetch_detail_info(url, title)
    magnet = detail_info.get("magnet") if detail_info else None
    if not magnet:
        logger.error(f"Failed to find magnet link for URL: {url}")
        raise HTTPException(status_code=404, detail="Could not find magnet link for this audiobook")
        
    # Redirect standard download cliens to the magnet link
    logger.info(f"Successfully resolved magnet link for {url}, redirecting client.")
    return RedirectResponse(magnet)

# --- Bayarr Library & Settings API ---

@app.get("/api/library")
async def api_get_library():
    return {"library": db.get_library()}

@app.post("/api/library")
async def api_add_library(request: Request):
    data = await request.json()
    if not data.get("title"):
        raise HTTPException(status_code=400, detail="Missing title")
    book = db.add_to_library(data)
    # Like the *arr apps, search as soon as a released book is added
    if book.get("status") == "Monitored":
        schedule_search(book)
    return {"success": True, "status": book.get("status")}

def _get_book_or_404(book_id: str):
    book = db.get_book(book_id)
    if not book:
        raise HTTPException(status_code=404, detail="Book not found")
    return book

@app.patch("/api/library/{book_id}")
async def api_edit_book(book_id: str, request: Request):
    _get_book_or_404(book_id)
    data = await request.json()
    fields = {k: v for k, v in data.items() if k in db.EDITABLE_BOOK_FIELDS}
    if "status" in fields and fields["status"] not in db.STATUSES:
        raise HTTPException(status_code=400, detail=f"Unknown status: {fields['status']}")
    if "title" in fields and not str(fields["title"]).strip():
        raise HTTPException(status_code=400, detail="Title cannot be empty")
    if "sequence" in fields:
        fields["sequence"] = library.format_sequence(fields["sequence"])
    if "runtime_min" in fields:
        try:
            fields["runtime_min"] = max(0, int(fields["runtime_min"] or 0))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Runtime must be a whole number of minutes")
    if "edition" in fields:
        if fields["edition"] not in editions.EDITIONS:
            raise HTTPException(status_code=400, detail=f"Unknown edition: {fields['edition']}")
        if fields["edition"] != editions.edition_of(_get_book_or_404(book_id)):
            fields.update(edition_reason="Set by you", edition_check=False)
        else:
            fields["edition_check"] = False  # Confirmed as it is
    db.update_book(book_id, **fields)
    return {"success": True, "book": db.get_book(book_id)}

@app.get("/api/library/{book_id}/match_candidates")
async def api_match_candidates(book_id: str, q: str = ""):
    """Audible books that might be this one, for choosing a match by hand."""
    book = _get_book_or_404(book_id)
    query = q.strip() or f"{(book.get('title') or '').split(':')[0]} {library.primary_author(book.get('authors'))}"
    try:
        return {"query": query, "candidates": await audible.match_candidates(query)}
    except Exception as e:
        logger.error(f"Audible search failed: {e}")
        raise HTTPException(status_code=502, detail="Couldn't search Audible.")

@app.post("/api/library/{book_id}/match")
async def api_match_book(book_id: str, request: Request):
    """Fills in a book's details from the chosen Audible edition."""
    book = _get_book_or_404(book_id)
    asin = (await request.json()).get("asin", "")
    products = await audible.get_products([asin]) if asin else []
    if not products:
        raise HTTPException(status_code=404, detail="That book wasn't found on Audible.")
    db.apply_audible_match(book_id, audible.product_to_book(products[0], prefer_series=book.get("series", "")))
    db.add_history("matched", book, f"Matched to Audible {asin}")
    return {"success": True, "book": db.get_book(book_id)}

@app.post("/api/lists/parse")
async def api_list_parse(request: Request):
    """Reads a Goodreads/StoryGraph export: its shelves and how many books each has."""
    text = (await request.json()).get("csv") or ""
    if len(text) > 20_000_000:
        raise HTTPException(status_code=400, detail="That file is too big.")
    try:
        return reading_list.parse(text)
    except (ValueError, csv.Error) as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/lists/match")
async def api_list_match(request: Request):
    """Looks up a shelf's books on Audible in the background."""
    shelf = (await request.json()).get("shelf") or ""
    if not reading_list.start_matching(shelf):
        raise HTTPException(status_code=409, detail="A list is already being matched.")
    return {"success": True, "total": reading_list.job["total"]}

@app.get("/api/lists/match")
async def api_list_match_status():
    return reading_list.job

@app.post("/api/lists/add")
async def api_list_add(request: Request):
    data = await request.json()
    status = "Unmonitored" if data.get("status") == "Unmonitored" else "Monitored"
    added = reading_list.add(data.get("asins") or [], status)
    schedule_searches([b for b in added if b["status"] == "Monitored"])
    return {"success": True, "added": len(added)}

@app.get("/api/authors")
async def api_authors():
    """Followed authors."""
    return {"authors": authors.summary()}

@app.get("/api/authors/detail")
async def api_author_detail(name: str):
    """An author's books on Audible, with library status."""
    if not name.strip():
        raise HTTPException(status_code=400, detail="Missing author")
    try:
        return await authors.detail(name.strip())
    except Exception as e:
        logger.error(f"Author lookup failed: {e}", exc_info=True)
        raise HTTPException(status_code=502, detail="Couldn't load the author's books from Audible.")

@app.post("/api/authors")
async def api_follow_author(request: Request):
    """Follows an author; add_asins are the books to add now."""
    data = await request.json()
    name = (data.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Missing author")
    entry, added = await authors.follow(name, data.get("add_asins") or [])
    schedule_searches([b for b in added if b["status"] == "Monitored"])
    return {"success": True, "author": entry, "added": len(added)}

@app.patch("/api/authors/{author_id}")
async def api_update_author(author_id: str, request: Request):
    if not authors.get(author_id):
        raise HTTPException(status_code=404, detail="Author not followed")
    data = await request.json()
    if "monitored" in data:
        authors.update(author_id, monitored=bool(data["monitored"]))
    return {"success": True}

@app.post("/api/authors/{author_id}/sync")
async def api_sync_author(author_id: str):
    entry = authors.get(author_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Author not followed")
    added = await authors.sync(entry, db.get_settings())
    schedule_searches([b for b in added if b["status"] == "Monitored"])
    return {"success": True, "added": len(added)}

@app.delete("/api/authors/{author_id}")
async def api_unfollow_author(author_id: str):
    authors.unfollow(author_id)
    return {"success": True}

@app.get("/api/indexers")
async def api_indexers():
    return {"indexers": indexers.public()}

@app.post("/api/indexers")
async def api_save_indexer(request: Request):
    """Adds or updates a Torznab indexer (a blank API key keeps the stored one)."""
    try:
        entry = indexers.save(await request.json())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"success": True, "id": entry["id"], "indexers": indexers.public()}

@app.delete("/api/indexers/{indexer_id}")
async def api_remove_indexer(indexer_id: str):
    indexers.remove(indexer_id)
    return {"success": True, "indexers": indexers.public()}

@app.post("/api/indexers/test")
async def api_test_indexer(request: Request):
    data = await request.json()
    if not str(data.get("url") or "").startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="The URL must start with http:// or https://")
    return await indexers.test(data)

@app.post("/api/abb/test")
async def api_test_abb(request: Request):
    """Reaches AudiobookBay with the form's (or the saved) address, cookie and user agent."""
    data = await request.json()
    url = (data.get("url") or "").strip()
    if url and not url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="The address must start with https://")
    return await scraper.test_connection(url, data.get("cookie") or None, data.get("user_agent") or "")

@app.get("/api/stats")
async def api_stats():
    """Library statistics for System > Stats."""
    return await asyncio.to_thread(stats.compute)

@app.get("/api/health")
async def api_health():
    """Books that need attention, with the fix for each kind of issue."""
    return await asyncio.to_thread(health.check)

@app.post("/api/health/deep")
async def api_health_deep():
    """Opens every audio file in the background: unreadable files, wrong lengths."""
    health.start_deep_check()
    return dict(health.deep_job)

@app.get("/api/health/deep")
async def api_health_deep_status():
    return dict(health.deep_job)

async def _fetch_cover(book):
    path = book.get("path") or ""
    if not book.get("imageUrl") or not os.path.isdir(path):
        return False
    name = await audiobookshelf.download_cover(book["imageUrl"], path)
    if name:
        db.update_book(book["id"], cover=name)
    return bool(name)

@app.post("/api/covers")
async def api_fetch_covers(request: Request):
    """Saves Audible's cover into the folders of these books (as cover.jpg)."""
    ids = (await request.json()).get("ids") or []
    fetched = 0
    for book_id in ids:
        book = db.get_book(book_id)
        if book and await _fetch_cover(book):
            fetched += 1
    return {"success": True, "fetched": fetched, "count": len(ids)}

@app.get("/api/organize")
async def api_organize_preview(rename_files: bool = False):
    """Proposed renames of existing folders to the Book Folder Format; nothing changes yet."""
    return await asyncio.to_thread(organize.plan, rename_files)

@app.post("/api/organize")
async def api_organize(request: Request):
    """Applies approved changes (folder renames/moves, optionally audio file renames)."""
    data = await request.json()
    ids = [i for i in data.get("book_ids") or [] if db.get_book(i)]
    if not ids:
        raise HTTPException(status_code=400, detail="Nothing approved.")
    results = await asyncio.to_thread(organize.apply, ids, bool(data.get("rename_files")))
    if any(r["ok"] and not r.get("unchanged") for r in results):
        await audiobookshelf.scan_library(db.get_settings())
    return {"results": results}

@app.get("/api/library/{book_id}/split")
async def api_split_proposal(book_id: str):
    """The books a collection folder holds, matched to its series on Audible."""
    _get_book_or_404(book_id)
    proposal = await splitter.propose(book_id)
    if proposal is None:
        raise HTTPException(status_code=400, detail="This book has no folder on disk.")
    return splitter.public(proposal)

@app.post("/api/library/{book_id}/split")
async def api_split(book_id: str, request: Request):
    """Creates a folder per chosen book (hardlinks; the original folder isn't changed)."""
    _get_book_or_404(book_id)
    choices = (await request.json()).get("books") or []
    try:
        return {"success": True, **await splitter.apply(book_id, choices)}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except OSError as e:
        logger.error(f"Split failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Couldn't create the folders: {e}")

@app.post("/api/library/bulk")
async def api_bulk(request: Request):
    """Applies one action to many books: status, remove, match (on Audible), edition (set
    it) or detect_edition (work it out again from Audible and the files)."""
    data = await request.json()
    known = {b["id"] for b in db.get_library()}
    ids = [i for i in data.get("ids", []) if i in known]
    action = data.get("action")
    if not ids:
        raise HTTPException(status_code=400, detail="No books selected.")
    if action == "status":
        status = data.get("status")
        if status not in db.STATUSES:
            raise HTTPException(status_code=400, detail=f"Unknown status: {status}")
        db.update_books({i: {"status": status} for i in ids})
        if status == "Monitored":
            schedule_searches([db.get_book(i) for i in ids])
        return {"success": True, "count": len(ids)}
    if action == "remove":
        for i in ids:
            db.remove_from_library(i)
        return {"success": True, "count": len(ids)}
    if action == "match":
        if not start_match_job(ids):
            raise HTTPException(status_code=409, detail="A match is already running.")
        return {"success": True, "count": len(ids)}
    if action == "convert":
        if not convert.available():
            raise HTTPException(status_code=409, detail="ffmpeg isn't installed")
        added, skipped = convert.enqueue(ids, source="bulk")
        return {"success": True, "count": added, "skipped": len(skipped)}
    if action == "edition":
        edition = data.get("edition")
        if edition not in editions.EDITIONS:
            raise HTTPException(status_code=400, detail=f"Unknown edition: {edition}")
        db.update_books({i: {"edition": edition, "edition_reason": "Set by you", "edition_check": False} for i in ids})
        return {"success": True, "count": len(ids)}
    if action == "detect_edition":
        count = await classify_editions(set(ids))
        return {"success": True, "count": count}
    raise HTTPException(status_code=400, detail=f"Unknown action: {action}")

@app.get("/api/library/match_status")
async def api_match_status():
    return dict(match_job)

@app.post("/api/library/{book_id}/import_anyway")
async def api_import_anyway(book_id: str):
    """Imports a download that was held for review; the next import check picks it up."""
    book = _get_book_or_404(book_id)
    if book.get("status") != "Needs Review":
        raise HTTPException(status_code=400, detail="This book isn't waiting for review.")
    db.update_book(book_id, status="Downloaded", skip_verify=True)
    db.add_history("approved", book, "Import approved despite the check")
    return {"success": True}

@app.post("/api/library/{book_id}/reject")
async def api_reject_download(book_id: str):
    """Rejects the downloaded release: it won't be grabbed again for this book, and the
    search starts over. The torrent itself is left in qBittorrent."""
    book = _get_book_or_404(book_id)
    if not book.get("download_hash"):
        raise HTTPException(status_code=400, detail="This book has no download to reject.")
    blocklist = list(dict.fromkeys((book.get("blocklist") or []) + [book["download_hash"]]))
    db.update_book(book_id, status="Monitored", blocklist=blocklist, download_hash="", review_reason="", skip_verify=False)
    db.add_history("rejected", book, f"Rejected release {book.get('release_title') or book['download_hash']}; searching again")
    schedule_search(db.get_book(book_id))
    return {"success": True}

@app.delete("/api/library/{book_id}")
async def api_remove_library(book_id: str):
    """Removes a book from Bayarr. Files on disk are never deleted."""
    _get_book_or_404(book_id)
    db.remove_from_library(book_id)
    return {"success": True}

@app.post("/api/library/{book_id}/search")
async def api_search_book(book_id: str):
    """Searches AudiobookBay for one book now and grabs the best match."""
    book = _get_book_or_404(book_id)
    settings = db.get_settings()
    if not settings.get("qbt_enabled"):
        raise HTTPException(status_code=400, detail="Download client is not enabled in settings.")
    grabbed = await auto_download_book(book, settings)
    return {"success": True, "grabbed": bool(grabbed)}

@app.get("/api/library/{book_id}/cover")
async def api_book_cover(book_id: str):
    book = _get_book_or_404(book_id)
    path, cover = book.get("path"), book.get("cover")
    if not path or not cover or os.path.basename(cover) != cover:
        raise HTTPException(status_code=404, detail="No cover")
    full = os.path.join(path, cover)
    if os.path.splitext(full)[1].lower() not in library.IMAGE_EXTENSIONS or not os.path.isfile(full):
        raise HTTPException(status_code=404, detail="No cover")
    return FileResponse(full, headers={"Cache-Control": "max-age=86400"})

@app.get("/api/library/{book_id}/files")
async def api_book_files(book_id: str):
    book = _get_book_or_404(book_id)
    path = book.get("path")
    if not path or not os.path.exists(path):
        return {"path": path, "exists": False, "files": []}
    files = await asyncio.to_thread(library.audio_files, path)
    base = path if os.path.isdir(path) else os.path.dirname(path)
    kept = await asyncio.to_thread(convert.originals, path)
    return {
        "path": path,
        "exists": True,
        "files": [{"name": library.display_name(os.path.relpath(f, base)), "size_bytes": size} for f, size in files],
        # Converting to M4B: whether it's possible, and the originals a conversion kept
        "convert": {"available": convert.available(), "reason": convert.can_convert(book),
                    "originals": len(kept), "originals_bytes": sum(size for _, size in kept)},
    }

@app.post("/api/library/{book_id}/convert")
async def api_convert(book_id: str):
    """Queues the book for conversion to one M4B with chapters."""
    _get_book_or_404(book_id)
    if not convert.available():
        raise HTTPException(status_code=409, detail="ffmpeg isn't installed")
    added, skipped = convert.enqueue([book_id])
    if not added:
        raise HTTPException(status_code=409, detail=skipped.get(book_id, "Couldn't queue it"))
    return convert.status()

@app.get("/api/convert")
async def api_convert_status():
    """The conversion queue: the book being converted, the ones waiting, recent results."""
    return convert.status()

@app.delete("/api/convert/{book_id}")
async def api_convert_remove(book_id: str):
    """Takes a book out of the queue, or cancels its conversion if it's running."""
    if not convert.remove(book_id):
        raise HTTPException(status_code=404, detail="It isn't queued.")
    return convert.status()

@app.post("/api/convert/queue_all")
async def api_convert_queue_all():
    """Queues every book on disk that isn't a single M4B yet."""
    if not convert.available():
        raise HTTPException(status_code=409, detail="ffmpeg isn't installed")
    added, _ = convert.enqueue([b["id"] for b in convert.eligible_books()], source="bulk")
    return {"success": True, "added": added, **convert.status()}

@app.post("/api/library/{book_id}/originals/delete")
async def api_delete_originals(book_id: str):
    """Deletes the original files a conversion kept (*.original)."""
    book = _get_book_or_404(book_id)
    if convert.job["running"] and convert.job["book_id"] == book_id:
        raise HTTPException(status_code=409, detail="It's being converted right now.")
    count, size = await asyncio.to_thread(convert.delete_originals, book)
    return {"success": True, "deleted": count, "bytes": size}

# --- Activity: queue and history ---

@app.get("/api/queue")
async def api_queue():
    """Books on their way in, with live progress from qBittorrent."""
    books = [b for b in db.get_library() if b.get("status") in ("Downloading", "Downloaded", "Needs Review")]
    settings = db.get_settings()
    torrents, reachable = {}, True
    hashes = [b["download_hash"] for b in books if b.get("download_hash")]
    if settings.get("qbt_enabled") and hashes:
        info = await get_torrents(settings.get("qbt_host"), settings.get("qbt_user"), settings.get("qbt_pass"), hashes)
        reachable = info is not None
        torrents = {t["hash"].lower(): t for t in info or []}
    queue = []
    for b in books:
        t = torrents.get(b.get("download_hash") or "", {})
        queue.append({
            "id": b["id"], "title": b.get("title"), "authors": b.get("authors"), "status": b.get("status"),
            "release_title": b.get("release_title", ""), "review_reason": b.get("review_reason", ""),
            "progress": t.get("progress"), "state": t.get("state"), "size_bytes": t.get("size"),
            "dlspeed": t.get("dlspeed"), "eta": t.get("eta"), "seeds": t.get("num_seeds"),
            "in_client": bool(t),
        })
    return {"queue": queue, "client_reachable": reachable}

@app.get("/api/history")
async def api_history(limit: int = 200):
    return {"history": db.get_history(max(1, min(limit, db.HISTORY_LIMIT)))}

# --- Series ---

@app.get("/api/series")
async def api_series():
    return {"series": db.get_series_list()}

@app.get("/api/series/index")
async def api_series_index():
    """Every series in the library (and every monitored one), for the Series page."""
    return {"series": await asyncio.to_thread(series_index.index), "refresh": dict(series_index.refresh_job)}

@app.get("/api/series/detail")
async def api_series_detail(key: str):
    """One series with every book Audible lists for it, owned or not."""
    try:
        detail = await series_index.detail(key)
    except Exception as e:
        logger.error(f"Series details failed: {e}", exc_info=True)
        raise HTTPException(status_code=502, detail="Couldn't load the series from Audible.")
    if detail is None:
        raise HTTPException(status_code=404, detail="Series not found")
    return detail

@app.post("/api/series/refresh")
async def api_series_refresh():
    """Looks up Audible's book lists for the library's series in the background."""
    series_index.start_refresh()
    return dict(series_index.refresh_job)

@app.get("/api/calendar")
async def api_calendar():
    """Trending Audible releases for the Calendar (library books come from /api/library)."""
    data = release_calendar.get()
    return {**data, "stale": release_calendar.is_stale(), "refresh": dict(release_calendar.refresh_job)}

@app.post("/api/calendar/refresh")
async def api_calendar_refresh():
    """Reloads trending releases from Audible in the background (about a minute)."""
    release_calendar.start_refresh()
    return dict(release_calendar.refresh_job)

@app.post("/api/series")
async def api_add_series(request: Request):
    """Monitors an Audible series. add_asins are the missing books to add now; books the
    series gets later on Audible are added automatically."""
    data = await request.json()
    asin = data.get("series_asin") or ""
    if not asin:
        raise HTTPException(status_code=400, detail="Open the series first so its Audible id is known.")
    try:
        catalog = await series_index.ensure_catalog(asin)
    except Exception as e:
        logger.error(f"Series lookup failed: {e}", exc_info=True)
        raise HTTPException(status_code=502, detail="Couldn't load the series from Audible.")
    title = data.get("title") or catalog.get("title", "")
    # Today's books count as known, so if this first sync fails, later ones still only add
    # new releases rather than everything that wasn't chosen
    series = db.add_series(asin, title, data.get("author", ""),
                           known_asins=[b["asin"] for b in catalog.get("books", []) + catalog.get("alternates", [])
                                        if b.get("asin")])
    added = await sync_series(series, db.get_settings(), selected=set(data.get("add_asins") or []))
    schedule_searches([b for b in added if b["status"] == "Monitored"])
    return {"success": True, "series": db.get_series(series["id"]), "added": len(added)}

@app.patch("/api/series/{series_id}")
async def api_edit_series(series_id: str, request: Request):
    if not db.get_series(series_id):
        raise HTTPException(status_code=404, detail="Series not found")
    data = await request.json()
    fields = {k: data[k] for k in ("monitored", "mode") if k in data}
    db.update_series(series_id, **fields)
    return {"success": True}

@app.post("/api/series/{series_id}/sync")
async def api_sync_series(series_id: str):
    series = db.get_series(series_id)
    if not series:
        raise HTTPException(status_code=404, detail="Series not found")
    added = await sync_series(series, db.get_settings())
    schedule_searches([b for b in added if b["status"] == "Monitored"])
    return {"success": True, "added": len(added)}

@app.delete("/api/series/{series_id}")
async def api_remove_series(series_id: str):
    """Stops tracking a series. Its books stay in the library."""
    db.remove_series(series_id)
    return {"success": True}

# --- qBittorrent ---

@app.post("/api/qbittorrent/test")
async def api_qbt_test(request: Request):
    """Checks the qBittorrent login. A blank password uses the saved one."""
    data = await request.json()
    settings = db.get_settings()
    host = (data.get("host") or settings.get("qbt_host") or "").strip()
    if not host.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="Enter the Web UI URL, starting with http:// or https://")
    try:
        version = await test_connection(host, data.get("user") or settings.get("qbt_user"),
                                        data.get("password") or settings.get("qbt_pass"))
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e) or "Couldn't connect to qBittorrent.")
    return {"success": True, "version": version}

# --- Backup ---

@app.get("/api/backup")
async def api_backup():
    """Downloads the database (library, series, history and settings)."""
    if not os.path.exists(db.DB_FILE):
        raise HTTPException(status_code=404, detail="Nothing to back up yet.")
    name = f"bayarr-backup-{datetime.date.today().isoformat()}.json"
    return FileResponse(db.DB_FILE, media_type="application/json", filename=name)

@app.post("/api/restore")
async def api_restore(request: Request):
    try:
        data = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="That file isn't valid JSON.")
    try:
        count = db.restore(data)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    logger.info(f"Database restored from a backup ({count} books)")
    return {"success": True, "books": count}

# --- Audiobookshelf ---

@app.post("/api/audiobookshelf/libraries")
async def api_abs_libraries(request: Request):
    """Tests the connection and lists book libraries. A blank token uses the saved one."""
    data = await request.json()
    settings = db.get_settings()
    url = (data.get("url") or settings.get("abs_url") or "").strip()
    token = data.get("token") or settings.get("abs_token")
    if not url.startswith(("http://", "https://")) or not token:
        raise HTTPException(status_code=400, detail="Enter the Audiobookshelf URL and API token.")
    try:
        return {"libraries": await audiobookshelf.list_libraries(url, token)}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Couldn't connect to Audiobookshelf: {e}")

# Results of the last scan, keyed by folder path. Imports may only use these, so the
# browser can't make Bayarr record arbitrary paths.
_last_scan = {}

@app.post("/api/library/scan")
async def api_scan_library(request: Request):
    """Scans a folder for existing books and reports which ones are new."""
    data = await request.json()
    root = (data.get("path") or db.get_settings().get("root_folder") or "").strip()
    if not root or not os.path.isdir(root):
        raise HTTPException(status_code=400, detail=f"Folder not found: {root or '(none set)'}")
    if library.is_system_folder(root) or library.is_system_folder(os.path.abspath(root)):
        raise HTTPException(status_code=400, detail=f"{root} is a system folder. Choose the folder that holds "
                                                    "your audiobooks, e.g. /audiobooks.")

    candidates = await asyncio.to_thread(library.scan_library, root)
    current = db.get_library()
    _last_scan.clear()
    for c in candidates:
        match = library.find_import_match(current, c)
        if not match:
            c["state"] = "new"
        elif library.same_path(match.get("path"), c["path"]) and match.get("status") in ("Imported", "Missing"):
            c["state"] = "in_library"
        else:
            c["state"] = "link"  # Tracked (e.g. Monitored) but not yet linked to these files
        c["match_title"] = match.get("title") if match else ""
        _last_scan[c["path"]] = c
    logger.info(f"Library scan of {root}: {len(candidates)} books found")
    return {"root": root, "books": candidates}

@app.post("/api/library/import")
async def api_import_library(request: Request):
    data = await request.json()
    chosen = [_last_scan[p] for p in data.get("paths", []) if p in _last_scan]
    if not chosen:
        raise HTTPException(status_code=400, detail="Nothing to import; scan the folder again.")
    fields = ("title", "authors", "narrators", "series", "sequence", "asin", "release_date",
              "description", "publisher", "path", "cover", "file_count", "size_bytes", "format",
              "edition", "edition_reason", "edition_check")
    # Editions changed in the preview
    chosen_editions = data.get("editions") or {}
    books = []
    for c in chosen:
        book = {k: c.get(k, "") for k in fields}
        edition = chosen_editions.get(c["path"])
        if edition in editions.EDITIONS and edition != c.get("edition"):
            book.update(edition=edition, edition_reason="Set by you", edition_check=False)
        books.append(book)
    added, linked = db.import_books(books)
    return {"success": True, "added": added, "linked": linked}

@app.post("/api/library/rescan")
async def api_rescan_library():
    """Refreshes file info for books on disk and flags ones whose files are gone."""
    missing, restored = await asyncio.to_thread(find_missing_books)

    def refresh():
        return {b["id"]: library.describe_files(b["path"]) for b in db.get_library()
                if b.get("status") == "Imported" and b.get("path")}
    db.update_books(await asyncio.to_thread(refresh))
    return {"success": True, "missing": missing, "restored": restored}

@app.get("/api/settings")
async def api_get_settings():
    settings = db.get_public_settings()
    settings["auth_from_env"] = auth.env_credentials_set()
    return {"settings": settings}

@app.post("/api/settings")
async def api_update_settings(request: Request):
    data = await request.json()
    db.update_settings(data)
    return {"success": True}

@app.post("/api/auth")
async def api_set_auth(request: Request):
    """Sets the UI login. The middleware has already authenticated the caller."""
    if auth.env_credentials_set():
        raise HTTPException(status_code=400, detail="Login is set by BAYARR_USERNAME/BAYARR_PASSWORD environment variables.")
    data = await request.json()
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    if not username or ":" in username:
        raise HTTPException(status_code=400, detail="Username is required and cannot contain ':'.")
    if len(password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters.")
    db.set_auth_credentials(username, auth.hash_password(password))
    return {"success": True}

@app.post("/api/send_to_client")
async def api_send_to_client(request: Request):
    data = await request.json()
    url = data.get("url")
    book = data.get("book") or {}
    title = book.get("title")
    # The release as Manual Search showed it (AudiobookBay or a Torznab indexer)
    release = {k: v for k, v in (data.get("release") or {}).items()
               if k in ("magnet_url", "download_url", "link", "title", "raw_title", "format", "size_str", "source")}
    release.setdefault("link", url)
    release.setdefault("title", title)
    if not (release.get("link") or release.get("download_url") or release.get("magnet_url")) or not title:
        raise HTTPException(status_code=400, detail="Missing url or book")
    if release.get("magnet_url") and not str(release["magnet_url"]).startswith("magnet:"):
        raise HTTPException(status_code=400, detail="Not a magnet link")

    settings = db.get_settings()
    if not settings.get("qbt_enabled"):
        raise HTTPException(status_code=400, detail="Download client is not enabled in settings.")

    logger.info(f"Send to client requested for: {release.get('download_url') or release.get('link')}")
    try:
        magnet, torrent = await indexers.get_download(release)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Couldn't get the download: {e}")
        magnet = torrent = None
    if not magnet and not torrent:
        raise HTTPException(status_code=404, detail="Failed to fetch magnet link")

    # A manual grab tracks the book so it gets imported when the download finishes
    entry = db.add_to_library(book)
    if await grab(entry, magnet, settings, release=release, torrent=torrent):
        return {"success": True}
    else:
        raise HTTPException(status_code=500, detail="Failed to send torrent to qBittorrent")

@app.post("/api/browse")
async def browse_directory_post(request: Request):
    """Folder picker. The path comes in the JSON body rather than the URL, so folder names
    that aren't valid UTF-8 survive the round trip."""
    data = await request.json()
    return await browse_directory(data.get("path") or "")

@app.get("/api/browse")
async def browse_directory(path: str = ""):
    if not path:
        if platform.system() == "Windows":
            path = "C:\\"
        else:
            path = "/"
            
    if not os.path.isabs(path):
        path = os.path.abspath(path)
        
    if not os.path.exists(path):
        # Fallback to root if path is invalid
        if platform.system() == "Windows":
            path = "C:\\"
        else:
            path = "/"
            
    dirs = []
    try:
        for entry in os.scandir(path):
            if entry.is_dir() and not entry.name.startswith('.'):
                dirs.append(entry.name)
    except Exception as e:
        logger.error(f"Error reading directory {path}: {e}")
        
    parent = os.path.dirname(path)
    if path == parent or (platform.system() == "Windows" and path.endswith(":\\")):
        parent = None # Top level
        
    return {
        "path": path,
        "display_path": library.display_name(path),
        "parent": parent,
        "dirs": [{"name": library.display_name(d), "path": os.path.join(path, d)}
                 for d in sorted(dirs, key=lambda s: library.display_name(s).lower())],
    }

@app.get("/api/search_audible")
async def search_audible(title: str = ""):
    """Searches Audible by title, author, narrator or series (the query is named title for
    older clients)."""
    if not title.strip():
        return {"products": []}
    try:
        data = await audible.search_keywords(title.strip())
        for product in data.get("products") or []:
            found = editions.classify_product(product)
            product["edition"], product["edition_reason"] = found["edition"], found["reason"]
        return data
    except Exception as e:
        logger.error(f"Error fetching from Audible: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch from Audible")

async def _manual_search(book):
    try:
        results, queries = await book_search.find_releases(book, db.get_settings(), mode="manual")
        return {"results": results, "queries": queries}
    except Exception as e:
        logger.error(f"Error searching ABB for UI: {e}", exc_info=True)
        return {"results": [], "queries": [], "error": str(e) if isinstance(e, RuntimeError) else "AudiobookBay search failed. Check the log."}

@app.get("/api/search_abb")
async def search_abb(title: str = "", author: str = ""):
    """AudiobookBay releases for a title and author, scored and best first."""
    return await _manual_search({"title": title, "authors": author})

@app.post("/api/search_abb")
async def search_abb_for_book(request: Request):
    """AudiobookBay releases for a book (series, number, narrator and length make the
    scores more accurate), best first."""
    book = (await request.json()).get("book") or {}
    saved = db.get_book(book.get("id")) if book.get("id") else None
    book = {**book, **(saved or {})}
    if not book.get("title"):
        raise HTTPException(status_code=400, detail="Missing book title")
    return await _manual_search(book)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
