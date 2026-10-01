"""Splits a library folder that holds several books (a collection, e.g. "Book 1 Title Part
1 of 2.m4b", "Book 2 Other Part 1 of 2.m4b", ...) into one folder per book, matched to
the books of its series on Audible.

The original folder is never changed: each book's files are hardlinked (copied when the
drive doesn't allow it) into a new folder next to it, so nothing is lost, no extra space
is used, and a torrent of the original keeps seeding."""
import asyncio
import logging
import os
import re

from app import audible, audiobookshelf, db, editions, series_index
from app.monitor import _copy_files  # Hardlinks, falling back to copies
from app.library import (DISC_FOLDER_RE, _natural_key, audio_files, build_folder_name, describe_files,
                         series_entries, series_key, titles_match)

logger = logging.getLogger(__name__)

# "Book 2 Golden Son Part 1 of 2 ...", "Bk.03 - Title", "Volume 1 Title"
_BOOK_NUMBER = re.compile(r"(?:^|[\s._\-\[(])(?:book|bk|vol(?:ume)?)[\s._-]*0*(\d{1,3}(?:\.\d)?)(?=$|[\s._\-\])])",
                          re.IGNORECASE)
# Where the title stops: "Part 1 of 2", "(1 of 3)", "CD 2", "Disc 1"
_PART_MARK = re.compile(r"[\s._\-(\[]*(?:part|pt|disc|disk|cd)[\s._-]*\d+|[\s._\-(\[]+\d+\s+of\s+\d+", re.IGNORECASE)


def _clean(text):
    return re.sub(r"\s+", " ", re.sub(r"[._]+", " ", text or "")).strip(" -_[]()")


def _group_key(rel):
    """(number, title guess) for one audio file of a collection, or None if it doesn't say
    which book it belongs to."""
    parts = rel.replace("\\", "/").split("/")
    # A subfolder per book ("Book 1 - Title/...", but not "CD1/...")
    if len(parts) > 1 and not DISC_FOLDER_RE.match(parts[0]):
        m = _BOOK_NUMBER.search(parts[0])
        title = _clean(parts[0][m.end():] if m else parts[0])
        return (m.group(1) if m else parts[0]), title
    stem = os.path.splitext(parts[-1])[0]
    m = _BOOK_NUMBER.search(stem)
    if not m:
        return None
    rest = stem[m.end():]
    cut = _PART_MARK.search(rest)
    return m.group(1), _clean(rest[:cut.start()] if cut else rest)


def detect_books(folder):
    """The books in a folder: [{"number", "title", "files": [relative paths], "size"}], in
    order. Fewer than two means it isn't a collection (or its files don't say)."""
    groups = {}
    for path, size in audio_files(folder):
        rel = os.path.relpath(path, folder)
        key = _group_key(rel)
        if key is None:
            return []  # Some files don't say which book they belong to: don't guess
        number, title = key
        group = groups.setdefault(number, {"number": number, "title": title, "files": [], "size": 0})
        group["files"].append(rel)
        group["size"] += size
        if not group["title"] and title:
            group["title"] = title
    books = sorted(groups.values(), key=lambda g: _natural_key(g["number"]))
    for g in books:
        g["files"].sort(key=_natural_key)
    return books


async def _series_asin(book, groups):
    """The Audible series: the one the book already knows, else found through its books."""
    entries = series_entries(book)
    for e in entries:
        if e.get("asin"):
            return e["asin"], e["name"]
    name = entries[0]["name"] if entries else ""
    for g in groups[:3]:
        if not g["title"]:
            continue
        data = await audible.search_keywords(f"{g['title']} {name}".strip(), num_results=10)
        for product in data.get("products") or []:
            for s in product.get("series") or []:
                if s.get("asin") and (not name or series_key(s.get("title")) == series_key(name)
                                      or series_key(name) in series_key(s.get("title"))):
                    return s["asin"], s.get("title", name)
    return "", name


def _match(group, catalog, edition):
    """The series entry for a group: same number and edition, else same title."""
    entries = (catalog or {}).get("books", []) + (catalog or {}).get("alternates", [])
    same_edition = [e for e in entries if editions.edition_of(e) == edition]
    for candidates in (same_edition, entries):
        found = next((e for e in candidates if e.get("catalog_sequence") == group["number"]), None) \
            or next((e for e in candidates if group["title"] and titles_match(e.get("title"), group["title"])), None)
        if found:
            return found
    return None


