"""Groups the library into series for the Series pages, using every series each book
belongs to and Audible's full list of books for each series."""
import asyncio
import logging
from collections import Counter

from app import audible, db
from app.library import normalize, primary_author, series_entries, series_key

logger = logging.getLogger(__name__)

WANTED = {"Monitored", "Unreleased", "Downloading", "Downloaded", "Needs Review", "Missing"}


def _seq_sort(seq, release_date=""):
    """Numbered books first, in order; unnumbered ones (umbrella series like a whole
    universe) by release date."""
    try:
        return (0, float(str(seq).lstrip("#")), release_date or "")
    except (TypeError, ValueError):
        return (1, 0.0, release_date or "9999")


class LibraryIndex:
    """Fast lookup of library books by ASIN, or by title and any shared author (books
    don't always list their authors in the same order)."""

    def __init__(self, library):
        self.by_asin = {b["asin"]: b for b in library if b.get("asin")}
        self.by_title = {}
        for b in library:
            title = normalize((b.get("title") or "").split(":")[0])
            for author in self._authors(b):
                self.by_title.setdefault((title, author), b)

    @staticmethod
    def _authors(book):
        return [normalize(a) for a in (book.get("authors") or "").split(",") if normalize(a)] or [""]

    def _by_title(self, title, book):
        for author in self._authors(book):
            found = self.by_title.get((normalize(title), author))
            if found:
                return found
        return None

    def find(self, book):
        found = self.by_asin.get(book.get("asin"))
        if found:
            return found
        title = book.get("title") or ""
        found = self._by_title(title.split(":")[0], book)
        if found:
            return found
        # Audible titles can carry the series name: "Universe: Book Title"
        if ":" in title:
            return self._by_title(title.partition(":")[2], book)
        return None


def build_groups():
    """{key: group}. A group is one series; key is "asin:<id>" when the Audible id is known,
    else "name:<normalized name>". Books appear in every series they belong to."""
    library = db.get_library()
    tracked = db.get_series_list()
    catalogs = db.get_catalogs()

    # Series known only by name join the Audible series with the same name
    name_to_asin = {}
    for t in tracked:
        name_to_asin.setdefault(series_key(t["title"]), t["asin"])
    for b in library:
        for e in series_entries(b):
            if e.get("asin"):
                name_to_asin.setdefault(series_key(e["name"]), e["asin"])

    groups = {}

    def group(key, title, asin):
        if key not in groups:
            groups[key] = {"key": key, "title": title, "asin": asin, "books": [], "tracked": None}
        return groups[key]

    for t in tracked:
        group("asin:" + t["asin"], t["title"], t["asin"])["tracked"] = t
    for b in library:
        seen = set()
        for e in series_entries(b):
            asin = e.get("asin") or name_to_asin.get(series_key(e["name"]), "")
            key = "asin:" + asin if asin else "name:" + series_key(e["name"])
            if key in seen:
                continue
            seen.add(key)
            group(key, e["name"], asin)["books"].append((b, e.get("sequence", "")))

    for g in groups.values():
        g["catalog"] = catalogs.get(g["asin"]) if g["asin"] else None
        if g["catalog"] and g["catalog"].get("title") and not g["tracked"]:
            g["title"] = g["catalog"]["title"]
    return groups


def _rows(group, index):
    """Every book in the series: Audible's list merged with the library's books."""
    rows, used = [], set()
    for cb in (group["catalog"] or {}).get("books", []):
        owned = index.find(cb)
        if owned:
            used.add(owned["id"])
        rows.append({"book": owned, "catalog": cb, "sequence": cb.get("catalog_sequence") or cb.get("sequence", "")})
    for b, seq in group["books"]:
        if b["id"] not in used:
            used.add(b["id"])
            rows.append({"book": b, "catalog": None, "sequence": seq})
    rows.sort(key=lambda r: _seq_sort(r["sequence"], (r["book"] or r["catalog"]).get("release_date")))
    return rows


def _cover(rows):
    for r in rows:
        b = r["book"]
        if b and b.get("path") and b.get("cover"):
            return f"/api/library/{b['id']}/cover"
    for r in rows:
        url = (r["book"] or {}).get("imageUrl") or (r["catalog"] or {}).get("imageUrl")
        if url:
            return url
    return ""


def series_author(rows):
    """The series' author: the most common lead author across its books, by Audible's
    order where known (a library book may list an illustrator or co-author first)."""
    votes = Counter(primary_author((r["catalog"] or r["book"] or {}).get("authors")) for r in rows)
    votes.pop("", None)
    return votes.most_common(1)[0][0] if votes else ""


def summarize(group, index):
    rows = _rows(group, index)
    statuses = [r["book"]["status"] for r in rows if r["book"]]
    tracked = group["tracked"]
    return {
        "key": group["key"],
        "title": group["title"],
        "asin": group["asin"],
        "author": series_author(rows) or (tracked or {}).get("author", ""),
        "cover": _cover(rows),
        "monitored": bool(tracked and tracked.get("monitored")),
        "series_id": (tracked or {}).get("id", ""),
        "owned": statuses.count("Imported"),
        "in_library": len(statuses),
        "wanted": sum(1 for st in statuses if st in WANTED),
        # Unknown until Audible's list for the series has been loaded
        "total": len(rows) if group["catalog"] else None,
        "missing": sum(1 for r in rows if not r["book"]) if group["catalog"] else None,
        # Audible dates books without a release date 2200-01-01
        "latest": max((d for r in rows if (d := (r["book"] or r["catalog"]).get("release_date") or "") < "2100"),
                      default=""),
        "catalog_loaded": bool(group["catalog"]),
    }


