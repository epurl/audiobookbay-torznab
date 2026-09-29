import asyncio
import logging
import datetime
import os
import re
import shutil
from . import db
from .scraper import search_for_book, fetch_detail_info
from .qbittorrent import send_to_qbittorrent, get_completed_torrents

logger = logging.getLogger(__name__)

SEARCH_INTERVAL = 6 * 60 * 60  # Release checks and searches for Monitored books
IMPORT_INTERVAL = 60           # Polling qBittorrent for finished downloads

AUDIO_EXTENSIONS = {'.mp3', '.m4b', '.m4a', '.flac', '.ogg', '.opus', '.aac'}

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


def schedule_search(book):
    """Searches for a book in the background, e.g. right after it's added to the library."""
    task = asyncio.create_task(_safe_auto_download(book))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def _safe_auto_download(book):
    try:
        settings = db.get_settings()
        if settings.get("qbt_enabled"):
            await auto_download_book(book, settings)
    except Exception as e:
        logger.error(f"Error searching for {book.get('title')}: {e}", exc_info=True)


async def check_library():
    settings = db.get_settings()
    today = datetime.date.today()

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
                    db.update_library_status(title, "Monitored")
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

    # Skip multi-book bundles unless the library book is itself one
    if (result_words & BUNDLE_WORDS) - title_words:
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

    return await grab(title, magnet, settings)


async def grab(title, magnet, settings):
    """Sends a magnet to qBittorrent and moves the library book to Downloading."""
    success = await send_to_qbittorrent(
        settings.get("qbt_host"),
        settings.get("qbt_user"),
        settings.get("qbt_pass"),
        magnet,
        title
    )
    if not success:
        logger.error(f"Failed to send {title} to qBittorrent")
        return False

    logger.info(f"Sent {title} to qBittorrent")
    db.update_book(title, status="Downloading", download_hash=db.extract_infohash(magnet))
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
            db.update_library_status(title, "Downloaded")
            book["status"] = "Downloaded"

        if not torrent.get("content_path"):
            logger.warning(f"Torrent {torrent.get('name')} completed but has no content_path")
            continue

        content_path = _map_content_path(torrent, settings.get("downloads_folder"))
        if not os.path.exists(content_path):
            logger.warning(f"Mapped path does not exist: {content_path}")
            continue

        author = book.get("authors", "").split(",")[0].strip()
        dest_folder_name = re.sub(r'[\\/:*?"<>|]', "", f"{author} - {title}").strip()
        dest_dir = os.path.join(root_folder, dest_folder_name)
        os.makedirs(dest_dir, exist_ok=True)

        logger.info(f"Importing {title} into {dest_dir}")
        try:
            # Large copies run in a thread so the web UI stays responsive
            copied = await asyncio.to_thread(_copy_audio_files, content_path, dest_dir)
            if copied == 0 and not os.listdir(dest_dir):
                os.rmdir(dest_dir)
                logger.warning(f"No audio files found in {content_path}; leaving {title} as Downloaded")
                continue
            logger.info(f"Successfully imported {title} ({copied} files)")
            db.update_library_status(title, "Imported")
        except Exception as e:
            logger.error(f"Failed to import {title}: {e}")


def _copy_audio_files(content_path, dest_dir):
    """Copies (never moves) audio files so the client keeps seeding. Returns files copied."""
    copied = 0
    if os.path.isfile(content_path):
        if os.path.splitext(content_path)[1].lower() in AUDIO_EXTENSIONS:
            shutil.copy2(content_path, dest_dir)
            copied += 1
        return copied
    for root, _, files in os.walk(content_path):
        for file in files:
            if os.path.splitext(file)[1].lower() in AUDIO_EXTENSIONS:
                dest_file = os.path.join(dest_dir, file)
                if not os.path.exists(dest_file):
                    shutil.copy2(os.path.join(root, file), dest_file)
                    copied += 1
    return copied
