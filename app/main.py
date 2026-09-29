import asyncio
import logging
import os
import platform

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app import auth, db
from app.monitor import grab, run_monitor_loop, schedule_search
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
            return f.read()
    except FileNotFoundError:
        return "UI not found. Please create app/static/index.html."

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

@app.delete("/api/library")
async def api_remove_library(title: str):
    db.remove_from_library(title)
    return {"success": True}

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
    db.add_to_library(book)
    if await grab(title, magnet, settings):
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
    
    url = "https://api.audible.com/1.0/catalog/products"
    params = {
        "title": title,
        "response_groups": "product_plan_details,product_desc,contributors,product_attrs,media,product_extended_attrs,series",
        "image_sizes": "500"
    }
    async with httpx.AsyncClient() as client:
        try:
            res = await client.get(url, params=params, timeout=10.0)
            res.raise_for_status()
            return res.json()
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
