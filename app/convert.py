"""Converting a book's audio files into a single M4B with chapters (one per file), tags and
its cover, using ffmpeg. Books wait in a queue (saved, so it survives restarts) and are
converted in the background one at a time: queued by hand, in bulk, or automatically
after a download is imported (Settings > Media Management).

The original files are kept, renamed to "<name>.original" (Audiobookshelf and BorgArr
ignore them), until you delete them. Imported files are copies, so a seeding download
isn't affected."""
import asyncio
import datetime
import json
import logging
import os
import re
import shutil
import tempfile

from app import db
from app.library import _natural_key, audio_files, describe_files, file_duration_min, safe_filename

logger = logging.getLogger(__name__)

ORIGINAL_SUFFIX = ".original"
AAC_CONTAINERS = {".m4a", ".m4b", ".mp4"}
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

QUEUE_FILE = os.path.join(db.CONFIG_DIR, "convert_queue.json")
RECENT_KEPT = 30
WAIT = 5  # seconds between checks of the queue

job = {"running": False, "book_id": "", "title": "", "progress": 0.0, "error": "", "finished": ""}
_state = None  # {"queue": [{"book_id", "title", "added", "source"}], "recent": [...]}
_proc = None   # The running ffmpeg, so it can be cancelled
_cancelled = False


def ffmpeg_path():
    return db.env("FFMPEG") or shutil.which("ffmpeg") or ""


def available():
    return bool(ffmpeg_path())


def originals(folder):
    """Original files kept after a conversion: [(path, size)]."""
    if not folder or not os.path.isdir(folder):
        return []
    found = []
    for root, _, files in os.walk(folder):
        for f in files:
            if f.endswith(ORIGINAL_SUFFIX):
                path = os.path.join(root, f)
                found.append((path, os.path.getsize(path)))
    return sorted(found)


def can_convert(book):
    """Why a book can't be converted, or "" if it can."""
    path = book.get("path") or ""
    if not os.path.isdir(path):
        return "The book has no folder"
    # Joining a folder that holds other books would make one file of all of them
    from app.library import same_path
    if same_path(path, db.get_settings().get("root_folder")):
        return "The book's folder is the Root Folder"
    inside = os.path.normcase(os.path.normpath(path)) + os.sep
    if any(b["id"] != book.get("id") and os.path.normcase(os.path.normpath(b.get("path") or "")).startswith(inside)
           for b in db.get_library()):
        return "Another book's folder is inside this book's folder"
    files = audio_files(path)
    if not files:
        return "No audio files"
    if len(files) == 1 and files[0][0].lower().endswith(".m4b"):
        return "It's already a single M4B"
    return ""


def _meta_escape(text):
    out = str(text or "")
    for ch in ("\\", "=", ";", "#", "\n"):
        out = out.replace(ch, "\\" + ch)
    return out


def _chapter_title(path, index):
    stem = os.path.splitext(os.path.basename(path))[0]
    return stem if stem.strip() else f"Part {index}"


