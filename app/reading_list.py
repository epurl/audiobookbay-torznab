"""Importing a want-to-read list: a Goodreads or StoryGraph export (or any CSV with title
and author columns), matched to Audible, then added to the library."""
import asyncio
import csv
import datetime
import io
import logging
import re

from app import audible, db
from app.series_index import LibraryIndex

logger = logging.getLogger(__name__)

_rows = []  # The last parsed list
job = {"running": False, "total": 0, "done": 0, "results": []}
_tasks = set()


def _col(row, *names):
    for name in names:
        for key, value in row.items():
            if key and key.strip().lower() == name.lower() and value:
                return value.strip()
    return ""


def _clean_title(title):
    """ "The Way of Kings (The Stormlight Archive, #1)" -> ("The Way of Kings", "The Stormlight Archive", "1")"""
    m = re.search(r"\s*\(([^()]*?),?\s*#\s*([\d.]+)\)\s*$", title or "")
    if m:
        return title[:m.start()].strip(), m.group(1).strip(), m.group(2)
    return (title or "").strip(), "", ""


def parse(text):
    """Reads the CSV: {"format", "shelves": {name: count}, "count"}. Rows are kept for matching."""
    global _rows
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    headers = {h.strip().lower() for h in reader.fieldnames or [] if h}
    if "exclusive shelf" in headers:
        kind = "Goodreads"
    elif "read status" in headers:
        kind = "StoryGraph"
    elif "title" in headers and ("author" in headers or "authors" in headers):
        kind = "CSV"
    else:
        raise ValueError("This doesn't look like a Goodreads or StoryGraph export (no Title and Author columns).")
    rows = []
    for row in reader:
        raw_title = _col(row, "Title")
        author = _col(row, "Author", "Authors")
        if not raw_title or not author:
            continue
        title, series, sequence = _clean_title(raw_title)
        shelf = _col(row, "Exclusive Shelf", "Read Status", "Shelf", "Status") or "all"
        rows.append({"title": title, "author": author.split(",")[0].strip(), "authors": author,
                     "series": series, "sequence": sequence, "shelf": shelf.lower()})
    _rows = rows
    shelves = {}
    for r in rows:
        shelves[r["shelf"]] = shelves.get(r["shelf"], 0) + 1
    return {"format": kind, "count": len(rows), "shelves": dict(sorted(shelves.items(), key=lambda kv: -kv[1]))}


def start_matching(shelf):
    """Looks up the shelf's books on Audible in the background."""
    if job["running"]:
        return False
    rows = [r for r in _rows if not shelf or r["shelf"] == shelf.lower()]
    job.update(running=True, total=len(rows), done=0, results=[])

    async def run():
        index = LibraryIndex(db.get_library())
        try:
            for i, row in enumerate(rows):
                result = {"index": i, "list_title": row["title"], "list_author": row["authors"], "match": None, "in_library": ""}
                try:
                    match = await audible.auto_match({"title": row["title"], "authors": row["author"],
                                                      "series": row["series"], "sequence": row["sequence"]})
                except Exception as e:
                    logger.warning(f"List import: couldn't look up {row['title']}: {e}")
                    match = None
                if match:
                    result["match"] = match
                    owned = index.find(match)
                    result["in_library"] = owned["status"] if owned else ""
                job["results"].append(result)
                job["done"] += 1
                await asyncio.sleep(0.2)  # Gentle on Audible's API
        finally:
            job["running"] = False

    task = asyncio.create_task(run())
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return True


def add(asins, status="Monitored"):
    """Adds the chosen matches. Returns the added library entries."""
    chosen = set(asins)
    added = []
    for result in job["results"]:
        match = result.get("match")
        if not match or match.get("asin") not in chosen or result.get("in_library"):
            continue
        entry = db.add_to_library({**match, "description": match.get("description", "")})
        if status == "Unmonitored" and entry.get("status") in ("Monitored", "Unreleased"):
            db.update_library_status(entry["id"], "Unmonitored")
            entry["status"] = "Unmonitored"
        result["in_library"] = entry["status"]
        added.append(entry)
    if added:
        db.add_history("list", None, f"Added {len(added)} book{'s' if len(added) != 1 else ''} from a reading list")
    return added


# --- Watched Goodreads lists ----------------------------------------------------
# Settings > Lists: shelves checked with the library (every 6 hours) and on demand. New
# books are looked up on Audible (clear matches only) and added; ones that aren't found
# are listed so you can search for them yourself.

UNMATCHED_KEPT = 100
RETRY_DAYS = 7          # Books not found on Audible are looked up again this often
RETRY_PER_CHECK = 25    # ... this many at a time, to go easy on Audible
_checking = set()  # list ids being checked


def watched():
    return db.get_settings().get("watched_lists") or []


def _update(list_id, **fields):
    lists = watched()
    for entry in lists:
        if entry["id"] == list_id:
            entry.update(fields)
    db.set_setting("watched_lists", lists)


def public(entry):
    """A watched list for the browser (without the ids of every book on it)."""
    return {k: v for k, v in entry.items() if k != "known"} | {"checking": entry["id"] in _checking}


