"""Scanning an existing audiobook folder and matching it against the library."""
import json
import logging
import os
import re
import struct

from app.editions import classify_local, edition_of

logger = logging.getLogger(__name__)

# .mp4 and .mka are audio-only containers some releases use (e.g. "001 Author (2020) Title.mp4")
AUDIO_EXTENSIONS = {'.mp3', '.m4b', '.m4a', '.mp4', '.flac', '.ogg', '.opus', '.aac', '.wma', '.mka'}
COVER_NAMES = ["cover.jpg", "cover.jpeg", "cover.png", "folder.jpg", "folder.png"]
IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp'}

# Subfolders that are parts of one book rather than separate books ("CD1", "Disc 2", "Part 3")
DISC_FOLDER_RE = re.compile(r'^(cd|disc|disk|part)\s*\d+', re.IGNORECASE)
# Folders created by NAS software that never hold books
SKIP_FOLDERS = {"@eadir", "#recycle", "$recycle.bin", ".trash", "lost+found"}
# System folders that are never scanned, even if a scan is started above them
SYSTEM_FOLDERS = {"/", "/proc", "/sys", "/dev", "/run", "/tmp", "/etc", "/usr", "/bin", "/sbin",
                  "/lib", "/lib64", "/boot", "/var", "/opt", "/srv", "/root", "/app", "/config"}


def is_system_folder(path):
    return os.path.normpath(path).replace("\\", "/") in SYSTEM_FOLDERS


def is_unsafe_folder(path):
    """A system folder or the top of a drive: never a book's folder, scanned or moved."""
    if not path:
        return False
    full = os.path.normpath(os.path.abspath(path))
    return is_system_folder(path) or is_system_folder(full) or os.path.dirname(full) == full

DEFAULT_NAMING_FORMAT = "{Author} - {Series} {SeriesNumber} - {Title}"
# Added after the title for dramatized and abridged books when the format has no {Edition},
# so two editions of a book never share a folder
EDITION_SUFFIX = {"abridged": "(Abridged)", "dramatized": "(Abridged)"}


def display_name(text):
    """A readable version of a file or folder name.

    On Linux, names that aren't valid UTF-8 (typically Windows-1252 names on a share
    mounted without iocharset=utf8, like "Babylon\x92s Ashes") arrive with undecodable
    bytes kept as surrogates. Those are fine for opening files but not for showing or
    searching; decode them as Windows-1252 instead."""
    if not isinstance(text, str) or not any("\udc80" <= ch <= "\udcff" for ch in text):
        return text
    raw = text.encode("utf-8", "surrogateescape")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


def format_sequence(seq):
    """'07' -> '7', '4.0' -> '4', '3.5' -> '3.5'; anything else is returned as-is."""
    if seq is None:
        return ""
    text = str(seq).strip().lstrip("#")
    try:
        num = float(text)
        return str(int(num)) if num.is_integer() else str(num)
    except ValueError:
        return text


def series_key(name):
    """Groups series names: "The Name" and "Name Series" are the same series."""
    return normalize(re.sub(r"\s+series$", "", name or "", flags=re.IGNORECASE))


def _seq_number(seq):
    try:
        return float(str(seq).lstrip("#"))
    except (TypeError, ValueError):
        return None


def series_entries(book):
    """All series a book belongs to, as [{"name", "asin", "sequence"}]. Older entries only
    have the single series/sequence/series_asin fields."""
    entries = [e for e in book.get("series_list") or [] if e.get("name")]
    if not entries and book.get("series"):
        entries = [{"name": book["series"], "asin": book.get("series_asin", ""), "sequence": book.get("sequence", "")}]
    return entries


def pick_main_series(entries):
    """The most specific series, used for folder names and card subtitles: a numbered
    series beats an unnumbered umbrella ("The Universe"), and the lowest number wins
    ("Sub-Series #1" over "Main Saga #4")."""
    numbered = [e for e in entries if _seq_number(e.get("sequence")) is not None]
    if numbered:
        return min(numbered, key=lambda e: _seq_number(e["sequence"]))
    return entries[0] if entries else {}


