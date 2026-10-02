"""Indexers (e.g. from Prowlarr or Jackett) searched alongside AudiobookBay: Torznab ones
for torrents and Newznab ones for Usenet (same feed format). And getting a release's
download: a magnet link, a .torrent file (whose info hash is read so the download can be
tracked), or an NZB for the Usenet client."""
import email.utils
import hashlib
import logging
import re
import urllib.parse
import uuid
import xml.etree.ElementTree as ET

import httpx

from app import db, scraper

logger = logging.getLogger(__name__)

TORZNAB_NS = "{http://torznab.com/schemas/2015/feed}"
NEWZNAB_NS = "{http://www.newznab.com/DTD/2010/feeds/attributes/}"
AUDIOBOOK_CATEGORIES = "3030"


# --- Settings ----------------------------------------------------------------

def get_all():
    return list(db.get_settings().get("indexers") or [])


def public():
    """Indexers for the browser: API keys are never sent back."""
    return [{**{k: v for k, v in i.items() if k != "api_key"}, "api_key_set": bool(i.get("api_key"))} for i in get_all()]


def save(indexer):
    """Adds or updates an indexer; a blank API key keeps the stored one."""
    url = (indexer.get("url") or "").strip()
    if not re.match(r"^https?://", url):
        raise ValueError("The URL must start with http:// or https://")
    items = get_all()
    existing = next((i for i in items if i.get("id") == indexer.get("id")), None)
    entry = {
        "id": (existing or {}).get("id") or uuid.uuid4().hex,
        "name": (indexer.get("name") or "").strip() or urllib.parse.urlparse(url).netloc,
        "url": url,
        "api_key": (indexer.get("api_key") or "").strip() or (existing or {}).get("api_key", ""),
        "categories": re.sub(r"[^\d,]", "", indexer.get("categories") or AUDIOBOOK_CATEGORIES) or AUDIOBOOK_CATEGORIES,
        "enabled": bool(indexer.get("enabled", True)),
        # Torznab (torrents) or Newznab (Usenet)
        "protocol": "usenet" if indexer.get("protocol") == "usenet" else "torrent",
    }
    items = [entry if i.get("id") == entry["id"] else i for i in items] if existing else items + [entry]
    db.set_setting("indexers", items)
    return entry


def remove(indexer_id):
    db.set_setting("indexers", [i for i in get_all() if i.get("id") != indexer_id])


# --- Searching ---------------------------------------------------------------

