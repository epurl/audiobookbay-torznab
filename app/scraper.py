import asyncio
import logging
import os
import re
import ssl
import urllib.parse
import urllib.request
from typing import Dict, List, Optional

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

BASE_URL = "https://audiobookbay.lu"
ABB_COOKIE = os.environ.get("ABB_COOKIE", "")
USER_AGENT = os.environ.get("ABB_USER_AGENT", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

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

def parse_title(raw_title: str, known_author: Optional[str] = None) -> tuple[str, str]:
    author = "Unknown"
    title = raw_title
    
    # Clean up common tags
    title = re.sub(r'\[.*?\]', '', title)
    title = re.sub(r'\(unabridged.*?\)', '', title, flags=re.IGNORECASE)
    title = re.sub(r',?\s*Chapterized', '', title, flags=re.IGNORECASE)
    
    # Try splitting by ' by '
    if ' by ' in title.lower():
        parts = re.split(r'(?i)\s+by\s+', title)
        if len(parts) == 2:
            author = parts[1].strip()
            title = parts[0].strip()
            return title, author

    # Try splitting by ' - '
    parts = title.split(' - ')
    if len(parts) >= 2:
        last_part = parts[-1].strip()
        first_part = parts[0].strip()
        
        # If we have a known author from the UI search, let's use that to figure out which part is the author
        if known_author:
            # If the known author matches the first part closely (e.g. Orson Scott Card == Orson Scott Card)
            if known_author.lower() in first_part.lower():
                author = first_part
                title = ' - '.join(parts[1:]).strip()
            # If the known author matches the last part closely
            elif known_author.lower() in last_part.lower():
                author = last_part
                title = ' - '.join(parts[:-1]).strip()
            else:
                # Fallback if no match
                author = last_part
                title = ' - '.join(parts[:-1]).strip()
        else:
            first_words = first_part.split()
            # Check if first part looks like a name (2 to 4 words, Title Case)
            is_first_part_name = False
            if 2 <= len(first_words) <= 4:
                # Check if all words start with uppercase
                if all(w[0].isupper() for w in first_words if w.strip()):
                    is_first_part_name = True

            # Use the length comparison or title case heuristic
            if is_first_part_name and len(last_part.split()) >= len(first_words):
                author = first_part
                title = ' - '.join(parts[1:]).strip()
            else:
                author = last_part
                title = ' - '.join(parts[:-1]).strip()
            
    # Clean up double spaces
    title = re.sub(r'\s+', ' ', title).strip()
    return title, author

def _parse_search_page(html: str, known_author: Optional[str] = None) -> List[Dict]:
    soup = BeautifulSoup(html, "lxml")
    results = []
    
    # Simple parse targeting standard post layout on the site
    posts = soup.select('div.post')
    
    for post in posts:
        title_element = post.select_one('div.postTitle h2 a')
        if not title_element:
            continue
            
        raw_title = title_element.text.strip()
        title, author = parse_title(raw_title, known_author)
        
        link = title_element.get('href')
        if link and not link.startswith('http'):
            link = BASE_URL + link
            
        # Try to extract size and category. They are usually text inside post details.
        # Example format: "Format: mp3 | Size: 1.2 GB | Bitrate: 64 kbps"
        content_text = post.getText(separator=' ', strip=True)
        
        size = "Unknown"
        size_match = re.search(r'Size:\s*([\d\.]+\s*(?:MB|GB|KB))', content_text, re.IGNORECASE)
        if size_match:
            size_str = size_match.group(1)
            size = size_str

        bitrate = "Unknown"
        bitrate_match = re.search(r'Bitrate:\s*(\d+\s*kbps)', content_text, re.IGNORECASE)
        if bitrate_match:
            bitrate = bitrate_match.group(1)
            
        language = "Unknown"
        lang_match = re.search(r'Language:\s*([\w]+)', content_text, re.IGNORECASE)
        if lang_match:
            language = lang_match.group(1)

        # Example: "Format: M4B / Bitrate: 64 Kbps"
        audio_format = "Unknown"
        format_match = re.search(r'Format:\s*([A-Za-z0-9]+)', content_text, re.IGNORECASE)
        if format_match:
            audio_format = format_match.group(1).upper()

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

        # Build basic result
        results.append({
            "title": title,
            "author": author,
            "link": link,
            "size_str": size,
            "size_bytes": size_bytes,
            "bitrate": bitrate,
            "language": language,
            "format": audio_format,
        })
        
    return results

async def search_audiobooks(query: str, offset: int = 0, limit: int = 100, known_author: Optional[str] = None) -> List[Dict]:
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
            return _parse_search_page(html, known_author)
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
    final_results = all_results[items_to_skip:items_to_skip + limit]
    
    # Fetch magnets concurrently
    async def populate_details(res):
        detail_info = await fetch_detail_info(res['link'], res['title'])
        if detail_info:
            res['magnet_url'] = detail_info.get('magnet')
            res['abb_narrator'] = detail_info.get('narrator', 'Unknown')
            
    if final_results:
        await asyncio.gather(*[populate_details(r) for r in final_results])
        
    return final_results

async def search_for_book(title: str, author: str = "", limit: int = 10) -> List[Dict]:
    """Searches ABB for a specific book, as used by the UI and the auto-downloader."""
    clean_title = title.strip()
    query = clean_title

    # Heuristic: if the title is very short (1-2 words), append the first word of the author
    # to avoid thousands of unrelated results (e.g. searching just "Lost")
    if len(query.split()) <= 2 and author:
        author_first = author.split()[0].replace(',', '')
        query = f"{query} {author_first}"

    if not query:
        return []

    results = await search_audiobooks(query, limit=limit, known_author=author)

    # Fallback to pure title search if the restrictive query yielded no results
    if not results and query != clean_title:
        logger.info(f"No results for '{query}', falling back to title only: '{clean_title}'")
        results = await search_audiobooks(clean_title, limit=limit, known_author=author)

    return results

async def fetch_detail_info(detail_url: str, title: str = "") -> Optional[Dict[str, str]]:
    """Fetches the detail page and extracts the InfoHash/magnet and the narrator."""
    # Only ever fetch AudiobookBay pages; the URL comes from API callers
    parsed = urllib.parse.urlparse(detail_url or "")
    if parsed.scheme not in ("http", "https") or parsed.netloc != urllib.parse.urlparse(BASE_URL).netloc:
        logger.warning(f"Refusing to fetch non-AudiobookBay URL: {detail_url}")
        return None
    logger.debug(f"Fetching detail page to extract info: {detail_url}")
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
    narrator = "Unknown"
    content_text = soup.getText(separator=' ', strip=True)
    narr_match = re.search(r'(?:Read by|Narrator|Narrated by):\s*([^•\|]+)', content_text, re.IGNORECASE)
    if narr_match:
        n_str = narr_match.group(1).strip()
        for sep in ['Format:', 'Bitrate:', 'File Size:', 'Size:', 'Posted:', 'Category:', 'Language:']:
            if sep in n_str:
                n_str = n_str.split(sep)[0].strip()
        if len(n_str) > 60:
            n_str = n_str[:60].strip()
        if n_str:
            narrator = n_str

    if infohash:
        # Build magnet link
        magnet = f"magnet:?xt=urn:btih:{infohash}"
        
        if title:
            magnet += f"&dn={urllib.parse.quote(title)}"
            
        trackers = []
        for row in soup.find_all('tr'):
            tds = row.find_all('td')
            if len(tds) >= 2 and "Tracker:" in tds[0].text:
                trackers.append(tds[1].text.strip())
                
        for tr in trackers:
            magnet += f"&tr={urllib.parse.quote(tr)}"
            
        return {"magnet": magnet, "narrator": narrator}
        
    # 2. Try looking for an existing magnet link in a href
    magnet_link = soup.find('a', href=re.compile(r'^magnet:'))
    if magnet_link:
        magnet = magnet_link.get('href')
        if title and "&dn=" not in magnet:
            magnet += f"&dn={urllib.parse.quote(title)}"
        return {"magnet": magnet, "narrator": narrator}
        
    return {"magnet": None, "narrator": narrator}