def merge_series_lists(*lists):
    """Combines series lists, one entry per series (matched by Audible id or name),
    keeping the Audible id and number wherever one list has them."""
    merged = []
    for entries in lists:
        for entry in entries or []:
            if not entry.get("name"):
                continue
            entry = {"name": entry["name"], "asin": entry.get("asin", ""), "sequence": format_sequence(entry.get("sequence", ""))}
            same = next((m for m in merged if (entry["asin"] and m["asin"] == entry["asin"])
                         or series_key(m["name"]) == series_key(entry["name"])), None)
            if same is None:
                merged.append(entry)
                continue
            for key in ("asin", "sequence"):
                if entry[key] and not same[key]:
                    same[key] = entry[key]
    return merged


def main_series_fields(entries):
    """The single-series fields kept on a book for its main series."""
    main = pick_main_series(entries)
    return {"series_list": entries, "series": main.get("name", ""),
            "sequence": main.get("sequence", ""), "series_asin": main.get("asin", "")}


def normalize(text):
    text = (text or "").lower()
    text = re.sub(r"^(the|a|an)\s+", "", text)
    return re.sub(r"[^a-z0-9]+", "", text)


# Edition words in titles ("Book Title Dramatized", "Book - Graphic Audio")
_EDITION_WORDS = re.compile(r"\b(dramati[sz]ed|adaptation|graphic\s?audio|full[\s-]?cast|(un)?abridged)\b", re.IGNORECASE)


# A bracketed edition tag in a title: "(Dramatized Adaptation)", "[Full Cast]", "(Abridged)"
_EDITION_TAG = re.compile(r"\s*[\(\[][^\)\]]*(?:dramati[sz]|adaptation|full[\s-]?cast|graphic\s?audio|abridged)[^\)\]]*[\)\]]",
                          re.IGNORECASE)


def title_keys(title):
    """(whole title, {whole title, the part before a colon, the part after it}), without
    bracketed tags or edition words: "Book Title (Dramatized Adaptation): Series, Book 1"
    -> ("booktitleseriesbook1", {"booktitleseriesbook1", "booktitle", "seriesbook1"})."""
    plain = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", "", title or "")
    plain = _EDITION_WORDS.sub("", plain).strip(" -") or (title or "")
    main, _, rest = plain.partition(":")
    full = normalize(plain)
    return full, {k for k in (full, normalize(main.strip()), normalize(rest.strip())) if k}


def title_key(title):
    """The whole title for grouping: "Series: Title" and "Series: Other Title" differ."""
    return title_keys(title)[0]


def titles_match(a, b):
    """The same book's title: equal, or one is the other's main title or subtitle part
    ("Book Title" / "Book Title: Ember Saga, Book 1", "First Light" /
    "Ember Saga: First Light"), but not two books of a series ("Series: One" /
    "Series: Two")."""
    full_a, keys_a = title_keys(a)
    full_b, keys_b = title_keys(b)
    return bool(full_a and full_b) and (full_a in keys_b or full_b in keys_a)


def primary_author(authors):
    return (authors or "").split(",")[0].split("&")[0].strip()


# "Series Name 3.5", "Series Name Book 1", "Series Name, Vol. 07", "Series Name #1"
_SERIES_WITH_NUMBER = re.compile(
    r'^(?P<series>.+?)[\s,]*(?:book|vol\.?|volume|no\.?|#)?\s*(?P<seq>\d+(?:\.\d+)?)$', re.IGNORECASE)
_BARE_NUMBER = re.compile(r'^(?:book|vol\.?|volume|no\.?|#)?\s*(\d+(?:\.\d+)?)$', re.IGNORECASE)


# "Book 2 Title Part 1 of 2 Series GA", "Vol. 3 - Title (2 of 3)"
_NUMBERED_NAME = re.compile(r"^(?:book|bk|vol(?:ume)?)\.?[\s._-]*0*(?P<num>\d{1,3}(?:\.\d)?)[\s._-]+(?P<rest>.+)$", re.IGNORECASE)
_NAME_PART = re.compile(r"[\s._\-(\[]*(?:part|pt)[\s._-]*0*(?P<part>\d+)(?:\s*of\s*(?P<of>\d+))?[)\]]?"
                        r"|[\s._\-(\[]+0*(?P<part2>\d+)\s+of\s+(?P<of2>\d+)[)\]]?", re.IGNORECASE)


