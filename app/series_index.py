"""Groups the library into series for the Series pages, using every series each book
belongs to and Audible's full list of books for each series."""
import asyncio
import logging
from collections import Counter, defaultdict

from app import audible, db
from app.editions import DRAMATIZED, NARRATED, edition_of
from app.library import normalize, primary_author, series_entries, series_key, title_keys

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
    don't always list their authors in the same order) in the same edition: a dramatized
    version is a different book from the narrated one."""

    def __init__(self, library):
        self.by_asin = {b["asin"]: b for b in library if b.get("asin")}
        self.by_full = defaultdict(list)  # whole title -> books
        self.by_part = defaultdict(list)  # whole title, main title or subtitle part -> books
        for b in library:
            full, keys = title_keys(b.get("title"))
            self.by_full[full].append(b)
            for key in keys:
                self.by_part[key].append(b)

    @staticmethod
    def _authors(book):
        return {normalize(a) for a in (book.get("authors") or "").split(",") if normalize(a)} or {""}

    def find(self, book):
        # Editions sold in parts: your copy may have any part's ASIN
        for asin in [book.get("asin")] + list(book.get("part_asins") or []):
            found = self.by_asin.get(asin)
            if found:
                return found
        full, keys = title_keys(book.get("title"))
        authors, edition = self._authors(book), edition_of(book)
        fits = lambda b: edition_of(b) == edition and self._authors(b) & authors
        # The exact title first, then this title with or without a subtitle ("Universe: Title")
        candidates = self.by_full.get(full, []) + self.by_part.get(full, []) + \
            [b for key in keys for b in self.by_full.get(key, [])]
        return next((b for b in candidates if fits(b)), None)


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


def _all_rows(group, index):
    """(rows, other editions): every book in the series, from Audible's list merged with
    the library's books, and the books' other editions (dramatized, abridged)."""
    rows, alternates, used = [], [], set()
    for cb in (group["catalog"] or {}).get("books", []):
        owned = index.find(cb)
        if owned:
            used.add(owned["id"])
        rows.append({"book": owned, "catalog": cb, "sequence": cb.get("catalog_sequence") or cb.get("sequence", "")})
    for cb in (group["catalog"] or {}).get("alternates", []):
        owned = index.find(cb)
        if owned:
            used.add(owned["id"])
        alternates.append({"book": owned, "catalog": cb, "sequence": cb.get("catalog_sequence") or cb.get("sequence", "")})
    for b, seq in group["books"]:
        if b["id"] not in used:
            used.add(b["id"])
            # A library book Audible doesn't list: another edition goes with the other editions
            row = {"book": b, "catalog": None, "sequence": seq}
            (alternates if group["catalog"] and edition_of(b) != NARRATED else rows).append(row)
    order = lambda r: _seq_sort(r["sequence"], (r["book"] or r["catalog"]).get("release_date"))
    rows.sort(key=order)
    alternates.sort(key=order)
    # A book counts as had when you have any edition of it
    for r in rows:
        if not r["book"]:
            r["other"] = next((a["book"] for a in alternates if a["book"] and a["sequence"] and a["sequence"] == r["sequence"]), None)
    return rows, alternates


def _rows(group, index):
    return _all_rows(group, index)[0]


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


def _held(row):
    """The library book for a row: its own edition, or another edition you have."""
    return row["book"] or row.get("other")


def summarize(group, index):
    rows = _rows(group, index)
    statuses = [_held(r)["status"] for r in rows if _held(r)]
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
        "missing": sum(1 for r in rows if not _held(r)) if group["catalog"] else None,
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
    title, books, alternates = await audible.get_series_books(asin, db.get_settings().get("language", "All"))
    db.save_catalog(asin, title, books, alternates)
    _attach_owned(asin, title, books + alternates)
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
    rows, alternates = _all_rows(group, lib_index)
    summary = summarize(group, lib_index)
    summary["rows"] = [_row_json(r) for r in rows]
    summary["alternates"] = [_row_json(r) for r in alternates]
    wanted = wanted_editions()
    summary["monitor_candidates"] = [r for r in summary["rows"] + summary["alternates"]
                                     if not r["book_id"] and r["catalog"] and r["edition"] in wanted]
    summary["edition_preference"] = db.get_settings().get("edition_preference", "narrated")
    summary["unresolved"] = not group["asin"]
    return summary


def _row_json(r):
    book, catalog = r["book"], r["catalog"]
    shown = book or catalog
    other = r.get("other")
    return {
        "sequence": r["sequence"],
        "book_id": (book or {}).get("id", ""),
        "status": (book or {}).get("status", ""),
        "title": shown.get("title", ""),
        "authors": shown.get("authors", ""),
        "narrators": shown.get("narrators", ""),
        "release_date": shown.get("release_date", ""),
        "runtime_min": (book or {}).get("runtime_min") or (catalog or {}).get("runtime_min") or 0,
        "asin": (book or {}).get("asin") or (catalog or {}).get("asin", ""),
        "edition": edition_of(shown),
        "catalog": catalog if not book else None,
        # Another edition of this book that you have
        "other": {"book_id": other["id"], "status": other.get("status", ""), "edition": edition_of(other)} if other else None,
    }


def wanted_editions(preference=None):
    """The editions Monitor Series offers and series syncs add, from the edition setting.
    Abridged editions count with narrated ones: a book is only listed abridged when
    Audible has no unabridged narration of it."""
    preference = preference or db.get_settings().get("edition_preference", "narrated")
    if preference == "dramatized":
        return {DRAMATIZED}
    if preference == "both":
        return {NARRATED, DRAMATIZED, "abridged"}
    return {NARRATED, "abridged"}


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
