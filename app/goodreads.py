"""Reading a Goodreads shelf through its public RSS feed (no login needed for a public
profile; a private one's feed link includes a key)."""
import logging
import re
import xml.etree.ElementTree as ET
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

logger = logging.getLogger(__name__)

PER_PAGE = 100  # Goodreads' feed pages
MAX_PAGES = 10
_AGENT = "Mozilla/5.0 (compatible; Bayarr)"


def feed_url(url, shelf=""):
    """The RSS feed for a Goodreads shelf from what you'd copy: the shelf page
    (goodreads.com/review/list/<id>-name?shelf=to-read), the profile
    (goodreads.com/user/show/<id>-name), the feed itself (review/list_rss/<id>?key=...),
    or just the user id. shelf overrides the link's shelf; the default is to-read."""
    text = (url or "").strip()
    if re.fullmatch(r"\d+", text):
        user, query = text, {}
    else:
        if text and "://" not in text:
            text = "https://" + text
        parsed = urlparse(text)
        if not parsed.netloc.lower().endswith("goodreads.com"):
            raise ValueError("That isn't a Goodreads link. Copy the link of your shelf, e.g. your Want to Read list.")
        m = re.search(r"/(?:review/list(?:_rss)?|user/show)/(\d+)", parsed.path) or re.search(r"/(\d+)", parsed.path)
        if not m:
            raise ValueError("Couldn't find the Goodreads user in that link. Open your shelf on Goodreads and copy its address.")
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
