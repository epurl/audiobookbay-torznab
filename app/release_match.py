"""Scores AudiobookBay releases against a library book.

Each release gets a score from 0 to 100, the reasons behind it, and the problems that
would stop it being downloaded automatically (wrong book, wrong number in the series,
a box set, another edition (dramatized or abridged), too small for the book's length...)."""
import re
import unicodedata

from app import editions
from app.db import extract_infohash
from app.scraper import split_release_title

# A release must have no problems and at least this score to be grabbed automatically
AUTO_MIN_SCORE = 45

STOPWORDS = {"the", "a", "an", "of", "and", "to", "in", "on", "at", "for", "or", "with"}
# Words in release titles that say nothing about which book it is
NOISE = STOPWORDS | {
    "unabridged", "audiobook", "audiobooks", "audio", "book", "books", "bk", "m4b", "mp3", "aac", "flac", "kbps",
    "chapterized", "chapterised", "chaptered", "chapters", "retail", "read", "by", "narrated", "narrator", "series",
    "edition", "vol", "volume", "part", "parts", "novel", "re", "ripped", "rip", "repost", "updated", "update",
    "remastered", "english", "version", "audible", "original", "no", "number", "s", "mb", "gb", "kb", "bitrate", "v2",
}
NUMBER_WORDS = {"one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6", "seven": "7",
                "eight": "8", "nine": "9", "ten": "10", "eleven": "11", "twelve": "12", "first": "1",
                "second": "2", "third": "3", "fourth": "4", "fifth": "5"}
NAME_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "phd", "md"}

BUNDLE_WORDS = {"collection", "boxset", "omnibus", "compilation", "novels", "audiobooks", "trilogy", "duology",
                "quartet", "quintet", "anthology", "shorts", "complete"}
VARIOUS_AUTHORS = {"various", "various authors", "multiple authors", "multiple", "anthology", "unknown", "va"}


def norm(text):
    """Lowercase, no accents or punctuation: "Book’s & Co." -> "books and co" """
    text = unicodedata.normalize("NFKD", str(text or ""))
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    text = re.sub(r"['’`´]", "", text.replace("&", " and "))
    return re.sub(r"[^a-z0-9.]+", " ", text).replace(" .", " ").strip(" .")


def tokens(text):
    out = []
    for t in norm(text).replace(".", " ").split():
        t = NUMBER_WORDS.get(t, t)
        out.append(t.lstrip("0") or "0" if t.isdigit() else t)
    return out


def _content(words):
    content = [w for w in words if w not in STOPWORDS]
    return content or list(words)


PLACEHOLDER_NAMES = {"unknown", "unknown author", "unknown narrator", "various", "n a", "none"}


def _names(text):
    """ "Jane Author, John Smith Jr." -> [["jane", "author"], ["john", "smith", "jr"]]"""
    if isinstance(text, (list, tuple)):
        text = ", ".join(text)
    return [tokens(n) for n in re.split(r",|;|&|\band\b", text or "")
            if tokens(n) and " ".join(tokens(n)) not in PLACEHOLDER_NAMES]


def _surname(name_tokens):
    rest = [t for t in name_tokens if t not in NAME_SUFFIXES]
    return rest[-1] if rest else (name_tokens[-1] if name_tokens else "")


def _number(value):
    """ "07" / "#7" / "7.0" -> "7"; "3.5" stays "3.5" """
    m = re.search(r"\d+(?:\.\d+)?", str(value or ""))
    if not m:
        return ""
    num = float(m.group())
    return str(int(num)) if num == int(num) else str(num)


def _bitrate(value):
    m = re.search(r"(\d+(?:\.\d+)?)\s*k", str(value or ""), re.IGNORECASE)
    return float(m.group(1)) if m else 0.0


def _phrase_in(phrase, text):
    return bool(phrase) and re.search(rf"(?:^| ){re.escape(phrase)}(?: |$)", text) is not None


