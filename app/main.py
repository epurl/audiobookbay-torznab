import asyncio
import datetime
import logging
import os
import platform

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app import audible, audiobookshelf, auth, db, library
from app.monitor import (auto_download_book, find_missing_books, grab, match_job, run_monitor_loop, schedule_search,
                         schedule_searches, start_match_job, sync_series)
from app.qbittorrent import get_torrents, test_connection
from app.scraper import fetch_detail_info, search_audiobooks, search_for_book
from app.torznab import build_caps, build_rss

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(title="Bayarr")
app.middleware("http")(auth.auth_middleware)

@app.on_event("startup")
async def startup_event():
    # Start the background monitor loop
    asyncio.create_task(run_monitor_loop())

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
    for asset in ("style.css", "script.js"):
        version = int(os.path.getmtime(os.path.join("app/static", asset)))
        page = page.replace(f"/static/{asset}\"", f"/static/{asset}?v={version}\"")
    return HTMLResponse(page, headers={"Cache-Control": "no-cache"})

@app.get("/favicon.ico")
async def favicon():
    """Ignore favicon requests."""
    return Response(status_code=204)

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

@app.post("/api/library/bulk")
async def api_bulk(request: Request):
    """Applies one action to many books: status, remove, or match (on Audible)."""
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
    return {
        "path": path,
        "exists": True,
        "files": [{"name": os.path.relpath(f, base), "size_bytes": size} for f, size in files],
    }

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

@app.post("/api/series")
async def api_add_series(request: Request):
    """Monitors an Audible series, given its ASIN or a library book that belongs to it."""
    data = await request.json()
    mode = data.get("mode") if data.get("mode") in ("all", "future") else "all"
    asin, title, author = data.get("series_asin"), data.get("title", ""), data.get("author", "")
    if data.get("book_id"):
        book = _get_book_or_404(data["book_id"])
        author = library.primary_author(book.get("authors"))
        asin, title = book.get("series_asin"), book.get("series", "")
        if not asin:
            # Books imported from folders have no Audible series id; look the book up
            asin, found_title = await audible.find_series_for_book(book.get("title", ""), author)
            title = title or found_title or ""
    if not asin:
        raise HTTPException(status_code=404, detail="Couldn't find this series on Audible.")

    series = db.add_series(asin, title, author, mode)
    try:
        added = await sync_series(series, db.get_settings())
    except Exception as e:
        logger.error(f"Series sync failed: {e}", exc_info=True)
        raise HTTPException(status_code=502, detail="Couldn't load the series from Audible.")
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

    candidates = await asyncio.to_thread(library.scan_library, root)
    current = db.get_library()
    _last_scan.clear()
    for c in candidates:
        match = library.find_match(current, c)
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
              "description", "path", "cover", "file_count", "size_bytes", "format")
    added, linked = db.import_books([{k: c.get(k, "") for k in fields} for c in chosen])
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
    if not url or not title:
        raise HTTPException(status_code=400, detail="Missing url or book")
    
    settings = db.get_settings()
    if not settings.get("qbt_enabled"):
        raise HTTPException(status_code=400, detail="Download client is not enabled in settings.")
        
    logger.info(f"Send to client requested for: {url}")
    detail_info = await fetch_detail_info(url, title)
    magnet = detail_info.get("magnet") if detail_info else None
    
    if not magnet:
        raise HTTPException(status_code=404, detail="Failed to fetch magnet link")
        
    # A manual grab tracks the book so it gets imported when the download finishes
    entry = db.add_to_library(book)
    if await grab(entry, magnet, settings):
        return {"success": True}
    else:
        raise HTTPException(status_code=500, detail="Failed to send torrent to qBittorrent")

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
        "parent": parent,
        "dirs": sorted(dirs, key=lambda s: s.lower())
    }

@app.get("/api/search_audible")
async def search_audible(title: str = ""):
    """Proxies search request to Audible API."""
    if not title:
        return {"products": []}
    try:
        return await audible.search(title=title)
    except Exception as e:
        logger.error(f"Error fetching from Audible: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch from Audible")

@app.get("/api/search_abb")
async def search_abb(title: str = "", author: str = ""):
    """JSON wrapper for Audiobookbay search, used by the UI."""
    try:
        results = await search_for_book(title, author)
        return {"results": results}
    except Exception as e:
        logger.error(f"Error searching ABB for UI: {e}")
        return {"results": []}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