def parse_numbered_name(name):
    """ "Book 1 Title Part 1 of 2 Series GA" -> {"sequence": "1", "title": "Title (Part 1 of 2)"}:
    the book's number, its title, and which part it is when it's sold in parts. None when the
    name doesn't start with a book number."""
    m = _NUMBERED_NAME.match((name or "").strip())
    if not m:
        return None
    rest = m.group("rest")
    part = _NAME_PART.search(rest)
    title = re.sub(r"\s+", " ", re.sub(r"[._]+", " ", rest[:part.start()] if part else rest)).strip(" -_[](),")
    if not title:
        return None
    if part and (part.group("of") or part.group("of2")):
        title += f" (Part {int(part.group('part') or part.group('part2'))} of {int(part.group('of') or part.group('of2'))})"
    return {"sequence": format_sequence(m.group("num")), "title": title}


def parse_folder_name(name, author=None):
    """Parses 'Author - [Series N -] Title' style folder names. When the author is already
    known (nested Author/Title folders), the name is parsed as '[Series N -] Title'."""
    parts = [p.strip() for p in re.split(r'\s+-\s+', name) if p.strip()]
    info = {"authors": author or "", "title": name.strip(), "series": "", "sequence": ""}
    if author is None:
        if len(parts) < 2:
            return info
        info["authors"] = parts[0]
        parts = parts[1:]
    if not parts:
        return info

    info["title"] = parts[-1]
    middle = parts[:-1]

    # Scan the middle parts from the end for a series name and number
    for i in range(len(middle) - 1, -1, -1):
        bare = _BARE_NUMBER.match(middle[i])
        if bare and i > 0:
            # "Series Name - #3": the number belongs to the part before it
            info["series"], info["sequence"] = middle[i - 1], format_sequence(bare.group(1))
            return info
        with_number = _SERIES_WITH_NUMBER.match(middle[i])
        if with_number:
            info["series"] = with_number.group("series").strip(" ,")
            info["sequence"] = format_sequence(with_number.group("seq"))
            return info
    if middle:
        info["series"] = middle[-1]
    return info


# Contributors listed among the authors: "Danusia Stok - translator"
_CONTRIBUTOR_ROLE = re.compile(r"\s-\s*(translator|editor|foreword|introduction|afterword|contributor|"
                               r"illustrator|adapter|adaptation|preface|compiler)\b", re.IGNORECASE)


def _names(value, skip_contributors=False):
    """Audiobookshelf stores people as ["Name"] or [{"name": "Name"}]."""
    if isinstance(value, str):
        return value
    names = []
    for item in value or []:
        name = item.get("name") if isinstance(item, dict) else item
        if name and not (skip_contributors and _CONTRIBUTOR_ROLE.search(str(name))):
            names.append(str(name).strip())
    return ", ".join(names)


def read_abs_metadata(folder):
    """Reads an Audiobookshelf metadata.json if the folder has one."""
    path = os.path.join(folder, "metadata.json")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        logger.warning(f"Could not read {path}: {e}")
        return None
    if not isinstance(data, dict) or not data.get("title"):
        return None

    # Audiobookshelf can list several series: ["Universe", "Saga #4", "Sub-series #1"]
    entries = []
    raw_series = data.get("series") or []
    if isinstance(raw_series, (str, dict)):
        raw_series = [raw_series]
    for item in raw_series:
        if isinstance(item, dict):
            name, sequence = item.get("name", ""), item.get("sequence", "")
        else:
            # "Series Name #3.5"
            name, _, sequence = str(item).rpartition(" #")
            if not name:
                name, sequence = str(item), ""
        if name and name.strip():
            entries.append({"name": name.strip(), "asin": "", "sequence": format_sequence(sequence)})

    release_date = data.get("publishedDate") or data.get("publishedYear") or ""
    return {
        "title": str(data["title"]).strip(),
        "subtitle": str(data.get("subtitle") or ""),
        "authors": _names(data.get("authors"), skip_contributors=True),
        "narrators": _names(data.get("narrators")),
        **main_series_fields(entries),
        "asin": data.get("asin") or "",
        "release_date": str(release_date),
        "description": data.get("description") or "",
        "publisher": str(data.get("publisher") or ""),
        # Used to tell the edition
        "abridged": data.get("abridged") is True,
        "genres": [str(g) for g in data.get("genres") or [] if g],
        "tags": [str(t) for t in data.get("tags") or [] if t],
    }


