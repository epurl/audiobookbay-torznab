"""GraphicAudio's store (graphicaudio.net) as a source for its dramatizations that Audible
doesn't sell: Audible often lists them in a series only as placeholders (no narrators,
length or date), and GraphicAudio sells most books in several parts.

There's no API, so the store's pages are read: a series page lists every release
("Series 1: Book Title 1 of 5"), and a release's page has its date, approximate length,
ISBN, description and cover. Requests are spaced out and cached."""
import asyncio
import html
import logging
import re
import time
from urllib.parse import quote_plus

import httpx

from app.library import series_key, titles_match

logger = logging.getLogger(__name__)

BASE = "https://www.graphicaudio.net"
_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
_SPACING = 1.0  # seconds between requests
_CACHE_TTL = 6 * 3600
_cache = {}  # url -> (time, final url, text)
_lock = asyncio.Lock()
_last = 0.0

# "The Stormlight Archive 1: The Way of Kings 1 of 5", "The Stormlight Archive: Dawnshard"
_NAME = re.compile(r"^(?P<series>.+?)(?:\s+(?P<num>\d+(?:\.\d+)?))?:\s*(?P<title>.+?)(?:\s+(?P<part>\d+)\s+of\s+(?P<of>\d+))?$")
_SETS = re.compile(r"\((?:series|download)\s+set\)|\bbox\s*set\b|\bbundle\b", re.IGNORECASE)


async def _get(url):
    """(final url, page text); cached, one request at a time, spaced out."""
    global _last
    hit = _cache.get(url)
    if hit and time.time() - hit[0] < _CACHE_TTL:
        return hit[1], hit[2]
    async with _lock:
        wait = _last + _SPACING - time.time()
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers={"User-Agent": _AGENT}) as client:
                res = await client.get(url)
        finally:
            _last = time.time()
    res.raise_for_status()
    _cache[url] = (time.time(), str(res.url), res.text)
    return str(res.url), res.text


def parse_name(name):
    """ "The Stormlight Archive 1: The Way of Kings 1 of 5" -> {"series", "sequence", "title",
    "part", "part_count"}; None for sets and bundles."""
    name = re.sub(r"\s+", " ", html.unescape(name or "")).strip()
    if not name or _SETS.search(name):
        return None
    m = _NAME.match(name)
    if not m:
        return {"series": "", "sequence": "", "title": name, "part": None, "part_count": None}
    return {"series": m.group("series").strip(), "sequence": m.group("num") or "", "title": m.group("title").strip(),
            "part": int(m.group("part")) if m.group("part") else None,
            "part_count": int(m.group("of")) if m.group("of") else None}


def _listed(page):
    """Every release listed on a series or search page: {"name", "url", "image", "dated",
    "preorder"}. Recent and upcoming releases show a date (without the year), and ones
    not out yet a Pre-Order button."""
    starts = [m.start() for m in re.finditer(r'<li class="item product product-item', page)]
    found = []
    for a, b in zip(starts, starts[1:] + [len(page)]):
        block = page[a:b]
        name = re.search(r'<h2 class="product name product-item-name"[^>]*>\s*(.*?)\s*</h2>', block, re.S)
        href = re.search(r'href="(https://www\.graphicaudio\.net/[^"#?]+\.html)"', block)
        if not name or not href:
            continue
        image = re.search(r'src="(https://www\.graphicaudio\.net/media/catalog/product/[^"]+)"', block)
        # Only what's shown counts: every entry's markup has a hidden pre-order button
        shown = html.unescape(re.sub(r"<script.*?</script>|<style.*?</style>|<[^>]+>", " ", block, flags=re.S))
        found.append({"name": html.unescape(re.sub(r"<[^>]+>|\s+", " ", name.group(1))).strip(), "url": href.group(1),
                      "image": image.group(1) if image else "", "dated": "Release Date:" in shown,
                      "preorder": bool(re.search(r"\bPre-Order\b", shown))})
    return found


def _items(page):
    """(name, url, image) of every release listed on a series or search page."""
    return [(i["name"], i["url"], i["image"]) for i in _listed(page)]


def release_title(info):
    """The title Bayarr gives a release, like Audible's GraphicAudio titles:
    "Book Title (Part 1 of 5) [Dramatized Adaptation]"."""
    part = f" (Part {info['part']} of {info['part_count']})" if info.get("part") and info.get("part_count") else ""
    return f"{info['title']}{part} [Dramatized Adaptation]"


