"""Importing a want-to-read list: a Goodreads or StoryGraph export (or any CSV with title
and author columns), matched to Audible, then added to the library."""
import asyncio
import csv
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
