"""AudiobookBay's newest uploads, watched like the *arr apps' RSS sync.

Every couple of hours the first pages of the site's newest posts are read (only as far as
the posts already seen last time) and every Monitored book is matched against them here,
without searching the site for each book. A clear match (the whole title and the author,
nothing ruling it out) has its page loaded and is grabbed. Full searches for a book then
only happen when it's added or released, when you click Search Now, and for books never
found, less and less often (see monitor.ABB_BACKOFF_DAYS)."""
import datetime
import json
import logging
import os
import re
import tempfile

from app import db, release_match, scraper

logger = logging.getLogger(__name__)

FEED_INTERVAL = 2 * 3600  # seconds between checks
FEED_PAGES = 5            # at most, per check (9 posts a page)
SEEN_KEPT = 500           # posts remembered, so each is matched once

_state = {"checked": "", "new": 0, "grabbed": 0, "error": ""}


def _file():
    return os.path.join(db.CONFIG_DIR, "abb_feed.json")


def _load_seen():
    try:
        with open(_file(), "r", encoding="utf-8") as f:
            return list(json.load(f).get("seen") or [])
    except (OSError, ValueError, AttributeError):
        return []


def _save_seen(seen):
    os.makedirs(db.CONFIG_DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=db.CONFIG_DIR, prefix=".abb-feed-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"seen": seen[:SEEN_KEPT]}, f)
    os.replace(tmp, _file())


def status():
    return dict(_state)


def _might_be(book, listing_words):
    """Every word of the book's main title is in the post's title (a clear match needs
    that anyway): skips scoring posts that can't be it."""
    title = (book.get("title") or "").split(":")[0]
    title = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", " ", title)
    words = [w for w in release_match.tokens(title) if w not in release_match.STOPWORDS]
    return bool(words) and all(w in listing_words for w in words)


async def new_uploads():
    """The posts that are new since the last check, newest first."""
    seen = _load_seen()
    known = set(seen)
    new = []
    for page in range(1, FEED_PAGES + 1):
        results, last = await scraper.search_page("", page)
        fresh = [r for r in results if r.get("link") and r["link"] not in known]
        new += fresh
        # Caught up with what was seen last time (or the first check: one page is enough)
        if len(fresh) < len(results) or page >= last or not seen:
            break
        if not scraper.background_allowed():
            break
    _save_seen([r["link"] for r in new] + seen)
    return new


async def check(settings):
    """Reads the new uploads and grabs the clear matches for Monitored books. Returns how
    many were grabbed."""
    from app import monitor  # The monitor runs this check
    _state.update(checked=datetime.datetime.now().isoformat(timespec="seconds"), new=0, grabbed=0, error="")
    try:
        new = await new_uploads()
    except Exception as e:
        _state["error"] = str(e)
        logger.warning(f"AudiobookBay new uploads: {e}")
        return 0
    _state["new"] = len(new)
    if not new:
        return 0
    words = {r["link"]: set(release_match.tokens(r.get("raw_title") or r.get("title") or "")) for r in new}
    grabbed = 0
    for book in db.get_library():
        if book.get("status") != "Monitored" or book["id"] in monitor._searching:
            continue
        posts = [r for r in new if _might_be(book, words[r["link"]])]
        if not posts:
            continue
        scored = [{**r, "source": "AudiobookBay new uploads", "protocol": "torrent",
                   **release_match.evaluate(book, r, settings)} for r in posts]
        hopeful = [r for r in scored if r["strong"]]
        if not hopeful:
            continue
        best = release_match.order(hopeful, settings)[0]
        # The page has the magnet, the narrator and the files: checked again with them
        await scraper.add_details([best])
        best.update(release_match.evaluate(book, best, settings))
        if not release_match.is_acceptable(best) or not best.get("magnet_url"):
            continue
        current = db.get_book(book["id"])
        if not current or current.get("status") != "Monitored" or book["id"] in monitor._searching:
            continue
        logger.info(f"New upload matches {book.get('title')}: {best.get('raw_title') or best.get('title')}")
        monitor._searching.add(book["id"])
        try:
            if await monitor.grab(current, best["magnet_url"], settings, release=best):
                grabbed += 1
        finally:
            monitor._searching.discard(book["id"])
    _state["grabbed"] = grabbed
    if grabbed:
        logger.info(f"AudiobookBay new uploads: grabbed {grabbed} book(s)")
    return grabbed
