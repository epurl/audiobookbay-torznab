"""Library health: books that need attention, each with a fix where there is one.

Quick checks look at the library and folder listings. The deep check (run on request in
the background) opens every audio file: unreadable files, and books whose length is far
from Audible's runtime."""
import asyncio
import datetime
import json
import logging
import os
import re
import tempfile
from collections import defaultdict

from app import db, editions, splitter
from app.library import audio_files, audio_length, normalize, part_number, primary_author, title_key

logger = logging.getLogger(__name__)

DEEP_FILE = os.path.join(db.CONFIG_DIR, "health_deep.json")

# What each kind of issue means, how serious it is, and the fix offered
KINDS = {
    "missing": ("Folder is gone", "error", "open"),
    "no_files": ("No audio files in the folder", "error", "open"),
    "unreadable": ("Audio files that can't be read", "error", "open"),
    "empty_files": ("Empty (0 byte) audio files", "error", "open"),
    "part_gap": ("Parts missing", "error", "open"),
    "wrong_length": ("Length doesn't match Audible", "warning", "open"),
    "needs_review": ("Download held for review", "warning", "activity"),
    "duplicate": ("Same book in more than one folder", "warning", "open"),
    "collection": ("Several books in one folder", "warning", "split"),
    "check_edition": ("Edition to check", "warning", "open"),
    "unmatched": ("Not matched on Audible", "info", "match"),
    "no_runtime": ("No runtime (the length check can't run)", "info", "match"),
    "no_cover": ("No cover", "info", "cover"),
}
_PART_OF = re.compile(r"(?:part|pt)?[\s._-]*0*(\d{1,3})[\s._-]*of[\s._-]*0*(\d{1,3})", re.IGNORECASE)


def _issue(kind, book, detail=""):
    label, severity, fix = KINDS[kind]
    return {"kind": kind, "label": label, "severity": severity, "fix": fix, "book_id": book["id"],
            "title": book.get("title", ""), "authors": book.get("authors", ""), "detail": detail}


def _part_gaps(files):
    """ "Part 1 of 3", "Part 3 of 3" -> "Part 2 of 3 is missing" """
    seen = defaultdict(set)
    for path, _ in files:
        stem = os.path.splitext(os.path.basename(path))[0]
        m = _PART_OF.search(stem)
        if m and 0 < int(m.group(1)) <= int(m.group(2)) <= 50:
            # Group by what's left of the name, so two books' parts aren't mixed up
            seen[(normalize(stem[:m.start()]), int(m.group(2)))].add(int(m.group(1)))
    gaps = []
    for (_, total), parts in seen.items():
        missing = sorted(set(range(1, total + 1)) - parts)
        if missing:
            gaps.append(f"Part {', '.join(map(str, missing))} of {total} missing")
    return gaps


def _shown_path(path, root):
    """A folder relative to the Root Folder when it's inside it."""
    try:
        rel = os.path.relpath(path, root) if root else path
    except ValueError:  # Another drive
        return path
    return path if rel.startswith("..") else rel


def _load_deep():
    try:
        with open(DEEP_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"checked": "", "books": {}}


