import httpx
from bs4 import BeautifulSoup
import re
import urllib.parse
from typing import List, Dict, Optional
import os
import logging

logger = logging.getLogger(__name__)

BASE_URL = "https://audiobookbay.lu"
ABB_COOKIE = os.environ.get("ABB_COOKIE", "")
USER_AGENT = os.environ.get("ABB_USER_AGENT", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

import urllib.request
import urllib.parse
import ssl
import asyncio

async def fetch_html(url: str, params: Optional[dict] = None) -> str:
    """Fetches HTML using urllib to bypass Cloudflare's httpx blocking."""
    headers = {"User-Agent": USER_AGENT}
    if ABB_COOKIE:
        headers["Cookie"] = ABB_COOKIE
    
    req = urllib.request.Request(url, headers=headers)
    
    # If we have params (search query), send as POST to avoid Cloudflare 301 on GET ?s=
    if params:
        query_string = urllib.parse.urlencode(params)
        req.data = query_string.encode('ascii')
        req.method = 'POST'
    
    # Bypass SSL verification
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE

    def fetch():
        with urllib.request.urlopen(req, context=context, timeout=30.0) as response:
            return response.read().decode('utf-8', errors='ignore')

    loop = asyncio.get_running_loop()
    html = await loop.run_in_executor(None, fetch)
    return html
def _parse_search_page(html: str) -> List[Dict]:
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
        # Often it comes after Title or Category in some formats, but trying to parse it
        # from the title leads to truncated titles because the format varies wildly
        # (e.g. Title - Author vs Author - Title). Pass full title to Jackett/Prowlarr.

        # Build basic result
        results.append({
            "title": title,
            "author": author,
            "link": link,
            "size_str": size,
            "size_bytes": size_bytes,
        })
        
    return results

async def search_audiobooks(query: str, offset: int = 0, limit: int = 100) -> List[Dict]:
    """Scrapes audiobookbay for the given query, supporting pagination."""
    
    # Audiobookbay generally returns 9 items per page
    start_page = (offset // 9) + 1
    
    # Fetch up to 5 pages per query to avoid spamming the server
    pages_to_fetch = min(5, max(1, (limit // 9) + 1))
    
    async def fetch_and_parse(page_num: int):
        if query:
            url = f"{BASE_URL}/page/{page_num}/"
            params = {"s": query}
        else:
            if page_num == 1:
                url = f"{BASE_URL}/"
            else:
                url = f"{BASE_URL}/page/{page_num}/"
            params = {}
            
        logger.debug(f"Fetching search results from {url} with params {params}")
        try:
            html = await fetch_html(url, params)
            return _parse_search_page(html)
        except Exception as e:
            logger.error(f"Error fetching search results for '{query}' on page {page_num}: {e}", exc_info=True)
            return []

    # Sequentially fetch pages to avoid Cloudflare rate limits and timeouts
    all_results = []
    for i in range(pages_to_fetch):
        page_res = await fetch_and_parse(start_page + i)
        if isinstance(page_res, list):
            all_results.extend(page_res)
            # If a page returned less than 9 items, it's the last available page of results
            if len(page_res) < 9:
                break
                
    # Calculate how many items to skip from the first fetched page
    items_to_skip = offset % 9
    return all_results[items_to_skip:items_to_skip + limit]

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
