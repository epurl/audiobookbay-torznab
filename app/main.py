from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.responses import RedirectResponse
import traceback

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
async def torznab_api(request: Request, t: str = "", q: str = "", author: str = "", title: str = ""):
    """Main Torznab endpoint for indexer queries."""
    
    # Return Capabilities
    if t == "caps":
        xml = build_caps()
        return Response(content=xml, media_type="application/xml")
        
    # Handle Search Queries
    if t in ("search", "book"):
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
            results = await search_audiobooks(search_query)
        except Exception as e:
            print(f"Error during search: {e}")
            traceback.print_exc()
            results = []
            
        host_url = f"{request.url.scheme}://{request.url.netloc}"
        xml = build_rss(results, host_url)
        return Response(content=xml, media_type="application/xml")

    # Fallback for unsupported operations
    return Response(content="<?xml version=\"1.0\" encoding=\"UTF-8\"?><error code=\"201\" description=\"Incorrect parameter\"/>", media_type="application/xml")

@app.get("/api/download")
async def download_magnet(url: str):
    """Simulates downloading a torrent by fetching the detail page and redirecting to the extracted magnet link."""
    if not url:
        raise HTTPException(status_code=400, detail="Missing url parameter")
        
    magnet = await get_magnet_link(url)
    if not magnet:
        raise HTTPException(status_code=404, detail="Could not find magnet link for this audiobook")
        
    # Redirect standard download cliens to the magnet link
    return RedirectResponse(magnet)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