async def add_watch(url, shelf="", name="", monitor="Monitored", add_existing=True):
    """Starts watching a shelf. Reads it once to check the link; without add_existing, the
    books already on it are skipped and only ones added later come in."""
    import uuid
    from app import goodreads
    feed = goodreads.feed_url(url, shelf)
    if any(entry["url"] == feed for entry in watched()):
        raise ValueError("That list is already being watched.")
    data = await goodreads.fetch(feed, max_pages=1 if add_existing else goodreads.MAX_PAGES)
    entry = {
        "id": uuid.uuid4().hex, "url": feed, "source": url.strip(),
        "name": name.strip() or data["title"] or "Goodreads list",
        "monitor": "Unmonitored" if monitor == "Unmonitored" else "Monitored", "enabled": True,
        "added": datetime.datetime.now().isoformat(timespec="seconds"),
        "known": [] if add_existing else [i["book_id"] for i in data["items"]],
        "last_check": "", "last_result": {}, "unmatched": [],
    }
    db.set_setting("watched_lists", watched() + [entry])
    start_check(entry["id"])
    return entry


def start_check(list_id):
    """Checks one list in the background. False if it's already being checked."""
    if list_id in _checking:
        return False
    _checking.add(list_id)

    async def run():
        try:
            await check_list(list_id)
        finally:
            _checking.discard(list_id)

    task = asyncio.create_task(run())
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return True


async def check_list(list_id):
    """Reads the list and adds the books that are new on it."""
    from app import goodreads
    from app.monitor import schedule_search
    entry = next((e for e in watched() if e["id"] == list_id), None)
    if not entry:
        return
    now = datetime.datetime.now().isoformat(timespec="seconds")
    try:
        data = await goodreads.fetch(entry["url"])
    except ValueError as e:
        logger.warning(f"Watched list {entry['name']}: {e}")
        _update(list_id, last_check=now, last_result={"error": str(e)})
        return
    known = set(entry.get("known") or [])
    new = [i for i in data["items"] if i["book_id"] not in known]
    on_list = {i["book_id"] for i in data["items"]}
    # Books taken off the list no longer need finding
    unmatched = [u for u in entry.get("unmatched") or [] if u["book_id"] in on_list]
    result = {"on_list": len(data["items"]), "new": len(new), "added": 0, "already": 0, "not_found": 0, "found_later": 0}
    settings = db.get_settings()

    def add(match):
        """Adds a match to the library (or counts it as already there)."""
        if LibraryIndex(db.get_library()).find(match):
            result["already"] += 1
            return
        added = db.add_to_library({**match, "description": match.get("description", "")})
        if entry["monitor"] == "Unmonitored" and added.get("status") in ("Monitored", "Unreleased"):
            db.update_library_status(added["id"], "Unmonitored")
        elif added.get("status") == "Monitored" and settings.get("qbt_enabled"):
            schedule_search(db.get_book(added["id"]))
        db.add_history("list", added, f"Added from {entry['name']}")
        result["added"] += 1

    for item in reversed(new):  # Oldest first, so the library gets them in the order you added them
        title, series, sequence = _clean_title(item["title"])
        try:
            match = await audible.auto_match({"title": title, "authors": item["author"], "series": series, "sequence": sequence})
        except Exception as e:
            logger.warning(f"Watched list {entry['name']}: couldn't look up {title}: {e}")
            continue  # Tried again next time
        known.add(item["book_id"])
        if not match:
            result["not_found"] += 1
            unmatched = [u for u in unmatched if u["book_id"] != item["book_id"]]
            unmatched.insert(0, {"book_id": item["book_id"], "title": title, "author": item["author"], "tried": now,
                                 "series": series, "sequence": sequence})
            continue
        add(match)
        await asyncio.sleep(0.2)  # Gentle on Audible's API

    # Books not found before are looked up again every week: Audible may have them by now
    cutoff = (datetime.datetime.now() - datetime.timedelta(days=RETRY_DAYS)).isoformat(timespec="seconds")
    due = [u for u in unmatched if (u.get("tried") or "") < cutoff][:RETRY_PER_CHECK]
    for u in due:
        try:
            match = await audible.auto_match({"title": u["title"], "authors": u["author"],
                                              "series": u.get("series", ""), "sequence": u.get("sequence", "")})
        except Exception as e:
            logger.warning(f"Watched list {entry['name']}: couldn't look up {u['title']} again: {e}")
            continue
        if match:
            unmatched = [x for x in unmatched if x["book_id"] != u["book_id"]]
            result["found_later"] += 1
            add(match)
        else:
            u["tried"] = now
        await asyncio.sleep(0.2)
    # Remembered, so a book you remove from the library isn't added back on the next check
    _update(list_id, known=sorted(known), unmatched=unmatched[:UNMATCHED_KEPT], last_check=now, last_result=result)
    if result["added"]:
        logger.info(f"Watched list {entry['name']}: added {result['added']} book(s)")


async def check_all():
    """Checks every enabled list (with the library check)."""
    for entry in watched():
        if entry.get("enabled", True) and entry["id"] not in _checking:
            _checking.add(entry["id"])
            try:
                await check_list(entry["id"])
            except Exception as e:
                logger.error(f"Watched list {entry.get('name')} failed: {e}", exc_info=True)
            finally:
                _checking.discard(entry["id"])
