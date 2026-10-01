"""Hands imported books to Audiobookshelf: metadata.json + cover, then a library scan."""
import asyncio
import json
import logging
import os
import re
from urllib.parse import urlparse

import httpx

from app.library import format_sequence, part_count, part_number

logger = logging.getLogger(__name__)

# Covers are only fetched from Amazon's image CDN (where Audible's cover URLs point)
_COVER_HOSTS = ("media-amazon.com", "ssl-images-amazon.com")


def abs_series(book):
    """(name, sequence) as Audiobookshelf should show the book's series, so it sorts:
    dramatizations are a series of their own ("Name (Dramatized)"), and a book sold in
    parts is numbered by part (book 1 in two parts: 1.1 and 1.2). None without a series."""
    name = (book.get("series") or "").strip()
    if not name:
        return None
    if book.get("edition") == "dramatized" and not name.endswith("(Dramatized)"):
        name += " (Dramatized)"
    sequence = format_sequence(book.get("sequence"))
    part = part_number(book.get("title"))
    if sequence and part and "." not in sequence:
        # Two digits once there are ten parts or more, so 1.10 sorts after 1.09
        sequence += f".{part:02d}" if (part_count(book.get("title")) or 0) >= 10 else f".{part}"
    return name, sequence


def _series_text(book):
    found = abs_series(book)
    if not found:
        return []
    return [f"{found[0]} #{found[1]}" if found[1] else found[0]]


def build_metadata(book):
    """Audiobookshelf's metadata.json format."""
    def people(value):
        return [p.strip() for p in (value or "").split(",") if p.strip()]

    series = _series_text(book)
    release = book.get("release_date") or ""
    title, _, subtitle = (book.get("title") or "").partition(": ")
    edition = book.get("edition")
    return {
        # Lets you filter dramatized editions in Audiobookshelf
        "tags": ["Dramatized"] if edition == "dramatized" else [],
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
        "abridged": edition == "abridged",
    }


def write_metadata(book, folder):
    """Writes metadata.json unless the folder already has one."""
    path = os.path.join(folder, "metadata.json")
    if os.path.exists(path):
        return False
    with open(path, "w", encoding="utf-8") as f:
        json.dump(build_metadata(book), f, indent=2, ensure_ascii=False)
    return True


def update_series_file(book, folder):
    """Rewrites only the series (and the Dramatized tag) in a book's existing metadata.json.
    Returns True if it changed; a folder without one is left alone."""
    path = os.path.join(folder or "", "metadata.json")
    if not os.path.isfile(path):
        return False
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    before = json.dumps(data, sort_keys=True)
    data["series"] = _series_text(book)
    tags = [t for t in data.get("tags") or [] if t != "Dramatized"]
    data["tags"] = tags + (["Dramatized"] if book.get("edition") == "dramatized" else [])
    if json.dumps(data, sort_keys=True) == before:
        return False
    partial = path + ".partial"
    with open(partial, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(partial, path)
    return True


# --- Series order in Audiobookshelf (Settings > Audiobookshelf) -------------------

order_job = {"running": False, "total": 0, "done": 0, "files": 0, "abs_updated": 0, "abs_missing": 0, "error": ""}
_tasks = set()


def _norm_folder(path):
    return os.path.basename(os.path.normpath(path or "")).lower()


async def _abs_items(client, library_id):
    res = await client.get(f"/api/libraries/{library_id}/items", params={"limit": 0})
    res.raise_for_status()
    return res.json().get("results", [])


def _find_item(items, book):
    """The Audiobookshelf item for a library book: by ASIN, else by folder name."""
    asin = book.get("asin")
    if asin:
        for item in items:
            if ((item.get("media") or {}).get("metadata") or {}).get("asin") == asin:
                return item
    folder = _norm_folder(book.get("path"))
    matches = [i for i in items if folder and _norm_folder(i.get("path") or i.get("relPath")) == folder]
    return matches[0] if len(matches) == 1 else None


async def fix_series_order(books, settings):
    """For each book: the series in its metadata.json, and in Audiobookshelf itself (through
    its API, so no rescan is needed) when a server and library are set."""
    order_job.update(running=True, total=len(books), done=0, files=0, abs_updated=0, abs_missing=0, error="")
    try:
        for book in books:
            if await asyncio.to_thread(update_series_file, book, book.get("path")):
                order_job["files"] += 1
        url, token, library_id = settings.get("abs_url"), settings.get("abs_token"), settings.get("abs_library_id")
        if not (url and token and library_id):
            order_job["done"] = len(books)
            return
        async with _client(url, token) as client:
            try:
                items = await _abs_items(client, library_id)
            except Exception as e:
                order_job["error"] = f"Couldn't read the Audiobookshelf library: {e}"
                return
            for book in books:
                item = _find_item(items, book)
                found = abs_series(book)
                if not item or not found:
                    order_job["abs_missing"] += 1 if not item else 0
                    order_job["done"] += 1
                    continue
                name, sequence = found
                try:
                    res = await client.patch(f"/api/items/{item['id']}/media",
                                             json={"metadata": {"series": [{"id": f"new-{item['id']}", "name": name, "sequence": sequence}]}})
                    res.raise_for_status()
                    order_job["abs_updated"] += 1
                except Exception as e:
                    logger.warning(f"Audiobookshelf: couldn't update the series of {book.get('title')}: {e}")
                    order_job["error"] = str(e)
                order_job["done"] += 1
    finally:
        order_job["running"] = False


def start_fix_series_order(books, settings):
    if order_job["running"]:
        return False
    task = asyncio.create_task(fix_series_order(books, settings))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
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