def _save_deep(data):
    os.makedirs(db.CONFIG_DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=db.CONFIG_DIR, prefix=".health-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, DEEP_FILE)


def _fingerprint(files):
    return f"{len(files)}:{sum(size for _, size in files)}"


def check():
    """Every issue in the library: {"issues": [...], "counts": {kind: n}, "deep": {...}}."""
    library = db.get_library()
    settings = db.get_settings()
    tolerance = settings.get("runtime_tolerance", 10)
    deep = _load_deep()
    issues = []
    on_disk = []
    for book in library:
        status = book.get("status")
        path = book.get("path") or ""
        if status == "Missing":
            issues.append(_issue("missing", book, path))
        if status == "Needs Review":
            issues.append(_issue("needs_review", book, book.get("review_reason", "")))
        if book.get("edition_check"):
            issues.append(_issue("check_edition", book, book.get("edition_reason", "")))
        if status != "Imported" or not path:
            continue
        if not book.get("asin") and not book.get("ga_url"):  # GraphicAudio releases aren't on Audible
            issues.append(_issue("unmatched", book))
        elif not book.get("runtime_min"):
            issues.append(_issue("no_runtime", book))
        if not os.path.exists(path):
            continue  # The next rescan marks it Missing
        on_disk.append(book)
        files = audio_files(path)
        if not files:
            issues.append(_issue("no_files", book, path))
            continue
        empty = [os.path.basename(f) for f, size in files if size == 0]
        if empty:
            issues.append(_issue("empty_files", book, ", ".join(empty[:5]) + (" …" if len(empty) > 5 else "")))
        for gap in _part_gaps(files):
            issues.append(_issue("part_gap", book, gap))
        result = deep["books"].get(book["id"])
        if result and result.get("fingerprint") != _fingerprint(files):
            result = None  # The files changed since the deep check
        cover = book.get("cover")
        has_cover_file = bool(cover) and os.path.isdir(path) and os.path.isfile(os.path.join(path, cover))
        if not has_cover_file and not (result and result.get("embedded_cover")):
            issues.append(_issue("no_cover", book, "Get Audible's cover" if book.get("imageUrl") else "Match it on Audible to get one"))
        if os.path.isdir(path) and len(files) > 1 and len(splitter.detect_books(path)) >= 2:
            issues.append(_issue("collection", book, "Split it into one folder per book"))
        if result:
            if result.get("unreadable"):
                issues.append(_issue("unreadable", book, ", ".join(result["unreadable"][:5])))
            expected, actual = book.get("runtime_min") or 0, result.get("minutes")
            if expected and actual and abs(actual - expected) / expected * 100 > tolerance:
                issues.append(_issue("wrong_length", book, f"The files run {actual} min; Audible lists {expected} min"))

    # The same book (title, author, edition) in more than one folder
    # (a book's parts, or another recording of it with its own ASIN, aren't copies)
    copies = defaultdict(list)
    for book in on_disk:
        copies[(title_key(book.get("title")), normalize(primary_author(book.get("authors"))), editions.edition_of(book),
                part_number(book.get("title")))].append(book)
    for same in copies.values():
        for book in same:
            others = [b for b in same if b is not book
                      and not (b.get("asin") and book.get("asin") and b["asin"] != book["asin"])]
            if others:
                shown = [_shown_path(b["path"], settings.get("root_folder")) for b in others]
                issues.append(_issue("duplicate", book, "Also in: " + ", ".join(shown)))

    order = list(KINDS)
    issues.sort(key=lambda i: (order.index(i["kind"]), i["authors"], i["title"]))
    counts = {k: sum(1 for i in issues if i["kind"] == k) for k in KINDS}
    return {"issues": issues, "counts": counts,
            "kinds": {k: {"label": v[0], "severity": v[1], "fix": v[2]} for k, v in KINDS.items()},
            "deep": {**deep_job, "checked": deep.get("checked", "")}}


# --- Deep check: open every audio file ---------------------------------------

deep_job = {"running": False, "total": 0, "done": 0}
_tasks = set()


def _deep_one(book):
    import mutagen
    files = audio_files(book["path"])
    unreadable, seconds, embedded_cover = [], 0.0, False
    for path, size in files:
        try:
            audio = mutagen.File(path)
            length = audio_length(path, audio) if audio is not None else 0  # A file without tags is falsy
            keys = [str(k) for k in (audio.tags.keys() if audio is not None and audio.tags else [])]
            embedded_cover = embedded_cover or any(k == "covr" or k.startswith("APIC") or k == "METADATA_BLOCK_PICTURE" for k in keys)
        except Exception:
            length = 0
        if not length:
            unreadable.append(os.path.basename(path))
        seconds += length or 0
    return {"fingerprint": _fingerprint(files), "unreadable": unreadable, "embedded_cover": embedded_cover,
            "minutes": round(seconds / 60) if not unreadable else None}


def start_deep_check():
    if deep_job["running"]:
        return False
    books = [b for b in db.get_library() if b.get("status") == "Imported" and b.get("path") and os.path.exists(b["path"])]
    deep_job.update(running=True, total=len(books), done=0)

    async def run():
        data = _load_deep()
        try:
            for book in books:
                try:
                    data["books"][book["id"]] = await asyncio.to_thread(_deep_one, book)
                except Exception as e:
                    logger.warning(f"Health: couldn't check {book.get('path')}: {e}")
                deep_job["done"] += 1
            data["checked"] = datetime.datetime.now().isoformat(timespec="seconds")
            known = {b["id"] for b in db.get_library()}
            data["books"] = {k: v for k, v in data["books"].items() if k in known}
            _save_deep(data)
        finally:
            deep_job["running"] = False

    task = asyncio.create_task(run())
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return True
