"""Manual Import, like Sonarr's: lists what's in a folder (the downloads folder unless you
pick another), guesses which book each item is, and imports the ones you choose as the
book you choose: a library book, or an Audible book (added to the library).

An item is a folder of audio files, a single audio file, an archive (or a folder of them),
or one book of a folder holding several (a pack). Files are copied into the library like
any import; with Move, the originals are deleted afterwards (a torrent of them stops
seeding)."""
import asyncio
import hashlib
import logging
import os
import shutil

from app import archives, audible, db, monitor
from app.library import (AUDIO_EXTENSIONS, _make_candidate, audio_files, build_folder_name, find_match,
                         plan_import_files, primary_author)

logger = logging.getLogger(__name__)

_items = {}  # id -> the item as last scanned; imports only use scanned items
job = {"running": False, "total": 0, "done": 0, "imported": 0, "failed": 0, "results": []}
_tasks = set()

# Files a download client is still writing
_UNFINISHED = (".!qb", ".part", ".partial", ".!ut", ".crdownload", ".tmp")


def _item_id(path, files):
    return hashlib.sha1((path + "|" + "|".join(files or [])).encode("utf-8", "surrogateescape")).hexdigest()[:16]


# A guess's details that describe the book (not where its files are)
_BOOK_INFO = ("title", "authors", "narrators", "series", "sequence", "series_list", "release_date", "description",
              "publisher", "language", "edition", "edition_reason", "edition_check", "imageUrl")


def _guess_fields(candidate):
    return {k: candidate.get(k) or "" for k in ("title", "authors", "narrators", "series", "sequence", "edition", "asin")}


def _library_match(library, guess):
    """The library book this probably is: a match not on disk yet, else one that is."""
    found = find_match(library, {**guess, "path": ""})
    if not found:
        return None
    on_disk = bool(found.get("path") and os.path.isdir(found["path"]))
    return {"book_id": found["id"], "title": found.get("title", ""), "status": found.get("status", ""), "on_disk": on_disk}


def _make_item(path, name, kind, files, size, file_count, guess, library):
    item = {"id": _item_id(path, files), "path": path, "name": name, "kind": kind, "files": files,
            "size": size, "file_count": file_count, "guess": guess, "match": _library_match(library, guess)}
    _items[item["id"]] = item
    return item


def scan(path):
    """The items in a folder, sorted by name."""
    from app.splitter import detect_books  # The splitter imports the monitor
    if not path or not os.path.isdir(path):
        raise ValueError("That folder doesn't exist.")
    library = db.get_library()
    items = []
    entries = sorted((e for e in os.scandir(path) if not e.name.startswith(".")), key=lambda e: e.name.lower())
    for entry in entries:
        full = entry.path
        try:
            if entry.is_dir():
                audio = audio_files(full)
                if audio:
                    candidate = _make_candidate(full, entry.name)
                    guess = _guess_fields(candidate)
                    groups = detect_books(full)
                    if len(groups) >= 2:
                        # A pack: each book is its own item
                        for g in groups:
                            sizes = [s for p, s in audio if os.path.relpath(p, full) in g["files"]]
                            items.append(_make_item(full, f"{entry.name} / {g['title'] or 'Book ' + g['number']}", "audio",
                                                    g["files"], sum(sizes), len(g["files"]),
                                                    {**guess, "title": g["title"] or guess["title"], "sequence": g["number"],
                                                     "asin": ""}, library))
                    else:
                        items.append(_make_item(full, entry.name, "audio", None, sum(s for _, s in audio), len(audio),
                                                guess, library))
                    continue
                packed = archives.find_archives(full)
                if packed:
                    guess = _guess_fields(_make_candidate(full, entry.name))
                    items.append(_make_item(full, entry.name, "archive", None, sum(os.path.getsize(a) for a in packed),
                                            len(packed), guess, library))
            elif entry.is_file() and not entry.name.lower().endswith(_UNFINISHED):
                stem, ext = os.path.splitext(entry.name)
                if ext.lower() in AUDIO_EXTENSIONS or archives.is_archive(entry.name):
                    guess = _guess_fields(_make_candidate(full, stem))
                    kind = "audio" if ext.lower() in AUDIO_EXTENSIONS else "archive"
                    items.append(_make_item(full, entry.name, kind, None, entry.stat().st_size, 1, guess, library))
        except OSError as e:
            logger.warning(f"Manual import: skipping {full}: {e}")
    return items


_audible_books = {}  # match id -> its details, so previews don't ask again


