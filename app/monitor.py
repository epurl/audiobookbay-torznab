import asyncio
import logging
import datetime
import os
import shutil
from . import archives, audible, audiobookshelf, book_search, convert, db, editions, indexers, release_match, scraper, usenet
from .library import (audio_files, build_folder_name, describe_files, find_match, plan_import_files,
                      read_abs_metadata, series_entries, series_key, titles_match, total_duration_min)
from .qbittorrent import delete_torrents, get_completed_torrents, get_torrents, send_to_qbittorrent, send_torrent_file

logger = logging.getLogger(__name__)

SEARCH_INTERVAL = 6 * 60 * 60  # Series syncs, release checks and searches for Monitored books
IMPORT_INTERVAL = 60           # Polling qBittorrent for finished downloads

# Keep references to fire-and-forget tasks so they aren't garbage collected mid-run
_background_tasks = set()


async def run_monitor_loop():
    logger.info("Starting background monitor (search every 6h, import check every 60s)...")
    await asyncio.gather(_search_loop(), _import_loop())


async def _search_loop():
    while True:
        try:
            await asyncio.to_thread(db.daily_backup)
        except Exception as e:
            logger.error(f"Daily backup failed: {e}")
        try:
            # Books from before editions existed (a no-op once they all have one)
            await classify_editions()
        except Exception as e:
            logger.error(f"Edition detection failed: {e}", exc_info=True)
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
                await check_active_downloads(settings)
                await import_completed_downloads(settings)
                from app import seeding  # Torrents of imported books: removed once they've seeded enough
                await seeding.check(settings)
            if usenet.client(settings):
                await check_usenet_downloads(settings)
        except Exception as e:
            logger.error(f"Error in import loop: {e}", exc_info=True)
        await asyncio.sleep(IMPORT_INTERVAL)


def _local_edition(book):
    """What a book's folder says about its edition (metadata.json, tags, names)."""
    path = book.get("path") or ""
    meta = read_abs_metadata(path) if os.path.isdir(path) else None
    return editions.classify_local(meta, [f for f, _ in audio_files(path)], path)


async def classify_editions(book_ids=None):
    """Works out the edition of books that don't have one yet, or of book_ids (again):
    Audible's edition for books with an ASIN, and what their files say. A folder tagged
    as a dramatization wins over an unabridged ASIN, and is flagged for checking."""
    # Books saved as "dramatized" before it was merged into abridged: just renamed
    renamed = {b["id"]: {"edition": editions.ABRIDGED} for b in db.get_library() if b.get("edition") == "dramatized"}
    if renamed:
        db.update_books(renamed)
        logger.info(f"Editions: {len(renamed)} dramatized book(s) are now abridged")
    books = [b for b in db.get_library()
             if (b["id"] in book_ids if book_ids is not None else b.get("edition") not in editions.EDITIONS)]
    if not books:
        return 0
    products = {}
    asins = [b["asin"] for b in books if b.get("asin")]
    if asins:
        try:
            products = {p["asin"]: p for p in await audible.get_products(asins) if p.get("asin")}
        except Exception as e:
            logger.warning(f"Couldn't look up editions on Audible: {e}")
    changes = {}
    for book in books:
        on_audible = editions.classify_product(products[book["asin"]]) if book.get("asin") in products else None
        if book.get("path") and os.path.exists(book["path"]):
            local = await asyncio.to_thread(_local_edition, book)
        else:
            local = editions.classify_fields(book)
        found = editions.combine(on_audible, local)
        changes[book["id"]] = {"edition": found["edition"], "edition_reason": found["reason"],
                               "edition_check": not found["sure"]}
    db.update_books(changes)
    flagged = sum(1 for c in changes.values() if c["edition_check"])
    counts = {e: sum(1 for c in changes.values() if c["edition"] == e) for e in editions.EDITIONS}
    logger.info(f"Editions: {counts['narrated']} unabridged, {counts['abridged']} abridged; {flagged} to check")
    return len(changes)


def _run_in_background(coro):
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


