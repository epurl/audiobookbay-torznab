from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.responses import RedirectResponse
import traceback
import logging

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

from app.scraper import search_audiobooks, get_magnet_link
from app.torznab import build_caps, build_rss

app = FastAPI(title="Audiobookbay Torznab Indexer")

@app.get("/")
async def root():
    """Root endpoint, useful for health checks."""
    return {"message": "Audiobookbay Torznab Indexer is running", "api_endpoint": "/api"}

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
async def download_magnet(url: str):
    """Simulates downloading a torrent by fetching the detail page and redirecting to the extracted magnet link."""
    logger.info(f"Download requested for URL: {url}")
    if not url:
        logger.warning("Download requested without URL parameter")
        raise HTTPException(status_code=400, detail="Missing url parameter")
        
    magnet = await get_magnet_link(url)
    if not magnet:
        logger.error(f"Failed to find magnet link for URL: {url}")
        raise HTTPException(status_code=404, detail="Could not find magnet link for this audiobook")
        
    # Redirect standard download cliens to the magnet link
    logger.info(f"Successfully resolved magnet link for {url}, redirecting client.")
    return RedirectResponse(magnet)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
