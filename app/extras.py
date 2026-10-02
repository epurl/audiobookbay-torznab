"""Telling a series' main books from its extras (novellas, short stories, collections,
companions, excerpts...) using Audible's series list.

Audible numbers a series' main books 1, 2, 3... and puts most extras between them (2.5) or
leaves them unnumbered; titles, lengths and descriptions say the rest. Tuned on Audible's
lists for 800+ series, it leans towards "main": a book is only called an extra (and then
ignored automatically) on clear evidence. Unclear ones (a full-length prequel numbered 0,
a long book numbered 4.5, an unnumbered novel) are "maybe": shown, never ignored.

The verdict for one book: None (a main book) or {"verdict": "extra" | "maybe", "label",
"reason"}."""
import re
import statistics

from app.editions import NARRATED, edition_of

# What a title or subtitle says about a book that isn't a main one. Only trusted for books
# Audible doesn't give a whole number (main titles use some of these words: "Stories",
# "Guide", "Tales"), and not when it's the series' own name or what its main books share.
_WORDS = [(label, re.compile(rx, re.I)) for label, rx in [
    ("Novella", r"\bnovell(a|as|ette|ettes)\b"),
    ("Short story", r"\bshort[\s-]stor(y|ies)\b|\ba [\w'’ ]{0,40}\b(story|tale)\s*$|\bshort(er)? fiction\b"),
    ("Stories", r"\bstories\b|\btales from\b|\bcollected tales\b"),
    ("Collection", r"\bcollection\b|\bcollected\b|\bomnibus\b|\bbox(ed)?[\s-]?set\b|\bbundle\b|\banthology\b|"
                   r"\b(books?|volumes?|novels?) \d+\s*(-|–|to|&|and|through)\s*\d+\b|\bnovels in one\b|"
                   r"\b(two|three|four|2|3|4) (bestselling |great |classic )?(novels|books|mysteries)\b|\bthe complete\b"),
    ("Companion", r"\bcompanion\b|\bguide\b|\bhandbook\b|\bencyclop|\b(the )?making of\b|\broleplaying\b|"
                  r"\bcookbook\b|\bcocktails\b|\bbehind the scenes\b|\bdocumentary\b|\breader\s*$|\bbonus scenes?\b"),
    ("Excerpt", r"\bexcerpt\b|\bsampler\b|\bpreview\b|\bprologue\b|\bsneak peek\b|\bfirst chapters?\b"),
    ("Spin-off", r"\b(from|in) the worlds? of\b|\bin (the )?[\w'’ ]{0,30}\buniverse\b"),
    ("Audio drama", r"\b(audible original|radio|bbc|full[\s-]cast|audio) drama|\bradio (play|crimes?)\b|\bpodcast\b|"
                    r"\blive in concert\b"),
    ("Graphic novel", r"\bgraphic novel\b"),
    ("Between the numbers", r"\bbetween[\s-]the[\s-]numbers?\b|\bholiday novel\b"),
]]
# An excerpt or sample, whatever its number
_SAMPLE = re.compile(r"[\(\[]\s*(excerpt|sample|preview|sampler)\s*[\)\]]|\bfree (excerpt|sample|preview)\b", re.I)
# What a description says about a collection; "a novella" alone isn't enough for a long book
_COLLECTION = re.compile(r"\banthology\b|\bcollection of\b|\bshort stories\b|\bnovellas\b", re.I)
_NOVELLA = re.compile(r"\bnovella\b", re.I)
_PARTS = re.compile(r"[\(\[]\s*(?:part\s+)?\d+\s+of\s+\d+\s*[\)\]]", re.I)  # "(Part 1 of 2)": a release in parts
_HALF = re.compile(r"\bpart (one|two|three|four|1|2|3|4|i|ii|iii|iv)\b", re.I)  # One part of a split main book
_NOT_BOOKS = {"Podcast", "Radio/TV Program"}
SHORT, SHORT_MAX = 0.5, 360  # Short: under half the main books' usual length, and 6 hours at most
FULL = 0.75                    # Full length: at least three quarters of it


def summary_hint(text):
    """What a book's description says about it, kept with Audible's series list (the
    descriptions themselves aren't): "collection", "novella" or ""."""
    text = re.sub(r"<[^>]+>", " ", text or "")
    if _COLLECTION.search(text):
        return "collection"
    return "novella" if _NOVELLA.search(text) else ""


def _number(seq):
    try:
        return float(seq)
    except (TypeError, ValueError):
        return None


def _whole(seq):
    n = _number(seq)
    return n is not None and n >= 1 and n.is_integer()


def _key(title):
    title = re.sub(r"[\(\[][^\)\]]*[\)\]]", "", (title or "").lower())
    return re.sub(r"[^a-z0-9]+", "", re.sub(r"^(the|a|an)\s+", "", title))


def _seq(book):
    return str(book.get("catalog_sequence") if book.get("catalog_sequence") is not None else book.get("sequence") or "")


