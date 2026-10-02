"""AudiobookBay: fetching pages and parsing search results and detail pages.

Search results come in two forms: plain posts, and posts whose HTML is base64-encoded
(<div class="post re-ab" style="display:none;">PGRpdi...</div>) and decoded by the
site's JavaScript. Both are parsed the same way once decoded."""
import asyncio
import base64
import binascii
import datetime
import logging
import os
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, List, Optional

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

BASE_URL = "https://audiobookbay.lu"  # The default; Settings > Indexers can change it
DEFAULT_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"


def _settings():
    from app import db  # Imported here: db is loaded after the scraper in some tools
    try:
        return db.get_settings()
    except Exception:
        return {}


def base_url():
    """The site's address: from Settings (the domain changes from time to time), else the default."""
    url = (_settings().get("abb_url") or "").strip().rstrip("/")
    return url if re.match(r"^https?://", url) else BASE_URL


def cookie():
    """The session cookie: from Settings, else the ABB_COOKIE environment variable."""
    return (_settings().get("abb_cookie") or os.environ.get("ABB_COOKIE", "")).strip()


def user_agent():
    return (_settings().get("abb_user_agent") or os.environ.get("ABB_USER_AGENT") or DEFAULT_USER_AGENT).strip()

RESULTS_PER_PAGE = 9
MAX_QUERY_LENGTH = 50  # The site's search box limit; longer queries find nothing

# Requests are spaced out so a big search doesn't hammer the site
_MIN_INTERVAL = 1.0
_next_slot = 0.0
PAUSE_AFTER_FAILURE = 5 * 60
_paused_until = 0.0


async def fetch_html(url: str, params: Optional[dict] = None) -> str:
    """Fetches HTML using urllib to bypass Cloudflare's httpx blocking."""
    headers = {"User-Agent": user_agent()}
    if cookie():
        headers["Cookie"] = cookie()

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

    global _paused_until
    if time.monotonic() < _paused_until:
        minutes = max(1, round((_paused_until - time.monotonic()) / 60))
        raise ConnectionError(f"AudiobookBay isn't responding; searches are paused for {minutes} more minute(s)")

    # Dropped connections, timeouts, rate limits and server errors are retried. If the
    # site still doesn't answer, searches pause for a while rather than keep trying.
    for backoff in (1, 3, None):
        try:
            return await _fetch_spaced(fetch)
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503, 504):
                raise
            error = e
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            error = e
        if backoff is None:
            _paused_until = time.monotonic() + PAUSE_AFTER_FAILURE
            logger.warning(f"AudiobookBay isn't responding ({error}); pausing searches for "
                           f"{PAUSE_AFTER_FAILURE // 60} minutes")
            raise error
        logger.info(f"AudiobookBay request failed ({error}); retrying in {backoff}s")
        await asyncio.sleep(backoff)


def is_paused():
    """True while searches are paused because the site stopped responding."""
    return time.monotonic() < _paused_until


async def _fetch_spaced(fetch):
    """Runs a request in the next free time slot."""
    global _next_slot
    now = time.monotonic()
    slot = max(now, _next_slot)
    _next_slot = slot + _MIN_INTERVAL
    if slot > now:
        await asyncio.sleep(slot - now)
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, fetch)


class _Cache:
    """A small time-limited cache (search pages and detail pages)."""

    def __init__(self, ttl, size):
        self.ttl, self.size, self.items = ttl, size, {}

    def get(self, key):
        hit = self.items.get(key)
        if hit and time.monotonic() - hit[0] < self.ttl:
            return hit[1]
        self.items.pop(key, None)
        return None

    def put(self, key, value):
        if len(self.items) >= self.size:
            oldest = min(self.items, key=lambda k: self.items[k][0])
            self.items.pop(oldest, None)
        self.items[key] = (time.monotonic(), value)
        return value


_search_cache = _Cache(ttl=15 * 60, size=300)
_detail_cache = _Cache(ttl=6 * 3600, size=1000)


# --- Values ------------------------------------------------------------------

