import asyncio
import logging
import datetime
import os
import re
import shutil
from . import audible, audiobookshelf, db
from .library import build_folder_name, describe_files, find_match, plan_import_files, total_duration_min
from .scraper import search_for_book, fetch_detail_info
from .qbittorrent import send_to_qbittorrent, get_completed_torrents

logger = logging.getLogger(__name__)

SEARCH_INTERVAL = 6 * 60 * 60  # Series syncs, release checks and searches for Monitored books
IMPORT_INTERVAL = 60           # Polling qBittorrent for finished downloads

# Words that mark multi-book bundles, which should never be grabbed for a single book
BUNDLE_WORDS = {"collection", "complete", "boxset", "box", "omnibus", "novels", "audiobooks"}

# Keep references to fire-and-forget tasks so they aren't garbage collected mid-run
_background_tasks = set()


async def run_monitor_loop():
    logger.info("Starting background monitor (search every 6h, import check every 60s)...")
    await asyncio.gather(_search_loop(), _import_loop())


async def _search_loop():
    while True:
        try:
            await check_library()
        except Exception as e:
            logger.error(f"Error in search loop: {e}", exc_info=True)
        await asyncio.sleep(SEARCH_INTERVAL)


async def _import_loop():
    while True:
        try:
            settings = db.get_settings()
            if settings.get("qbt_enabled"):
                await import_completed_downloads(settings)
        except Exception as e:
            logger.error(f"Error in import loop: {e}", exc_info=True)
        await asyncio.sleep(IMPORT_INTERVAL)


def _run_in_background(coro):
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


def schedule_search(book):
    """Searches for a book in the background, e.g. right after it's added to the library."""
    schedule_searches([book])


def schedule_searches(books):
    """Searches for books one at a time in the background, so adding a whole series
    doesn't fire dozens of AudiobookBay requests at once."""
    async def run():
        for book in books:
            try:
                settings = db.get_settings()
                if not settings.get("qbt_enabled"):
                    return
                current = db.get_book(book["id"])
                if current and current.get("status") == "Monitored":
                    await auto_download_book(current, settings)
            except Exception as e:
                logger.error(f"Error searching for {book.get('title')}: {e}", exc_info=True)
    _run_in_background(run())


def find_missing_books():
    """Marks Imported books whose folder has disappeared as Missing (and restores ones
    that came back). Skipped when the folder's parent is unreachable, e.g. an unmounted
    drive, so a temporary outage doesn't flag the whole library."""
    missing = restored = 0
    for book in db.get_library():
        path = book.get("path")
        if not path or book.get("status") not in ("Imported", "Missing"):
            continue
        if not os.path.isdir(os.path.dirname(os.path.normpath(path))):
            continue
        exists = os.path.exists(path)
        if book["status"] == "Imported" and not exists:
            logger.warning(f"Files for '{book.get('title')}' are gone from {path}; marking Missing")
            db.update_library_status(book["id"], "Missing")
            db.add_history("missing", book, f"Folder is gone: {path}")
            missing += 1
        elif book["status"] == "Missing" and exists:
            db.update_library_status(book["id"], "Imported")
            restored += 1
    return missing, restored


async def sync_series(series, settings):
    """Adds books from a monitored Audible series that aren't in the library yet, and fills
    in Audible details (ASIN, runtime, series) on books that are. Returns the new books."""
    title, books = await audible.get_series_books(series["asin"], settings.get("language", "All"))
    library = db.get_library()
    added = []
    for book in books:
        existing = find_match(library, book)
        if existing:
            fill = {k: book[k] for k in ("asin", "series_asin", "series", "sequence", "runtime_min", "narrators",
                                         "description", "publisher", "imageUrl")
                    if book.get(k) and not existing.get(k)}
            if fill:
                db.update_book(existing["id"], **fill)
            continue
        # 'future' only wants books released after the series was added
        status = db.initial_status(book.get("release_date"))
        if series.get("mode") == "future" and status != "Unreleased" \
                and (book.get("release_date") or "") < series["added"][:10]:
            status = "Unmonitored"
        entry = db.add_to_library(book, status=status)
        library.append(entry)
        added.append(entry)
    db.update_series(series["id"], title=title or series.get("title"),
                     last_sync=datetime.datetime.now().isoformat(timespec="seconds"))
    if added:
        wanted = sum(1 for b in added if b["status"] in ("Monitored", "Unreleased"))
        db.add_history("series", {"title": series.get("title")},
                       f"Added {len(added)} book{'s' if len(added) != 1 else ''} from the series ({wanted} wanted)")
    return added