def build(book, files, durations, cover, out_path, workdir, encode=False):
    """The ffmpeg command, and the concat list and metadata files it reads."""
    list_path = os.path.join(workdir, "files.txt")
    with open(list_path, "w", encoding="utf-8") as f:
        for path in files:
            f.write("file '" + os.path.abspath(path).replace("'", "'\\''") + "'\n")
    meta_path = os.path.join(workdir, "metadata.txt")
    year = (book.get("release_date") or "")[:4]
    lines = [";FFMETADATA1", f"title={_meta_escape(book.get('title'))}", f"album={_meta_escape(book.get('title'))}",
             f"artist={_meta_escape(book.get('authors'))}", f"album_artist={_meta_escape(book.get('authors'))}",
             f"composer={_meta_escape(book.get('narrators'))}", "genre=Audiobook"]
    if year.isdigit():
        lines.append(f"date={year}")
    start = 0
    for i, (path, minutes) in enumerate(zip(files, durations), 1):
        end = start + int(round(minutes * 60000))
        lines += ["", "[CHAPTER]", "TIMEBASE=1/1000", f"START={start}", f"END={end}",
                  f"title={_meta_escape(_chapter_title(path, i))}"]
        start = end
    with open(meta_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    copy = not encode and all(os.path.splitext(p)[1].lower() in AAC_CONTAINERS for p in files)
    cmd = [ffmpeg_path(), "-hide_banner", "-nostdin", "-nostats", "-loglevel", "warning", "-y", "-f", "concat", "-safe", "0", "-i", list_path,
           "-i", meta_path]
    if cover:
        cmd += ["-i", cover]
    cmd += ["-map", "0:a", "-map_metadata", "1", "-map_chapters", "1"]
    if cover:
        cmd += ["-map", "2:v", "-c:v", "copy", "-disposition:v:0", "attached_pic"]
    if copy:
        cmd += ["-c:a", "copy"]
    else:
        # Close to the source: total size over length, rounded, between 32 and 128 kbps
        total_bytes = sum(os.path.getsize(p) for p in files)
        kbps = total_bytes * 8 / 1000 / max(1, sum(durations) * 60)
        bitrate = min(128, max(32, int(round(kbps / 16)) * 16))
        cmd += ["-c:a", "aac", "-b:a", f"{bitrate}k"]
    cmd += ["-movflags", "+faststart", "-progress", "pipe:1", "-f", "mp4", out_path]
    return cmd, copy


async def _run(book):
    folder = book["path"]
    files = sorted((f for f, _ in audio_files(folder)), key=_natural_key)
    # ffmpeg reads the files from a list, one per line: a name with a line break in it (a
    # crafted download) could add entries of its own, e.g. a URL for ffmpeg to fetch
    odd = next((f for f in files + [book.get("cover") or ""] if _CONTROL.search(f)), None)
    if odd:
        raise RuntimeError(f"A file name has a line break or control character in it ({odd!r}); rename it first")
    durations = await asyncio.to_thread(lambda: [file_duration_min(f) for f in files])
    if any(not d for d in durations):
        bad = [os.path.basename(f) for f, d in zip(files, durations) if not d]
        raise RuntimeError(f"Can't read the length of {', '.join(bad[:3])}")
    total_ms = sum(durations) * 60000
    stem = safe_filename((book.get("title") or "").split(":")[0]) or "Audiobook"
    out = os.path.join(folder, stem + ".m4b")
    if os.path.exists(out) and out not in files:
        raise RuntimeError(f"{stem}.m4b already exists in the folder")
    # Renaming over originals kept from an earlier conversion would lose them
    kept = [os.path.basename(f) for f in files if os.path.exists(f + ORIGINAL_SUFFIX)]
    if kept:
        raise RuntimeError(f"Originals from an earlier conversion are still there ({kept[0]}{ORIGINAL_SUFFIX}); delete them first")
    # Not an audio extension, so a crash can't leave a half file that looks like the book
    partial = os.path.join(folder, f"{stem}.m4b.converting")
    workdir = os.path.join(db.CONFIG_DIR, "convert")
    os.makedirs(workdir, exist_ok=True)
    cover = os.path.join(folder, book["cover"]) if book.get("cover") and os.path.isfile(os.path.join(folder, book["cover"])) else ""
    cmd, copied = build(book, files, durations, cover, partial, workdir)
    logger.info(f"Converting {book.get('title')} to M4B ({'copying' if copied else 're-encoding'} {len(files)} files)")
    try:
        code, errors = await _ffmpeg(cmd, total_ms)
        if _cancelled:
            raise RuntimeError("Cancelled")
        if code != 0 and copied:
            # AAC files with different settings can't be joined as they are: encode instead
            logger.info(f"Copying the audio failed ({errors[-1] if errors else code}); re-encoding")
            cmd, _ = build(book, files, durations, cover, partial, workdir, encode=True)
            code, errors = await _ffmpeg(cmd, total_ms)
        if code != 0 or not os.path.isfile(partial):
            raise RuntimeError("ffmpeg failed: " + (errors[-1] if errors else f"exit code {code}"))
        # Nothing is switched over unless the new file checks out
        problem = await verify(partial, sum(durations), len(files))
        if problem:
            raise RuntimeError(f"{problem}; the original files are unchanged")
        if _cancelled:  # Cancelled while the new file was being checked
            raise RuntimeError("Cancelled")
        renamed = []
        try:
            for f in files:
                os.rename(f, f + ORIGINAL_SUFFIX)
                renamed.append(f)
            os.replace(partial, out)
        except OSError as e:
            # Put back what was renamed, so the book is as it was
            for f in reversed(renamed):
                try:
                    if os.path.exists(f + ORIGINAL_SUFFIX) and not os.path.exists(f):
                        os.rename(f + ORIGINAL_SUFFIX, f)
                except OSError:
                    logger.error(f"Couldn't rename {f}{ORIGINAL_SUFFIX} back")
            raise RuntimeError(f"Couldn't switch to the converted file: {e}; the original files are unchanged")
    except BaseException:
        if os.path.exists(partial):
            os.remove(partial)
        raise
    # Disc folders left with only .original files stay; they hold the originals
    db.update_book(book["id"], **describe_files(folder))
    db.add_history("converted", book, f"Converted {len(files)} file{'s' if len(files) != 1 else ''} to {os.path.basename(out)}")
    return out


def chapter_count(path):
    """The number of chapters in an M4B, or None if they can't be read."""
    try:
        from mutagen.mp4 import MP4
        chapters = MP4(path).chapters
    except Exception:
        return None
    return len(chapters) if chapters is not None else 0


def decode_command(path):
    """ffmpeg decoding the whole file, reporting only errors."""
    return [ffmpeg_path(), "-hide_banner", "-nostdin", "-v", "error", "-i", path, "-map", "0:a", "-f", "null", "-"]


async def verify(path, expected_min, files):
    """Checks a converted file: its length matches the originals' total (within a minute
    plus 0.1%), it has a chapter per original file, and it decodes from start to end
    without errors. Returns what's wrong, or ""."""
    length = await asyncio.to_thread(file_duration_min, path)
    allowed = 1.0 + expected_min * 0.001
    if not length or abs(length - expected_min) > allowed:
        return f"The converted file runs {length or 0:.1f} min instead of {expected_min:.1f} min"
    chapters = await asyncio.to_thread(chapter_count, path)
    if chapters is not None and chapters != files:
        return f"The converted file has {chapters} chapters instead of {files}"
    # A full decode finds damage the length alone can't show
    proc = await asyncio.create_subprocess_exec(*decode_command(path), stdout=asyncio.subprocess.DEVNULL,
                                                stderr=asyncio.subprocess.PIPE)
    _, err = await proc.communicate()
    errors = [line for line in err.decode(errors="ignore").splitlines() if line.strip()]
    if proc.returncode != 0 or errors:
        return "The converted file doesn't play through cleanly: " + (errors[0] if errors else f"exit code {proc.returncode}")
    return ""


async def _lines(stream):
    """A process's output line by line, split on newlines or carriage returns (ffmpeg redraws
    its status line with \\r), with no limit on how long a line gets."""
    buffer = b""
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            break
        *lines, buffer = re.split(rb"[\r\n]", buffer + chunk)
        buffer = buffer[-65536:]
        for line in lines:
            if line.strip():
                yield line.decode(errors="ignore").strip()
    if buffer.strip():
        yield buffer.decode(errors="ignore").strip()


async def _ffmpeg(cmd, total_ms):
    """Runs ffmpeg, following its progress. Returns (exit code, last error lines)."""
    global _proc
    job["progress"] = 0.0
    proc = _proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    errors = []

    async def read_progress():
        async for line in _lines(proc.stdout):
            key, _, value = line.partition("=")
            if key == "out_time_ms" and value.isdigit() and total_ms:
                job["progress"] = min(0.99, int(value) / 1000 / total_ms)

    async def read_errors():
        async for line in _lines(proc.stderr):
            errors.append(line)
            del errors[:-20]

    try:
        await asyncio.gather(read_progress(), read_errors())
        return await proc.wait(), errors
    finally:
        # Never leave ffmpeg running (and writing) after something went wrong here
        if proc.returncode is None:
            proc.kill()
            await proc.wait()


# --- The queue ----------------------------------------------------------------

def _load():
    global _state
    if _state is None:
        try:
            with open(QUEUE_FILE, "r", encoding="utf-8") as f:
                _state = json.load(f)
        except (OSError, ValueError):
            _state = {}
        _state.setdefault("queue", [])
        _state.setdefault("recent", [])
    return _state


def _save():
    os.makedirs(db.CONFIG_DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=db.CONFIG_DIR, prefix=".convert-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(_load(), f)
    os.replace(tmp, QUEUE_FILE)


def queued_ids():
    return [q["book_id"] for q in _load()["queue"]]


def enqueue(book_ids, source="manual"):
    """Adds books to the queue (skipping ones that can't be converted or are already
    queued). Returns (added, {book_id: why it wasn't added})."""
    state = _load()
    present = set(queued_ids()) | ({job["book_id"]} if job["running"] else set())
    added, skipped = 0, {}
    for book_id in book_ids:
        book = db.get_book(book_id)
        reason = "Not in the library" if not book else ("Already queued" if book_id in present else can_convert(book))
        if reason:
            skipped[book_id] = reason
            continue
        state["queue"].append({"book_id": book_id, "title": book.get("title", ""), "source": source,
                               "added": datetime.datetime.now().isoformat(timespec="seconds")})
        present.add(book_id)
        added += 1
    if added:
        _save()
    return added, skipped


def remove(book_id):
    """Takes a book out of the queue, or cancels its conversion if it's running."""
    global _cancelled
    if job["running"] and job["book_id"] == book_id:
        _cancelled = True
        if _proc and _proc.returncode is None:
            _proc.terminate()
        return True
    state = _load()
    before = len(state["queue"])
    state["queue"] = [q for q in state["queue"] if q["book_id"] != book_id]
    if len(state["queue"]) != before:
        _save()
        return True
    return False


def eligible_books():
    """Books on disk that aren't a single M4B yet (for queueing the existing library)."""
    return [b for b in db.get_library() if b.get("status") == "Imported" and not can_convert(b)]


def status():
    state = _load()
    return {"available": available(), "current": dict(job) if job["running"] else None,
            "queue": [{**q, "position": i + 1} for i, q in enumerate(state["queue"])],
            "recent": list(reversed(state["recent"]))}  # The last RECENT_KEPT, newest first


def _record(book_id, title, result, message=""):
    state = _load()
    state["recent"].append({"book_id": book_id, "title": title, "result": result, "message": message,
                            "finished": datetime.datetime.now().isoformat(timespec="seconds")})
    del state["recent"][:-RECENT_KEPT]
    _save()


async def _convert_next():
    """Converts the first book in the queue. Returns False when the queue is empty."""
    global _cancelled
    state = _load()
    if not state["queue"]:
        return False
    item = state["queue"].pop(0)
    _save()
    book = db.get_book(item["book_id"])
    if not book or can_convert(book):
        _record(item["book_id"], item["title"], "skipped", "Not in the library" if not book else can_convert(book))
        return True
    _cancelled = False
    job.update(running=True, book_id=book["id"], title=book.get("title", ""), progress=0.0, error="", finished="")
    try:
        await _run(book)
        job["finished"] = "ok"
        message = ""
        if db.get_settings().get("delete_originals_after_convert"):
            count, size = await asyncio.to_thread(delete_originals, db.get_book(book["id"]))
            message = f"Deleted {count} original file{'s' if count != 1 else ''}"
        _record(book["id"], book.get("title", ""), "converted", message)
    except Exception as e:
        cancelled = _cancelled
        logger.error(f"Converting {book.get('title')} {'was cancelled' if cancelled else 'failed'}: {e}")
        job["error"] = "Cancelled" if cancelled else str(e)
        job["finished"] = "cancelled" if cancelled else "error"
        _record(book["id"], book.get("title", ""), job["finished"], "" if cancelled else str(e))
    finally:
        job["running"] = False
    return True


async def run_queue():
    """The background worker: converts queued books one at a time."""
    while True:
        try:
            if available() and await _convert_next():
                continue
        except Exception as e:
            logger.error(f"Conversion queue error: {e}", exc_info=True)
        await asyncio.sleep(WAIT)


def delete_originals(book):
    """Deletes the .original files kept by a conversion. Returns (count, bytes)."""
    removed = originals(book.get("path"))
    for path, _ in removed:
        os.remove(path)
    # Folders (e.g. CD1) left empty
    for root, dirs, files in os.walk(book["path"], topdown=False):
        if root != book["path"] and not os.listdir(root):
            os.rmdir(root)
    if removed:
        db.add_history("converted", book, f"Deleted {len(removed)} original file{'s' if len(removed) != 1 else ''}")
    return len(removed), sum(size for _, size in removed)
