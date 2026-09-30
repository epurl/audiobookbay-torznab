"""Audible catalog API: book search, product lookups and series listings."""
import logging
import re

import httpx

from app.library import format_sequence, normalize, primary_author

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


def _pick_series(product, prefer=""):
    """A book can be in several series (a whole "Universe" and its "Prequel Trilogy");
    keep the one matching what the library already has, else Audible's first."""
    all_series = product.get("series") or []
    if prefer:
        want = normalize(prefer)
        for s in all_series:
            name = normalize(s.get("title", ""))
            if name and (name == want or name in want or want in name):
                return s
    return all_series[0] if all_series else {}


def product_to_book(product, sequence=None, prefer_series=""):
    """Converts an Audible product into a library book."""
    series = _pick_series(product, prefer_series)
    authors = [a["name"] for a in product.get("authors") or [] if not _CONTRIBUTOR_ROLE.search(a.get("name", ""))]
    return {
        "title": product.get("title") or "",
        "authors": ", ".join(authors),
        "narrators": ", ".join(n["name"] for n in product.get("narrators") or []),
        "imageUrl": (product.get("product_images") or {}).get("500", ""),
        "release_date": product.get("release_date") or product.get("issue_date") or "",
        "asin": product.get("asin") or "",
        "series": series.get("title", ""),
        "series_asin": series.get("asin", ""),
        "sequence": format_sequence(sequence if sequence is not None else series.get("sequence", "")),
        "runtime_min": product.get("runtime_length_min") or 0,
        "description": product.get("publisher_summary") or "",
        "publisher": product.get("publisher_name") or "",
        "language": (product.get("language") or "").capitalize(),
    }


def _is_dramatized(product):
    text = f"{product.get('title', '')} {product.get('publisher_name', '')}".lower()
    return "dramatized" in text or "graphicaudio" in text.replace(" ", "")


async def get_series_books(series_asin, language="All"):
    """Every book in a series, one entry per book: dramatized adaptations, box sets and
    duplicate regional editions are dropped (the earliest edition is kept)."""
    data = await _get(f"{API}/{series_asin}", {"response_groups": "relationships,product_desc"})
    series = data.get("product") or {}
    children = [r for r in series.get("relationships") or []
                if r.get("relationship_to_product") == "child" and r.get("relationship_type") == "series"]
    sequences = {r["asin"]: r.get("sequence", "") for r in children}
    products = await get_products(sequences)

    best = {}
    for product in products:
        seq = sequences.get(product.get("asin"), "")
        if re.search(r"[-,]", seq) or _is_dramatized(product):
            continue  # "1-3" box sets and full-cast adaptations
        if language.lower() != "all" and product.get("language") and product["language"].lower() != language.lower():
            continue
        book = product_to_book(product, seq)
        book["series"] = book["series"] or series.get("title", "")
        book["series_asin"] = series_asin
        key = (book["sequence"], normalize(book["title"].split(":")[0]))
        current = best.get(key)
        if current is None or (book["release_date"] or "9999") < (current["release_date"] or "9999"):
            best[key] = book
    return series.get("title", ""), list(best.values())


async def find_series_for_book(title, author):
    """Looks up a book on Audible to find its series (for books imported from folders)."""
    data = await search(title=title.split(":")[0], author=author, num_results=10)
    want = normalize(title.split(":")[0])
    for product in data.get("products") or []:
        if normalize((product.get("title") or "").split(":")[0]) == want and product.get("series"):
            s = product["series"][0]
            return s.get("asin"), s.get("title")
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
