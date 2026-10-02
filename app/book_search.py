"""Finds a book on AudiobookBay with several searches.

The site's search matches words anywhere in a post (title, description, keywords), so a
short title alone returns hundreds of unrelated books, and the title with the author's
first name ("Title Jane") finds books mentioning both words. The surname with the
title is usually exact. Searches run from most to least specific, each result is scored
against the book, and the search stops once there's a clear match."""
import asyncio
import logging
import re

from app import indexers, scraper
from app.release_match import AUTO_MIN_SCORE, evaluate, order, rank_key, tokens
from app.library import primary_author

logger = logging.getLogger(__name__)

# How much searching each kind of search may do (pages fetched, detail pages loaded)
LIMITS = {
    "auto": {"pages": 4, "details": 3},
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
    # Without bracketed tags ("(Part 1 of 2)", "(Dramatized Adaptation)"): the site's search
    # wants every word, and releases rarely spell those the same way
    whole = re.sub(r"\s{2,}", " ", re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", " ", book.get("title") or "")).strip() \
        or (book.get("title") or "").strip()
    title = whole.split(":")[0].strip()
    full_title = whole
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

    def add(res, query):
        key = res.get("link") or res.get("download_url") or res.get("magnet_url")
        if not key or key in found:
            return
        res = dict(res)  # cached pages stay as the site sent them
        res.setdefault("source", "AudiobookBay")
        res.setdefault("protocol", "torrent")
        res.update(evaluate(book, res, settings))
        res["query"] = query
        found[key] = res

    queries = build_queries(book)
    # Torznab indexers (e.g. Prowlarr): their own search, with the two most specific queries
    # Each indexer needs its kind of download client: Newznab ones a Usenet client
    # Automatic searches skip torrent sources without qBittorrent (Manual Search still shows
    # them: a magnet link can be copied by hand)
    from app import usenet
    torrents_ok = bool(settings.get("qbt_enabled")) or mode == "manual"
    sources = [i for i in indexers.get_all() if i.get("enabled", True)
               and (torrents_ok if indexers.protocol(i) == "torrent" else bool(usenet.client(settings)))]
    if sources:
        for query, _ in queries[:2]:
            batches = await asyncio.gather(*(indexers.search(i, query) for i in sources), return_exceptions=True)
            for indexer, batch in zip(sources, batches):
                if isinstance(batch, Exception):
                    logger.warning(f"Indexer {indexer.get('name')} search '{query}' failed: {batch}")
                    continue
                for res in batch:
                    add(res, query)
            queries_run.append(query)
            if strong():
                break

    abb = settings.get("abb_enabled", True) and torrents_ok
    author_only = scraper.clean_query(primary_author(book.get("authors")) or "").lower()
    for query, max_pages in (queries if abb else []):
        if mode == "auto" and query.lower() == author_only:
            max_pages = 1  # Automatic searches: the author's newest page, not three
        if pages_used >= limits["pages"] or scraper.is_paused():
            break
        if strong() and mode == "auto":
            break  # An indexer already found it
        if query not in queries_run:
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
                add(res, query)
            if page >= last or len(results) < scraper.RESULTS_PER_PAGE or strong():
                break
        if strong() and (mode == "auto" or len(queries_run) >= 3):
            break

    if not found and error is not None:
        raise RuntimeError(f"AudiobookBay search failed: {error}")

    # AudiobookBay detail pages (narrator, files, abridged, magnet) for the most promising releases
    candidates = sorted((r for r in found.values() if r.get("source") == "AudiobookBay"), key=rank_key)
    worth = [r for r in candidates if r["score"] >= AUTO_MIN_SCORE - 15 or not r["problems"]][:limits["details"]]
    await scraper.add_details(worth)
    for res in worth:
        res.update(evaluate(book, res, settings))

    ranked = order(list(found.values()), settings)
    logger.info(f"Search: {len(ranked)} releases for '{book.get('title')}' from {pages_used} AudiobookBay page(s)"
                f"{f' and {len(sources)} indexer(s)' if sources else ''} ({'; '.join(queries_run)})")
    return ranked, queries_run


def describe(result):
    """One line for the log: why a release was or wasn't chosen."""
    why = result.get("problems") or result.get("reasons") or []
    return f"{result.get('score', 0)} {result.get('raw_title') or result.get('title')} ({'; '.join(why[:3])})"
