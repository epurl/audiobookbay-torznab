"""Following an author: every book Audible lists for them, and their new releases added
automatically (like monitoring a series), in the editions the edition setting asks for."""
import datetime
import logging
import re
import uuid

from app import audible, db
from app.library import normalize, part_number, title_key
from app.series_index import LibraryIndex, wanted_editions

logger = logging.getLogger(__name__)

PAGES = 3  # x 50 newest books per author


def _same_author(name, authors):
    want = normalize(name)
    return any(normalize(a) == want for a in (authors or "").split(","))


def _box_set(book):
    seq = str(book.get("sequence") or "")
    return bool(re.search(r"\d\s*[-,]\s*\d", seq)) or bool(
        re.search(r"\b(box(ed)?\s*set|collection|omnibus|books?\s+\d+\s*[-–]\s*\d+)\b", book.get("title") or "", re.IGNORECASE))


async def catalog(name):
    """The author's books on Audible, newest first: one entry per book and edition (the
    earliest of duplicate regional editions), without box sets."""
    language = (db.get_settings().get("language") or "All").lower()
    books, seen = [], {}
    for page in range(PAGES):  # Audible numbers result pages from 0
        data = await audible._get(audible.API, {"author": name, "products_sort_by": "-ReleaseDate", "num_results": 50,
                                                "page": page, "response_groups": audible.RESPONSE_GROUPS, "image_sizes": "500"})
        products = data.get("products") or []
        for product in products:
            book = audible.product_to_book(product)
            if not _same_author(name, book["authors"]) or _box_set(book):
                continue
            if language != "all" and book.get("language") and book["language"].lower() != language:
                continue
            key = (title_key(book["title"]), book["edition"], part_number(book["title"]))  # Parts are separate releases
            if key in seen:
                if (book["release_date"] or "9999") < (seen[key]["release_date"] or "9999"):
                    books[books.index(seen[key])] = book
                    seen[key] = book
                continue
            seen[key] = book
            books.append(book)
        if len(products) < 50:
            break
    books.sort(key=lambda b: b.get("release_date") or "", reverse=True)
    return books


def followed():
    return list(db.get_settings().get("followed_authors") or [])


def _save(items):
    db.set_setting("followed_authors", items)


def get(author_id=None, name=None):
    return next((a for a in followed() if a["id"] == author_id or (name and normalize(a["name"]) == normalize(name))), None)


async def detail(name):
    """The author's page: their books with library status, and what Follow would offer."""
    books = await catalog(name)
    index = LibraryIndex(db.get_library())
    wanted = wanted_editions()
    today = datetime.date.today().isoformat()
    ignored = db.ignored_ids()
    rows = []
    for b in books:
        owned = index.find(b)
        rows.append({**b, "book_id": (owned or {}).get("id", ""), "status": (owned or {}).get("status", ""),
                     "upcoming": (b.get("release_date") or "") > today, "ignored": db.is_ignored(b, ignored)})
    entry = get(name=name)
    # The library's own spelling of the name, if any
    spelled = next((a.strip() for b in db.get_library() for a in (b.get("authors") or "").split(",")
                    if normalize(a) == normalize(name)), name)
    name = spelled
    return {
        "name": entry["name"] if entry else name,
        "followed": entry,
        "books": rows,
        "owned": sum(1 for r in rows if r["status"] == "Imported"),
        "in_library": sum(1 for r in rows if r["book_id"]),
        "upcoming": sum(1 for r in rows if r["upcoming"]),
        "candidates": [r for r in rows if not r["book_id"] and r["edition"] in wanted],
    }


async def follow(name, add_asins=()):
    """Follows an author: books chosen now are added, and later releases automatically."""
    books = await catalog(name)
    items = followed()
    entry = get(name=name)
    if entry is None:
        entry = {"id": uuid.uuid4().hex, "name": name, "monitored": True, "known_asins": [],
                 "added": datetime.datetime.now().isoformat(timespec="seconds"), "last_sync": ""}
        items.append(entry)
    entry["monitored"] = True
    entry["known_asins"] = sorted(set(entry["known_asins"]) | {b["asin"] for b in books if b.get("asin")})
    entry["last_sync"] = datetime.datetime.now().isoformat(timespec="seconds")
    _save([entry if i["id"] == entry["id"] else i for i in items])
    added = []
    index = LibraryIndex(db.get_library())
    for b in books:
        if b.get("asin") in set(add_asins) and not index.find(b):
            added.append(db.add_to_library({**b, "description": ""}))
            db.unignore(b)  # Chosen by you: no longer ignored
    if added:
        db.add_history("author", {"title": name}, f"Added {len(added)} book{'s' if len(added) != 1 else ''} by {name}")
    return entry, added


async def sync(entry, settings):
    """Adds the author's books that are new on Audible since the last check."""
    books = await catalog(entry["name"])
    index = LibraryIndex(db.get_library())
    known = set(entry.get("known_asins") or [])
    wanted = wanted_editions(settings.get("edition_preference"))
    ignored = db.ignored_ids()
    added = []
    for b in books:
        if (b.get("asin") and b["asin"] not in known and b["edition"] in wanted and not index.find(b)
                and not db.is_ignored(b, ignored)):
            added.append(db.add_to_library({**b, "description": ""}))
    update(entry["id"], known_asins=sorted(known | {b["asin"] for b in books if b.get("asin")}),
           last_sync=datetime.datetime.now().isoformat(timespec="seconds"))
    if added:
        db.add_history("author", {"title": entry["name"]},
                       f"Added {len(added)} new book{'s' if len(added) != 1 else ''} by {entry['name']}")
    return added


def update(author_id, **fields):
    _save([{**a, **fields} if a["id"] == author_id else a for a in followed()])


def unfollow(author_id):
    _save([a for a in followed() if a["id"] != author_id])


def summary():
    """Followed authors for the list page, with how many of their books are in the library."""
    library = db.get_library()
    out = []
    for a in followed():
        mine = [b for b in library if _same_author(a["name"], b.get("authors"))]
        out.append({**{k: a[k] for k in ("id", "name", "monitored", "added", "last_sync")},
                    "known": len(a.get("known_asins") or []), "in_library": len(mine),
                    "on_disk": sum(1 for b in mine if b.get("status") == "Imported"),
                    "cover": next((b.get("imageUrl") for b in mine if b.get("imageUrl")), "")})
    return sorted(out, key=lambda a: a["name"].lower())