async def check_library():
    settings = db.get_settings()
    today = datetime.date.today()

    await asyncio.to_thread(find_missing_books)

    for series in db.get_series_list():
        if series.get("monitored"):
            try:
                await sync_series(series, settings)
            except Exception as e:
                logger.error(f"Could not sync series {series.get('title')}: {e}")

    for book in db.get_library():
        title = book.get("title")
        status = book.get("status")

        # 1. Graduate Unreleased to Monitored once released. This doesn't need a
        # download client, so it runs even when qBittorrent is disabled.
        if status == "Unreleased" and book.get("release_date"):
            try:
                dt = datetime.datetime.strptime(book["release_date"], "%Y-%m-%d").date()
                if dt <= today:
                    logger.info(f"Book '{title}' is now released! Upgrading status to Monitored.")
                    db.update_library_status(book["id"], "Monitored")
                    db.add_history("released", book, "Released today; now Monitored")
                    status = "Monitored"
            except ValueError:
                pass

        # 2. Search for Monitored books
        if status == "Monitored":
            if not settings.get("qbt_enabled"):
                continue
            logger.info(f"Searching for Monitored book: {title}")
            await auto_download_book(book, settings)


def _words(text):
    return re.findall(r"[a-z0-9]+", (text or "").lower())


def score_result(book, result, settings):
    """Scores an ABB result for a library book. Returns None if it should not be grabbed."""
    # Releases rejected before for this book
    result_hash = db.extract_infohash(result.get("magnet_url"))
    if result_hash and result_hash in (book.get("blocklist") or []):
        return None

    # Language
    pref_lang = settings.get("language", "All").lower()
    if pref_lang != "all" and result.get("language") and result["language"].lower() != pref_lang:
        return None

    # Format
    is_m4b = (result.get("format") or "").upper() == "M4B"
    format_pref = settings.get("format_preference", "prefer_m4b")
    if format_pref == "m4b_only" and not is_m4b:
        return None

    # Title relevance: every word of the main title (before any subtitle) must appear
    main_title = (book.get("title") or "").split(":")[0]
    title_words = set(_words(main_title))
    result_words = set(_words(result.get("title")))
    if not title_words or not title_words.issubset(result_words):
        return None

    # Skip multi-book bundles and dramatized versions unless the library book is one
    if (result_words & BUNDLE_WORDS) - title_words:
        return None
    if "dramatized" in result_words and "dramatized" not in title_words:
        return None

    score = 0
    if is_m4b and format_pref == "prefer_m4b":
        score += 5

    narrators = book.get("narrators", "")
    abb_narrator = result.get("abb_narrator", "Unknown")
    if settings.get("auto_match_narrator", True) and narrators and narrators != "Unknown Narrator" and abb_narrator != "Unknown":
        aud_last_name = narrators.split(",")[0].split()[-1] if narrators.split() else ""
        if aud_last_name and aud_last_name in abb_narrator:
            score += 10

    return score


def choose_best_result(book, results, settings):
    best_match, best_score = None, None
    for res in results:
        score = score_result(book, res, settings)
        if score is not None and (best_score is None or score > best_score):
            best_match, best_score = res, score
    return best_match


