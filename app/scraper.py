"""AudiobookBay: fetching pages and parsing search results and detail pages.

Search results come in two forms: plain posts, and posts whose HTML is base64-encoded
(<div class="post re-ab" style="display:none;">PGRpdi...</div>) and decoded by the
site's JavaScript. Both are parsed the same way once decoded."""
import asyncio
import base64
import binascii
import contextvars
import datetime
import json
import logging
import os
import re
import ssl
import tempfile
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

# --- Being a light user of the site --------------------------------------------------
# Every request is counted and spaced out: wider apart for automatic work than for a search
# you start. At the first sign the site is unhappy (a block, a rate limit, a server error, a
# Cloudflare check) all requests pause, and longer each time it happens again; a successful
# request resets that. Automatic work also stops for the day after BACKGROUND_DAILY_CAP.

# What a request is for: set by the caller ("background" for automatic searches and the
# new-uploads check, "torznab" for Prowlarr & co.); anything else is a search you started
PURPOSE = contextvars.ContextVar("abb_purpose", default="interactive")
SPACING = {"interactive": 1.0, "torznab": 2.0, "background": 4.0}  # seconds between requests
BACKGROUND_DAILY_CAP = 300
PAUSES = (5 * 60, 30 * 60, 2 * 3600, 6 * 3600)  # after the 1st, 2nd, 3rd, later warning in a row
_next_slot = 0.0
_paused_until = 0.0
_pause_level = 0
_pause_reason = ""
_counts = {}  # date -> {purpose: requests}
_inflight = {}  # a page being fetched -> the task fetching it, so callers share one request


class SiteUnavailable(ConnectionError):
    """AudiobookBay isn't asked right now: paused after a warning, or the day's automatic
    requests are used up."""


def _human(seconds):
    return f"{round(seconds / 3600)} hours" if seconds >= 2 * 3600 else f"{max(1, round(seconds / 60))} minutes"


def _count(purpose):
    today = datetime.date.today().isoformat()
    for day in sorted(_counts)[:-7]:  # A week is kept
        del _counts[day]
    day = _counts.setdefault(today, {})
    day[purpose] = day.get(purpose, 0) + 1


def requests_today(purpose=None):
    day = _counts.get(datetime.date.today().isoformat(), {})
    return day.get(purpose, 0) if purpose else sum(day.values())


def is_paused():
    """True while requests are paused after a warning sign from the site."""
    return time.monotonic() < _paused_until


def background_allowed():
    """Automatic work may ask the site: not paused, and the day's allowance not used up."""
    return not is_paused() and requests_today("background") < BACKGROUND_DAILY_CAP


def status():
    """For Settings > Indexers: requests today, and any pause and why."""
    left = max(0, _paused_until - time.monotonic())
    return {"paused": left > 0, "paused_for": round(left), "reason": _pause_reason if left > 0 else "",
            "today": {**_counts.get(datetime.date.today().isoformat(), {})}, "total_today": requests_today(),
            "background_cap": BACKGROUND_DAILY_CAP}


def resume():
    """Lifts a pause (the Test button: you've just checked the site answers)."""
    global _paused_until, _pause_level, _pause_reason
    _paused_until, _pause_level, _pause_reason = 0.0, 0, ""


def _pause(reason, at_least=0):
    global _paused_until, _pause_level, _pause_reason
    seconds = max(PAUSES[min(_pause_level, len(PAUSES) - 1)], at_least)
    _pause_level += 1
    _paused_until = time.monotonic() + seconds
    _pause_reason = reason
    logger.warning(f"AudiobookBay: {reason}; pausing all requests to it for {_human(seconds)}")


