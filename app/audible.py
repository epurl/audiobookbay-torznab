"""Audible catalog API: book search, product lookups and series listings."""
import asyncio
import logging
import re

import httpx

from app import editions
from app.library import format_sequence, main_series_fields, normalize, primary_author, series_key

logger = logging.getLogger(__name__)

API = "https://api.audible.com/1.0/catalog/products"
RESPONSE_GROUPS = "product_plan_details,product_desc,contributors,product_attrs,media,product_extended_attrs,series"
_CHUNK = 50  # ASINs per lookup


async def _get(url, params):
    """Audible's API, retried twice when it fails for a moment (a time-out, 429 or 5xx)."""
    for attempt in range(3):
        try:
            async with httpx.AsyncClient() as client:
                res = await client.get(url, params=params, timeout=20.0)
            res.raise_for_status()
            return res.json()
        except (httpx.TransportError, httpx.HTTPStatusError) as e:
            transient = isinstance(e, httpx.TransportError) or e.response.status_code == 429 or e.response.status_code >= 500
            if not transient or attempt == 2:
                raise
            await asyncio.sleep(1 + attempt * 2)


async def search(title="", author="", num_results=None):
    params = {"response_groups": RESPONSE_GROUPS, "image_sizes": "500"}
    if title:
        params["title"] = title
    if author:
        params["author"] = author
    if num_results:
        params["num_results"] = num_results
    return await _get(API, params)


async def search_keywords(query, num_results=50):
    """Audible's general search: matches titles, authors, narrators and series names,
    so "Series Name", "Author" and "Title Author" all work."""
    return await _get(API, {"keywords": query, "response_groups": RESPONSE_GROUPS, "image_sizes": "500",
                            "num_results": num_results, "products_sort_by": "Relevance"})


async def get_products(asins):
    products = []
    asins = list(asins)
    for i in range(0, len(asins), _CHUNK):
        data = await _get(API, {"asins": ",".join(asins[i:i + _CHUNK]),
                                "response_groups": RESPONSE_GROUPS, "image_sizes": "500"})
        products.extend(data.get("products") or [])
    return products


# Audible lists translators, editors etc. among the authors: "J. Torres - translator"
_CONTRIBUTOR_ROLE = re.compile(r"\s-\s*(translator|editor|foreword|introduction|afterword|contributor|"
                               r"illustrator|adapter|adaptation|preface|compiler)\b", re.IGNORECASE)


def product_series(product):
    """Every series Audible lists for a product, as [{"name", "asin", "sequence"}]."""
    return [{"name": x.get("title", ""), "asin": x.get("asin", ""), "sequence": format_sequence(x.get("sequence", ""))}
            for x in product.get("series") or [] if x.get("title")]


def _strip_series_tag(title, entries):
    """ "Book Title (Series Name, Book Two)" -> "Book Title": a bracketed tag that only
    names the book's series or its number. Other brackets ("(Unabridged)") are kept."""
    names = {series_key(e["name"]) for e in entries} - {""}
    numbered = r"\b(book|volume|vol|part)\.?\s+\w+\s*$"
    m = re.match(r"^(.+?)\s*[\(\[]([^\)\]]+)[\)\]]\s*$", title or "")
    if m:
        tag = m.group(2)
        if any(name in normalize(tag) for name in names) or re.match(numbered.replace(r"\b", "^", 1), tag.lower()):
            return m.group(1).strip()
    # "Book Title: Series Name, Book 12" (not "Book Title: A Series Name Novella")
    m = re.match(r"^(.+?):\s*(.+)$", title or "")
    if m:
        tag = m.group(2)
        if any(name in normalize(tag) for name in names) and re.search(numbered, tag.lower()):
            return m.group(1).strip()
    return title


def product_to_book(product, prefer_series=""):
    """Converts an Audible product into a library book. Its main series is the one matching
    prefer_series (what the library already has), else the most specific one."""
    entries = product_series(product)
    fields = main_series_fields(entries)
    if prefer_series:
        want = series_key(prefer_series)
        chosen = next((e for e in entries if series_key(e["name"]) == want), None) or next(
            (e for e in entries if want and (want in series_key(e["name"]) or series_key(e["name"]) in want)), None)
        if chosen:
            fields.update(series=chosen["name"], sequence=chosen["sequence"], series_asin=chosen["asin"])
    authors = [a["name"] for a in product.get("authors") or [] if not _CONTRIBUTOR_ROLE.search(a.get("name", ""))]
    edition = editions.classify_product(product)
    # Audible lists some series' books it doesn't sell (e.g. dramatizations sold elsewhere) as
    # placeholders: no narrators or length, released "2200-01-01". They're still books you
    # can get, so they keep no date (not Unreleased forever) and are marked
    placeholder = "placeholder" in (product.get("publisher_name") or "").lower()
    return {
        "placeholder": placeholder,
        "title": _strip_series_tag(product.get("title") or "", entries),
        "subtitle": product.get("subtitle") or "",
        "authors": ", ".join(authors),
        "narrators": ", ".join(n["name"] for n in product.get("narrators") or []),
        "imageUrl": (product.get("product_images") or {}).get("500", ""),
        "release_date": "" if placeholder else (product.get("release_date") or product.get("issue_date") or ""),
        "asin": product.get("asin") or "",
        **fields,
        "runtime_min": product.get("runtime_length_min") or 0,
        "description": product.get("publisher_summary") or "",
        "publisher": "" if placeholder else (product.get("publisher_name") or ""),
        "language": (product.get("language") or "").capitalize(),
        "edition": edition["edition"],
        "edition_reason": edition["reason"],
    }