def release_numbers(body, series_names=()):
    """Numbers that say which book of a series a release is, and any ranges ("1-6")
    that mark a box set. Returns (numbers, ranges)."""
    low = body.lower()
    numbers, ranges = set(), []
    for m in re.finditer(r"(\w+)?\W*(?<![\d.])(\d{1,3})\s*(?:-|–|~|to|thru|through|&)\s*(\d{1,3})(?![\d.])", low):
        # "Parts 1-3" and "CD 1-12" are one book split into files
        if m.group(1) in ("part", "parts", "pt", "pts", "disc", "discs", "cd", "cds", "chapter", "chapters", "ch", "file", "files"):
            continue
        a, b = int(m.group(2)), int(m.group(3))
        if a < b <= 200:
            ranges.append((a, b))
    patterns = [
        r"\b(?:book|bk|volume|vol|no|number)\s*\.?\s*#?\s*(\d+(?:\.\d+)?)",
        r"#\s*(\d+(?:\.\d+)?)",
        r"[\[(][^\])]*?(?<![\d.])(\d+(?:\.\d+)?)\s*[\])]",              # [Series Name 7] / (Series 1)
        r"^\s*[a-z][a-z' ]*?\s+0*(\d+(?:\.\d+)?)\s*[:\-]",                # Series 01 - Book Title
        r"[\[(]\s*0*(\d+(?:\.\d+)?)\s*[\])]",                              # Series [01]
    ]
    for name in series_names:
        key = norm(name)
        if key:
            patterns.append(rf"{re.escape(key)}\s*(?:book|bk|vol|volume|#)?\s*0*(\d+(?:\.\d+)?)")
    normalized = norm(low)
    for i, pattern in enumerate(patterns):
        target = normalized if i >= 5 else low
        for m in re.finditer(pattern, target):
            value = float(m.group(1))
            # Years ("The Land (1941)") aren't book numbers
            if value < 500 and not any(a <= value <= b for a, b in ranges):
                numbers.add(_number(m.group(1)))
    return {n for n in numbers if n}, ranges


def _book_series_names(book):
    names = [book.get("series") or ""]
    names += [e.get("name", "") for e in book.get("series_list") or []]
    return [n for n in dict.fromkeys(names) if n]