async def auto_download_book(book, settings):
    title = book.get("title")
    author = book.get("authors", "").split(',')[0].strip()  # Primary author

    results = await search_for_book(title.split(":")[0], author)
    if not results:
        logger.info(f"No results found for {title}")
        return False

    best_match = choose_best_result(book, results, settings)
    if not best_match:
        logger.info(f"No suitable match found for {title} based on filters.")
        return False

    logger.info(f"Found match for {title}: {best_match.get('title')} ({best_match.get('format')})")
    magnet = best_match.get("magnet_url")
    if not magnet:
        detail_info = await fetch_detail_info(best_match.get("link"), best_match.get("title"))
        magnet = detail_info.get("magnet") if detail_info else None
    if not magnet:
        logger.error(f"Failed to extract magnet for {title}")
        return False

    return await grab(book, magnet, settings, release=best_match)


async def grab(book, magnet, settings, release=None):
    """Sends a magnet to qBittorrent and moves the library book to Downloading."""
    title = book.get("title")
    success = await send_to_qbittorrent(
        settings.get("qbt_host"),
        settings.get("qbt_user"),
        settings.get("qbt_pass"),
        magnet,
        title
    )
    if not success:
        logger.error(f"Failed to send {title} to qBittorrent")
        db.add_history("failed", book, "Could not send the release to qBittorrent")
        return False

    logger.info(f"Sent {title} to qBittorrent")
    release_name = (release or {}).get("title", "")
    db.update_book(book["id"], status="Downloading", download_hash=db.extract_infohash(magnet),
                   release_title=release_name, review_reason="", skip_verify=False)
    details = ", ".join(x for x in (release_name, (release or {}).get("format"), (release or {}).get("size_str")) if x)
    db.add_history("grabbed", book, f"Sent to qBittorrent{': ' + details if details else ''}")
    return True


def _find_book_for_torrent(library, torrent):
    """Matches a torrent to a library book by info hash, falling back to the bayarr tag."""
    active = [b for b in library if b.get("status") in ("Downloading", "Downloaded")]

    torrent_hash = (torrent.get("hash") or "").lower()
    for book in active:
        if torrent_hash and book.get("download_hash") == torrent_hash:
            return book

    tag_list = [t.strip() for t in (torrent.get("tags") or "").split(",")]
    # "talkarr-" is the tag prefix used before the rename to Bayarr
    tag = next((t for t in tag_list if t.startswith(("bayarr-", "talkarr-"))), None)
    if not tag:
        return None
    safe_title_from_tag = tag.split("-", 1)[1]
    # The tag was created via title.replace(",", "").strip()
    return next((b for b in active if b.get("title", "").replace(",", "").strip() == safe_title_from_tag), None)


def _map_content_path(torrent, downloads_folder):
    """Translates qBittorrent's path into this container's path (remote path mapping)."""
    content_path = torrent.get("content_path", "")
    if not downloads_folder:
        return content_path
    save_path = torrent.get("save_path", "")
    try:
        # Keep any nesting below the save path (e.g. a torrent's subfolder)
        rel = os.path.relpath(content_path, save_path) if save_path else None
    except ValueError:
        rel = None
    if not rel or rel.startswith(".."):
        rel = os.path.basename(content_path.rstrip("/\\"))
    return os.path.join(downloads_folder, rel)


def check_download(book, audio_files, settings):
    """Checks the downloaded files rather than trusting the AudiobookBay listing.
    Returns a reason to hold the book for review, or '' if it looks right."""
    if settings.get("format_preference") == "m4b_only":
        not_m4b = sorted({os.path.splitext(f)[1].lower() for f in audio_files} - {".m4b", ".m4a"})
        if not_m4b:
            return f"Listed as M4B, but the download contains {', '.join(not_m4b)} files."

    expected = book.get("runtime_min") or 0
    if settings.get("verify_runtime", True) and expected:
        actual = total_duration_min(audio_files)
        if actual is None:
            logger.warning(f"Could not read the length of {book.get('title')}; skipping the runtime check")
            return ""
        tolerance = settings.get("runtime_tolerance", 10)
        off = abs(actual - expected) / expected * 100
        if off > tolerance:
            return (f"The files run {actual} min, but Audible lists {expected} min ({off:.0f}% off, "
                    f"allowed {tolerance}%). It may be a different book, or abridged.")
    return ""


