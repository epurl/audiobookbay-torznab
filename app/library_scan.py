"""Keeping the library in step with the Root Folder (every 6 hours, and Library > Rescan):

- A book whose folder is gone is looked for elsewhere in the Root Folder (renamed or moved
  there, e.g. by Audiobookshelf or by hand): the same ASIN in its metadata.json, or the
  same audio files (names and sizes). Found: its path is updated. Not found: Missing.
- Folders in the Root Folder no book uses are added (Settings > Media Management), then
  matched on Audible when it's clear-cut. Folders changed in the last few minutes are left
  for next time, so a copy or import in progress isn't picked up half done.
- A book without an ASIN takes the one in its metadata.json (e.g. matched in
  Audiobookshelf), with Audible's details.
- File details (count, size, format, and the fingerprint used to find moved folders) are
  refreshed."""
import asyncio
import logging
import os
import time

from app import audible, db, library

logger = logging.getLogger(__name__)

SETTLE_SECONDS = 10 * 60  # A folder changed this recently may still be being written


def _norm(path):
    return os.path.normcase(os.path.abspath(path or ""))


def _settled(path):
    """Nothing in the folder has changed for a while."""
    newest = 0.0
    try:
        newest = os.path.getmtime(path)
        for root, _, files in os.walk(path):
            for f in files:
                newest = max(newest, os.path.getmtime(os.path.join(root, f)))
    except OSError:
        return False
    return time.time() - newest > SETTLE_SECONDS


def _asin_in(path):
    return ((library.read_abs_metadata(path) or {}).get("asin") or "") if os.path.isdir(path) else ""


def scan(settings):
    """Brings the library up to date with the disk. Returns a summary, with the ids of
    books added from new folders."""
    summary = {"missing": 0, "restored": 0, "moved": 0, "added": 0, "linked": 0, "added_ids": []}
    root = settings.get("root_folder") or ""
    root_ok = bool(root) and os.path.isdir(root)
    books = db.get_library()
    tracked = {_norm(b["path"]) for b in books if b.get("path")}

    candidates = None  # Folders in the Root Folder no book uses, found when first needed
    fingerprints = {}

    def untracked():
        nonlocal candidates
        if candidates is None:
            candidates = [c for c in library.scan_library(root) if _norm(c["path"]) not in tracked] if root_ok else []
        return candidates

    def fingerprint(path):
        if path not in fingerprints:
            fingerprints[path] = library.fingerprint(path)
        return fingerprints[path]

    # Folders that are gone: moved, or Missing
    for book in books:
        path = book.get("path")
        if not path or book.get("status") not in ("Imported", "Missing"):
            continue
        if not os.path.isdir(os.path.dirname(os.path.normpath(path))):
            continue  # The drive isn't there (e.g. unmounted): don't conclude anything
        if os.path.exists(path):
            if book["status"] == "Missing":
                db.update_library_status(book["id"], "Imported")
                summary["restored"] += 1
            continue
        moved = next((c for c in untracked() if os.path.isdir(c["path"])
                      and ((book.get("asin") and _asin_in(c["path"]) == book["asin"])
                           or (book.get("fingerprint") and fingerprint(c["path"]) == book["fingerprint"]))), None)
        if moved:
            candidates.remove(moved)
            db.update_book(book["id"], path=moved["path"], cover=moved.get("cover", ""), status="Imported",
                           **library.describe_files(moved["path"]))
            db.add_history("moved", book, f"Found at {moved['path']} (was {path})")
            logger.info(f"{book.get('title')} moved to {moved['path']}")
            summary["moved"] += 1
        elif book["status"] == "Imported":
            logger.warning(f"Files for '{book.get('title')}' are gone from {path}; marking Missing")
            db.update_library_status(book["id"], "Missing")
            db.add_history("missing", book, f"Folder is gone: {path}")
            summary["missing"] += 1

    # New folders
    if root_ok and settings.get("auto_add_folders", True):
        new = [c for c in untracked() if _settled(c["path"])]
        if new:
            before = {b["id"] for b in db.get_library()}
            added, linked = db.import_books(new)
            summary["added"], summary["linked"] = added, linked
            summary["added_ids"] = [b["id"] for b in db.get_library() if b["id"] not in before]
            for b in db.get_library():
                if b["id"] in summary["added_ids"]:
                    db.add_history("found", b, f"New folder in the Root Folder: {b.get('path')}")
            if added or linked:
                logger.info(f"Library scan: {added} new folder(s) added, {linked} linked to tracked books")

    # File details, and the fingerprints that find moved folders next time
    db.update_books({b["id"]: library.describe_files(b["path"]) for b in db.get_library()
                     if b.get("status") == "Imported" and b.get("path") and os.path.isdir(b["path"])})
    return summary


async def adopt_asins():
    """Books without an ASIN take the one in their metadata.json, with Audible's details.
    Returns how many."""
    books = [b for b in db.get_library() if b.get("status") == "Imported" and b.get("path") and not b.get("asin")]
    found = {}
    for b in books:
        asin = await asyncio.to_thread(_asin_in, b["path"])
        if asin:
            found[b["id"]] = asin
    if not found:
        return 0
    adopted = 0
    asins = sorted(set(found.values()))
    products = {}
    for i in range(0, len(asins), 40):
        try:
            for p in await audible.get_products(asins[i:i + 40]):
                products[p.get("asin")] = p
        except Exception as e:
            logger.warning(f"Library scan: couldn't look up ASINs on Audible: {e}")
            return adopted
    for book_id, asin in found.items():
        book = db.get_book(book_id)
        if asin in products:
            db.apply_audible_match(book_id, audible.product_to_book(products[asin], prefer_series=book.get("series", "")))
        else:
            db.update_book(book_id, asin=asin)
        db.add_history("matched", book, f"ASIN {asin} from its metadata.json")
        adopted += 1
    return adopted


async def run(settings):
    """The whole scan: the disk, then ASINs from metadata.json, then Audible matches for
    new books that have none."""
    summary = await asyncio.to_thread(scan, settings)
    summary["asins"] = await adopt_asins()
    unmatched = [i for i in summary["added_ids"] if not (db.get_book(i) or {}).get("asin")]
    if unmatched:
        from app.monitor import start_match_job
        start_match_job(unmatched)  # Clear matches only; the rest stay as named
    return summary