def _find_cover(folder, files):
    lower = {f.lower(): f for f in files}
    for name in COVER_NAMES:
        if name in lower:
            return lower[name]
    images = sorted(f for f in files if os.path.splitext(f)[1].lower() in IMAGE_EXTENSIONS)
    return images[0] if images else ""


def audio_files(path):
    """Audio files of a book (a folder, or a single file), as (full path, size) pairs."""
    if os.path.isfile(path):
        return [(path, os.path.getsize(path))] if os.path.splitext(path)[1].lower() in AUDIO_EXTENSIONS else []
    found = []
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d.lower() not in SKIP_FOLDERS]
        for f in files:
            if os.path.splitext(f)[1].lower() in AUDIO_EXTENSIONS:
                full = os.path.join(root, f)
                try:
                    found.append((full, os.path.getsize(full)))
                except OSError:
                    pass
    return sorted(found)


def _fingerprint(files):
    import hashlib
    parts = sorted(f"{os.path.basename(f).lower()}:{size}" for f, size in files)
    return hashlib.sha1("|".join(parts).encode("utf-8", "replace")).hexdigest()[:16] if parts else ""


def fingerprint(path):
    """The book's audio files by name and size: the same files in another folder (renamed or
    moved) have the same fingerprint."""
    return _fingerprint(audio_files(path))


def describe_files(path):
    files = audio_files(path)
    formats = sorted({os.path.splitext(f)[1].lstrip(".").upper() for f, _ in files})
    return {
        "file_count": len(files),
        "size_bytes": sum(size for _, size in files),
        "format": ", ".join(formats),
        "fingerprint": _fingerprint(files),
    }


def _make_candidate(path, name, cover="", parents=()):
    meta = read_abs_metadata(path) if os.path.isdir(path) else None
    name = display_name(name)
    parents = tuple(display_name(p) for p in parents)
    if parents:
        # Nested layouts: Author/Title or Author/Series/Title
        parsed = parse_folder_name(name, author=parents[0])
        if len(parents) >= 2 and not parsed["series"]:
            with_number = _SERIES_WITH_NUMBER.match(parents[-1])
            parsed["series"] = with_number.group("series").strip(" ,") if with_number else parents[-1]
    else:
        parsed = parse_folder_name(name)
    # "Vol. 4 - Title" isn't by an author called "Vol. 4"
    if not parsed["authors"] or parsed["title"] == name.strip() or _BARE_NUMBER.match(parsed["authors"]):
        numbered = parse_numbered_name(name)
        if numbered:
            parsed.update(title=numbered["title"], sequence=numbered["sequence"])
            if _BARE_NUMBER.match(parsed["authors"]):
                parsed["authors"] = ""
    # A loose file: its folder's name may say the author and edition ("Author - Series GraphicAudio")
    folder = path if os.path.isdir(path) else os.path.dirname(path)
    if not os.path.isdir(path) and not parsed["authors"]:
        parsed["authors"] = parse_folder_name(display_name(os.path.basename(folder)))["authors"]
    parsed_series = [{"name": parsed["series"], "asin": "", "sequence": parsed["sequence"]}] if parsed["series"] else []
    if meta:
        info = meta
        # metadata.json sometimes lacks fields the folder name has
        if not info.get("authors") and parsed.get("authors"):
            info["authors"] = parsed["authors"]
        if not info.get("series_list"):
            info.update(main_series_fields(parsed_series))
        source = "metadata.json"
    else:
        info = {**parsed, **main_series_fields(parsed_series), "narrators": "", "asin": "", "release_date": "", "description": ""}
        # "Book 2 Title": the number helps matching on Audible even without a series name
        info["sequence"] = info["sequence"] or parsed["sequence"]
        source = "folder name"
    found = classify_local(meta, [f for f, _ in audio_files(path)], folder)
    return {**info, "path": path, "cover": cover, "source": source, **describe_files(path),
            "edition": found["edition"], "edition_reason": found["reason"], "edition_check": not found["sure"]}


