"""Finds a book on AudiobookBay with several searches.

The site's search matches words anywhere in a post (title, description, keywords), so a
short title alone returns hundreds of unrelated books, and the title with the author's
first name ("Title Jane") finds books mentioning both words. The surname with the
title is usually exact. Searches run from most to least specific, each result is scored
against the book, and the search stops once there's a clear match."""
import logging
import re

from app import scraper
from app.release_match import AUTO_MIN_SCORE, evaluate, order, rank_key, tokens
from app.library import primary_author

logger = logging.getLogger(__name__)

# How much searching each kind of search may do (pages fetched, detail pages loaded)
LIMITS = {
    "auto": {"pages": 6, "details": 3},
    "manual": {"pages": 10, "details": 10},
}


def _surname(author):
    words = [w for w in re.split(r"\s+", author.strip()) if w.lower().strip(".,") not in ("jr", "sr", "ii", "iii", "phd")]
    return words[-1].strip(".,") if words else ""


def _plain_title(title):
    """ "The Book Title" -> "Book Title"; drops punctuation the site's search trips on."""
    title = re.sub(r"^(the|a|an)\s+", "", title.strip(), flags=re.IGNORECASE)
    return re.sub(r"[^\w\s'&-]", " ", title).strip()


def build_queries(book):
    """[(query, pages)] from most to least specific."""
    title = (book.get("title") or "").split(":")[0].strip()
    full_title = (book.get("title") or "").strip()
    author = primary_author(book.get("authors")) or ""
    surname = _surname(author)
    series = (book.get("series") or "").strip()
    sequence = str(book.get("sequence") or "").strip().lstrip("#")
    short = len(tokens(title)) <= 2  # One- or two-word titles: many unrelated books share these

    queries = []
    if title and surname:
        queries.append((f"{title} {surname}", 1))
    if title and author and author != surname:
        queries.append((f"{title} {author}", 1))
    if full_title != title and author:
        queries.append((f"{full_title} {surname}", 1))
    if series and sequence:
        queries.append((f"{series} {sequence}", 1))
    if title:
        queries.append((title, 3 if short else 2))
    plain = _plain_title(title)
    if plain and plain.lower() != title.lower() and surname:
        queries.append((f"{plain} {surname}", 1))
    if series and surname:
        queries.append((f"{series} {surname}", 2))
    if author:
        queries.append((author, 3))

    seen, out = set(), []
    for query, pages in queries:
        query = scraper.clean_query(query)
        if query and query.lower() not in seen:
            seen.add(query.lower())
            out.append((query, pages))
    return out


async def find_releases(book, settings, mode="auto"):
    """Every release found for the book, scored, best first. In "auto" mode the search
    stops at the first clear match; "manual" looks further for alternatives."""
    limits = LIMITS[mode]
    found = {}
    pages_used = 0
    queries_run = []
    error = None

    def strong():
        return any(r["strong"] for r in found.values())

    for query, max_pages in build_queries(book):
        if pages_used >= limits["pages"] or scraper.is_paused():
            break
        queries_run.append(query)
        for page in range(1, max_pages + 1):
            if pages_used >= limits["pages"]:
                break
            try:
                results, last = await scraper.search_page(query, page)
            except Exception as e:
                logger.warning(f"AudiobookBay search '{query}' failed: {e}")
                error = e
                break
            pages_used += 1
            for res in results:
                if res["link"] in found:
                    continue
                res = dict(res)  # the cached page stays as the site sent it
                res.update(evaluate(book, res, settings))
                res["query"] = query
                found[res["link"]] = res
            if page >= last or len(results) < scraper.RESULTS_PER_PAGE or strong():
                break
        if strong() and (mode == "auto" or len(queries_run) >= 3):
            break

    if not found and error is not None:
        raise RuntimeError(f"AudiobookBay search failed: {error}")

    # Detail pages (narrator, files, abridged, magnet) for the most promising releases
    candidates = sorted(found.values(), key=rank_key)
    worth = [r for r in candidates if r["score"] >= AUTO_MIN_SCORE - 15 or not r["problems"]][:limits["details"]]
    await scraper.add_details(worth)
    for res in worth:
        res.update(evaluate(book, res, settings))

    ranked = order(list(found.values()), settings)
    logger.info(f"AudiobookBay: {len(ranked)} releases for '{book.get('title')}' from {pages_used} page(s) "
                f"({'; '.join(queries_run)})")
    return ranked, queries_run


def describe(result):
    """One line for the log: why a release was or wasn't chosen."""
    why = result.get("problems") or result.get("reasons") or []
    return f"{result.get('score', 0)} {result.get('raw_title') or result.get('title')} ({'; '.join(why[:3])})"