def downloads_enabled(settings):
    """A download client is set up: qBittorrent, or a Usenet client."""
    return bool(settings.get("qbt_enabled") or usenet.client(settings))


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
                if not downloads_enabled(settings):
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


def _known_part(book, every_edition, known):
    """True for a part of a book sold in parts whose other parts were already known: series
    lists used to record only a book's first part."""
    if not book.get("part"):
        return False
    base = audible._split_part(book.get("title"))[0]
    return any(o.get("asin") in known for o in every_edition
               if o is not book and o.get("part") and o.get("catalog_sequence") == book.get("catalog_sequence")
               and o.get("edition") == book.get("edition") and audible._split_part(o.get("title"))[0] == base)


async def sync_series(series, settings, selected=None):
    """Refreshes a monitored series from Audible. Books already in the library get the
    series recorded on them. Books are only added when chosen (selected, when the series is
    first monitored) or new: ones Audible didn't list before, in the editions the edition
    setting asks for. Returns the added books."""
    from app.series_index import LibraryIndex, ensure_catalog, plain_title, series_author, wanted_editions
    catalog = await ensure_catalog(series["asin"], force=True)
    books = catalog.get("books", [])
    every_edition = books + catalog.get("alternates", [])
    editions_wanted = wanted_editions(settings.get("edition_preference"))
    title = plain_title(series.get("title") or catalog.get("title", ""))
    index = LibraryIndex(db.get_library())
    known = series.get("known_asins")
    added = []
    for book in every_edition:
        if index.find(book):
            continue
        if selected is not None:
            wanted = book.get("asin") in selected
        else:
            # Series monitored before known_asins existed: treat today's list as known
            wanted = (known is not None and book.get("asin") not in known and not _known_part(book, every_edition, known)
                      and editions.edition_of(book) in editions_wanted)
        if not wanted:
            continue
        from app import graphicaudio
        entry = db.add_to_library({**(await graphicaudio.with_details(book)), "description": ""})
        added.append(entry)
    all_asins = {b.get("asin") for b in every_edition if b.get("asin")}
    author = series_author([{"catalog": b, "book": None} for b in books]) or series.get("author", "")
    db.update_series(series["id"], title=title, author=author,
                     known_asins=sorted(set(known or []) | all_asins),
                     last_sync=datetime.datetime.now().isoformat(timespec="seconds"))
    if added:
        db.add_history("series", {"title": title},
                       f"Added {len(added)} book{'s' if len(added) != 1 else ''} from the series")
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

    from app import authors  # Followed authors' new releases
    for author in authors.followed():
        if author.get("monitored"):
            try:
                await authors.sync(author, settings)
            except Exception as e:
                logger.error(f"Could not check new books by {author.get('name')}: {e}")

    from app import reading_list  # Watched Goodreads lists (Settings > Lists)
    await reading_list.check_all()

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
            if not downloads_enabled(settings):
                continue
            if scraper.is_paused():
                continue  # AudiobookBay isn't responding; try again next round
            logger.info(f"Searching for Monitored book: {title}")
            await auto_download_book(book, settings)


def score_result(book, result, settings):
    """Scores an ABB result for a library book. Returns None if it should not be grabbed."""
    evaluation = release_match.evaluate(book, result, settings)
    return evaluation["score"] if release_match.is_acceptable(evaluation) else None


def choose_best_result(book, results, settings):
    return release_match.choose_best(book, results, settings)


async def auto_download_book(book, settings):
    title = book.get("title")
    try:
        results, _ = await book_search.find_releases(book, settings, mode="auto")
    except RuntimeError as e:
        logger.warning(f"Couldn't search for {title}: {e}")
        return False
    if not results:
        logger.info(f"No results found for {title}")
        return False

    best_match = next((r for r in results if release_match.is_acceptable(r)), None)
    if not best_match:
        closest = "; ".join(book_search.describe(r) for r in results[:3])
        logger.info(f"No suitable match found for {title}. Closest: {closest}")
        return False

    logger.info(f"Found match for {title}: {book_search.describe(best_match)}")
    if best_match.get("protocol") == "usenet":
        return await grab_usenet(book, best_match, settings)
    try:
        magnet, torrent = await indexers.get_download(best_match)
    except Exception as e:
        logger.error(f"Couldn't get the download for {title}: {e}")
        return False
    if not magnet and not torrent:
        logger.error(f"Failed to extract magnet for {title}")
        return False

    return await grab(book, magnet, settings, release=best_match, torrent=torrent)