def _looks_like_challenge(html):
    """A Cloudflare check instead of the page (markers only its challenge pages have)."""
    head = html[:20000]
    return "_cf_chl_opt" in head or "cf-browser-verification" in head or "<title>just a moment" in head.lower()


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

    context = _tls_context()

    def fetch():
        with urllib.request.urlopen(req, context=context, timeout=30.0) as response:
            return response.read().decode('utf-8', errors='ignore')

    purpose = PURPOSE.get()
    if is_paused():
        raise SiteUnavailable(f"AudiobookBay requests are paused for {_human(_paused_until - time.monotonic())} "
                              f"({_pause_reason})")
    if purpose == "background" and not background_allowed():
        raise SiteUnavailable(f"Today's {BACKGROUND_DAILY_CAP} automatic AudiobookBay requests are used up")

    # A dropped connection or time-out is tried once more. Anything that says the site is
    # unhappy (a block, a rate limit, a server error, a Cloudflare check) isn't retried:
    # all requests pause instead.
    for attempt in (1, 2):
        try:
            html = await _fetch_spaced(fetch, purpose)
        except urllib.error.HTTPError as e:
            if e.code in (403, 429, 500, 502, 503, 504):
                retry_after = e.headers.get("Retry-After", "") if e.headers else ""
                _pause(f"it answered {e.code}" + (" (rate limit)" if e.code == 429 else "")
                       + (": your cookie may need refreshing" if e.code == 403 else ""),
                       int(retry_after) if retry_after.isdigit() else 0)
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            if attempt == 2:
                _pause(f"it isn't responding ({e})")
                raise
            logger.info(f"AudiobookBay request failed ({e}); trying once more in 5s")
            await asyncio.sleep(5)
            continue
        if _looks_like_challenge(html):
            _pause("it returned a Cloudflare check instead of the page: refresh your cookie in Settings > Indexers")
            raise SiteUnavailable("AudiobookBay returned a Cloudflare check instead of the page. Add a fresh "
                                  "AudiobookBay cookie in Settings > Indexers.")
        resume_level()
        return html


def resume_level():
    """A request went through: the next warning starts the pauses from the shortest again."""
    global _pause_level
    _pause_level = 0


def _tls_context(verify=None):
    """The site's certificate is checked (so nobody in between can swap results or read the
    cookie), unless Settings > Indexers turns that off for a mirror with a broken one."""
    context = ssl.create_default_context()
    if verify is None:
        verify = _settings().get("abb_verify_tls", True)
    if not verify:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context


async def _fetch_spaced(fetch, purpose="interactive"):
    """Runs a request in the next free time slot, and counts it."""
    global _next_slot
    now = time.monotonic()
    slot = max(now, _next_slot)
    _next_slot = slot + SPACING.get(purpose, 1.0)
    if slot > now:
        await asyncio.sleep(slot - now)
    _count(purpose)
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, fetch)


async def _shared(key, make):
    """One request for a page however many callers want it at once (e.g. Prowlarr and a
    BorgArr search asking for the same page)."""
    task = _inflight.get(key)
    if task is None:
        task = asyncio.ensure_future(make())
        _inflight[key] = task
        task.add_done_callback(lambda t: _inflight.pop(key, None) if _inflight.get(key) is t else None)
    return await asyncio.shield(task)


class _Cache:
    """A small time-limited cache (search pages and detail pages). With a file, entries are
    kept there too, so they survive restarts."""

    def __init__(self, ttl, size, file=None):
        self.ttl, self.size, self.items, self.file, self.loaded = ttl, size, {}, file, file is None

    def _load(self):
        if self.loaded:
            return
        self.loaded = True
        try:
            with open(self.file(), "r", encoding="utf-8") as f:
                stored = json.load(f)
            now = time.time()
            self.items = {k: (t, v) for k, (t, v) in stored.items() if now - t < self.ttl}
        except (OSError, ValueError, TypeError):
            self.items = {}

    def _save(self):
        if not self.file:
            return
        try:
            path = self.file()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".abb-cache-", suffix=".json")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.items, f)
            os.replace(tmp, path)
        except OSError as e:
            logger.warning(f"Couldn't save the AudiobookBay cache: {e}")

    def get(self, key, ttl=None):
        self._load()
        hit = self.items.get(key)
        if hit and time.time() - hit[0] < (ttl or self.ttl):
            return hit[1]
        return None

    def put(self, key, value):
        self._load()
        if len(self.items) >= self.size:
            oldest = min(self.items, key=lambda k: self.items[k][0])
            self.items.pop(oldest, None)
        self.items[key] = (time.time(), value)
        self._save()
        return value


def _detail_file():
    from app import db
    return os.path.join(db.CONFIG_DIR, "abb_details.json")