def _human(size):
    if not size:
        return "Unknown"
    for unit, factor in (("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024)):
        if size >= factor:
            return f"{size / factor:.2f} {unit}"
    return f"{size} B"


def _guess(pattern, text):
    m = re.search(pattern, text or "", re.IGNORECASE)
    return m.group(1) if m else ""


def protocol(indexer):
    return "usenet" if indexer.get("protocol") == "usenet" else "torrent"


def release_key(result):
    """What identifies a release (for rejecting it): a torrent's info hash, for a torrent
    only offered as a .torrent file its page (the hash isn't known until it's fetched), or
    for Usenet its NZB link."""
    if result.get("protocol") == "usenet":
        ident = result.get("download_url") or result.get("link") or result.get("raw_title") or ""
        return "nzb:" + hashlib.sha1(ident.encode("utf-8", "replace")).hexdigest()[:20]
    infohash = db.extract_infohash(result.get("magnet_url"))
    if infohash:
        return infohash
    ident = result.get("link") or result.get("download_url") or ""
    return "url:" + hashlib.sha1(ident.encode("utf-8", "replace")).hexdigest()[:20] if ident else ""


def blocklist_keys(book):
    """What to add to a book's blocklist when its download is rejected: the release's key
    and the download's hash (a .torrent release is known by both)."""
    return [k for k in (book.get("release_key"), book.get("download_hash")) if k]


def _number(value):
    """A size or count from a feed ("123", "123.0"); 0 when it isn't a number."""
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return 0


def _item(item, indexer):
    # Newznab uses the same attributes under its own namespace
    attrs = {a.get("name"): a.get("value") for a in item.findall(f"{TORZNAB_NS}attr")}
    attrs.update({a.get("name"): a.get("value") for a in item.findall(f"{NEWZNAB_NS}attr")})
    usenet = protocol(indexer) == "usenet"
    title = (item.findtext("title") or "").strip()
    enclosure = item.find("enclosure")
    download = (enclosure.get("url") if enclosure is not None else "") or item.findtext("link") or ""
    size = (_number(item.findtext("size")) or _number(enclosure.get("length") if enclosure is not None else 0)
            or _number(attrs.get("size")))
    magnet = "" if usenet else attrs.get("magneturl") or (download if download.startswith("magnet:") else "")
    if not usenet and not magnet and attrs.get("infohash"):
        magnet = f"magnet:?xt=urn:btih:{attrs['infohash']}&dn={urllib.parse.quote(title)}"
    posted = ""
    try:
        posted = email.utils.parsedate_to_datetime(item.findtext("pubDate") or "").date().isoformat()
    except (TypeError, ValueError):
        pass
    # Indexers usually name releases "Author - Title"; AudiobookBay uses "Title - Author"
    known = (attrs.get("author") or "").strip()
    first, _, rest = title.partition(" - ")
    if known and rest and first.strip().lower() == known.lower():
        parsed_title, author = scraper.parse_title(rest)[0], known
    else:
        parsed_title, author = scraper.parse_title(title)
    parsed_title = re.sub(r"\s*\b\d{2,3}\s*k(?:bps)?\b", "", parsed_title, flags=re.IGNORECASE).strip()
    fmt = _guess(r"\b(m4b|mp3|m4a|flac|aac|opus)\b", title).upper()
    bitrate = _guess(r"\b(\d{2,3})\s*k(?:bps)?\b", title)
    seeders = None if usenet else attrs.get("seeders")
    result = {
        "title": attrs.get("booktitle") or parsed_title,
        "author": known or author,
        "raw_title": title,
        "release_name": title,
        "link": item.findtext("comments") or item.findtext("guid") or download,
        "download_url": "" if download.startswith("magnet:") else download,
        "magnet_url": magnet,
        "size_bytes": size,
        "size_str": _human(size),
        "format": fmt or "Unknown",
        "bitrate": f"{bitrate} Kbps" if bitrate else "Unknown",
        "language": attrs.get("language") or "Unknown",
        "posted": posted,
        "seeders": int(seeders) if seeders and seeders.isdigit() else None,
        "categories": [],
        "keywords": [],
        "source": indexer.get("name") or "Indexer",
        "protocol": "usenet" if usenet else "torrent",
    }
    result["release_key"] = release_key(result)
    return result


async def search(indexer, query, limit=50):
    """Results from one Torznab indexer for a free-text query."""
    params = {"t": "search", "q": query, "cat": indexer.get("categories") or AUDIOBOOK_CATEGORIES, "limit": limit}
    if indexer.get("api_key"):
        params["apikey"] = indexer["api_key"]
    async with httpx.AsyncClient(follow_redirects=True) as client:
        res = await client.get(indexer["url"], params=params, timeout=30.0)
        res.raise_for_status()
    root = ET.fromstring(res.content)
    error = root if root.tag == "error" else root.find("error")
    if error is not None:
        raise RuntimeError(error.get("description") or "The indexer returned an error")
    return [_item(item, indexer) for item in root.iter("item")]


async def test(indexer):
    """Checks an indexer's URL and API key with a capabilities request and a search."""
    stored = next((i for i in get_all() if i.get("id") == indexer.get("id")), {})
    indexer = {**indexer, "api_key": indexer.get("api_key") or stored.get("api_key", "")}
    params = {"t": "caps"}
    if indexer.get("api_key"):
        params["apikey"] = indexer["api_key"]
    try:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            res = await client.get(indexer["url"], params=params, timeout=20.0)
        if res.status_code in (401, 403):
            return {"ok": False, "message": "The API key was refused."}
        res.raise_for_status()
        root = ET.fromstring(res.content)
        if root.tag == "error":
            return {"ok": False, "message": root.get("description") or "The indexer returned an error."}
        results = await search(indexer, "the", limit=10)
    except Exception as e:
        return {"ok": False, "message": f"Couldn't reach it: {e}"}
    return {"ok": True, "message": f"Connected; a test search found {len(results)} result{'s' if len(results) != 1 else ''}."}


# --- Downloads ---------------------------------------------------------------

def _span(data, i):
    """End of the bencoded value starting at i."""
    c = data[i:i + 1]
    if c == b"i":
        return data.index(b"e", i) + 1
    if c.isdigit():
        colon = data.index(b":", i)
        return colon + 1 + int(data[i:colon])
    if c in (b"l", b"d"):
        i += 1
        while data[i:i + 1] != b"e":
            i = _span(data, i)
        return i + 1
    raise ValueError("Not a torrent file")


def torrent_infohash(data):
    """The info hash of a .torrent file (SHA-1 of its bencoded info dictionary)."""
    if data[:1] != b"d":
        raise ValueError("Not a torrent file")
    i = 1
    while data[i:i + 1] != b"e":
        key_end = _span(data, i)
        key = data[data.index(b":", i) + 1:key_end]
        value_end = _span(data, key_end)
        if key == b"info":
            return hashlib.sha1(data[key_end:value_end]).hexdigest()
        i = value_end
    raise ValueError("The torrent file has no info section")


def _allowed(url):
    """Downloads are only fetched from configured indexers' hosts."""
    host = urllib.parse.urlparse(url).netloc.lower()
    return any(urllib.parse.urlparse(i.get("url", "")).netloc.lower() == host for i in get_all())


async def fetch_download(url):
    """("magnet", link) or ("torrent", bytes) for an indexer's download link, which may
    redirect to a magnet link."""
    if not _allowed(url):
        raise ValueError("That download isn't from one of your indexers.")
    async with httpx.AsyncClient(follow_redirects=False) as client:
        for _ in range(5):
            res = await client.get(url, timeout=30.0)
            location = res.headers.get("location", "")
            if res.is_redirect and location:
                if location.startswith("magnet:"):
                    return "magnet", location
                url = urllib.parse.urljoin(url, location)
                continue
            res.raise_for_status()
            torrent_infohash(res.content)  # Checks that it really is a torrent
            return "torrent", res.content
    raise ValueError("Too many redirects")


async def fetch_nzb(url):
    """An NZB from one of your indexers (checked to really be one)."""
    if not _allowed(url):
        raise ValueError("That download isn't from one of your indexers.")
    async with httpx.AsyncClient(follow_redirects=True) as client:
        res = await client.get(url, timeout=30.0)
        res.raise_for_status()
    if b"<nzb" not in res.content[:4000].lower():
        raise ValueError("The indexer didn't return an NZB.")
    return res.content


async def get_download(result):
    """(magnet, torrent bytes) for a release from any source."""
    if result.get("magnet_url"):
        return result["magnet_url"], None
    if result.get("download_url"):
        kind, value = await fetch_download(result["download_url"])
        return (value, None) if kind == "magnet" else (None, value)
    detail = await scraper.fetch_detail_info(result.get("link"), result.get("title"))
    return (detail or {}).get("magnet"), None