async def grab(book, magnet, settings, release=None, torrent=None):
    """Sends a magnet (or a .torrent file) to qBittorrent and moves the library book to
    Downloading."""
    title = book.get("title")
    if torrent:
        success = await send_torrent_file(settings.get("qbt_host"), settings.get("qbt_user"), settings.get("qbt_pass"),
                                          torrent, title)
    else:
        success = await send_to_qbittorrent(settings.get("qbt_host"), settings.get("qbt_user"), settings.get("qbt_pass"),
                                            magnet, title)
    if not success:
        logger.error(f"Failed to send {title} to qBittorrent")
        db.add_history("failed", book, "Could not send the release to qBittorrent")
        return False

    logger.info(f"Sent {title} to qBittorrent")
    release_name = (release or {}).get("raw_title") or (release or {}).get("title", "")
    download_hash = indexers.torrent_infohash(torrent) if torrent else db.extract_infohash(magnet)
    db.update_book(book["id"], status="Downloading", download_hash=download_hash, release_key=download_hash,
                   release_title=release_name, review_reason="", skip_verify=False)
    details = ", ".join(x for x in (release_name, (release or {}).get("format"), (release or {}).get("size_str"),
                                    (release or {}).get("source")) if x and x != "Unknown")
    db.add_history("grabbed", book, f"Sent to qBittorrent{': ' + details if details else ''}")
    return True


async def grab_usenet(book, release, settings):
    """Fetches a Usenet release's NZB from its indexer and hands it to NZBGet or SABnzbd."""
    title = book.get("title")
    name = release.get("raw_title") or release.get("title") or title
    try:
        nzb = await indexers.fetch_nzb(release.get("download_url") or "")
        download_id = await usenet.add(settings, nzb, name)
    except Exception as e:
        logger.error(f"Couldn't send {title} to the Usenet client: {e}")
        db.add_history("failed", book, f"Could not send the release to the Usenet client: {e}")
        return False
    client_name = "NZBGet" if download_id.startswith("nzbget:") else "SABnzbd"
    logger.info(f"Sent {title} to {client_name}")
    db.update_book(book["id"], status="Downloading", download_hash=download_id,
                   release_key=release.get("release_key") or indexers.release_key(release),
                   release_title=name, review_reason="", skip_verify=False)
    details = ", ".join(x for x in (name, release.get("format"), release.get("size_str"), release.get("source"))
                        if x and x != "Unknown")
    db.add_history("grabbed", book, f"Sent to {client_name}{': ' + details if details else ''}")
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


# qBittorrent states where a download is making no progress
STALLED_STATES = {"stalledDL", "metaDL", "error", "missingFiles"}