def _title_keys(title):
    """A title and its parts around a colon: "Series: Book Title" also matches
    "Series" and "Book Title"."""
    main, _, rest = title.partition(":")
    return {k for k in (normalize(title), normalize(main), normalize(rest)) if k}


def _strip_edition(title):
    """ "Book Title (Full Cast Edition)" -> "Book Title" """
    return re.sub(r"\s*[\(\[][^\)\]]*(edition|adaptation|dramati[sz]ed)[^\)\]]*[\)\]]", "", title, flags=re.IGNORECASE).strip()


def _same_book(a, b):
    """Two Audible products in one series slot that are the same book: overlapping titles,
    the same subtitle ("Series Book 1" / "Series, Book 1"), or the same narrator at
    nearly the same length (one recording sold under two titles)."""
    if not a["catalog_sequence"]:
        return normalize(a["title"]) == normalize(b["title"])
    if _title_keys(a["title"]) & _title_keys(b["title"]):
        return True
    if a.get("subtitle") and normalize(a["subtitle"]) == normalize(b.get("subtitle")):
        return True
    ra, rb = a.get("runtime_min") or 0, b.get("runtime_min") or 0
    return bool(a.get("narrators") and a["narrators"] == b.get("narrators")
                and ra and rb and abs(ra - rb) <= 0.05 * max(ra, rb))


# Editions sold in parts: "Book Title (Part 1 of 2) (Dramatized Adaptation)", "Book Title (1 of 3)"
_PART = re.compile(r"\s*[\(\[]\s*(?:part\s+)?(\d+)\s+of\s+(\d+)\s*[\)\]]", re.IGNORECASE)


def _split_part(title):
    """ "Book Title (Part 1 of 2) (Dramatized Adaptation)" -> ("Book Title (Dramatized Adaptation)", 1)"""
    m = _PART.search(title or "")
    if not m:
        return title, None
    return re.sub(r"\s{2,}", " ", (title[:m.start()] + title[m.end():]).strip()), int(m.group(1))


def part_of(title):
    """(part, number of parts) for a release sold in parts, else (None, None)."""
    m = _PART.search(title or "")
    return (int(m.group(1)), int(m.group(2))) if m else (None, None)


async def get_series_books(series_asin, language="All"):
    """Every release in a series: (title, books, alternates). books has the narrated
    editions (or a book's other edition when it has no narration); alternates are the
    books' other editions (dramatized, abridged). A book sold in parts is listed part by
    part, each its own release. Box sets and duplicate regional editions are dropped (the
    earliest edition is kept)."""
    data = await _get(f"{API}/{series_asin}", {"response_groups": "relationships,product_desc"})
    series = data.get("product") or {}
    children = [r for r in series.get("relationships") or []
                if r.get("relationship_to_product") == "child" and r.get("relationship_type") == "series"]
    sequences = {r["asin"]: r.get("sequence", "") for r in children}
    products = await get_products(sequences)

    def add(found, book):
        # Regional editions and retitled ones ("Book Title" / "Series: Book Title") are one
        # book: same number and an overlapping title. Keep the earliest.
        same = next((k for k in found if k["catalog_sequence"] == book["catalog_sequence"]
                     and k["edition"] == book["edition"] and _same_book(k, book)), None)
        if same is None:
            found.append(book)
        elif (book["release_date"] or "9999") < (same["release_date"] or "9999"):
            found[found.index(same)] = book

    kept, others = [], []
    parted = {}  # (number, edition, title, part) -> book
    for product in products:
        seq = sequences.get(product.get("asin"), "")
        if re.search(r"[-,]", seq):
            continue  # "1-3" box sets
        if language.lower() != "all" and product.get("language") and product["language"].lower() != language.lower():
            continue
        book = product_to_book(product)
        # This book's number within the series being listed (it may be in several)
        book["catalog_sequence"] = format_sequence(seq)
        if not any(e["asin"] == series_asin for e in book["series_list"]):
            book["series_list"].append({"name": series.get("title", ""), "asin": series_asin, "sequence": format_sequence(seq)})
        base, part = _split_part(book["title"])
        if part is not None:
            book["part"], book["part_count"] = part_of(book["title"])
            # Regional duplicates of a part: keep the earliest
            key = (book["catalog_sequence"], book["edition"], normalize(base), part)
            if key not in parted or (book["release_date"] or "9999") < (parted[key]["release_date"] or "9999"):
                parted[key] = book
            continue
        add(kept if book["edition"] == editions.NARRATED else others, book)
    for book in parted.values():
        (kept if book["edition"] == editions.NARRATED else others).append(book)

    # A book with no narrated edition is listed by its other edition
    alternates = []
    for book in others:
        narrated = any(k["catalog_sequence"] == book["catalog_sequence"] and
                       (not book["catalog_sequence"] or _title_keys(k["title"]) & _title_keys(book["title"]))
                       or _title_keys(k["title"]) & _title_keys(_strip_edition(book["title"])) for k in kept)
        if narrated or any(k["catalog_sequence"] == book["catalog_sequence"] and k["catalog_sequence"] for k in kept):
            alternates.append(book)
        else:
            kept.append(book)
    return series.get("title", ""), kept, alternates


