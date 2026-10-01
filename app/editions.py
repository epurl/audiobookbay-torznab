"""Which edition a book is: unabridged (the whole book read by one or a few narrators) or
abridged (anything else: shortened readings, and dramatizations such as GraphicAudio
full-cast productions, radio plays and Audible Original performances, which adapt the
book rather than read it).

No single Audible field tells them apart: dramatizations are labelled "unabridged" too,
and "Performance" also marks authors reading their own books. So several signals are
combined, and local books are judged from metadata.json, the files' tags and names.

The unabridged edition is stored as "narrated"; books saved as "dramatized" before the
two were merged count as abridged."""
import logging
import os
import re

logger = logging.getLogger(__name__)

NARRATED, ABRIDGED = "narrated", "abridged"
DRAMATIZED = ABRIDGED  # A dramatization isn't the book read as written: it's on the abridged side
EDITIONS = (NARRATED, ABRIDGED)
LABELS = {NARRATED: "Unabridged", ABRIDGED: "Abridged"}
_OLD = {"dramatized": ABRIDGED}  # Editions saved before

# Words that only appear on dramatized productions
DRAMA_WORDS = re.compile(
    r"\b(dramati[sz]ed|dramati[sz]ation|full[\s_-]?cast|graphic[\s_-]?audio|radio\s+(?:drama|play|production|adaptation|series)"
    r"|audio\s+(?:drama|play|movie)|bbc\s+radio|cinematic\s+audio|booktrack)\b", re.IGNORECASE)
ABRIDGED_WORD = re.compile(r"\babridged\b", re.IGNORECASE)  # never matches "unabridged"
# Narrators that aren't people
_NOT_A_NAME = re.compile(r"^(full[\s-]?cast|various|uncredited|unknown.*)$", re.IGNORECASE)
MANY_NARRATORS = 4


def label(edition):
    return LABELS.get(edition or NARRATED, LABELS[NARRATED])


def stored_edition(value):
    """An edition as saved (old "dramatized" counts as abridged), or None if it's not one."""
    value = _OLD.get(value, value)
    return value if value in EDITIONS else None


def edition_of(book):
    """A library book's edition; books from before editions existed count as narrated."""
    edition = (book or {}).get("edition")
    edition = _OLD.get(edition, edition)
    return edition if edition in EDITIONS else NARRATED


def _result(edition, reason, sure=True):
    return {"edition": edition, "reason": reason, "sure": sure}


def _people(value):
    if isinstance(value, (list, tuple)):
        names = [str(v.get("name") if isinstance(v, dict) else v) for v in value]
    else:
        names = str(value or "").split(",")
    return [n.strip() for n in names if n and n.strip()]


def _narrator_count(narrators):
    return sum(1 for n in _people(narrators) if not _NOT_A_NAME.match(n))


def _words(text, where):
    """Dramatized or abridged from wording; None if neither shows."""
    m = DRAMA_WORDS.search(text or "")
    if m:
        return _result(DRAMATIZED, f'"{m.group(0)}" in the {where}')
    m = ABRIDGED_WORD.search(text or "")
    if m:
        return _result(ABRIDGED, f'"abridged" in the {where}')
    return None


def classify_product(product):
    """An Audible product's edition."""
    if (product.get("format_type") or "").lower() == "abridged":
        return _result(ABRIDGED, "Audible lists it as abridged")
    narrators = _people(product.get("narrators"))
    publisher = product.get("publisher_name") or ""
    series_names = ", ".join(s.get("title") or "" for s in product.get("series") or [])
    for text, where in ((f"{product.get('title', '')} {product.get('subtitle') or ''}", "title"),
                        (", ".join(narrators), "narrators"), (publisher, "publisher"),
                        (series_names, "series name")):  # "Series [Dramatized Adaptation]"
        found = _words(text, where)
        if found and found["edition"] == DRAMATIZED:
            return found
    content_type = (product.get("content_type") or "").lower()
    if content_type == "radio/tv program":
        return _result(DRAMATIZED, "Audible lists it as a radio production")
    # Many voices alone can be a multi-narrator reading; with another sign it's a production
    count = _narrator_count(narrators)
    if count >= MANY_NARRATORS:
        signs = [s for s, ok in (("a performance", content_type == "performance"),
                                 ("an original recording", (product.get("format_type") or "").lower() == "original_recording"),
                                 ("a BBC production", "bbc" in publisher.lower())) if ok]
        if signs:
            return _result(DRAMATIZED, f"{count} narrators, and Audible lists it as {signs[0]}")
    return _result(NARRATED, "Audible lists it as a narrated edition")