def scan_library(root):
    """Finds books under root. A book is a folder that holds audio files (directly or in
    CD/Disc subfolders), or a single audio file sitting loose in a folder of folders."""
    root = os.path.abspath(root)
    candidates = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d.lower() not in SKIP_FOLDERS
                             and not is_system_folder(os.path.join(dirpath, d)))
        has_audio = any(os.path.splitext(f)[1].lower() in AUDIO_EXTENSIONS for f in filenames)
        disc_dirs = [d for d in dirnames if DISC_FOLDER_RE.match(d)]

        if dirpath != root and (has_audio or (disc_dirs and len(disc_dirs) == len(dirnames))):
            parents = tuple(os.path.relpath(dirpath, root).split(os.sep)[:-1])
            candidates.append(_make_candidate(dirpath, os.path.basename(dirpath),
                                              _find_cover(dirpath, filenames), parents))
            dirnames[:] = []  # Everything below belongs to this book
            continue

        if dirpath == root:
            # Loose audio files in the root: each one is a single-file book
            for f in sorted(filenames):
                stem, ext = os.path.splitext(f)
                if ext.lower() in AUDIO_EXTENSIONS:
                    candidates.append(_make_candidate(os.path.join(dirpath, f), stem))
    return candidates


def same_path(a, b):
    if not a or not b:
        return False
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


def author_keys(authors):
    keys = {normalize(a) for a in (authors or "").split(",") if normalize(a)}
    return keys or {""}


# Releases sold in parts: "Book Title (Part 1 of 2)", "Book Title (1 of 3)"
_PART_OF = re.compile(r"[\(\[]\s*(?:part\s+)?(\d+)\s+of\s+(\d+)\s*[\)\]]", re.IGNORECASE)


def part_number(title):
    """The part a release is of a book sold in parts, or None."""
    m = _PART_OF.search(title or "")
    return int(m.group(1)) if m else None


def part_count(title):
    """How many parts a book sold in parts has, or None."""
    m = _PART_OF.search(title or "")
    return int(m.group(2)) if m else None


def find_match(library, book):
    """Finds the library entry for a book by path, ASIN, or title + a shared author in the
    same edition (a dramatized version is a separate entry from the narrated one)."""
    for entry in library:
        if same_path(entry.get("path"), book.get("path")):
            return entry
    asin = book.get("asin")
    if asin:
        for entry in library:
            if entry.get("asin") == asin:
                return entry
    ga_url = book.get("ga_url")  # A GraphicAudio release Audible doesn't sell
    if ga_url:
        for entry in library:
            if entry.get("ga_url") == ga_url:
                return entry
    title = book.get("title")
    authors = author_keys(book.get("authors"))
    edition = edition_of(book)
    if not title_key(title):
        return None
    # Each part of a book sold in parts is its own release
    part = book.get("part") or part_number(title)
    narrators = author_keys(book.get("narrators")) - {""}

    def other_recording(e):
        """Two Audible products with different narrators: e.g. a new narration of the same
        book (regional duplicates of one recording share the narrator, and stay one book)."""
        theirs = author_keys(e.get("narrators")) - {""}
        return bool(asin and e.get("asin") and e["asin"] != asin and narrators and theirs and not narrators & theirs)

    same = [e for e in library if author_keys(e.get("authors")) & authors and edition_of(e) == edition
            and part_number(e.get("title")) == part and not other_recording(e)]
    # The exact title first, then a title that's this one with or without its subtitle
    return next((e for e in same if title_key(e.get("title")) == title_key(title)), None) or \
        next((e for e in same if titles_match(e.get("title"), title)), None)


def find_import_match(library, book):
    """find_match for a folder being imported: the entry for this folder, or a tracked book
    not on disk yet (e.g. Monitored) to link it to. A book already on disk in another
    folder is a second copy, so it isn't matched."""
    for entry in library:
        if same_path(entry.get("path"), book.get("path")):
            return entry
    elsewhere = lambda e: e.get("path") and not same_path(e["path"], book.get("path")) and os.path.exists(e["path"])
    return find_match([e for e in library if not elsewhere(e)], book)