async def series_releases(series_name):
    """Every release GraphicAudio lists for a series: [{"name", "url", "image", ...parsed}].
    Found through the store's search, which goes straight to a series page when the
    name matches one. Empty when GraphicAudio doesn't have it."""
    plain = re.sub(r"\s*[\[(][^\])]*(dramati[sz]|adaptation|graphic\s*audio)[^\])]*[\])]", "", series_name or "", flags=re.I).strip()
    if not plain:
        return []
    try:
        final, page = await _get(f"{BASE}/catalogsearch/result/?q={quote_plus(plain)}")
        if "/our-productions/series/" not in final:
            # A results page: follow a matching release to its series page
            match = next((url for name, url, _ in _items(page)
                          if (parse_name(name) or {}).get("series") and series_key(parse_name(name)["series"]) == series_key(plain)), None)
            if not match:
                return []
            _, product = await _get(match)
            link = re.search(r'<div class="series-name"[^>]*>\s*<a href="([^"]+)"', product)
            if not link:
                return []
            final, page = await _get(link.group(1) + "?product_list_limit=100")
        elif "product_list_limit" not in final:
            final, page = await _get(final.split("?")[0] + "?product_list_limit=100")
    except httpx.HTTPError as e:
        logger.warning(f"GraphicAudio: couldn't read the series {plain}: {e}")
        return []
    # Everything on the series page is in the series, whatever it's called there ("Name Saga")
    releases = []
    for item in _listed(page):
        info = parse_name(item["name"])
        if not info:
            continue
        release = {**info, "name": item["name"], "url": item["url"], "image": item["image"], "release_date": ""}
        if item["dated"] or item["preorder"]:
            # Recent or not out yet: the exact date (with its year) from its own page
            release["release_date"] = (await release_details(item["url"])).get("release_date", "")
        releases.append(release)
    return releases


async def search(query):
    """GraphicAudio's releases for a search: [{"title", "series", "sequence", "part",
    "part_count", "ga_url", "imageUrl", "preorder", "authors"}]. The store's search goes
    straight to a series or author page when the words name one."""
    if not (query or "").strip():
        return []
    final, page = await _get(f"{BASE}/catalogsearch/result/?q={quote_plus(query.strip())}")
    if "product_list_limit" not in final:
        final, page = await _get(final + ("&" if "?" in final else "?") + "product_list_limit=100")
    author = ""
    if "/our-productions/authors/" in final:
        heading = re.search(r'<span class="base"[^>]*>(.*?)</span>', page)
        author = html.unescape(heading.group(1)).strip() if heading else ""
    results = []
    for item in _listed(page):
        info = parse_name(item["name"])
        if not info:
            continue
        results.append({"title": release_title(info), "series": info["series"], "sequence": info["sequence"],
                        "part": info["part"], "part_count": info["part_count"], "ga_url": item["url"],
                        "imageUrl": item["image"], "preorder": item["preorder"], "authors": author})
    return results


async def release_details(url):
    """A release's page: {"release_date", "runtime_min" (approximate), "isbn", "genre",
    "description", "imageUrl", "authors", "sku"}; {} if it can't be read."""
    try:
        _, page = await _get(url)
    except httpx.HTTPError as e:
        logger.warning(f"GraphicAudio: couldn't read {url}: {e}")
        return {}

    def field(css):
        m = re.search(r'<div class="%s">\s*(.*?)\s*</div>' % css, page, re.S)
        return html.unescape(re.sub(r"<[^>]+>|\s+", " ", m.group(1))).strip() if m else ""

    details = {}
    released = re.sub(r"^Release Date:\s*", "", field("product-releasedate"))
    try:
        details["release_date"] = time.strftime("%Y-%m-%d", time.strptime(released, "%b %d, %Y"))
    except ValueError:
        pass
    hours = re.search(r"([\d.]+)\s*Hours?", field("product-runningtime"), re.I)
    if hours:
        details["runtime_min"] = int(float(hours.group(1)) * 60)
        details["runtime_approx"] = True
    isbn = re.search(r"ISBN #:\s*(\d{10,13})", page)
    if isbn:
        details["isbn"] = isbn.group(1)
    genre = re.sub(r"^Genre:\s*", "", field("product-genre"))
    if genre:
        details["genre"] = genre
    desc = re.search(r'<div class="product-description">\s*(.*?)\s*</div>', page, re.S)
    if desc:
        text = html.unescape(re.sub(r"<br\s*/?>", "\n", desc.group(1)))
        details["description"] = re.sub(r"<[^>]+>", "", text).strip()
    image = re.search(r'<link itemprop="image" href="([^"]+)"', page)
    if image:
        # The original upload rather than a resized copy
        details["imageUrl"] = re.sub(r"/cache/[0-9a-f]+/", "/", image.group(1))
    author = re.search(r'itemprop="author">\s*([^<]+?)\s*<', page)
    if author:
        details["authors"] = html.unescape(author.group(1))
    sku = re.search(r'data-product-sku="([^"]+)"', page)
    if sku:
        details["sku"] = sku.group(1)
    return details