_UNITS = {"KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3, "TB": 1024 ** 4}


def parse_size(text):
    """ "751.4 MBs" / "1.2 GB" -> (display string, bytes)"""
    m = re.search(r"([\d.,]+)\s*([KMGT])B", text or "", re.IGNORECASE)
    if not m:
        return "Unknown", 0
    try:
        num = float(m.group(1).replace(",", ""))
    except ValueError:
        return "Unknown", 0
    unit = m.group(2).upper() + "B"
    return f"{m.group(1)} {unit}", int(num * _UNITS[unit])


def parse_date(text):
    """ "5 Oct 2023" -> "2023-10-05" """
    for fmt in ("%d %b %Y", "%d %B %Y", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime((text or "").strip(), fmt).date().isoformat()
        except ValueError:
            continue
    return ""


def _plain(element):
    """An element's text with single spaces; ABB pads with &nbsp; (sometimes double-escaped)."""
    text = element.get_text(" ", strip=True) if element is not None else ""
    return re.sub(r"\s+", " ", text.replace("\xa0", " ").replace("&nbsp;", " ").replace("&nbsp", " ")).strip()


_FIELDS = {
    "format": r"Format:\s*([A-Za-z0-9]+)",
    "bitrate": r"Bitrate:\s*([\d.]+\s*K?bps|[A-Za-z]+)",
    "size": r"(?:Combined File Size|File Size|Size):\s*([\d.,]+\s*[KMGT]Bs?)",
    "posted": r"Posted:\s*(\d{1,2} [A-Za-z]{3,9} \d{4})",
    "language": r"Language:\s*([A-Za-z]+)",
    "uploader": r"Shared by:\s*([^\s|]+)",
}


def _field(text, name):
    m = re.search(_FIELDS[name], text, re.IGNORECASE)
    return m.group(1).strip() if m else ""


def _split_list(text):
    """ "Adventure\xa0 LitRPG\xa0" / "Fae&nbsp Fantasy&nbsp" -> ["Adventure", "LitRPG"]"""
    parts = re.split(r"\xa0|&nbsp;?|,|\s{2,}", text or "")
    return [p.strip() for p in parts if p.strip()]


def split_release_title(raw_title):
    """ABB titles end with " - Author": "Book Title [Series Name 7] - Jane Author".
    Returns (title, author); the author is "" when the title has no " - "."""
    parts = re.split(r"\s+-\s+", (raw_title or "").strip())
    if len(parts) >= 2:
        return " - ".join(parts[:-1]).strip(), parts[-1].strip()
    return (raw_title or "").strip(), ""


def parse_title(raw_title: str, known_author: Optional[str] = None) -> tuple[str, str]:
    """A display title and author for a listing: tags like [Series 7] and (Unabridged)
    are dropped from the title."""
    body, author = split_release_title(raw_title)
    # "Author - Title - Author" and "Author - Title" (author first)
    if known_author and " - " in body:
        first, _, rest = body.partition(" - ")
        if known_author.lower() in first.lower():
            body = rest
    title = re.sub(r"\[.*?\]", "", body)
    title = re.sub(r"\(\s*unabridged.*?\)", "", title, flags=re.IGNORECASE)
    title = re.sub(r",?\s*Chapteri[sz]ed", "", title, flags=re.IGNORECASE)
    title = re.sub(r"\s+", " ", title).strip(" -")
    return title or body, author or "Unknown"


# --- Search result pages -----------------------------------------------------

_B64 = re.compile(r"^[A-Za-z0-9+/=\s]{40,}$")


def _decode_post(post):
    """The post's HTML, decoding it when the site sent it base64-encoded."""
    text = post.get_text(strip=True)
    if post.select_one("div.postTitle") or not _B64.match(text or ""):
        return post
    try:
        decoded = base64.b64decode(re.sub(r"\s+", "", text)).decode("utf-8", errors="replace")
    except (binascii.Error, ValueError):
        return post
    return BeautifulSoup(f"<div class='post'>{decoded}</div>", "lxml").select_one("div.post")


def _parse_listing(post):
    """One search result: title, author, link, size, format, language and more."""
    title_link = post.select_one("div.postTitle h2 a, div.postTitle a")
    if not title_link:
        return None
    raw_title = re.sub(r"\s+", " ", title_link.get_text(" ", strip=True))
    link = title_link.get("href") or ""
    if link and not link.startswith("http"):
        link = urllib.parse.urljoin(base_url() + "/", link)

    text = _plain(post)
    info = post.select_one("div.postInfo")
    categories, keywords = [], []
    if info is not None:
        raw = info.get_text("\n")
        cat = re.search(r"Category:(.*?)(?:\n|Language:|$)", raw, re.S)
        categories = _split_list(cat.group(1)) if cat else []
        kw = re.search(r"Keywords:(.*)", raw, re.S)
        keywords = _split_list(kw.group(1)) if kw else []
    size_str, size_bytes = parse_size(_field(text, "size"))
    fmt = _field(text, "format").upper()
    bitrate = _field(text, "bitrate")
    image = post.select_one("div.postContent img") or post.select_one("img")
    title, author = parse_title(raw_title)
    return {
        "title": title,
        "author": author,
        "raw_title": raw_title,
        "link": link,
        "size_str": size_str,
        "size_bytes": size_bytes,
        "bitrate": bitrate or "Unknown",
        "language": _field(text, "language") or "Unknown",
        "format": fmt or "Unknown",
        "categories": categories,
        "keywords": keywords,
        "posted": parse_date(_field(text, "posted")),
        "uploader": _field(text, "uploader"),
        "cover": (image.get("src") or "") if image else "",
    }


def parse_search_page(html: str) -> List[Dict]:
    """Every result on a search page, in the site's order. The sidebar is ignored."""
    soup = BeautifulSoup(html, "lxml")
    content = soup.select_one("#content") or soup
    results = []
    for post in content.select("div.post"):
        listing = _parse_listing(_decode_post(post))
        if listing:
            results.append(listing)
    return results


def last_page(html: str) -> int:
    pages = [int(n) for n in re.findall(r"/page/(\d+)/", html)]
    return max(pages, default=1)


# Kept for callers that parse pages directly
def _parse_search_page(html: str, known_author: Optional[str] = None) -> List[Dict]:
    return parse_search_page(html)


def clean_query(query: str) -> str:
    """Trims a query to the site's 50-character limit at a word boundary."""
    query = re.sub(r"\s+", " ", re.sub(r"[^\w\s'&.-]", " ", query or "")).strip()
    if len(query) <= MAX_QUERY_LENGTH:
        return query
    return query[:MAX_QUERY_LENGTH].rsplit(" ", 1)[0]


async def search_page(query: str, page: int = 1) -> tuple[List[Dict], int]:
    """One page of search results and the number of pages there are. Cached for a while."""
    query = clean_query(query)
    key = (query.lower(), page)
    cached = _search_cache.get(key)
    if cached is not None:
        return cached
    url = f"{base_url()}/page/{page}/" if page > 1 else f"{base_url()}/"
    html = await fetch_html(url, {"s": query} if query else None)
    if "cf-browser-verification" in html or "challenge-platform" in html and "postTitle" not in html:
        raise RuntimeError("AudiobookBay returned a Cloudflare check instead of results. Add your AudiobookBay cookie in Settings > Indexers.")
    results = parse_search_page(html)
    return _search_cache.put(key, (results, last_page(html)))


async def search_audiobooks(query: str, offset: int = 0, limit: int = 100, known_author: Optional[str] = None) -> List[Dict]:
    """Search results for a free-text query, with magnets (for Torznab clients)."""
    start_page = (offset // RESULTS_PER_PAGE) + 1
    pages_to_fetch = min(5, max(1, (limit // RESULTS_PER_PAGE) + 1))

    all_results = []
    for page in range(start_page, start_page + pages_to_fetch):
        try:
            results, pages = await search_page(query, page)
        except Exception as e:
            logger.error(f"Error fetching search results for '{query}' on page {page}: {e}", exc_info=True)
            break
        all_results.extend(results)
        if len(results) < RESULTS_PER_PAGE or page >= pages:
            break

    skip = offset % RESULTS_PER_PAGE
    final_results = all_results[skip:skip + limit]
    # Detail pages (with the magnet) only for the first few results and ones already
    # cached: loading every one, a second apart, would outlast the client's time-out. The
    # rest link to /api/download, which finds the magnet when the release is grabbed.
    await add_details([r for i, r in enumerate(final_results)
                       if i < TORZNAB_DETAILS or _detail_cache.get(r.get("link")) is not None])
    return final_results


TORZNAB_DETAILS = 10


# --- Detail pages ------------------------------------------------------------

def parse_detail(html: str, title: str = "") -> Dict:
    """Everything useful on a book's page: magnet, authors, narrators, format, bitrate,
    abridged or not, file list and total size."""
    soup = BeautifulSoup(html, "lxml")
    post = soup.select_one("#content div.post") or soup.select_one("div.post") or soup
    text = _plain(post)

    # Info hash, trackers and total size from the torrent table
    infohash, trackers, total = None, [], ""
    for row in post.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) >= 2:
            label = cells[0].get_text(strip=True)
            value = cells[1].get_text(strip=True)
            if label.startswith("Info Hash"):
                infohash = value
            elif label.startswith("Combined File Size"):
                total = _plain(cells[1])
            elif label.startswith(("Tracker", "Announce URL")) and value and value not in trackers:
                trackers.append(value)
    if not infohash:
        m = re.search(r"Info Hash:\s*([0-9a-fA-F]{40})", text)
        infohash = m.group(1) if m else None

    magnet = None
    if infohash:
        magnet = f"magnet:?xt=urn:btih:{infohash}"
        if title:
            magnet += f"&dn={urllib.parse.quote(title)}"
        for tr in trackers:
            magnet += f"&tr={urllib.parse.quote(tr)}"
    else:
        link = post.find("a", href=re.compile(r"^magnet:"))
        if link:
            magnet = link.get("href")
            if title and "&dn=" not in magnet:
                magnet += f"&dn={urllib.parse.quote(title)}"

    desc = post.select_one("div.desc") or post
    authors = [a.get_text(" ", strip=True) for a in desc.select("span.author")]
    narrators = [n.get_text(" ", strip=True) for n in desc.select("span.narrator")]
    desc_text = desc.get_text(" | ", strip=True).replace("\xa0", " ")
    if not authors:
        m = re.search(r"Written by:?\s*\|?\s*([^|]+)", desc_text, re.IGNORECASE)
        authors = [m.group(1).strip()] if m else []
    if not narrators:
        m = re.search(r"(?:Read by|Narrated by|Narrator)s?:?\s*\|?\s*([^|]+)", desc_text, re.IGNORECASE)
        if m:
            narrators = [n.strip() for n in re.split(r",|&|\band\b", m.group(1)) if n.strip()]
    fmt = (desc.select_one("span.format").get_text(strip=True) if desc.select_one("span.format")
           else _field(_plain(desc), "format"))
    bitrate = (desc.select_one("span.bitrate").get_text(strip=True) if desc.select_one("span.bitrate")
               else _field(_plain(desc), "bitrate"))
    # "Unabridged" / "Abridged" is its own line next to the format, before the blurb
    abridged = any(piece.strip().lower() == "abridged" for piece in desc_text.split("|")[:15])

    files = []
    for cell in post.select("td[colspan]"):
        m = re.match(r"(.+\.(?:m4b|m4a|mp3|aac|flac|ogg|opus|wma|wav))\s+([\d.,]+\s*[KMGT]Bs?)$", _plain(cell), re.IGNORECASE)
        if m:
            files.append({"name": m.group(1).strip(), "size_bytes": parse_size(m.group(2))[1]})
    total_str, total_bytes = parse_size(total or _field(text, "size"))
    date = soup.select_one("meta[itemprop=datePublished]")

    narrator = ", ".join(narrators) if narrators else "Unknown"
    return {
        "magnet": magnet,
        "narrator": narrator[:120],
        "narrators": narrators,
        "authors": authors,
        "format": (fmt or "").upper(),
        "bitrate": bitrate,
        "abridged": abridged,
        "files": files,
        "size_bytes": total_bytes,
        "size_str": total_str if total_bytes else "",
        "posted": (date.get("content") if date else "") or "",
    }


async def fetch_detail(detail_url: str, title: str = "") -> Optional[Dict]:
    """A book's detail page, parsed. Cached for a few hours."""
    # Only ever fetch AudiobookBay pages; the URL comes from API callers
    parsed = urllib.parse.urlparse(detail_url or "")
    allowed = {urllib.parse.urlparse(base_url()).netloc, urllib.parse.urlparse(BASE_URL).netloc}
    if parsed.scheme not in ("http", "https") or parsed.netloc not in allowed:
        logger.warning(f"Refusing to fetch non-AudiobookBay URL: {detail_url}")
        return None
    cached = _detail_cache.get(detail_url)
    if cached is None:
        logger.debug(f"Fetching detail page: {detail_url}")
        try:
            html = await fetch_html(detail_url)
        except Exception as e:
            logger.error(f"Error fetching detail page {detail_url}: {e}", exc_info=True)
            return None
        cached = parse_detail(html)
        # A page without a magnet is usually a Cloudflare check or a hiccup: not kept, so
        # the next try fetches it again rather than failing for hours
        if cached.get("magnet"):
            _detail_cache.put(detail_url, cached)
        elif "challenge-platform" in html or "cf-browser-verification" in html:
            logger.warning(f"AudiobookBay returned a Cloudflare check for {detail_url}; a fresh cookie may help")
    detail = dict(cached)
    if title and detail.get("magnet") and "&dn=" not in detail["magnet"]:
        detail["magnet"] = detail["magnet"].replace("&tr=", f"&dn={urllib.parse.quote(title)}&tr=", 1) \
            if "&tr=" in detail["magnet"] else detail["magnet"] + f"&dn={urllib.parse.quote(title)}"
    return detail


async def fetch_detail_info(detail_url: str, title: str = "") -> Optional[Dict[str, str]]:
    """The magnet link and narrator from a book's page."""
    detail = await fetch_detail(detail_url, title)
    if detail is None:
        return None
    return {"magnet": detail["magnet"], "narrator": detail["narrator"]}


def merge_detail(result, detail):
    """Adds what a detail page says to a search result."""
    if not detail:
        return result
    result["details_loaded"] = True
    result["magnet_url"] = detail.get("magnet")
    result["abb_narrator"] = detail.get("narrator") or "Unknown"
    result["narrators"] = detail.get("narrators") or []
    result["authors"] = detail.get("authors") or []
    result["abridged"] = detail.get("abridged", False)
    result["files"] = detail.get("files") or []
    for key in ("format", "bitrate"):
        if detail.get(key) and result.get(key) in (None, "", "Unknown"):
            result[key] = detail[key]
    if detail.get("size_bytes") and not result.get("size_bytes"):
        result["size_bytes"], result["size_str"] = detail["size_bytes"], detail["size_str"]
    if detail.get("posted") and not result.get("posted"):
        result["posted"] = detail["posted"]
    return result


async def add_details(results, concurrency=3):
    """Loads the detail page of each result (magnet, narrator, files)."""
    semaphore = asyncio.Semaphore(concurrency)

    async def load(res):
        async with semaphore:
            merge_detail(res, await fetch_detail(res["link"], res.get("title", "")))

    if results:
        await asyncio.gather(*[load(r) for r in results])
    return results


async def test_connection(url="", cookie_value=None, agent=""):
    """Reaches the site and says whether the cookie logs in. Unsaved values from the form
    can be tried before saving them."""
    global _paused_until
    url = (url or base_url()).strip().rstrip("/")
    headers = {"User-Agent": (agent or user_agent()).strip()}
    value = cookie() if cookie_value is None else cookie_value.strip()
    if value:
        headers["Cookie"] = value
    req = urllib.request.Request(url + "/", headers=headers)
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE

    def fetch():
        with urllib.request.urlopen(req, context=context, timeout=20.0) as response:
            return response.read().decode("utf-8", errors="ignore")

    try:
        html = await asyncio.get_running_loop().run_in_executor(None, fetch)
    except Exception as e:
        return {"ok": False, "logged_in": False, "message": f"Couldn't reach {url}: {e}"}
    _paused_until = 0.0  # It answers again: no need to keep searches paused
    if "challenge-platform" in html and "postTitle" not in html:
        return {"ok": False, "logged_in": False, "message": "Cloudflare blocked the request. A fresh cookie (with the matching user agent) usually helps."}
    logged_in = bool(re.search(r"log\s*out|logout", html, re.IGNORECASE))
    if logged_in:
        return {"ok": True, "logged_in": True, "message": "Connected and logged in."}
    if value:
        return {"ok": True, "logged_in": False, "message": "Connected, but the cookie doesn't log in (it may have expired). Searching still works."}
    return {"ok": True, "logged_in": False, "message": "Connected (no cookie set)."}