def _new_book(original, group, match, settings, title=None, number=None):
    """The library entry (and folder name) for one book of the collection."""
    edition = editions.edition_of(original)
    series_name = next((e["name"] for e in series_entries(original)), "") or (match or {}).get("series", "")
    book = {
        "title": title or (match or {}).get("title") or group["title"] or f"{original.get('title')} {group['number']}",
        "authors": (match or {}).get("authors") or original.get("authors", ""),
        "narrators": (match or {}).get("narrators") or original.get("narrators", ""),
        "series": series_name,
        "sequence": number or group["number"],
        "series_asin": (match or {}).get("series_asin", ""),
        "asin": (match or {}).get("asin", ""),
        "runtime_min": (match or {}).get("runtime_min") or 0,
        "imageUrl": (match or {}).get("imageUrl", ""),
        "release_date": (match or {}).get("release_date", ""),
        "publisher": (match or {}).get("publisher", ""),
        "language": (match or {}).get("language", original.get("language", "")),
        "edition": edition,
        "edition_reason": f"Split from {original.get('title')}",
    }
    return book, build_folder_name(settings.get("naming_format"), book)


_proposals = {}  # book id -> the last proposal, which a split may only use


async def propose(book_id):
    book = db.get_book(book_id)
    if not book or not book.get("path") or not os.path.isdir(book["path"]):
        return None
    groups = detect_books(book["path"])
    proposal = {"book_id": book_id, "title": book.get("title"), "path": book["path"],
                "edition": editions.edition_of(book), "series": None, "groups": []}
    if len(groups) < 2:
        _proposals[book_id] = proposal
        return proposal
    catalog = None
    try:
        asin, name = await _series_asin(book, groups)
        if asin:
            catalog = await series_index.ensure_catalog(asin)
            proposal["series"] = {"asin": asin, "name": (catalog or {}).get("title") or name}
    except Exception as e:
        logger.warning(f"Split: couldn't load the series from Audible: {e}")
    settings = db.get_settings()
    parent = os.path.dirname(os.path.normpath(book["path"]))
    for i, g in enumerate(groups):
        match = _match(g, catalog, proposal["edition"])
        new, folder = _new_book(book, g, match, settings)
        proposal["groups"].append({
            "index": i, "number": g["number"], "title": new["title"], "files": g["files"], "size": g["size"],
            "match": {k: match.get(k) for k in ("title", "asin", "edition", "runtime_min", "narrators", "imageUrl",
                                                 "release_date", "catalog_sequence", "part_asins")} if match else None,
            "folder": folder, "exists": os.path.exists(os.path.join(parent, folder)),
            "_match": match,
        })
    _proposals[book_id] = proposal
    return proposal


def public(proposal):
    """The proposal without internal fields, for the browser."""
    return {**proposal, "groups": [{k: v for k, v in g.items() if not k.startswith("_")} for g in proposal["groups"]]}


async def apply(book_id, choices):
    """Creates the chosen books: [{"index", "title", "number"}] from the last proposal."""
    proposal = _proposals.get(book_id)
    original = db.get_book(book_id)
    if not proposal or not original or original.get("path") != proposal["path"]:
        raise ValueError("Open the split again; the book changed.")
    settings = db.get_settings()
    source = proposal["path"]
    parent = os.path.dirname(os.path.normpath(source))
    cover_src = os.path.join(source, original["cover"]) if original.get("cover") else ""
    made = []
    for choice in choices:
        group = next((g for g in proposal["groups"] if g["index"] == choice.get("index")), None)
        if group is None:
            continue
        number = str(choice.get("number") or group["number"]).strip()
        title = str(choice.get("title") or "").strip() or None
        new, folder = _new_book(original, group, group["_match"], settings, title=title, number=number)
        dest = os.path.join(parent, folder)
        if os.path.normcase(os.path.normpath(dest)) == os.path.normcase(os.path.normpath(source)):
            raise ValueError(f'"{folder}" is the original folder; give the book another title.')
        if os.path.exists(dest) and os.listdir(dest):
            raise ValueError(f'A folder named "{folder}" already exists.')
        plan = [(os.path.join(source, rel), os.path.basename(rel)) for rel in group["files"]]
        await asyncio.to_thread(_copy_files, plan, dest, True)
        cover = await audiobookshelf.download_cover(new.get("imageUrl", ""), dest)
        if not cover and cover_src and os.path.isfile(cover_src):
            ext = os.path.splitext(cover_src)[1].lower() or ".jpg"
            await asyncio.to_thread(_copy_files, [(cover_src, "cover" + ext)], dest, True)
            cover = "cover" + ext
        audiobookshelf.write_metadata(new, dest)
        made.append({**new, "path": dest, "cover": cover or "", **describe_files(dest)})
    if not made:
        raise ValueError("Choose at least one book.")
    db.remove_from_library(book_id)  # The combined entry; its folder is left as it is
    added, linked = db.import_books(made)
    db.add_history("split", original, f"Split into {len(made)} books: " + ", ".join(b["title"] for b in made))
    _proposals.pop(book_id, None)
    logger.info(f"Split {original.get('title')} into {len(made)} books next to {source}")
    return {"created": len(made), "added": added, "linked": linked, "original": source}
