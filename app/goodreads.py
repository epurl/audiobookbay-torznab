"""Reading Goodreads: a shelf through its public RSS feed (no login needed for a public
profile; a private one's feed link includes a key), or a Listopia list (goodreads.com/list/
show/...) from its pages, top first."""
import asyncio
import logging
import re
import xml.etree.ElementTree as ET
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

PER_PAGE = 100  # Goodreads' feed pages
MAX_PAGES = 10
LIST_TOPS = (50, 100, 250, 500)  # How much of a Listopia list can be watched (100 books a page)
_AGENT = "Mozilla/5.0 (compatible; BorgArr)"


def _goodreads_link(url):
    """A link pasted by hand, parsed; raises if it isn't goodreads.com."""
    text = (url or "").strip()
    if text and "://" not in text:
        text = "https://" + text
    parsed = urlparse(text)
    host = (parsed.hostname or "").lower()
    if host != "goodreads.com" and not host.endswith(".goodreads.com"):
        raise ValueError("That isn't a Goodreads link. Copy the link of your shelf (e.g. your Want to Read list) "
                         "or of a Listopia list.")
    return parsed


def parse_link(url, shelf=""):
    """("list", the list's address) for a Listopia list, else ("shelf", the shelf's RSS
    feed). Raises ValueError with a readable reason for anything else."""
    text = (url or "").strip()
    if not re.fullmatch(r"\d+", text):
        m = re.match(r"/list/show/(\d+)", _goodreads_link(text).path)
        if m:
            return "list", f"https://www.goodreads.com/list/show/{m.group(1)}"
    return "shelf", feed_url(text, shelf)


def feed_url(url, shelf=""):
    """The RSS feed for a Goodreads shelf from what you'd copy: the shelf page
    (goodreads.com/review/list/<id>-name?shelf=to-read), the profile
    (goodreads.com/user/show/<id>-name), the feed itself (review/list_rss/<id>?key=...),
    or just the user id. shelf overrides the link's shelf; the default is to-read."""
    text = (url or "").strip()
    if re.fullmatch(r"\d+", text):
        user, query = text, {}
    else:
        parsed = _goodreads_link(text)
        # Only a shelf, a profile or a shelf's feed: any other page's number (a book's, a
        # list's) isn't a user's
        m = re.search(r"/(?:review/list(?:_rss)?|user/show)/(\d+)", parsed.path)
        if not m:
            raise ValueError("That Goodreads link isn't a shelf, a profile or a Listopia list. Open the shelf (e.g. "
                             "Want to Read) or the list on Goodreads and copy its address.")
        user = m.group(1)
        query = {k: v[0] for k, v in parse_qs(parsed.query).items() if k in ("shelf", "key")}
    if shelf.strip():
        query["shelf"] = shelf.strip()
    query.setdefault("shelf", "to-read")
    return f"https://www.goodreads.com/review/list_rss/{user}?{urlencode(query)}"


def _text(item, tag):
    found = item.find(tag)
    return (found.text or "").strip() if found is not None and found.text else ""


def parse_feed(xml_text):
    """(title, [{"book_id", "title", "author", "isbn", "image"}]) from one feed page."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        raise ValueError("Goodreads didn't return the list (is the profile private? Use the shelf's RSS link then).")
    channel = root.find("channel")
    if channel is None:
        raise ValueError("Goodreads didn't return the list. If the profile is private, use the shelf's RSS link (it includes a key).")
    items = []
    for item in channel.findall("item"):
        book_id = _text(item, "book_id")
        title = _text(item, "title")
        if not book_id or not title:
            continue
        items.append({"book_id": book_id, "title": title, "author": _text(item, "author_name"),
                      "isbn": _text(item, "isbn"), "image": _text(item, "book_large_image_url") or _text(item, "book_image_url")})
    return _text(channel, "title"), items


async def fetch(url, max_pages=MAX_PAGES):
    """Every book on the shelf, newest first: {"title", "items"}. Raises ValueError with a
    readable reason."""
    title, items, seen = "", [], set()
    async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers={"User-Agent": _AGENT}) as client:
        for page in range(1, max_pages + 1):
            try:
                res = await client.get(url, params={"page": page} if page > 1 else None)
            except httpx.HTTPError as e:
                raise ValueError(f"Couldn't reach Goodreads ({e.__class__.__name__}).")
            if res.status_code == 404:
                raise ValueError("Goodreads has no such list.")
            if res.status_code != 200:
                raise ValueError(f"Goodreads answered {res.status_code}.")
            page_title, page_items = parse_feed(res.text)
            title = title or page_title
            new = [i for i in page_items if i["book_id"] not in seen]
            seen.update(i["book_id"] for i in new)
            items += new
            if len(page_items) < PER_PAGE or not new:
                break
    return {"title": title, "items": items}


# --- Listopia lists ------------------------------------------------------------------

def parse_list_page(html):
    """(list name, [{"book_id", "title", "author", "isbn", "image"}], whether there's a next
    page) from one page of a Listopia list."""
    soup = BeautifulSoup(html, "lxml")
    name = soup.title.get_text(" ", strip=True) if soup.title else ""
    name = re.sub(r"\s*\(\d[\d,]*\s+books?\)\s*$", "", re.sub(r"\s*[|-]\s*Goodreads\s*$", "", name)).strip()
    items = []
    for row in soup.select('tr[itemtype="http://schema.org/Book"]'):
        link = row.select_one("a.bookTitle")
        found = re.search(r"/book/show/(\d+)", (link.get("href") or "") if link else "")
        if not link or not found:
            continue
        title = (link.select_one("[itemprop=name]") or link).get_text(" ", strip=True)
        author = row.select_one("a.authorName [itemprop=name]") or row.select_one("a.authorName")
        cover = row.select_one("img.bookCover")
        if title:
            items.append({"book_id": found.group(1), "title": title, "isbn": "",
                          "author": author.get_text(" ", strip=True) if author else "",
                          "image": (cover.get("src") or "") if cover else ""})
    return name, items, soup.select_one("a.next_page") is not None


async def fetch_list(url, top=100):
    """The first `top` books of a Listopia list, in its order: {"title", "items"}. Raises
    ValueError with a readable reason."""
    title, items, seen = "", [], set()
    async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers={"User-Agent": _AGENT}) as client:
        for page in range(1, -(-top // PER_PAGE) + 1):
            if page > 1:
                await asyncio.sleep(1)  # Gently, a page at a time
            try:
                res = await client.get(url, params={"page": page} if page > 1 else None)
            except httpx.HTTPError as e:
                raise ValueError(f"Couldn't reach Goodreads ({e.__class__.__name__}).")
            if res.status_code == 404:
                raise ValueError("Goodreads has no such list.")
            if res.status_code != 200:
                raise ValueError(f"Goodreads answered {res.status_code}.")
            page_title, page_items, more = parse_list_page(res.text)
            if page == 1 and not page_items:
                raise ValueError("Goodreads didn't return the list's books.")
            title = title or page_title
            new = [i for i in page_items if i["book_id"] not in seen]
            seen.update(i["book_id"] for i in new)
            items += new
            if not more or not new:
                break
    return {"title": title, "items": items[:top]}


async def fetch_watched(entry):
    """A watched list's books: a Listopia list's top books, or a shelf's."""
    if entry.get("kind") == "list":
        return await fetch_list(entry["url"], int(entry.get("top") or 100))
    return await fetch(entry["url"])