def _mp4_sample_seconds(path):
    """An MP4's audio length counted from its sample table, or None."""
    from mutagen.mp4 import Atoms

    try:
        with open(path, "rb") as f:
            atoms = Atoms(f)
            for trak in atoms[b"moov"].findall(b"trak"):
                ok, hdlr = trak[b"mdia", b"hdlr"].read(f)
                if not ok or hdlr[8:12] != b"soun":
                    continue
                ok, mdhd = trak[b"mdia", b"mdhd"].read(f)
                ok_stts, stts = trak[b"mdia", b"minf", b"stbl", b"stts"].read(f)
                if not ok or not ok_stts:
                    return None
                unit = struct.unpack(">I", mdhd[20:24] if mdhd[0] == 1 else mdhd[12:16])[0]
                count = struct.unpack(">I", stts[4:8])[0]
                samples = sum(n * delta for n, delta in struct.iter_unpack(">II", stts[8:8 + 8 * count]))
                return samples / unit if unit and samples else None
    except Exception:
        return None
    return None


def audio_length(path, audio):
    """Play time in seconds of a file mutagen has read, or 0. Some encoders write an M4B's
    length in 32 bits, which wraps after about 27 hours at 44.1 kHz (a 31-hour book reads
    as 4); the sample table's count doesn't wrap."""
    from mutagen.mp4 import MP4

    length = getattr(getattr(audio, "info", None), "length", 0) or 0
    if isinstance(audio, MP4):
        counted = _mp4_sample_seconds(path)
        if counted and counted > length + 60:
            return counted
    return length


def total_duration_min(paths):
    """Total play time of audio files in minutes, or None if any file can't be read."""
    import mutagen  # Only needed for download checks

    total = 0.0
    for path in paths:
        try:
            audio = mutagen.File(path)
        except Exception:
            audio = None
        length = audio_length(path, audio) if audio is not None else 0
        if not length:
            logger.warning(f"Could not read the duration of {path}")
            return None
        total += length
    return round(total / 60)


def file_duration_min(path):
    """One audio file's play time in minutes, or None if it can't be read."""
    import mutagen

    try:
        audio = mutagen.File(path)
    except Exception:
        return None
    length = audio_length(path, audio) if audio is not None else 0
    return length / 60 if length else None


# Codec and bitrate tags in file names: "Book_AAC-LC.m4b", "Book [xHE-AAC].m4b", "Book 64k.mp3"
_ENCODING_TAG = re.compile(r"(?<![a-z0-9])(x?he[-_ .]?aac(?:[-_ .]?v2)?|aac[-_ .]?lc|usac|aac|opus|mp3|flac|\d{2,3}[-_ .]?k(?:bps)?)(?![a-z0-9])",
                           re.IGNORECASE)


def _encoding_variant(rel):
    """ "08_Book_[ID]_xHE-AAC.m4b" -> ("08bookid", (("xheaac",), ".m4b"))"""
    stem, ext = os.path.splitext(rel)
    tags = tuple(re.sub(r"[-_ .]", "", t.lower()) for t in _ENCODING_TAG.findall(stem))
    return normalize(_ENCODING_TAG.sub(" ", stem)), (tags, ext.lower())


def _variant_rank(variant, size):
    """Which copy to keep: widely playable AAC in M4B first, then the bigger (better) one.
    Many players, browsers included, can't decode xHE-AAC."""
    tags, ext = variant
    joined = " ".join(tags)
    rank = 4 if "xheaac" in joined or "usac" in joined else 1 if "heaac" in joined else 0
    rank += {".m4b": 0, ".m4a": 0, ".mp3": 1}.get(ext, 2)
    return rank, -size


# A part of a book sold in parts, in a file or folder name: "Part 2", "Pt. 2", "(2 of 3)"
_NAMED_PART = re.compile(r"(?<![a-z])(?:part|pt)\.?[\s_-]*0*(\d{1,2})(?!\d)|[\(\[]\s*0*(\d{1,2})\s+of\s+\d{1,2}\s*[\)\]]",
                         re.IGNORECASE)


def _named_part(rel):
    """The part a file says it is (its name, else its folder's), or None."""
    *folders, name = rel.replace("\\", "/").split("/")
    for piece in [os.path.splitext(name)[0]] + folders[::-1]:
        m = _NAMED_PART.search(piece)
        if m:
            return int(m.group(1) or m.group(2))
    return None


