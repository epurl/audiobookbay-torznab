"""Converting a book's audio files into a single M4B with chapters (one per file), tags and
its cover, using ffmpeg. Runs in the background, one book at a time.

The original files are kept, renamed to "<name>.original" (Audiobookshelf and Bayarr
ignore them), until you delete them. If a file is a hardlink of a seeding download,
renaming it doesn't affect the download."""
import asyncio
import logging
import os
import shutil

from app import db
from app.library import _natural_key, audio_files, describe_files, file_duration_min, safe_filename

logger = logging.getLogger(__name__)

ORIGINAL_SUFFIX = ".original"
AAC_CONTAINERS = {".m4a", ".m4b", ".mp4"}

job = {"running": False, "book_id": "", "title": "", "progress": 0.0, "error": "", "finished": ""}
_tasks = set()


def ffmpeg_path():
    return os.environ.get("BAYARR_FFMPEG") or shutil.which("ffmpeg") or ""


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
    cmd = [ffmpeg_path(), "-hide_banner", "-nostdin", "-y", "-f", "concat", "-safe", "0", "-i", list_path,
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
    durations = await asyncio.to_thread(lambda: [file_duration_min(f) for f in files])
    if any(not d for d in durations):
        bad = [os.path.basename(f) for f, d in zip(files, durations) if not d]
        raise RuntimeError(f"Can't read the length of {', '.join(bad[:3])}")
    total_ms = sum(durations) * 60000
    stem = safe_filename((book.get("title") or "").split(":")[0]) or "Audiobook"
    out = os.path.join(folder, stem + ".m4b")
    if os.path.exists(out) and out not in files:
        raise RuntimeError(f"{stem}.m4b already exists in the folder")
    # Not an audio extension, so a crash can't leave a half file that looks like the book
    partial = os.path.join(folder, f"{stem}.m4b.converting")
    workdir = os.path.join(db.CONFIG_DIR, "convert")
    os.makedirs(workdir, exist_ok=True)
    cover = os.path.join(folder, book["cover"]) if book.get("cover") and os.path.isfile(os.path.join(folder, book["cover"])) else ""
    cmd, copied = build(book, files, durations, cover, partial, workdir)
    logger.info(f"Converting {book.get('title')} to M4B ({'copying' if copied else 're-encoding'} {len(files)} files)")
    code, errors = await _ffmpeg(cmd, total_ms)
    if code != 0 and copied:
        # AAC files with different settings can't be joined as they are: encode instead
        logger.info(f"Copying the audio failed ({errors[-1] if errors else code}); re-encoding")
        cmd, _ = build(book, files, durations, cover, partial, workdir, encode=True)
        code, errors = await _ffmpeg(cmd, total_ms)
    if code != 0 or not os.path.isfile(partial):
        if os.path.exists(partial):
            os.remove(partial)
        raise RuntimeError("ffmpeg failed: " + (errors[-1] if errors else f"exit code {code}"))

    # Nothing is switched over unless the new file checks out
    problem = await verify(partial, sum(durations), len(files))
    if problem:
        os.remove(partial)
        raise RuntimeError(f"{problem}; the original files are unchanged")
    for f in files:
        os.rename(f, f + ORIGINAL_SUFFIX)
    os.replace(partial, out)
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


async def _ffmpeg(cmd, total_ms):
    """Runs ffmpeg, following its progress. Returns (exit code, last error lines)."""
    job["progress"] = 0.0
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    errors = []

    async def read_progress():
        async for line in proc.stdout:
            key, _, value = line.decode(errors="ignore").strip().partition("=")
            if key == "out_time_ms" and value.isdigit() and total_ms:
                job["progress"] = min(0.99, int(value) / 1000 / total_ms)

    async def read_errors():
        async for line in proc.stderr:
            errors.append(line.decode(errors="ignore").strip())
            del errors[:-20]

    await asyncio.gather(read_progress(), read_errors())
    return await proc.wait(), errors


def start(book_id):
    book = db.get_book(book_id)
    if job["running"]:
        raise RuntimeError(f"Already converting {job['title']}")
    if not available():
        raise RuntimeError("ffmpeg isn't installed")
    reason = can_convert(book or {})
    if reason:
        raise RuntimeError(reason)
    job.update(running=True, book_id=book_id, title=book.get("title", ""), progress=0.0, error="", finished="")

    async def run():
        try:
            await _run(book)
            job["finished"] = "ok"
        except Exception as e:
            logger.error(f"Converting {book.get('title')} failed: {e}")
            job["error"] = str(e)
            job["finished"] = "error"
        finally:
            job["running"] = False
            job["progress"] = 1.0 if job["finished"] == "ok" else job["progress"]

    task = asyncio.create_task(run())
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


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
