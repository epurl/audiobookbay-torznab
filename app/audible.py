"""Audible catalog API: book search, product lookups and series listings."""
import logging
import re

import httpx

from app.library import format_sequence, main_series_fields, normalize, primary_author, series_key

logger = logging.getLogger(__name__)

API = "https://api.audible.com/1.0/catalog/products"
RESPONSE_GROUPS = "product_plan_details,product_desc,contributors,product_attrs,media,product_extended_attrs,series"
_CHUNK = 50  # ASINs per lookup


async def _get(url, params):
    async with httpx.AsyncClient() as client:
        res = await client.get(url, params=params, timeout=20.0)
        res.raise_for_status()
        return res.json()


async def search(title="", author="", num_results=None):
    params = {"response_groups": RESPONSE_GROUPS, "image_sizes": "500"}
    if title:
        params["title"] = title
    if author:
        params["author"] = author
    if num_results:
        params["num_results"] = num_results
    return await _get(API, params)


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
    return {
        "title": product.get("title") or "",
        "subtitle": product.get("subtitle") or "",
        "authors": ", ".join(authors),
        "narrators": ", ".join(n["name"] for n in product.get("narrators") or []),
        "imageUrl": (product.get("product_images") or {}).get("500", ""),
        "release_date": product.get("release_date") or product.get("issue_date") or "",
        "asin": product.get("asin") or "",
        **fields,
        "runtime_min": product.get("runtime_length_min") or 0,
        "description": product.get("publisher_summary") or "",
        "publisher": product.get("publisher_name") or "",
        "language": (product.get("language") or "").capitalize(),
    }


def _title_keys(title):
    """A title and its parts around a colon: "Mistborn: The Final Empire" also matches
    "Mistborn" and "The Final Empire"."""
    main, _, rest = title.partition(":")
    return {k for k in (normalize(title), normalize(main), normalize(rest)) if k}


def _strip_edition(title):
    """ "Blood of Elves (Full Cast Edition)" -> "Blood of Elves" """
    return re.sub(r"\s*[\(\[][^\)\]]*(edition|adaptation|dramati[sz]ed)[^\)\]]*[\)\]]", "", title, flags=re.IGNORECASE).strip()


def _same_book(a, b):
    """Two Audible products in one series slot that are the same book: overlapping titles,
    the same subtitle ("Mistborn Book 1" / "Mistborn, Book 1"), or the same narrator at
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


def _is_dramatized(product):
    """Alternate versions of a book, not the book itself: dramatized and full-cast
    adaptations (e.g. GraphicAudio) and music-enhanced "Booktrack" editions."""
    narrators = " ".join(n.get("name", "") for n in product.get("narrators") or [])
    text = f"{product.get('title', '')} {product.get('publisher_name', '')} {narrators}".lower()
    return any(word in text.replace("-", " ") for word in ("dramatized", "full cast", "booktrack")) \
        or "graphicaudio" in text.replace(" ", "")


async def get_series_books(series_asin, language="All"):
    """Every book in a series, one entry per book: dramatized adaptations, box sets and
    duplicate regional editions are dropped (the earliest edition is kept)."""
    data = await _get(f"{API}/{series_asin}", {"response_groups": "relationships,product_desc"})
    series = data.get("product") or {}
    children = [r for r in series.get("relationships") or []
                if r.get("relationship_to_product") == "child" and r.get("relationship_type") == "series"]
    sequences = {r["asin"]: r.get("sequence", "") for r in children}
    products = await get_products(sequences)

    kept, alternates = [], []
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
        # Regional editions and retitled ones ("The Final Empire" / "Mistborn: The Final
        # Empire") are one book: same number and an overlapping title. Keep the earliest.
        if _is_dramatized(product):
            alternates.append(book)
            continue
        same = next((k for k in kept if k["catalog_sequence"] == book["catalog_sequence"]
                     and _same_book(k, book)), None)
        if same is None:
            kept.append(book)
        elif (book["release_date"] or "9999") < (same["release_date"] or "9999"):
            kept[kept.index(same)] = book
    # Full-cast and dramatized versions are only listed when there's no regular edition
    for book in alternates:
        regular = any(k["catalog_sequence"] == book["catalog_sequence"] and
                      (not book["catalog_sequence"] or _title_keys(k["title"]) & _title_keys(book["title"]))
                      or _title_keys(k["title"]) & _title_keys(_strip_edition(book["title"])) for k in kept)
        if not regular and not any(k["catalog_sequence"] == book["catalog_sequence"] and k["catalog_sequence"]
                                   for k in kept):
            kept.append(book)
    return series.get("title", ""), kept


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
    2 same subtitle part ("Universe: Book Title" for "Book Title"), None otherwise."""
    main, _, rest = title.partition(":")
    if normalize(title) == want:
        return 0
    if normalize(main) == want:
        return 1
    if rest and normalize(rest) == want:
        return 2
    return None


async def auto_match(book):
    """Finds the Audible edition of a library book, or None when it isn't clear-cut.
    The title and first author must match; dramatized versions are skipped. Among matches,
    the closest title wins, then a matching series number, then the earliest edition."""
    title = (book.get("title") or "").split(":")[0].strip()
    author = primary_author(book.get("authors"))
    if not title:
        return None
    data = await search(title=title, author=author, num_results=20)
    want_title, want_author = normalize(title), normalize(author)
    found = []
    for product in data.get("products") or []:
        if _is_dramatized(product):
            continue
        candidate = product_to_book(product, prefer_series=book.get("series", ""))
        rank = _title_rank(want_title, candidate["title"])
        if rank is None:
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