def pick_part(base, audio_rels, title, expected_min=0, tolerance=10):
    """For a book sold in parts ("Book Title (Part 1 of 2)"), its own part's files from a
    download holding several parts. Only when every file says which part it is, they are
    all parts of this book, and the chosen files run as long as the part should: track
    files merely numbered "Part 01".."Part 25" are left alone."""
    want, count = part_number(title), part_count(title)
    if not want or not count or not expected_min or len(audio_rels) < 2:
        return audio_rels
    parts = {rel: _named_part(rel) for rel in audio_rels}
    numbers = set(parts.values())
    if None in numbers or len(numbers) < 2 or max(numbers) > count or want not in numbers:
        return audio_rels
    chosen = [rel for rel, p in parts.items() if p == want]
    durations = [file_duration_min(os.path.join(base, rel)) for rel in chosen]
    if any(not d for d in durations) or abs(sum(durations) - expected_min) / expected_min * 100 > tolerance:
        return audio_rels
    logger.info(f"The download holds parts {', '.join(map(str, sorted(numbers)))}; importing part {want}")
    return chosen


def pick_one_copy(base, audio_rels, expected_min=0, tolerance=10):
    """Some releases hold the same recording more than once, in different encodings
    ("Book_AAC-LC.m4b" and "Book_xHE-AAC.m4b"). Keeps one copy: files named alike apart
    from codec tags, or (with Audible's runtime) files that are each the whole book."""
    if len(audio_rels) < 2:
        return audio_rels

    def size(rel):
        try:
            return os.path.getsize(os.path.join(base, rel))
        except OSError:
            return 0

    by_variant = {}
    for rel in audio_rels:
        name, variant = _encoding_variant(rel)
        by_variant.setdefault(variant, []).append((name, rel))
    if len(by_variant) >= 2:
        names = [sorted(n for n, _ in files) for files in by_variant.values()]
        if all(n == names[0] for n in names):
            best = min(by_variant, key=lambda v: _variant_rank(v, sum(size(r) for _, r in by_variant[v])))
            kept = [rel for _, rel in by_variant[best]]
            logger.info(f"The download has the same audio in {len(by_variant)} encodings; importing {', '.join(kept)}")
            return kept

    # Each file is the whole book on its own: copies, not parts. Files numbered alike
    # ("Part 1", "Part 2") are parts however long each is (a book sold in parts)
    stems = [_encoding_variant(os.path.basename(rel))[0] for rel in audio_rels]
    numbered_alike = len(set(stems)) > 1 and len({re.sub(r"\d+", "#", n) for n in stems}) == 1
    if expected_min and not numbered_alike:
        durations = [file_duration_min(os.path.join(base, rel)) for rel in audio_rels]
        if all(d and abs(d - expected_min) / expected_min * 100 <= tolerance for d in durations):
            best = min(audio_rels, key=lambda rel: _variant_rank(_encoding_variant(rel)[1], size(rel)))
            logger.info(f"Each of the {len(audio_rels)} audio files is the whole book; importing {best}")
            return [best]
    return audio_rels


def _natural_key(path):
    """Sorts 'CD2/01.mp3' before 'CD10/01.mp3' and 'Part 9' before 'Part 10'."""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", path.replace("\\", "/"))]


NAME_BYTES = 200  # Under the 255 bytes most filesystems allow, leaving room for " - Part 01.mp3"


def fit_name(text, limit=NAME_BYTES):
    """A file or folder name cut to fit the filesystem (at a character boundary)."""
    raw = (text or "").encode("utf-8")
    if len(raw) <= limit:
        return text or ""
    return raw[:limit].decode("utf-8", "ignore").rstrip(" .-_,")


def safe_filename(text):
    return fit_name(re.sub(r'[\\/:*?"<>|]', "", text or "").strip(" .")).strip(" .")


def _safe_to_copy(path, extensions):
    """A download's file that may be copied into the library. A symbolic link (some
    cross-seeding setups use them) only when it leads to a file of the same kind outside
    BorgArr's config folder: "book.mp3" pointing at the database would otherwise put it in
    the library, where Audiobookshelf shows it."""
    if not os.path.islink(path):
        return True
    from app import db  # (db imports this module)
    real = os.path.realpath(path)
    config = os.path.realpath(db.CONFIG_DIR)
    inside_config = real == config or real.startswith(config.rstrip(os.sep) + os.sep)
    ok = os.path.isfile(real) and os.path.splitext(real)[1].lower() in extensions and not inside_config
    if not ok:
        logger.warning(f"Not importing {path}: it's a link to {real}")
    return ok


