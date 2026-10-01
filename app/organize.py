"""Organize: renames existing book folders (and optionally their audio files) to the Book
Folder Format, like Sonarr's Organize. Nothing changes until a proposed change is approved.

Books inside the Root Folder end up directly in it; books elsewhere are renamed where they
are. Folders are renamed or moved on the same drive only (instantly, so hardlinks and
Audiobookshelf's file ids are kept); a move that would need copying is refused."""
import logging
import os

from app import db
from app.library import _natural_key, audio_files, build_folder_name, same_path, safe_filename

logger = logging.getLogger(__name__)


def _within(path, folder):
    try:
        return os.path.commonpath([os.path.normcase(os.path.abspath(path)),
                                   os.path.normcase(os.path.abspath(folder))]) == os.path.normcase(os.path.abspath(folder))
    except ValueError:  # Different drives
        return False


def _file_renames(book, folder):
    """[(current relative path, new name)] when audio files are renamed like downloads:
    "Title.m4b", or "Title - Part 01.mp3"... in disc and track order."""
    files = sorted((os.path.relpath(f, folder) for f, _ in audio_files(folder)), key=_natural_key)
    stem = safe_filename((book.get("title") or "").split(":")[0]) or "Audiobook"
    width = max(2, len(str(len(files))))
    renames = []
    for i, rel in enumerate(files, 1):
        ext = os.path.splitext(rel)[1].lower()
        new = f"{stem}{ext}" if len(files) == 1 else f"{stem} - Part {i:0{width}d}{ext}"
        if rel.replace("\\", "/") != new:
            renames.append((rel, new))
    return renames


def plan(rename_files=False, book_ids=None):
    """Proposed changes: [{book_id, title, current, proposed, files, conflict}], plus how
    many books already match the format."""
    settings = db.get_settings()
    root = settings.get("root_folder") or ""
    template = settings.get("naming_format")
    items, unchanged, taken = [], 0, {}
    books = [b for b in db.get_library() if b.get("status") == "Imported" and b.get("path")]
    books.sort(key=lambda b: (b.get("authors") or "", b.get("title") or ""))
    for book in books:
        if book_ids is not None and book["id"] not in book_ids:
            continue
        current = os.path.normpath(book["path"])
        if not os.path.exists(current):
            continue
        parent = root if root and os.path.isdir(root) and _within(current, root) else os.path.dirname(current)
        proposed = os.path.normpath(os.path.join(parent, build_folder_name(template, book)))
        is_file = os.path.isfile(current)
        files = _file_renames(book, current) if rename_files and not is_file else []
        if current == proposed and not files:
            unchanged += 1
            continue
        conflict = ""
        key = os.path.normcase(proposed)
        if key in taken:
            conflict = f'"{taken[key]}" would get the same folder'
        elif os.path.exists(proposed) and not same_path(current, proposed):
            conflict = "A folder with that name already exists"
        taken.setdefault(key, book.get("title"))
        items.append({
            "book_id": book["id"], "title": book.get("title", ""), "authors": book.get("authors", ""),
            "current": current, "proposed": proposed, "single_file": is_file,
            "current_rel": os.path.relpath(current, root) if root and _within(current, root) else current,
            "proposed_rel": os.path.relpath(proposed, root) if root and _within(proposed, root) else proposed,
            "files": [{"from": a, "to": b} for a, b in files], "conflict": conflict,
        })
    return {"root": root, "format": template, "items": items, "unchanged": unchanged}


def _remove_empty_parents(folder, stop):
    """Removes folders left empty by a move (e.g. an Author folder), up to the root."""
    folder = os.path.normpath(folder)
    while folder and not same_path(folder, stop) and os.path.isdir(folder) and not os.listdir(folder):
        os.rmdir(folder)
        folder = os.path.dirname(folder)


def apply_one(item):
    """Carries out one proposed change. Raises OSError/ValueError with a readable reason."""
    if item["conflict"]:
        raise ValueError(item["conflict"])
    current, proposed = item["current"], item["proposed"]
    if not os.path.exists(current):
        raise ValueError("The folder is gone")
    if current != proposed:
        if os.path.exists(proposed) and not same_path(current, proposed):
            raise ValueError("A folder with that name already exists")
        os.makedirs(os.path.dirname(proposed), exist_ok=True)
        try:
            if item["single_file"]:
                os.makedirs(proposed, exist_ok=True)
                os.rename(current, os.path.join(proposed, os.path.basename(current)))
            elif same_path(current, proposed):
                # Only the letter case changes (case-insensitive drives need a detour)
                detour = proposed + ".organizing"
                os.rename(current, detour)
                os.rename(detour, proposed)
            else:
                os.rename(current, proposed)
        except OSError as e:
            if getattr(e, "errno", None) == 18:  # EXDEV: another drive
                raise ValueError("It's on a different drive from the Root Folder; not moved (that would mean copying)")
            raise
    for f in item["files"]:
        src, dest = os.path.join(proposed, f["from"]), os.path.join(proposed, f["to"])
        if os.path.exists(src) and not os.path.exists(dest):
            os.rename(src, dest)
    # Disc folders emptied by flattening, and parent folders emptied by the move
    for f in item["files"]:
        sub = os.path.dirname(os.path.join(proposed, f["from"]))
        if not same_path(sub, proposed):
            _remove_empty_parents(sub, proposed)
    root = db.get_settings().get("root_folder") or ""
    if current != proposed:
        _remove_empty_parents(os.path.dirname(current), root or os.path.dirname(os.path.dirname(current)))
    return proposed


def apply(book_ids, rename_files=False):
    """Applies the approved changes: [{book_id, ok, path or error}]."""
    proposal = plan(rename_files, set(book_ids))
    results = []
    for item in proposal["items"]:
        book = db.get_book(item["book_id"])
        try:
            path = apply_one(item)
        except (OSError, ValueError) as e:
            logger.warning(f"Organize: couldn't change {item['current']}: {e}")
            results.append({"book_id": item["book_id"], "ok": False, "error": str(e)})
            continue
        db.update_book(item["book_id"], path=path)
        db.add_history("organized", book, f"{item['current']} -> {path}")
        results.append({"book_id": item["book_id"], "ok": True, "path": path})
    done = {r["book_id"] for r in results}
    for book_id in book_ids:
        if book_id not in done:
            results.append({"book_id": book_id, "ok": True, "path": (db.get_book(book_id) or {}).get("path"), "unchanged": True})
    return results