# Search pages for an hour (the newest uploads, which change, for 20 minutes); book pages,
# whose magnet and files don't change, for 30 days, kept on disk
SEARCH_TTL, FEED_TTL = 3600, 20 * 60
_search_cache = _Cache(ttl=SEARCH_TTL, size=300)
_detail_cache = _Cache(ttl=30 * 24 * 3600, size=3000, file=_detail_file)


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
    # The newest uploads (no query) change through the day; a search's results much less
    cached = _search_cache.get(key, None if query else FEED_TTL)
    if cached is not None:
        return cached

    async def load():
        url = f"{base_url()}/page/{page}/" if page > 1 else f"{base_url()}/"
        html = await fetch_html(url, {"s": query} if query else None)  # Raises on a Cloudflare check
        return _search_cache.put(key, (parse_search_page(html), last_page(html)))

    return await _shared(("search",) + key, load)


async def search_audiobooks(query: str, offset: int = 0, limit: int = 100, known_author: Optional[str] = None) -> List[Dict]:
    """Search results for a free-text query, with magnets (for Torznab clients)."""
    start_page = (offset // RESULTS_PER_PAGE) + 1
    # The newest uploads (an app's RSS check, no query): one page; a search: at most two
    pages_to_fetch = 1 if not (query or "").strip() else min(TORZNAB_PAGES, max(1, (limit // RESULTS_PER_PAGE) + 1))

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
    # Detail pages (with the magnet) only where already cached: the rest link to
    # /api/download, which loads the page when the release is actually grabbed
    await add_details([r for r in final_results if _detail_cache.get(r.get("link")) is not None])
    return final_results


TORZNAB_PAGES = 2


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
        async def load():
            logger.debug(f"Fetching detail page: {detail_url}")
            parsed_page = parse_detail(await fetch_html(detail_url))  # Raises on a Cloudflare check
            # A page without a magnet (a hiccup) isn't kept, so the next try fetches it again
            if parsed_page.get("magnet"):
                _detail_cache.put(detail_url, parsed_page)
            return parsed_page

        try:
            cached = await _shared(("detail", detail_url), load)
        except SiteUnavailable as e:
            logger.warning(f"Not loading {detail_url}: {e}")
            return None
        except Exception as e:
            logger.error(f"Error fetching detail page {detail_url}: {e}", exc_info=True)
            return None
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


async def test_connection(url="", cookie_value=None, agent="", verify=None):
    """Reaches the site and says whether the cookie logs in. Unsaved values from the form
    can be tried before saving them."""
    url = (url or base_url()).strip().rstrip("/")
    headers = {"User-Agent": (agent or user_agent()).strip()}
    value = cookie() if cookie_value is None else cookie_value.strip()
    if value:
        headers["Cookie"] = value
    req = urllib.request.Request(url + "/", headers=headers)
    context = _tls_context(verify)

    def fetch():
        with urllib.request.urlopen(req, context=context, timeout=20.0) as response:
            return response.read().decode("utf-8", errors="ignore")

    try:
        _count("interactive")
        html = await asyncio.get_running_loop().run_in_executor(None, fetch)
    except Exception as e:
        if isinstance(getattr(e, "reason", e), ssl.SSLCertVerificationError):
            return {"ok": False, "logged_in": False,
                    "message": f"{url} has an invalid certificate ({getattr(e, 'reason', e)}). Check the address; "
                               "only if it's right, turn off Check the Site's Certificate."}
        return {"ok": False, "logged_in": False, "message": f"Couldn't reach {url}: {e}"}
    if _looks_like_challenge(html):
        return {"ok": False, "logged_in": False, "message": "Cloudflare blocked the request. A fresh cookie (with the matching user agent) usually helps."}
    resume()  # It answers again: no need to keep requests paused
    logged_in = bool(re.search(r"log\s*out|logout", html, re.IGNORECASE))
    if logged_in:
        return {"ok": True, "logged_in": True, "message": "Connected and logged in."}
    if value:
        return {"ok": True, "logged_in": False, "message": "Connected, but the cookie doesn't log in (it may have expired). Searching still works."}
    return {"ok": True, "logged_in": False, "message": "Connected (no cookie set)."}
