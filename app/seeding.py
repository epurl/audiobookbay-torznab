"""Seeding: watches the torrents Bayarr grabbed once their book is imported, and removes
them from qBittorrent after a ratio or a time (Settings > Download Client > Seeding).

Only torrents of imported books are touched: their files have been copied into the
library, so removing the torrent (and its downloaded files) leaves the book as it is."""
import logging
import os
import time

from app import db
from app.library import same_path
from app.qbittorrent import delete_torrents, get_torrents

logger = logging.getLogger(__name__)


def _creds(settings):
    return settings.get("qbt_host"), settings.get("qbt_user"), settings.get("qbt_pass")


def seeded_seconds(torrent):
    """How long the torrent has been seeding: qBittorrent's count, else since it completed."""
    if torrent.get("seeding_time") is not None:
        return max(0, int(torrent["seeding_time"]))
    done = torrent.get("completion_on") or 0
    return max(0, int(time.time() - done)) if done > 0 else 0


def limit_reached(torrent, settings):
    """Why the torrent has seeded enough ("" if it hasn't, or no limit is set)."""
    ratio, days = float(settings.get("seed_ratio") or 0), float(settings.get("seed_days") or 0)
    if ratio and (torrent.get("ratio") or 0) >= ratio:
        return f"ratio {torrent.get('ratio', 0):.2f} reached {ratio:g}"
    seconds = seeded_seconds(torrent)
    if days and seconds >= days * 86400:
        return f"seeded for {seconds / 86400:.1f} days (limit {days:g})"
    return ""


def _remove_in(torrent, settings):
    """Seconds until the time limit removes it (None without one)."""
    days = float(settings.get("seed_days") or 0)
    return max(0, int(days * 86400 - seeded_seconds(torrent))) if days else None


def _watched():
    from app.usenet import is_usenet_id  # Usenet downloads don't seed
    return [b for b in db.get_library() if b.get("status") == "Imported" and b.get("download_hash")
            and not is_usenet_id(b["download_hash"])]


def _safe_to_delete_files(book, torrent, settings):
    """The torrent's files aren't the library's copy (an import from the download folder
    itself, or a library inside the downloads, would be)."""
    if not book.get("path") or not os.path.isdir(book["path"]):
        return False
    from app.monitor import _map_content_path
    content = _map_content_path(torrent, settings.get("downloads_folder")) if torrent.get("content_path") else ""
    if not content:
        return True
    a, b = os.path.normcase(os.path.abspath(content)), os.path.normcase(os.path.abspath(book["path"]))
    return not (same_path(a, b) or b.startswith(a.rstrip(os.sep) + os.sep) or a.startswith(b.rstrip(os.sep) + os.sep))


async def _remove(book, torrent, settings, why, delete_files):
    delete_files = delete_files and _safe_to_delete_files(book, torrent, settings)
    if not await delete_torrents(*_creds(settings), [book["download_hash"]], delete_files=delete_files):
        logger.warning(f"Couldn't remove the torrent of {book.get('title')} from qBittorrent")
        return False
    db.update_book(book["id"], download_hash="")
    db.add_history("seeded", book, f"Removed from qBittorrent: {why}" + (", with its downloaded files" if delete_files else ""))
    logger.info(f"Removed the torrent of {book.get('title')}: {why}")
    return True


async def check(settings):
    """Removes the torrents that have seeded enough (when the setting is on). A torrent
    you removed yourself is no longer watched."""
    books = _watched()
    if not books:
        return
    torrents = await get_torrents(*_creds(settings), [b["download_hash"] for b in books])
    if torrents is None:
        return  # qBittorrent unreachable
    by_hash = {t["hash"].lower(): t for t in torrents}
    for book in books:
        torrent = by_hash.get(book["download_hash"])
        if torrent is None:
            db.update_book(book["id"], download_hash="")  # Removed in qBittorrent
            continue
        if (torrent.get("progress") or 0) < 1 or not settings.get("seed_cleanup"):
            continue
        why = limit_reached(torrent, settings)
        if why:
            await _remove(book, torrent, settings, why, settings.get("seed_delete_files", True))


async def status(settings):
    """The watched torrents for Activity: ratio, seeding time, and when each is removed."""
    books = _watched()
    if not books or not settings.get("qbt_enabled"):
        return {"torrents": [], "client_reachable": True}
    torrents = await get_torrents(*_creds(settings), [b["download_hash"] for b in books])
    if torrents is None:
        return {"torrents": [], "client_reachable": False}
    by_hash = {t["hash"].lower(): t for t in torrents}
    rows = []
    for book in books:
        t = by_hash.get(book["download_hash"])
        if not t:
            continue
        rows.append({
            "book_id": book["id"], "title": book.get("title", ""), "state": t.get("state"),
            "ratio": round(t.get("ratio") or 0, 2), "uploaded": t.get("uploaded") or 0, "size": t.get("size") or 0,
            "upspeed": t.get("upspeed") or 0, "seeded_seconds": seeded_seconds(t),
            "limit_reached": limit_reached(t, settings) if settings.get("seed_cleanup") else "",
            "remove_in": _remove_in(t, settings) if settings.get("seed_cleanup") else None,
        })
    rows.sort(key=lambda r: -r["seeded_seconds"])
    return {"torrents": rows, "client_reachable": True}


async def remove_now(book_id, settings, delete_files):
    """Removes one watched torrent now. Returns an error message, or ""."""
    book = db.get_book(book_id)
    if not book or not book.get("download_hash"):
        return "That book has no torrent being watched."
    torrents = await get_torrents(*_creds(settings), [book["download_hash"]])
    if torrents is None:
        return "Can't reach qBittorrent."
    if not torrents:
        db.update_book(book_id, download_hash="")
        return ""
    ok = await _remove(book, torrents[0], settings, "removed by you", delete_files)
    return "" if ok else "qBittorrent didn't remove it."