def plan_import_files(content_path, title, rename=True, expected_min=0, tolerance=10, only=None):
    """Decides where a download's files go inside the book folder.

    Returns (audio, cover): audio is a list of (source, relative destination) in play order,
    cover is one (source, 'cover.ext') pair or None. With rename on, audio files become
    'Title.ext' or 'Title - Part 01.ext'; with it off they keep their names and subfolders,
    so files that share a name on different discs don't collide. When the download has
    the same audio in several encodings, only one copy is planned (see pick_one_copy).
    With only (relative paths), just those audio files are planned: one book of a pack.
    """
    if os.path.isfile(content_path):
        base = os.path.dirname(content_path)
        name = os.path.basename(content_path)
        ok = os.path.splitext(name)[1].lower() in AUDIO_EXTENSIONS and _safe_to_copy(content_path, AUDIO_EXTENSIONS)
        audio_rels = [name] if ok else []
        image_rels = []
    else:
        base = content_path
        audio_rels, image_rels = [], []
        for root, dirs, files in os.walk(content_path):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d.lower() not in SKIP_FOLDERS]
            for f in files:
                rel = os.path.relpath(os.path.join(root, f), base)
                ext = os.path.splitext(f)[1].lower()
                if ext in AUDIO_EXTENSIONS and _safe_to_copy(os.path.join(root, f), AUDIO_EXTENSIONS):
                    audio_rels.append(rel)
                elif ext in IMAGE_EXTENSIONS and _safe_to_copy(os.path.join(root, f), IMAGE_EXTENSIONS):
                    image_rels.append(rel)

    if only is not None:
        wanted = {os.path.normpath(r) for r in only}
        audio_rels = [r for r in audio_rels if os.path.normpath(r) in wanted]
    audio_rels = pick_part(base, audio_rels, title, expected_min, tolerance)
    audio_rels = pick_one_copy(base, audio_rels, expected_min, tolerance)
    audio_rels.sort(key=_natural_key)
    stem = safe_filename((title or "").split(":")[0]) or "Audiobook"
    width = max(2, len(str(len(audio_rels))))
    audio = []
    for i, rel in enumerate(audio_rels, 1):
        ext = os.path.splitext(rel)[1].lower()
        if not rename:
            dest = rel
        elif len(audio_rels) == 1:
            dest = f"{stem}{ext}"
        else:
            dest = f"{stem} - Part {i:0{width}d}{ext}"
        audio.append((os.path.join(base, rel), dest))

    cover = None
    if image_rels:
        image_rels.sort(key=lambda r: (os.path.basename(r).lower() not in COVER_NAMES, _natural_key(r)))
        src = image_rels[0]
        cover = (os.path.join(base, src), "cover" + os.path.splitext(src)[1].lower())
    return audio, cover


def build_folder_name(template, book):
    """Fills the naming template; segments left empty (e.g. no series) are dropped."""
    edition = EDITION_SUFFIX.get(book.get("edition") or "", "")
    name = template or DEFAULT_NAMING_FORMAT
    if edition and "{Edition}" not in name:
        name += " {Edition}"
    values = {
        "{Author}": primary_author(book.get("authors")),
        "{Authors}": book.get("authors") or "",
        # "Title (Dramatized Adaptation)" already says what the edition suffix would
        "{Title}": (_EDITION_TAG.sub("", book.get("title") or "").strip() if edition else (book.get("title") or "").strip()),
        "{Series}": book.get("series") or "",
        # A number with no series name to go with it says nothing
        "{SeriesNumber}": format_sequence(book.get("sequence")) if book.get("series") else "",
        "{Year}": (book.get("release_date") or "")[:4],
        "{Edition}": edition,
    }
    for token, value in values.items():
        name = name.replace(token, value)
    segments = [re.sub(r"\s+", " ", s).strip() for s in name.split(" - ")]
    name = " - ".join(s for s in segments if s)
    name = fit_name(re.sub(r'[\\/:*?"<>|]', "", name).strip(" ."))
    return name or fit_name(re.sub(r'[\\/:*?"<>|]', "", book.get("title") or "Unknown"))