async def audible_book(asin, prefer_series=""):
    """A chosen match: an Audible ASIN, or a GraphicAudio release's page (for dramatizations
    Audible doesn't sell)."""
    from app import graphicaudio
    if asin not in _audible_books:
        if graphicaudio.is_release_url(asin):
            found = await graphicaudio.release(asin)
            if not found:
                return None
            _audible_books[asin] = found
            return found
        products = await audible.get_products([asin])
        if not products:
            return None
        _audible_books[asin] = audible.product_to_book(products[0], prefer_series=prefer_series)
    return _audible_books[asin]


async def suggest_for(guess):
    """The Audible book something probably is: its ASIN's (from metadata.json), else a clear
    match (title and author, same edition, same part)."""
    if guess.get("asin"):
        found = await audible_book(guess["asin"], guess.get("series", ""))
        if found:
            return found
    if not guess.get("title"):
        return None
    found = await audible.auto_match(guess)
    if found and found.get("asin"):
        _audible_books[found["asin"]] = found
        return found
    # A dramatization Audible doesn't sell: GraphicAudio's own release
    from app import editions, graphicaudio
    if editions.edition_of(guess) == editions.ABRIDGED:
        found = await graphicaudio.find_release(guess)
        if found:
            _audible_books[found["ga_url"]] = found
            return found
    return None


async def suggest(item_id):
    """The Audible book an item probably is (title and author must match), or None."""
    item = _items.get(item_id)
    if not item:
        raise KeyError(item_id)
    return await suggest_for(item["guess"])


async def preview(source, guess, asin, settings):
    """Where a book would go: {"folder", "files"} in the Root Folder, named from its Audible
    match when there is one, else from the guess."""
    book = dict(guess)
    if asin:
        found = await audible_book(asin, guess.get("series", ""))
        if found:
            book.update({k: v for k, v in found.items() if v})
    audio, _ = await asyncio.to_thread(plan_import_files, source, *monitor._plan_args(book, settings))
    folder = build_folder_name(settings.get("naming_format"), book)
    root = settings.get("root_folder") or ""
    return {"folder": folder, "files": [dest for _, dest in audio],
            "exists": bool(root) and os.path.isdir(os.path.join(root, folder))}


def register(path, guess):
    """An item for something found elsewhere (Import Existing), so it can be imported here."""
    kind = "archive" if os.path.isfile(path) and archives.is_archive(path) else "audio"
    return _make_item(path, os.path.basename(path), kind, None, 0, 0, guess, [])


async def _book_for(item, choice, settings):
    """(library book, whether it was added for this import). choice is {"book_id"},
    {"asin"} (an Audible book) or {"as_is": True} (the item's own name and details)."""
    library = db.get_library()
    if choice.get("book_id"):
        book = db.get_book(choice["book_id"])
        if not book:
            raise ValueError("That book isn't in the library any more.")
    elif choice.get("asin"):
        found = await audible_book(choice["asin"], item["guess"].get("series", ""))
        if not found:
            raise ValueError("That book wasn't found on Audible.")
        book = find_match(library, {**found, "path": ""})
        if book and not book.get("path"):
            db.apply_audible_match(book["id"], found)  # A tracked book: fill in Audible's details
            book = db.get_book(book["id"])
        elif not book:
            # Downloaded while it's being imported, so the search loop leaves it alone
            book = db.add_to_library({**found, "description": found.get("description", "")}, status="Downloaded")
    elif choice.get("as_is"):
        guess = item["guess"]
        if not guess.get("title"):
            raise ValueError("The item has no title to use.")
        book = db.add_to_library({k: guess[k] for k in _BOOK_INFO if guess.get(k) not in (None, "")}, status="Downloaded")
    else:
        raise ValueError("Choose which book it is.")
    if book.get("path") and os.path.isdir(book["path"]):
        raise ValueError(f"{book.get('title')} is already on disk at {book['path']}.")
    # add_to_library returns the entry already there when the book is tracked
    return book, book["id"] not in {b["id"] for b in library}


def _remove_sources(item, audio, cover):
    """Move: deletes what was imported from the source. A whole item goes; for one book of a
    pack only its files, and the folder too once it holds no audio."""
    path = item["path"]
    if os.path.isfile(path):
        os.remove(path)
        return
    if item["kind"] == "archive" or item["files"] is None:
        shutil.rmtree(path)
        return
    for src, _ in audio:  # The pack's cover stays: the other books use it
        if os.path.exists(src):
            os.remove(src)
    if not audio_files(path):
        shutil.rmtree(path)