def index():
    groups = build_groups()
    lib_index = LibraryIndex(db.get_library())
    return [summarize(g, lib_index) for g in groups.values()]


async def resolve_asin(group):
    """Finds the Audible id of a series known only by name, via one of its books."""
    for book, _ in group["books"][:3]:
        asin, _name = await audible.find_series_for_book(book.get("title", ""), primary_author(book.get("authors")),
                                                         series_name=group["title"])
        if asin:
            db.set_series_asin(group["title"], asin)
            return asin
    return ""


async def ensure_catalog(asin, force=False):
    catalog = db.get_catalog(asin)
    if catalog and not force and db.catalog_is_fresh(catalog):
        return catalog
    title, books = await audible.get_series_books(asin, db.get_settings().get("language", "All"))
    db.save_catalog(asin, title, books)
    _attach_owned(asin, title, books)
    return db.get_catalog(asin)


_FILL_FROM_AUDIBLE = ("asin", "runtime_min", "narrators", "publisher", "imageUrl", "release_date")


def audible_author_order(current, audible_authors):
    """The library book's authors in Audible's order, when Audible lists all of them (e.g.
    "Illustrator, Author" from a metadata.json becomes "Author, Illustrator"). Names are
    only reordered, never added or removed. None when there's nothing to change."""
    names = [n.strip() for n in (current or "").split(",") if n.strip()]
    order = [n.strip() for n in (audible_authors or "").split(",") if n.strip()]
    mine = {normalize(n): n for n in names}
    if not order or not all(normalize(n) in mine for n in order):
        return None
    listed = {normalize(n) for n in order}
    new = [mine[normalize(n)] for n in order] + [n for n in names if normalize(n) not in listed]
    return ", ".join(new) if new != names else None


def _attach_owned(asin, title, catalog_books):
    """Records the series (with its number) on library books Audible lists in it, and fills
    in Audible details they lack (the runtime is what the download length check uses)."""
    index = LibraryIndex(db.get_library())
    for cb in catalog_books:
        owned = index.find(cb)
        if not owned:
            continue
        db.add_series_to_books({"name": title, "asin": asin, "sequence": cb.get("catalog_sequence") or ""}, {owned["id"]})
        fill = {k: cb[k] for k in _FILL_FROM_AUDIBLE if cb.get(k) and not owned.get(k)}
        reordered = audible_author_order(owned.get("authors"), cb.get("authors"))
        if reordered:
            fill["authors"] = reordered
        if fill:
            db.update_book(owned["id"], **fill)


async def detail(key):
    groups = build_groups()
    group = groups.get(key)
    if group is None and key.startswith("name:"):
        # A series known by name may since have been matched to its Audible id
        group = next((g for g in groups.values() if series_key(g["title"]) == key[5:]
                      or any(series_key(e["name"]) == key[5:] for b, _ in g["books"] for e in series_entries(b))), None)
    if group is None:
        if not key.startswith("asin:"):
            return None
        # A series from Search results that isn't in the library yet
        group = {"key": key, "title": "", "asin": key[5:], "books": [], "tracked": None, "catalog": None}
    if not group["asin"]:
        asin = await resolve_asin(group)
        if asin:
            group = build_groups().get("asin:" + asin, group)
            group["asin"] = asin
    if group["asin"]:
        group["catalog"] = await ensure_catalog(group["asin"])
        group["title"] = (group["tracked"] or {}).get("title") or group["catalog"].get("title") or group["title"]
        # Attaching may have moved books between groups; rebuild from the saved data
        fresh = build_groups().get("asin:" + group["asin"])
        if fresh:
            fresh["title"] = group["title"]
            group = fresh
    lib_index = LibraryIndex(db.get_library())
    rows = _rows(group, lib_index)
    summary = summarize(group, lib_index)
    summary["rows"] = [{
        "sequence": r["sequence"],
        "book_id": (r["book"] or {}).get("id", ""),
        "status": (r["book"] or {}).get("status", ""),
        "title": (r["book"] or r["catalog"]).get("title", ""),
        "authors": (r["book"] or r["catalog"]).get("authors", ""),
        "narrators": (r["book"] or r["catalog"]).get("narrators", ""),
        "release_date": (r["book"] or r["catalog"]).get("release_date", ""),
        "runtime_min": (r["book"] or {}).get("runtime_min") or (r["catalog"] or {}).get("runtime_min") or 0,
        "asin": (r["book"] or {}).get("asin") or (r["catalog"] or {}).get("asin", ""),
        "catalog": r["catalog"] if not r["book"] else None,
    } for r in rows]
    summary["unresolved"] = not group["asin"]
    return summary


# Background lookup of Audible ids and book lists for every series in the library
refresh_job = {"running": False, "total": 0, "done": 0, "failed": 0}
_refresh_tasks = set()


def start_refresh():
    if refresh_job["running"]:
        return False
    groups = [g for g in build_groups().values() if not g["catalog"] or not db.catalog_is_fresh(g["catalog"])]
    refresh_job.update(running=True, total=len(groups), done=0, failed=0)

    async def run():
        try:
            for g in groups:
                try:
                    asin = g["asin"] or await resolve_asin(g)
                    if asin:
                        await ensure_catalog(asin)
                except Exception as e:
                    logger.warning(f"Couldn't load series {g['title']} from Audible: {e}")
                    refresh_job["failed"] += 1
                refresh_job["done"] += 1
                await asyncio.sleep(0.5)  # Gentle on Audible's API
        finally:
            refresh_job["running"] = False

    task = asyncio.create_task(run())
    _refresh_tasks.add(task)
    task.add_done_callback(_refresh_tasks.discard)
    return True