def evaluate(book, result, settings, blocklist=None):
    """Scores one release for a book: {"score", "verdict", "reasons", "problems"}.
    Releases with problems are never grabbed automatically."""
    reasons, problems = [], []
    score = 0

    raw = result.get("raw_title") or ""
    if not raw:
        raw = result.get("title") or ""
        if result.get("author") and result["author"] != "Unknown":
            raw += " - " + result["author"]
    body, rel_author = split_release_title(raw)
    if result.get("authors"):
        rel_author = ", ".join(result["authors"])
    body_words = tokens(body)
    body_set = set(body_words)
    body_norm = " ".join(body_words)

    title = book.get("title") or ""
    main_title = title.split(":")[0]
    main_words = tokens(main_title)
    main_content = _content(main_words)
    title_all = set(tokens(title)) | set(tokens(book.get("subtitle")))
    series_names = _book_series_names(book)
    series_words = {w for n in series_names for w in tokens(n)}
    author_names = _names(book.get("authors"))
    narrator_names = _names(book.get("narrators"))

    # --- Blocklist ---
    result_hash = extract_infohash(result.get("magnet_url"))
    if result_hash and result_hash in (blocklist if blocklist is not None else book.get("blocklist") or []):
        problems.append("You rejected this release before")
        score -= 50

    # --- Title ---
    hits = [w for w in main_content if w in body_set]
    coverage = len(hits) / len(main_content) if main_content else 0
    stripped = " ".join(tokens(re.sub(r"[\[(][^\])]*[\])]", " ", body)))
    if coverage == 1:
        score += 40
        if stripped in (" ".join(main_words), " ".join(tokens(title))):
            score += 5
            reasons.append("Title matches exactly")
        elif _phrase_in(" ".join(main_words), body_norm):
            reasons.append("Title matches")
        else:
            score -= 5
            reasons.append("Title words all present, not in order")
    elif coverage >= 0.6:
        score += int(40 * coverage) - 10
        problems.append(f"Title only partly matches ({len(hits)} of {len(main_content)} words)")
    else:
        score += int(20 * coverage)
        problems.append("Title doesn't match")

    # --- Author ---
    rel_author_words = set(tokens(rel_author))
    if rel_author_words and rel_author_words <= series_words | NOISE:
        rel_author_words = set()  # "Book Title - Series Name": the series where the author goes
    author_state = "unknown"
    if author_names:
        surnames = {_surname(n) for n in author_names}
        if surnames & (rel_author_words or set()):
            full = any(all(t in rel_author_words for t in n if len(t) > 1) for n in author_names)
            author_state = "full" if full else "surname"
        elif norm(rel_author) in VARIOUS_AUTHORS:
            author_state = "various"
        elif surnames & body_set:
            author_state = "surname"  # "Author - Title" order
        elif rel_author_words:
            author_state = "mismatch"
    if author_state == "full":
        score += 20
        reasons.append("Author matches")
    elif author_state == "surname":
        score += 15
        reasons.append("Author's surname matches")
    elif author_state == "various":
        score -= 10
        problems.append("Multiple authors: a collection")
    elif author_state == "mismatch":
        score -= 30
        problems.append(f"Different author: {rel_author}")

    # --- Number in the series ---
    numbers, ranges = release_numbers(body, series_names)
    numbers -= {w for w in main_words if w.isdigit()}  # numbers that are part of the title
    sequence = _number(book.get("sequence"))
    if sequence and numbers:
        if sequence in numbers:
            score += 10
            reasons.append(f"Book {sequence} of the series")
        elif not ranges:
            score -= 30
            problems.append(f"Different book in the series (#{', #'.join(sorted(numbers, key=float))})")
    elif series_words and series_words & body_set and author_state in ("full", "surname"):
        score += 3

    # --- Box sets and collections ---
    title_bundle_words = BUNDLE_WORDS & title_all
    bundle_words = (BUNDLE_WORDS & body_set) - title_bundle_words
    if "box" in body_set and "set" in body_set and "box" not in title_all:
        bundle_words.add("box set")
    if "books" in body_set and (ranges or len(numbers) > 1):
        bundle_words.add("books")
    if ranges and not re.search(r"\d+\s*-\s*\d+", title):
        bundle_words.add("range")
    if bundle_words or author_state == "various":
        score -= 40
        what = f"Books {ranges[0][0]}-{ranges[0][1]}" if ranges else ", ".join(sorted(bundle_words - {"range"})) or "several books"
        problems.append(f"Box set or collection ({what})")

    # --- Edition: narrated, dramatized or abridged, matched both ways ---
    wanted = book.get("edition") if book.get("edition") in editions.EDITIONS else         (editions.classify_fields(book) or {}).get("edition", editions.NARRATED)
    found = editions.release_edition(raw, result.get("keywords"), result.get("categories"),
                                     result.get("narrators") or [], result.get("abridged"))
    if found["edition"] != wanted:
        many_voices_book = len(narrator_names) >= editions.MANY_NARRATORS
        if found["sure"]:
            score -= 40
            why = f" ({found['reason']})" if found["reason"] else ""
            if found["edition"] == editions.NARRATED:
                problems.append(f"Not a {editions.label(wanted).lower()} edition; you want the {editions.label(wanted).lower()} one")
            else:
                problems.append(f"{editions.label(found['edition'])} edition{why}")
        elif not many_voices_book:
            score -= 15
            reasons.append(f"May be {editions.label(found['edition']).lower()} ({found['reason']})")

    # --- Words that aren't the title, author or series (a different book?) ---
    allowed = title_all | series_words | rel_author_words | {w for n in author_names for w in n} \
        | {w for n in narrator_names for w in n} | NOISE
    extras = [w for w in dict.fromkeys(body_words) if w not in allowed and not w.isdigit() and len(w) > 1]
    if len(extras) > 2:
        penalty = min(21, 3 * (len(extras) - 2))
        score -= penalty
        reasons.append(f"Extra words in the title: {' '.join(extras[:6])}")

    # --- Narrator ---
    if settings.get("auto_match_narrator", True) and narrator_names:
        rel_narrators = result.get("narrators") or []
        rel_narrator_text = ", ".join(rel_narrators) if rel_narrators else result.get("abb_narrator") or ""
        if rel_narrator_text and rel_narrator_text != "Unknown":
            rel_words = set(tokens(rel_narrator_text))
            if {_surname(n) for n in narrator_names} & rel_words:
                score += 10
                reasons.append("Narrator matches")
            else:
                score -= 8
                reasons.append(f"Different narrator: {rel_narrator_text}")

    # --- Format ---
    fmt = (result.get("format") or "").upper()
    format_pref = settings.get("format_preference", "prefer_m4b")
    if fmt == "M4B":
        if format_pref == "prefer_m4b":
            score += 5
        reasons.append("M4B")
    elif format_pref == "m4b_only":
        problems.append(f"Not M4B ({fmt or 'unknown format'})")

    # --- Size against the book's length ---
    runtime = book.get("runtime_min") or 0
    size = result.get("size_bytes") or 0
    if runtime and size:
        # The bitrate the files would have if they're exactly as long as the book. The stated
        # bitrate is often wrong, so it only confirms a fit; the size itself decides problems.
        implied = size * 8 / 1000 / (runtime * 60)
        bitrate = _bitrate(result.get("bitrate"))
        ratio = implied / bitrate if bitrate else None
        if ratio is not None and 0.8 <= ratio <= 1.25:
            score += 10
            reasons.append("Size fits the book's length")
        elif implied < 12:
            score -= 20
            problems.append("Too small for the book's length: abridged or incomplete?")
        elif implied > 640 or ratio is not None and ratio > 2.5:
            score -= 20
            problems.append("Too large for the book's length: several books?")
        elif 24 <= implied <= 400:
            score += 5
            reasons.append("Size is plausible for the book's length")
        else:
            score -= 5
            reasons.append("Size is unusual for the book's length")

    # --- Your release preferences ---
    score += _apply_preferences(result, raw, book, settings, reasons, problems)

    # --- Language ---
    pref_lang = (settings.get("language") or "All").lower()
    lang = (result.get("language") or "").lower()
    if pref_lang != "all" and lang and lang != "unknown" and lang != pref_lang:
        score -= 30
        problems.append(f"Language: {result['language']}")

    score = max(0, min(100, score))
    if problems:
        verdict = "rejected"
    elif score >= 75:
        verdict = "match"
    elif score >= AUTO_MIN_SCORE:
        verdict = "possible"
    else:
        verdict = "weak"
    # Clearly this book: the whole title and the author, and nothing wrong with it
    strong = not problems and coverage == 1 and author_state in ("full", "surname") and score >= AUTO_MIN_SCORE
    return {"score": score, "verdict": verdict, "reasons": reasons, "problems": problems, "strong": strong}


