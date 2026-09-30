"""Audible catalog API: book search, product lookups and series listings."""
import logging
import re

import httpx

from app.library import format_sequence, normalize

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


def product_to_book(product, sequence=None):
    """Converts an Audible product into a library book."""
    series = (product.get("series") or [{}])[0]
    return {
        "title": product.get("title") or "",
        "authors": ", ".join(a["name"] for a in product.get("authors") or []),
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