async def check_active_downloads(settings):
    """Watches downloads in progress: one with no progress for too long is rejected and
    the next-best release is grabbed; one removed from qBittorrent goes back to Monitored."""
    downloading = [b for b in db.get_library() if b.get("status") == "Downloading" and b.get("download_hash")
                   and not usenet.is_usenet_id(b["download_hash"])]
    if not downloading:
        return
    creds = (settings.get("qbt_host"), settings.get("qbt_user"), settings.get("qbt_pass"))
    torrents = await get_torrents(*creds, [b["download_hash"] for b in downloading])
    if torrents is None:
        return  # qBittorrent unreachable: don't draw conclusions
    by_hash = {t["hash"].lower(): t for t in torrents}
    now = datetime.datetime.now()
    stall_hours = settings.get("stall_hours", 6)

    for book in downloading:
        torrent = by_hash.get(book["download_hash"])
        if torrent is None:
            logger.info(f"{book['title']} was removed from qBittorrent; back to Monitored")
            db.update_book(book["id"], status="Monitored", download_hash="", stalled_since="")
            db.add_history("failed", book, "The torrent was removed from qBittorrent; the book is Monitored again")
            continue

        if torrent.get("state") not in STALLED_STATES or not stall_hours:
            if book.get("stalled_since"):
                db.update_book(book["id"], stalled_since="")
            continue
        if not book.get("stalled_since"):
            db.update_book(book["id"], stalled_since=now.isoformat(timespec="seconds"))
            continue
        stalled_for = now - datetime.datetime.fromisoformat(book["stalled_since"])
        if stalled_for < datetime.timedelta(hours=stall_hours):
            continue

        progress = round((torrent.get("progress") or 0) * 100)
        logger.warning(f"Giving up on {book['title']}: stalled for {stall_hours}h at {progress}%")
        if settings.get("remove_stalled", True):
            await delete_torrents(*creds, [book["download_hash"]], delete_files=True)
        blocklist = list(dict.fromkeys((book.get("blocklist") or []) + [book.get("release_key") or book["download_hash"]]))
        db.update_book(book["id"], status="Monitored", download_hash="", stalled_since="", blocklist=blocklist)
        db.add_history("stalled", book, f"No progress for {stall_hours} hours (stuck at {progress}%, "
                                        f"state {torrent.get('state')}); rejected, searching for another release")
        schedule_search(db.get_book(book["id"]))