async def find_series_for_book(title, author, series_name=""):
    """Looks up a book on Audible to find a series it belongs to (for books imported from
    folders). With series_name, returns that series if Audible lists it for the book."""
    data = await search(title=title.split(":")[0], author=author, num_results=10)
    want = normalize(title.split(":")[0])
    for product in data.get("products") or []:
        if _title_rank(want, product.get("title") or "") is None or not product.get("series"):
            continue
        entries = product_series(product)
        if series_name:
            key = series_key(series_name)
            chosen = next((e for e in entries if series_key(e["name"]) == key), None) or next(
                (e for e in entries if key in series_key(e["name"]) or series_key(e["name"]) in key), None)
        else:
            chosen = entries[0]
        if chosen and chosen["asin"]:
            return chosen["asin"], chosen["name"]
    return None, None


async def match_candidates(query, limit=10):
    """Audible books for a free-text query, for picking a match by hand."""
    data = await _get(API, {"keywords": query, "response_groups": RESPONSE_GROUPS,
                            "image_sizes": "500", "num_results": limit})
    return [product_to_book(p) for p in data.get("products") or []]


def _title_rank(want, title):
    """How well an Audible title matches a library title: 0 exact, 1 same main title,
    2 same subtitle part ("Universe: Book Title" for "Book Title"), None otherwise.
    Bracketed series tags ("Book Title (Series Name)") are ignored."""
    title = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", "", title).strip() or title
    main, _, rest = title.partition(":")
    if normalize(title) == want:
        return 0
    if normalize(main) == want:
        return 1
    if rest and normalize(rest) == want:
        return 2
    return None


# "Title: Spin-off Volume 1", "Title: Book 2": a subtitle that numbers another book or series
_NUMBERED_SUBTITLE = re.compile(r"(?:\bvol(?:ume)?\.?|\bbook|\bissue|#)\s*\d", re.IGNORECASE)


async def auto_match(book):
    """Finds the Audible edition of a library book, or None when it isn't clear-cut.
    The title and first author must match, in the book's edition (narrated unless it's
    dramatized or abridged). Among matches, the closest title wins, then a matching series
    number, then the earliest edition."""
    # A release sold in parts: search for the book, then keep only the same part
    part = part_of(book.get("title"))[0]
    title = _split_part(book.get("title") or "")[0].split(":")[0].strip()
    author = primary_author(book.get("authors"))
    if not title:
        return None
    data = await search(title=title, author=author, num_results=20)
    want_title, want_author = normalize(title), normalize(author)
    found = []
    edition = editions.edition_of(book)
    for product in data.get("products") or []:
        candidate = product_to_book(product, prefer_series=book.get("series", ""))
        if candidate["edition"] != edition:
            continue  # A dramatized folder matches the dramatized edition, and so on
        if part_of(candidate["title"])[0] != part:
            continue  # Part 1 matches part 1; a book not sold in parts matches only a whole book
        rank = _title_rank(want_title, candidate["title"])
        if rank is None:
            continue
        if rank == 1:
            # Only the main title matches; a numbered subtitle is a different book unless
            # it's in the book's own series ("Title: Spin-off Volume 1" isn't "Title")
            subtitle = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", "", candidate["title"]).partition(":")[2]
            same_series = bool(book.get("series")) and any(
                series_key(e["name"]) == series_key(book["series"]) for e in candidate.get("series_list") or [])
            if _NUMBERED_SUBTITLE.search(subtitle) and not same_series:
                continue
        if want_author and normalize(primary_author(candidate["authors"])) != want_author:
            continue
        found.append((rank, candidate))
    if not found:
        return None
    best_rank = min(rank for rank, _ in found)
    found = [c for rank, c in found if rank == best_rank]
    if book.get("sequence"):
        same_number = [c for c in found if c["sequence"] == format_sequence(book["sequence"])]
        found = same_number or found
    return min(found, key=lambda c: c["release_date"] or "9999")