def _keep_source(source, dest, root_folder, book_id):
    """Why a move must keep the originals ("" when they can go): they are the imported
    copy (the files were already where they belong), or hold it, the library or another
    book."""
    norm = lambda p: os.path.normcase(os.path.abspath(p))
    inside = lambda a, b: a == b or a.startswith(b.rstrip(os.sep) + os.sep)  # a is b or inside it
    src = norm(source)
    if not dest or inside(norm(dest), src) or inside(src, norm(dest)):
        return "they're already where the book belongs"
    if root_folder and inside(norm(root_folder), src):
        return "the folder holds the library"
    other = next((b for b in db.get_library() if b["id"] != book_id and b.get("path") and inside(norm(b["path"]), src)), None)
    if other:
        return f"the folder holds {other.get('title')}"
    return ""


async def _import_one(item, choice, mode, settings, root_folder):
    """Returns (ok, message, book id)."""
    book, created = await _book_for(item, choice, settings)
    staging = os.path.join(root_folder, monitor.UNPACK_FOLDER, "manual-" + item["id"])
    imported = False
    try:
        source, files = item["path"], item["files"]
        if item["kind"] == "archive":
            await asyncio.to_thread(shutil.rmtree, staging, True)
            await asyncio.to_thread(archives.extract_all, archives.find_archives(source), staging)
            source, files = staging, None
        audio, cover = await asyncio.to_thread(plan_import_files, source, *monitor._plan_args(book, settings), files)
        if not audio:
            raise ValueError("There are no audio files in it.")
        # Not a reason to stop: you chose the book. Noted in the result.
        warning = await asyncio.to_thread(monitor.check_download, book, [s for s, _ in audio], settings, source)
        note = " (manual import" + (", moved" if mode == "move" else "") + ")"
        if not await monitor._place_book(book, audio, cover, settings, root_folder, item["kind"] == "archive", note):
            raise ValueError("Copying the files failed; see History.")
        imported = True
        if mode == "move":
            keep = _keep_source(item["path"], (db.get_book(book["id"]) or {}).get("path"), root_folder, book["id"])
            try:
                if keep:
                    warning = (warning + " " if warning else "") + f"The originals were kept: {keep}."
                else:
                    await asyncio.to_thread(_remove_sources, item, audio, cover)
            except OSError as e:
                warning = (warning + " " if warning else "") + f"Imported, but the originals couldn't be deleted: {e}"
        return True, warning or "", book["id"]
    finally:
        await asyncio.to_thread(shutil.rmtree, staging, True)
        try:
            os.rmdir(os.path.dirname(staging))  # Only when nothing else is being unpacked
        except OSError:
            pass
        if created and not imported:
            db.remove_from_library(book["id"])


def start(choices, mode):
    """Imports [{"id", "book_id" | "asin" | "as_is"}] in the background."""
    if job["running"]:
        raise RuntimeError("An import is already running.")
    settings = db.get_settings()
    root_folder = settings.get("root_folder")
    if not root_folder or not os.path.isdir(root_folder):
        raise ValueError("Set the Root Folder in Settings > Media Management first.")
    work = [(c, _items.get(c.get("id"))) for c in choices]
    if not work or any(item is None for _, item in work):
        raise ValueError("Scan the folder again; it has changed.")
    job.update(running=True, total=len(work), done=0, imported=0, failed=0, results=[])

    async def run():
        try:
            for choice, item in work:
                try:
                    ok, message, book_id = await _import_one(item, choice, mode, settings, root_folder)
                except Exception as e:
                    ok, message, book_id = False, str(e), choice.get("book_id", "")
                    logger.warning(f"Manual import of {item['name']} failed: {e}")
                job["imported" if ok else "failed"] += 1
                job["results"].append({"id": item["id"], "name": item["name"], "path": item["path"], "ok": ok,
                                       "message": message, "book_id": book_id})
                if ok:
                    _items.pop(item["id"], None)
                job["done"] += 1
            if job["imported"]:
                from app import audiobookshelf
                await audiobookshelf.scan_library(settings)
        finally:
            job["running"] = False

    task = asyncio.create_task(run())
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return dict(job)


def search_query(item_id):
    """The Audible search an item starts with: its title and author."""
    guess = (_items.get(item_id) or {}).get("guess") or {}
    return f"{(guess.get('title') or '').split(':')[0]} {primary_author(guess.get('authors'))}".strip()