def check_download(book, audio_files, settings, folder=""):
    """Checks the downloaded files rather than trusting the AudiobookBay listing.
    Returns a reason to hold the book for review, or '' if it looks right."""
    # The edition: the length can't tell a dramatization from the narration (they often
    # run about as long), but the files' tags and names usually can
    wanted = editions.edition_of(book)
    found = editions.classify_files(audio_files, folder)
    if found and found["edition"] != wanted and wanted != editions.DRAMATIZED:
        return (f"You wanted the {editions.label(wanted).lower()} edition, but this looks "
                f"{editions.label(found['edition']).lower()} ({found['reason']}).")
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
        if book.get("runtime_approx"):
            tolerance = max(tolerance, 25)  # GraphicAudio only gives whole hours
        off = abs(actual - expected) / expected * 100
        if off > tolerance:
            source = "GraphicAudio lists about" if book.get("runtime_approx") else "Audible lists"
            return (f"The files run {actual} min, but {source} {expected} min ({off:.0f}% off, "
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

        # Downloads that are archives are unpacked into a folder of their own, imported from
        # there, and the folder removed afterwards; the archive itself keeps seeding
        staging = os.path.join(root_folder, UNPACK_FOLDER, book["id"])
        try:
            if not await _import_download(book, content_path, staging, settings, root_folder):
                continue
        finally:
            await asyncio.to_thread(shutil.rmtree, staging, True)
            try:
                os.rmdir(os.path.dirname(staging))
            except OSError:
                pass

        error = await audiobookshelf.scan_library(settings)
        if error:
            db.add_history("failed", book, f"Audiobookshelf scan failed: {error}")


UNPACK_FOLDER = ".bayarr-unpack"  # In the Root Folder; library scans skip folders starting with "."


def _map_usenet_path(path, settings):
    """The Usenet client's path for a finished job, as Bayarr sees it: the job's folder inside
    Settings > Usenet > Completed Downloads Folder when that's set."""
    folder = settings.get("usenet_downloads_folder")
    if not folder or not path:
        return path
    return os.path.join(folder, os.path.basename(path.rstrip("/\\")))


async def check_usenet_downloads(settings):
    """Follows Usenet jobs: a finished one is imported and its download deleted (nothing to
    seed); a failed one is rejected and the next-best release searched for; one removed
    from the client puts the book back to Monitored."""
    books = [b for b in db.get_library() if b.get("status") in ("Downloading", "Downloaded")
             and usenet.is_usenet_id(b.get("download_hash"))]
    if not books:
        return
    states = await usenet.status(settings, [b["download_hash"] for b in books])
    if states is None:
        return  # The client can't be reached: don't draw conclusions
    root_folder = settings.get("root_folder")
    for book in books:
        state = states.get(book["download_hash"]) or {}
        title = book.get("title", "")
        if state.get("state") == "missing":
            logger.info(f"{title} was removed from the Usenet client; back to Monitored")
            db.update_book(book["id"], status="Monitored", download_hash="")
            db.add_history("failed", book, "The download was removed from the Usenet client; the book is Monitored again")
        elif state.get("state") == "failed":
            logger.warning(f"Usenet download of {title} failed: {state.get('message')}")
            blocklist = list(dict.fromkeys((book.get("blocklist") or []) + [book.get("release_key") or book["download_hash"]]))
            db.update_book(book["id"], status="Monitored", download_hash="", blocklist=blocklist)
            db.add_history("failed", book, f"{state.get('message') or 'The download failed'}; rejected, searching for another release")
            await usenet.forget(settings, book["download_hash"])
            schedule_search(db.get_book(book["id"]))
        elif state.get("state") == "completed":
            if book.get("status") == "Downloading":
                logger.info(f"Download finished for {title}")
                db.update_library_status(book["id"], "Downloaded")
                book["status"] = "Downloaded"
            if not root_folder or not os.path.isdir(root_folder):
                continue
            content = _map_usenet_path(state.get("path"), settings)
            if not content or not os.path.exists(content):
                logger.warning(f"Finished Usenet download not found: {content}")
                continue
            staging = os.path.join(root_folder, UNPACK_FOLDER, book["id"])
            try:
                imported = await _import_download(book, content, staging, settings, root_folder)
            finally:
                await asyncio.to_thread(shutil.rmtree, staging, True)
            if not imported:
                continue
            # Imported (copied): the download isn't needed any more
            db.update_book(book["id"], download_hash="")
            if settings.get("usenet_remove_completed", True) and not _inside_library(content, root_folder):
                await asyncio.to_thread(_remove_download, content)
            await usenet.forget(settings, book["download_hash"])
            error = await audiobookshelf.scan_library(settings)
            if error:
                db.add_history("failed", book, f"Audiobookshelf scan failed: {error}")


def _inside_library(path, root_folder):
    a, b = os.path.normcase(os.path.abspath(path)), os.path.normcase(os.path.abspath(root_folder))
    return a == b or a.startswith(b.rstrip(os.sep) + os.sep) or b.startswith(a.rstrip(os.sep) + os.sep)


def _remove_download(path):
    try:
        shutil.rmtree(path) if os.path.isdir(path) else os.remove(path)
    except OSError as e:
        logger.warning(f"Couldn't delete the finished download {path}: {e}")


def _hold_for_review(book, reason):
    logger.warning(f"Holding {book.get('title', '')} for review: {reason}")
    db.update_book(book["id"], status="Needs Review", review_reason=reason)
    db.add_history("needs_review", book, reason)


async def _import_download(book, content_path, staging, settings, root_folder):
    """Imports one finished download into the library. Returns True once it's imported."""
    title = book.get("title", "").strip()
    plan_args = (title, settings.get("rename_files", True), book.get("runtime_min") or 0,
                 settings.get("runtime_tolerance", 10))
    # In a thread: telling copies of the book from its parts may read the files' lengths
    source = content_path
    audio, cover = await asyncio.to_thread(plan_import_files, source, *plan_args)
    unpacked = False
    if not audio:
        packed = archives.find_archives(content_path)
        if not packed:
            logger.warning(f"No audio files found in {content_path}; leaving {title} as Downloaded")
            return False
        logger.info(f"Unpacking {len(packed)} archive{'s' if len(packed) != 1 else ''} for {title}")
        await asyncio.to_thread(shutil.rmtree, staging, True)
        try:
            await asyncio.to_thread(archives.extract_all, packed, staging)
        except ValueError as e:
            _hold_for_review(book, str(e))
            return False
        source, unpacked = staging, True
        audio, cover = await asyncio.to_thread(plan_import_files, source, *plan_args)
        if not audio:
            _hold_for_review(book, f"There are no audio files in {os.path.basename(packed[0])}.")
            return False

    others = []
    if not book.get("skip_verify"):
        reason = await asyncio.to_thread(check_download, book, [src for src, _ in audio], settings, source)
        if reason:
            # A pack of several books ("Series 01 - Title", "Series 02 - ..."): just this book's files
            picked = await _pick_from_pack(book, source, settings)
            if not picked:
                _hold_for_review(book, reason)
                return False
            audio, cover, others = picked

    note = " (unpacked from an archive)" if unpacked else ""
    if others:
        note += f" (one book of a pack of {len(others) + 1})"
    if not await _place_book(book, audio, cover, settings, root_folder, unpacked, note):
        return False
    # The pack's other books: imported too when they're in the library but not on disk yet
    for group in others:
        await _import_pack_sibling(book, group, source, settings, root_folder, unpacked)
    return True


async def _place_book(book, audio, cover, settings, root_folder, unpacked=False, note=""):
    """Copies a book's planned files into its folder and marks it Imported."""
    title = book.get("title", "").strip()
    dest_dir = os.path.join(root_folder, build_folder_name(settings.get("naming_format"), book))
    logger.info(f"Importing {title} into {dest_dir}")
    try:
        # Large copies run in a thread so the web UI stays responsive. Unpacked files are
        # Bayarr's own temporary copies on the library's drive, so they're moved instead
        plan = audio + ([cover] if cover else [])
        copied = await asyncio.to_thread(_copy_files, plan, dest_dir, unpacked)
        cover_name = cover[1] if cover else ""
        if settings.get("write_metadata", True):
            cover_name = await audiobookshelf.download_cover(book.get("imageUrl", ""), dest_dir) or cover_name
            await asyncio.to_thread(audiobookshelf.write_metadata, book, dest_dir)
        logger.info(f"Successfully imported {title} ({copied} files copied)")
        db.update_book(book["id"], status="Imported", path=dest_dir, cover=cover_name,
                       review_reason="", skip_verify=False, **describe_files(dest_dir))
        detail = f"{len(audio)} file{'s' if len(audio) != 1 else ''} into {dest_dir}"
        db.add_history("imported", book, detail + note)
        # Settings > Media Management: convert downloads that aren't a single M4B
        if settings.get("auto_convert_m4b") and convert.available():
            added, _ = convert.enqueue([book["id"]], source="auto")
            if added:
                logger.info(f"Queued {title} for conversion to M4B")
    except Exception as e:
        logger.error(f"Failed to import {title}: {e}")
        db.add_history("failed", book, f"Import failed: {e}")
        return False
    return True


def _pack_number(book, series_name=""):
    """The book's number in the series (the named one, else its main series), e.g. "1"."""
    entries = series_entries(book)
    if series_name:
        entries = [e for e in entries if series_key(e.get("name")) == series_key(series_name)] or entries
    return release_match._number(entries[0].get("sequence")) if entries else ""


def _group_for(book, groups, series_name=""):
    """The group of a pack that is this book: the same title, else the same number."""
    by_title = [g for g in groups if g["title"] and titles_match(g["title"], book.get("title"))]
    if len(by_title) == 1:
        return by_title[0]
    number = _pack_number(book, series_name)
    by_number = [g for g in groups if number and release_match._number(g["number"]) == number]
    return by_number[0] if len(by_number) == 1 else None


def _plan_args(book, settings):
    return (book.get("title", "").strip(), settings.get("rename_files", True), book.get("runtime_min") or 0,
            settings.get("runtime_tolerance", 10))


async def _pick_from_pack(book, source, settings):
    """When a download holds several books, this book's files: (audio, cover, the other
    groups), or None if it isn't a pack, the book isn't in it, or its files don't check out
    either (the length has to match Audible's, so a wrong pick is held for review)."""
    if os.path.isfile(source) or not book.get("runtime_min"):
        return None
    from .splitter import detect_books  # The splitter imports this module
    groups = await asyncio.to_thread(detect_books, source)
    if len(groups) < 2:
        return None
    group = _group_for(book, groups)
    if not group:
        return None
    audio, cover = await asyncio.to_thread(plan_import_files, source, *_plan_args(book, settings), group["files"])
    if not audio or await asyncio.to_thread(check_download, book, [src for src, _ in audio], settings, source):
        return None
    logger.info(f"{book.get('title')}: the download holds {len(groups)} books; importing book {group['number']}")
    return audio, cover, [g for g in groups if g is not group]


async def _import_pack_sibling(book, group, source, settings, root_folder, unpacked):
    """Imports another book of a pack if it's in the library (same series) and not on disk
    yet. Nothing is held for review: this book wasn't what was grabbed."""
    series = {series_key(e.get("name")): e.get("name") for e in series_entries(book) if e.get("name")}
    for other in db.get_library():
        if other["id"] == book["id"] or other.get("path") or other.get("status") not in ("Monitored", "Missing"):
            continue
        shared = [name for key, name in series.items()
                  if any(series_key(e.get("name")) == key for e in series_entries(other))]
        if not shared or _group_for(other, [group], shared[0]) is not group:
            continue
        if editions.edition_of(other) != editions.edition_of(book):
            continue
        audio, cover = await asyncio.to_thread(plan_import_files, source, *_plan_args(other, settings), group["files"])
        if not audio:
            return
        reason = await asyncio.to_thread(check_download, other, [src for src, _ in audio], settings, source)
        if reason:
            logger.info(f"Not importing {other.get('title')} from the pack of {book.get('title')}: {reason}")
            return
        await _place_book(other, audio, cover, settings, root_folder, unpacked,
                          f" (from the pack downloaded for {book.get('title')})")
        return


def _copy_files(plan, dest_dir, move=False):
    """Copies files so the client keeps seeding the originals. Copies are written under a
    temporary name first, so an interrupted copy is redone on the next check rather than
    mistaken for a finished one. With move (files Bayarr unpacked itself), they're moved
    instead when they're on the same drive. Returns the number of files added."""
    copied = 0
    for src, rel in plan:
        dest = os.path.join(dest_dir, rel)
        if os.path.exists(dest):
            continue
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        if move:
            try:
                os.replace(src, dest)
                copied += 1
                continue
            except OSError:
                move = False  # Another drive: copy instead
        partial = dest + ".partial"
        shutil.copy2(src, partial)
        os.replace(partial, dest)
        copied += 1
    return copied


# --- Matching library books to Audible --------------------------------------

match_job = {"running": False, "total": 0, "done": 0, "matched": 0, "unsure": 0, "failed": 0}


def start_match_job(book_ids):
    """Matches books to Audible one at a time in the background. Returns False if a job is
    already running."""
    if match_job["running"]:
        return False
    match_job.update(running=True, total=len(book_ids), done=0, matched=0, unsure=0, failed=0)

    async def run():
        try:
            for book_id in book_ids:
                book = db.get_book(book_id)
                try:
                    found = await audible.auto_match(book) if book else None
                    if found:
                        db.apply_audible_match(book_id, found)
                        match_job["matched"] += 1
                    else:
                        match_job["unsure"] += 1
                except Exception as e:
                    logger.warning(f"Audible match failed for {book.get('title') if book else book_id}: {e}")
                    match_job["failed"] += 1
                match_job["done"] += 1
                await asyncio.sleep(0.3)  # Gentle on Audible's API
            db.add_history("matched", None, f"Matched {match_job['matched']} of {match_job['total']} books on Audible"
                                            f" ({match_job['unsure']} not clear-cut, {match_job['failed']} errors)")
        finally:
            match_job["running"] = False
    _run_in_background(run())
    return True