def is_acceptable(evaluation):
    return not evaluation["problems"] and evaluation["score"] >= AUTO_MIN_SCORE


def _is_m4b(result):
    return (result.get("format") or "").upper() == "M4B"


def rank_key(result):
    """Clear matches first, then by score, then M4B, then newest."""
    return (not result.get("strong"), -result.get("score", 0), not _is_m4b(result),
            "".join(chr(255 - ord(c)) for c in result.get("posted") or ""))


# Among clear matches this close to the best, the preferred format wins over a few points
FORMAT_WINDOW = 10


def order(results, settings):
    """Results best first. Once releases are clearly the right book, small score
    differences (a series number in the title) shouldn't beat the preferred format."""
    ranked = sorted(results, key=rank_key)
    if settings.get("format_preference", "prefer_m4b") != "prefer_m4b" or not ranked or not ranked[0].get("strong"):
        return ranked
    top = ranked[0]["score"]
    close = [r for r in ranked if r.get("strong") and r["score"] >= top - FORMAT_WINDOW]
    rest = [r for r in ranked if not any(r is c for c in close)]
    return [r for r in close if _is_m4b(r)] + [r for r in close if not _is_m4b(r)] + rest


def score_results(book, results, settings):
    """Adds score, verdict, reasons and problems to each result; best first."""
    for res in results:
        res.update(evaluate(book, res, settings))
    results[:] = order(results, settings)
    return results