_root_folder_warned = False


async def import_completed_downloads(settings):
    global _root_folder_warned
    root_folder = settings.get("root_folder")
    if not root_folder or not os.path.isdir(root_folder):
        if not _root_folder_warned:
            logger.error(f"Root folder not configured or does not exist: {root_folder}. Cannot import.")
            _root_folder_warned = True
        return
    _root_folder_warned = False

    library = db.get_library()
    if not any(b.get("status") in ("Downloading", "Downloaded") for b in library):
        return

    logger.debug("Polling qBittorrent for completed audiobooks...")
    completed = await get_completed_torrents(
        settings.get("qbt_host"),
        settings.get("qbt_user"),
        settings.get("qbt_pass")
    )

    for torrent in completed or []:
        book = _find_book_for_torrent(library, torrent)
        if not book:
            continue

        title = book.get("title", "").strip()
        if book.get("status") == "Downloading":
            logger.info(f"Download finished for {title}")
            db.update_library_status(book["id"], "Downloaded")
            book["status"] = "Downloaded"

        if not torrent.get("content_path"):
            logger.warning(f"Torrent {torrent.get('name')} completed but has no content_path")
            continue

        content_path = _map_content_path(torrent, settings.get("downloads_folder"))
        if not os.path.exists(content_path):
            logger.warning(f"Mapped path does not exist: {content_path}")
            continue

        audio, cover = plan_import_files(content_path, title, rename=settings.get("rename_files", True))
        if not audio:
            logger.warning(f"No audio files found in {content_path}; leaving {title} as Downloaded")
            continue

        if not book.get("skip_verify"):
            reason = await asyncio.to_thread(check_download, book, [src for src, _ in audio], settings)
            if reason:
                logger.warning(f"Holding {title} for review: {reason}")
                db.update_book(book["id"], status="Needs Review", review_reason=reason)
                db.add_history("needs_review", book, reason)
                continue

        dest_dir = os.path.join(root_folder, build_folder_name(settings.get("naming_format"), book))
        logger.info(f"Importing {title} into {dest_dir}")
        try:
            # Large copies run in a thread so the web UI stays responsive
            plan = audio + ([cover] if cover else [])
            copied = await asyncio.to_thread(_copy_files, plan, dest_dir)
            cover_name = cover[1] if cover else ""
            if settings.get("write_metadata", True):
                cover_name = await audiobookshelf.download_cover(book.get("imageUrl", ""), dest_dir) or cover_name
                await asyncio.to_thread(audiobookshelf.write_metadata, book, dest_dir)
            logger.info(f"Successfully imported {title} ({copied} files copied)")
            db.update_book(book["id"], status="Imported", path=dest_dir, cover=cover_name,
                           review_reason="", skip_verify=False, **describe_files(dest_dir))
            db.add_history("imported", book, f"{len(audio)} file{'s' if len(audio) != 1 else ''} into {dest_dir}")
        except Exception as e:
            logger.error(f"Failed to import {title}: {e}")
            db.add_history("failed", book, f"Import failed: {e}")
            continue

        error = await audiobookshelf.scan_library(settings)
        if error:
            db.add_history("failed", book, f"Audiobookshelf scan failed: {error}")


def _copy_files(plan, dest_dir):
    """Copies (never moves) files so the client keeps seeding. Each file is written under a
    temporary name first, so an interrupted copy is redone on the next check rather than
    being mistaken for a finished one. Returns the number of files copied."""
    copied = 0
    for src, rel in plan:
        dest = os.path.join(dest_dir, rel)
        if os.path.exists(dest):
            continue
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        partial = dest + ".partial"
        shutil.copy2(src, partial)
        os.replace(partial, dest)
        copied += 1
    return copied
