import httpx
from bs4 import BeautifulSoup
import re
import urllib.parse
from typing import List, Dict, Optional
import os
import logging

logger = logging.getLogger(__name__)

BASE_URL = "http://audiobookbay.lu"
ABB_COOKIE = os.environ.get("ABB_COOKIE", "")
USER_AGENT = os.environ.get("ABB_USER_AGENT", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

async def fetch_html(url: str, params: Optional[dict] = None) -> str:
    headers = {"User-Agent": USER_AGENT}
    if ABB_COOKIE:
        headers["Cookie"] = ABB_COOKIE
        
    async with httpx.AsyncClient(follow_redirects=True, verify=False) as client:
        response = await client.get(
            url, 
            params=params,
            headers=headers
        )
        response.raise_for_status()
        return response.text

async def search_audiobooks(query: str) -> List[Dict]:
    """Scrapes audiobookbay for the given query."""
    
    # Audiobookbay search URL structure: /page/1?s=term
    url = f"{BASE_URL}/page/1"
    params = {"s": query} if query else {}
    
    logger.debug(f"Fetching search results from {url} with params {params}")
    try:
        html = await fetch_html(url, params)
    except Exception as e:
        logger.error(f"Error fetching search results for '{query}': {e}", exc_info=True)
        return []

    soup = BeautifulSoup(html, "lxml")
    results = []
    
    # Simple parse targeting standard post layout on the site
    posts = soup.select('div.post')
    
    for post in posts:
        title_element = post.select_one('div.postTitle h2 a')
        if not title_element:
            continue
            
        title = title_element.text.strip()
        link = title_element.get('href')
        if link and not link.startswith('http'):
            link = BASE_URL + link
            
        # Try to extract size and category. They are usually text inside post details.
        # Example format: "Format: mp3 | Size: 1.2 GB"
        content_text = post.getText(separator=' ', strip=True)
        
        size = "Unknown"
        size_match = re.search(r'Size:\s*([\d\.]+\s*(?:MB|GB|KB))', content_text, re.IGNORECASE)
        if size_match:
            size_str = size_match.group(1)
            size = size_str

        # Estimate size in bytes for torznab
        size_bytes = 0
        if size != "Unknown":
            try:
                num_match = re.search(r'[\d\.]+', size)
                if num_match:
                    num = float(num_match.group())
                    if "GB" in size.upper():
                        size_bytes = int(num * 1024 * 1024 * 1024)
                    elif "MB" in size.upper():
                        size_bytes = int(num * 1024 * 1024)
                    elif "KB" in size.upper():
                        size_bytes = int(num * 1024)
            except Exception:
                pass

        # Try to find author
        author = "Unknown"
        # Often it comes after Title or Category in some formats. Let's do a basic regex.
        # Sometimes title is "Author - Title"
        if "-" in title:
            parts = title.split("-", 1)
            author = parts[0].strip()
            # Clean title
            title = parts[1].strip()

        # Build basic result
        results.append({
            "title": title,
            "author": author,
            "link": link,
            "size_str": size,
            "size_bytes": size_bytes,
        })
        
    return results

async def get_magnet_link(detail_url: str) -> Optional[str]:
    """Fetches the detail page and extracts the InfoHash to build a magnet link."""
    logger.debug(f"Fetching detail page to extract magnet link: {detail_url}")
    try:
        html = await fetch_html(detail_url)
    except Exception as e:
        logger.error(f"Error fetching detail page {detail_url}: {e}", exc_info=True)
        return None
        
    soup = BeautifulSoup(html, "lxml")
    
    # 1. Try to find the infohash directly from the table
    infohash = None
    
    cells = soup.find_all('td')
    for i, cell in enumerate(cells):
        if "Info Hash:" in cell.text:
            if i + 1 < len(cells):
                infohash = cells[i+1].text.strip()
                break
                
    if infohash:
        # Build magnet link
        return f"magnet:?xt=urn:btih:{infohash}"
        
    # 2. Try looking for an existing magnet link in a href
    magnet_link = soup.find('a', href=re.compile(r'^magnet:'))
    if magnet_link:
        return magnet_link.get('href')
        
    return None