def classify_fields(book):
    """A book's edition from its own details (title, narrators, publisher)."""
    for key, where in (("title", "title"), ("subtitle", "title"), ("narrators", "narrators"), ("publisher", "publisher")):
        found = _words(book.get(key), where)
        if found:
            return found
    return None


def _tag_text(path):
    """All text in an audio file's tags (title, album, artist, comment, publisher...)."""
    try:
        import mutagen
        audio = mutagen.File(path)
    except Exception:
        return ""
    if audio is None or not getattr(audio, "tags", None):
        return ""
    parts = []
    try:
        items = audio.tags.items()
    except AttributeError:
        return ""
    for key, value in items:
        key = str(key)
        if "covr" in key or "APIC" in key or key.startswith("PIC"):
            continue  # Cover art
        values = value if isinstance(value, list) else [value]
        for v in values:
            if isinstance(v, (bytes, bytearray)):
                continue
            parts.append(str(v)[:400])
    return " | ".join(parts)


def classify_files(paths, folder=""):
    """A downloaded or local book's edition from its files' tags (the first few files)
    and from the folder and file names. None when nothing points either way."""
    paths = list(paths)
    for path in paths[:3]:
        found = _words(_tag_text(path), "file tags")
        if found:
            return found
    names = [(os.path.basename(os.path.normpath(folder)), "folder name")] if folder else []
    names += [(os.path.basename(p), "file names") for p in paths[:5]]
    for name, where in names:
        found = _words(re.sub(r"[_.]+", " ", name or ""), where)
        if found:
            return found
    return None


def classify_local(meta, audio_paths, folder):
    """Import Existing: a folder's edition from metadata.json, the files' tags and names.
    "sure" is False when the evidence is only suggestive (many narrators)."""
    meta = meta or {}
    if meta.get("abridged") is True:
        return _result(ABRIDGED, "metadata.json marks it abridged")
    text = " | ".join(str(meta.get(k) or "") for k in ("title", "subtitle", "publisher", "narrators"))
    text += " | " + " | ".join(str(x) for x in (meta.get("genres") or []) + (meta.get("tags") or []))
    found = _words(text, "metadata.json")
    if found:
        return found
    found = classify_files(audio_paths, folder)
    if found:
        return found
    count = _narrator_count(meta.get("narrators"))
    if count >= MANY_NARRATORS:
        return _result(NARRATED, f"{count} narrators: it may be a dramatization", sure=False)
    return _result(NARRATED, "No signs of an abridgement or a dramatization")


def combine(audible, local):
    """An Audible edition (from the book's ASIN) and what the files say. Clear local
    evidence wins: a folder tagged GraphicAudio is dramatized even when its ASIN points
    to the narrated edition (a mismatched ASIN), and is flagged for checking."""
    if local and local["edition"] != NARRATED and audible and audible["edition"] != local["edition"]:
        return {**local, "sure": False,
                "reason": f"{local['reason']}, but its ASIN is the {label(audible['edition']).lower()} edition"}
    if audible:
        return audible
    return local or _result(NARRATED, "No signs of an abridgement or a dramatization")


def release_edition(raw_title, keywords=(), categories=(), narrators=(), abridged=False):
    """An AudiobookBay release's edition from its title, keywords, categories and (from
    its page) narrators."""
    text = " ".join([raw_title or ""] + list(keywords or []) + list(categories or []))
    found = _words(text, "listing")
    if found:
        return found
    if abridged:
        return _result(ABRIDGED, "the release page says abridged")
    names = _people(narrators)
    if any(_NOT_A_NAME.match(n) and "cast" in n.lower() for n in names):
        return _result(DRAMATIZED, "a full cast")
    if _narrator_count(names) >= MANY_NARRATORS:
        return _result(DRAMATIZED, f"{_narrator_count(names)} narrators", sure=False)
    return _result(NARRATED, "")
