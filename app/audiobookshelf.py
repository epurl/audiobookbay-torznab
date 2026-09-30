"""Hands imported books to Audiobookshelf: metadata.json + cover, then a library scan."""
import json
import logging
import os
import re
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

# Covers are only fetched from Amazon's image CDN (where Audible's cover URLs point)
_COVER_HOSTS = ("media-amazon.com", "ssl-images-amazon.com")


def build_metadata(book):
    """Audiobookshelf's metadata.json format."""
    def people(value):
        return [p.strip() for p in (value or "").split(",") if p.strip()]

    series = []
    if book.get("series"):
        series.append(f"{book['series']} #{book['sequence']}" if book.get("sequence") else book["series"])
    release = book.get("release_date") or ""
    title, _, subtitle = (book.get("title") or "").partition(": ")
    return {
        "tags": [],
        "chapters": [],
        "title": title.strip(),
        "subtitle": subtitle.strip() or None,
        "authors": people(book.get("authors")),
        "narrators": people(book.get("narrators")),
        "series": series,
        "genres": [],
        "publishedYear": release[:4] or None,
        "publishedDate": release if len(release) == 10 else None,
        "publisher": book.get("publisher") or None,
        "description": book.get("description") or None,
        "isbn": None,
        "asin": book.get("asin") or None,
        "language": book.get("language") or None,
        "explicit": False,
        "abridged": False,
    }


def write_metadata(book, folder):
    """Writes metadata.json unless the folder already has one."""
    path = os.path.join(folder, "metadata.json")
    if os.path.exists(path):
        return False
    with open(path, "w", encoding="utf-8") as f:
        json.dump(build_metadata(book), f, indent=2, ensure_ascii=False)
    return True


async def download_cover(image_url, folder):
    """Saves Audible's cover (at 1000px) as cover.jpg. Returns the file name or ''."""
    host = urlparse(image_url or "").hostname or ""
    if not image_url.startswith("https://") or not host.endswith(_COVER_HOSTS):
        return ""
    large = re.sub(r"\._SL\d+_\.", "._SL1000_.", image_url)
    try:
        async with httpx.AsyncClient() as client:
            res = await client.get(large, timeout=20.0)
            res.raise_for_status()
    except Exception as e:
        logger.warning(f"Could not download cover {large}: {e}")
        return ""
    with open(os.path.join(folder, "cover.jpg"), "wb") as f:
        f.write(res.content)
    return "cover.jpg"


def _client(url, token):
    return httpx.AsyncClient(base_url=url.rstrip("/"), headers={"Authorization": f"Bearer {token}"}, timeout=15.0)


async def list_libraries(url, token):
    """Book libraries on an Audiobookshelf server, as [{id, name}]."""
    async with _client(url, token) as client:
        res = await client.get("/api/libraries")
        res.raise_for_status()
        libraries = res.json().get("libraries", [])
    return [{"id": lib["id"], "name": lib.get("name", "")} for lib in libraries if lib.get("mediaType") == "book"]


async def scan_library(settings):
    """Asks Audiobookshelf to rescan the configured library. Returns an error message or ''."""
    url, token, library_id = settings.get("abs_url"), settings.get("abs_token"), settings.get("abs_library_id")
    if not (url and token and library_id):
        return ""
    try:
        async with _client(url, token) as client:
            res = await client.post(f"/api/libraries/{library_id}/scan")
            res.raise_for_status()
        return ""
    except Exception as e:
        logger.warning(f"Audiobookshelf scan failed: {e}")
        return str(e)