def choose_best(book, results, settings):
    """The best release to grab automatically, or None."""
    acceptable = []
    for res in results:
        ev = evaluate(book, res, settings)
        if is_acceptable(ev):
            acceptable.append((res, {**res, **ev}))
    if not acceptable:
        return None
    best = order([merged for _, merged in acceptable], settings)[0]
    return next(res for res, merged in acceptable if merged is best)


def _setting_list(value):
    return [v.strip() for v in str(value or "").split(",") if v.strip()]


def _name_in(name, text_words):
    """A person's name in a list of names: their surname alone is enough when it's distinctive."""
    words = tokens(name)
    if not words:
        return False
    return all(w in text_words for w in words) or (len(words) > 1 and len(words[-1]) > 3 and words[-1] in text_words
                                                  and words[0][0] in {w[0] for w in text_words})


def _apply_preferences(result, raw, book, settings, reasons, problems):
    """Release preferences from Settings: words, uploaders, narrators, bitrate and size.
    Adds to reasons and problems; returns the change to the score."""
    change = 0
    text = " " + " ".join(tokens(" ".join([raw] + list(result.get("keywords") or []) + list(result.get("categories") or [])))) + " "
    for word in _setting_list(settings.get("blocked_words")):
        if f" {' '.join(tokens(word))} " in text:
            problems.append(f'Contains "{word}" (blocked in Settings)')
    bonus = 0
    for word in _setting_list(settings.get("preferred_words")):
        if f" {' '.join(tokens(word))} " in text:
            bonus += 10
            reasons.append(f'Has "{word}"')
    change += min(bonus, 20)

    uploader = (result.get("uploader") or "").strip()
    if uploader and uploader.lower() in {u.lower() for u in _setting_list(settings.get("blocked_uploaders"))}:
        problems.append(f"Shared by {uploader} (blocked in Settings)")

    narrators = result.get("narrators") or []
    narrator_text = ", ".join(narrators) if narrators else (result.get("abb_narrator") or "")
    if narrator_text and narrator_text != "Unknown":
        words = set(tokens(narrator_text))
        if any(_name_in(n, words) for n in _setting_list(settings.get("pref_narrators"))):
            change += 15
            reasons.append("A narrator you prefer")
        if any(_name_in(n, words) for n in _setting_list(settings.get("avoid_narrators"))):
            change -= 25
            reasons.append("A narrator you avoid")

    min_bitrate = settings.get("min_bitrate") or 0
    if min_bitrate:
        stated = _bitrate(result.get("bitrate"))
        size, runtime = result.get("size_bytes") or 0, book.get("runtime_min") or 0
        implied = size * 8 / 1000 / (runtime * 60) if size and runtime else 0
        # The stated bitrate, else the one the size implies (with some leeway)
        if (stated and stated < min_bitrate) or (not stated and implied and implied < min_bitrate * 0.85):
            problems.append(f"{round(stated or implied)} kbps, below your minimum of {min_bitrate}")

    max_gb = settings.get("max_size_gb") or 0
    size = result.get("size_bytes") or 0
    if max_gb and size > max_gb * 1024 ** 3:
        problems.append(f"{size / 1024 ** 3:.1f} GB, over your maximum of {max_gb:g} GB")
    return change
