"""Trending Audible releases for the Calendar: recent and upcoming books from Audible's
best-seller lists (overall and per genre) and new books from the authors in the library.

Audible's genre filter is loose (it boosts a genre rather than restricting to it), so
each book's genres come from its own category path and filtering happens on our side.
The list is cached and refreshed in the background."""
import asyncio
import datetime
import json
import logging
import os
import tempfile
from collections import Counter

from app import audible, db
from app.library import normalize, primary_author

logger = logging.getLogger(__name__)

CACHE_FILE = os.path.join(db.CONFIG_DIR, "release_calendar.json")
MAX_AGE = datetime.timedelta(hours=12)
PAST_DAYS, FUTURE_DAYS = 90, 365      # Releases kept around today
BESTSELLER_PAGES = 10                 # x 50: Audible's top 500, pre-orders included
GENRE_PAGES = 2                       # x 50 per genre
AUTHOR_LIMIT = 40                     # Library authors checked for new books
RESPONSE_GROUPS = "product_desc,contributors,product_attrs,product_extended_attrs,rating,series,media,category_ladders"

_cache = None
refresh_job = {"running": False, "total": 0, "done": 0, "failed": 0}
_tasks = set()


def _load():
    global _cache
    if _cache is None:
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                _cache = json.load(f)
        except (OSError, ValueError):
            _cache = {"fetched": "", "items": [], "genres": []}
    return _cache


def _save(data):
    global _cache
    _cache = data
    os.makedirs(db.CONFIG_DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=db.CONFIG_DIR, prefix=".calendar-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, CACHE_FILE)


# Raised when the way the list is built changes, so saved lists are rebuilt
CACHE_VERSION = 2


def is_stale():
    fetched = _load().get("fetched")
    if not fetched or _load().get("version") != CACHE_VERSION:
        return True
    return datetime.datetime.now() - datetime.datetime.fromisoformat(fetched) > MAX_AGE


def get():
    return _load()


def _genres(product):
    """ (top-level genres, sub-genres) from the product's category paths."""
    tops, subs = [], []
    for ladder in product.get("category_ladders") or []:
        names = [x.get("name") for x in ladder.get("ladder") or [] if x.get("name")]
        if names and names[0] not in tops:
            tops.append(names[0])
        if len(names) > 1 and names[1] not in subs:
            subs.append(names[1])
    return tops, subs


def _item(product):
    book = audible.product_to_book(product)
    rating = (product.get("rating") or {}).get("overall_distribution") or {}
    tops, subs = _genres(product)
    return {
        **{k: book[k] for k in ("asin", "title", "subtitle", "authors", "narrators", "imageUrl", "release_date",
                                "series", "sequence", "series_asin", "series_list", "runtime_min", "publisher",
                                "language", "edition")},
        "genres": tops,
        "subgenres": subs,
        "rating": float(rating.get("display_average_rating") or 0),
        "ratings": int(rating.get("num_ratings") or 0),
        "rank": None,          # Position in Audible's overall best-sellers
        "genre_rank": None,    # Best position in a genre's best-sellers
        "from_author": False,  # A new book by an author in the library
    }


async def _products(params):
    data = await audible._get(audible.API, {"response_groups": RESPONSE_GROUPS, "image_sizes": "500",
                                            "num_results": 50, **params})
    return data.get("products") or []


async def _top_level_genres():
    data = await audible._get("https://api.audible.com/1.0/catalog/categories", {"categories_num_levels": 1})
    return [(c["name"], c["id"]) for c in data.get("categories") or [] if c.get("name") and c.get("id")]


def _library_authors():
    counts = Counter(primary_author(b.get("authors")) for b in db.get_library())
    counts.pop("", None)
    return [name for name, _ in counts.most_common(AUTHOR_LIMIT)]


async def build():
    settings = db.get_settings()
    language = (settings.get("language") or "All").lower()
    today = datetime.date.today()
    start = (today - datetime.timedelta(days=PAST_DAYS)).isoformat()
    end = (today + datetime.timedelta(days=FUTURE_DAYS)).isoformat()
    items = {}

    def keep(product):
        date = product.get("release_date") or product.get("issue_date") or ""
        if not product.get("asin") or not (start <= date <= end):
            return None
        lang = (product.get("language") or "").lower()
        if language != "all" and lang and lang != language:
            return None
        if product["asin"] not in items:
            items[product["asin"]] = _item(product)
        return items[product["asin"]]

    genres = await _top_level_genres()
    authors = _library_authors()
    # Audible numbers result pages from 0
    steps = ([("bestsellers", page) for page in range(BESTSELLER_PAGES)]
             + [("genre", (g, page)) for g in genres for page in range(GENRE_PAGES)]
             + [("author", a) for a in authors])
    refresh_job.update(total=len(steps), done=0, failed=0)

    for kind, arg in steps:
        try:
            if kind == "bestsellers":
                for i, product in enumerate(await _products({"products_sort_by": "BestSellers", "page": arg})):
                    item = keep(product)
                    if item:
                        rank = arg * 50 + i + 1
                        item["rank"] = min(item["rank"] or rank, rank)
            elif kind == "genre":
                (name, genre_id), page = arg
                for i, product in enumerate(await _products({"products_sort_by": "BestSellers", "category_id": genre_id,
                                                             "page": page})):
                    item = keep(product)
                    if item and name in item["genres"]:
                        rank = page * 50 + i + 1
                        item["genre_rank"] = min(item["genre_rank"] or rank, rank)
            else:
                want = normalize(arg)
                for product in await _products({"author": arg, "products_sort_by": "-ReleaseDate", "num_results": 10}):
                    item = keep(product)
                    if item and any(normalize(a) == want for a in item["authors"].split(",")):
                        item["from_author"] = True
        except Exception as e:
            logger.warning(f"Calendar: couldn't load {kind} {arg}: {e}")
            refresh_job["failed"] += 1
        refresh_job["done"] += 1
        await asyncio.sleep(0.3)  # Gentle on Audible's API

    data = {
        "version": CACHE_VERSION,
        "fetched": datetime.datetime.now().isoformat(timespec="seconds"),
        "items": sorted(items.values(), key=lambda x: (x["release_date"], x["rank"] or 9999)),
        "genres": [name for name, _ in genres],
    }
    _save(data)
    logger.info(f"Calendar: {len(data['items'])} releases from {len(steps)} Audible lists")
    return data


def start_refresh():
    if refresh_job["running"]:
        return False
    refresh_job.update(running=True, total=0, done=0, failed=0)

    async def run():
        try:
            await build()
        except Exception as e:
            logger.error(f"Calendar refresh failed: {e}", exc_info=True)
        finally:
            refresh_job["running"] = False

    task = asyncio.create_task(run())
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return True