def _text(book, series_title):
    """Title and subtitle without the series' own name: a series called "... Stories" or
    "... Collection" says nothing about one of its books."""
    text = f"{book.get('title') or ''} :: {book.get('subtitle') or ''}"
    name = re.sub(r"^(the|a|an)\s+", "", re.sub(r"\s*[\(\[].*?[\)\]]", "", series_title or "").strip(), flags=re.I)
    return re.sub(re.escape(name), " ", text, flags=re.I) if len(name) >= 4 else text


def _labels(book, series_title):
    text = _text(book, series_title)
    return [label for label, rx in _WORDS if rx.search(text)]


def context(books, series_title=""):
    """What's usual in a series (one edition side of it): its main books' length, whether
    it's numbered at all, and the words its main books share."""
    whole = [b for b in books if _whole(_seq(b))]
    lengths = [b.get("runtime_min") or 0 for b in whole
               if b.get("runtime_min") and not _PARTS.search(b.get("title") or "") and edition_of(b) == NARRATED]
    if not lengths:  # A side without unabridged main books (dramatizations): its own
        lengths = [b.get("runtime_min") or 0 for b in whole if b.get("runtime_min") and not _PARTS.search(b.get("title") or "")]
    titles = {_key(b.get("title")) for b in books}
    whole_titles = {_key(b.get("title")) for b in whole}
    # Numbered: whole-numbered main books, not a handful among unnumbered ones (a whole universe)
    numbered = len(whole_titles) >= 2 and len(whole_titles) >= 0.2 * len(titles)
    counted = whole if numbered else books
    counts = {}
    for b in counted:
        for label in set(_labels(b, series_title)):
            counts[label] = counts.get(label, 0) + 1
    limit = (0.5 if numbered else 0.25) * len(counted)
    return {
        "title": series_title,
        "usual": statistics.median(lengths) if lengths else 0,
        "numbered": numbered,
        "whole_titles": whole_titles,
        "whole_numbers": {int(_number(_seq(b))) for b in whole},
        "common": {label for label, n in counts.items() if n >= max(2, limit)},
    }


def _extra(label, reason):
    return {"verdict": "extra", "label": label, "reason": reason}


def _maybe(label, reason):
    return {"verdict": "maybe", "label": label, "reason": reason}


def classify(book, ctx):
    """None for a main book, else {"verdict", "label", "reason"} (see the module)."""
    seq = _seq(book)
    n = _number(seq)
    title = book.get("title") or ""
    if _SAMPLE.search(f"{title} {book.get('subtitle') or ''}"):
        return _extra("Excerpt", "An excerpt or sample")
    if _whole(seq):
        return None
    found = next((label for label in _labels(book, ctx["title"]) if label not in ctx["common"]), "")
    length = 0 if _PARTS.search(title) else book.get("runtime_min") or 0
    usual = ctx["usual"]
    short = bool(usual and length and length < SHORT * usual and length <= SHORT_MAX)
    full = bool(usual and length and length >= FULL * usual)
    hint = book.get("summary_hint") or ""
    collection = hint == "collection" or (hint == "novella" and not full)
    if _key(title) in ctx["whole_titles"] and not found:
        return None  # Another edition of a main book
    if book.get("content_type") in _NOT_BOOKS:
        return _extra("Podcast", "A podcast or radio programme, not a book")
    if not ctx["numbered"]:  # No main books to set it apart from
        return _maybe(found or "Unnumbered", "This series doesn't number its books, so its main ones can't be told apart"
                      + (f" (its title says: {found.lower()})" if found else ""))
    if found:
        return _extra(found, f"Its title says: {found.lower()}")
    if n is not None and n >= 1:  # 2.5, 12.75: numbered between the main books
        if int(n) not in ctx["whole_numbers"]:
            return _maybe(f"#{seq}", f"Numbered {seq}, but there's no main book {int(n)} to put it after")
        if re.search(r"\b(vol(ume)?|book)\.?\s*" + re.escape(seq) + r"\b", title, re.I):
            return _maybe(f"#{seq}", f"Numbered {seq}, but sold as a volume of the series")
        if _HALF.search(title):
            return _maybe(f"#{seq}", f"Numbered {seq}, but it may be part of a main book")
        if full and not collection:
            return _maybe(f"#{seq}", f"Numbered {seq}, between the main books, but as long as one")
        if full:
            return _extra("Collection", f"Numbered {seq}, and its description calls it a collection")
        return _extra(f"#{seq}", f"Numbered {seq}: between the main books")
    if n is not None:  # 0, 0.5: before book 1
        if short:
            return _extra("Short prequel", f"Numbered {seq} (before book 1) and short")
        if collection and length:
            return _extra("Collection", f"Numbered {seq} (before book 1), and its description calls it a collection")
        return _maybe("Prequel", f"Numbered {seq}: a prequel, perhaps a full novel")
    if short:
        return _extra("Short", "Unnumbered, and much shorter than the main books")
    if collection and length:
        return _extra("Collection", "Unnumbered, and its description calls it a collection")
    return _maybe("Unnumbered", "Unnumbered: maybe a side book, maybe a main one Audible hasn't numbered yet")


def classify_all(books, series_title=""):
    """Verdicts for one side of a series' list, by position in it."""
    ctx = context(books, series_title)
    return [classify(b, ctx) for b in books]