async def with_details(book):
    """A GraphicAudio release with its page's details filled in (date, approximate length,
    description, ISBN, full-size cover). Other books are returned as they are."""
    if not book.get("ga_url"):
        return book
    details = await release_details(book["ga_url"])
    filled = dict(book)
    for key in ("release_date", "runtime_min", "runtime_approx", "description", "isbn"):
        if details.get(key) and not filled.get(key):
            filled[key] = details[key]
    if details.get("imageUrl"):
        filled["imageUrl"] = details["imageUrl"]
    if details.get("authors") and not filled.get("authors"):
        filled["authors"] = details["authors"]
    return filled


def fill_series(series_title, series_asin, books, alternates, releases):
    """Adds GraphicAudio's releases to an Audible series list where Audible has only
    placeholders (or nothing) for a book: the placeholders are replaced by GraphicAudio's
    parts. Books Audible really sells are kept as they are."""
    entries = books + alternates
    author = next((b.get("authors") for b in entries if b.get("authors")), "")
    # Dramatizations Audible really sells (the unabridged narration is another edition)
    sold = [b for b in entries if not b.get("placeholder") and b.get("edition") == "abridged"]
    added = []
    for rel in releases:
        number = rel["sequence"]
        same_book = [b for b in sold if number and b.get("catalog_sequence") == number]
        # Audible sells this book, or this part of it (GraphicAudio often releases parts first)
        if same_book and (not rel.get("part") or any(not b.get("part") or b.get("part") == rel["part"] for b in same_book)):
            continue
        if not number and any(titles_match(b.get("title"), rel["title"]) for b in sold):
            continue
        entry = {
            "title": release_title(rel), "authors": author, "narrators": "", "imageUrl": rel.get("image", ""),
            "release_date": rel.get("release_date", ""), "asin": "", "series": series_title, "series_asin": series_asin,
            "sequence": number,
            "series_list": [{"name": series_title, "asin": series_asin, "sequence": number}],
            "catalog_sequence": number, "runtime_min": 0, "publisher": "GraphicAudio", "language": "English",
            "edition": "abridged", "edition_reason": "a GraphicAudio dramatization", "part": rel.get("part"),
            "part_count": rel.get("part_count"), "ga_url": rel["url"],
        }
        added.append(entry)
    if not added:
        return books, alternates
    filled = {b["catalog_sequence"] for b in added if b["catalog_sequence"]}
    keep = lambda b: not (b.get("placeholder") and b.get("catalog_sequence") in filled)
    return [b for b in books if keep(b)], [b for b in alternates if keep(b)] + added


def needs_graphicaudio(series_title, books, alternates):
    """A dramatized series where Audible lists books it doesn't sell, or sells only some
    parts of a book sold in parts."""
    entries = books + alternates
    placeholders = any(b.get("placeholder") for b in entries) and (
        re.search(r"dramati[sz]|graphic\s*audio", series_title or "", re.I)
        or any(b.get("edition") == "abridged" and not b.get("placeholder") for b in entries))
    parts = {}
    for b in entries:
        if b.get("part_count") and b.get("edition") == "abridged" and not b.get("placeholder"):
            parts.setdefault((b.get("catalog_sequence"), b["part_count"]), set()).add(b.get("part"))
    missing_parts = any(len(found) < count for (_, count), found in parts.items())
    return bool(placeholders or missing_parts)
