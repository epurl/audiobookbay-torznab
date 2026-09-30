"""Scanning an existing audiobook folder and matching it against the library."""
import json
import logging
import os
import re

logger = logging.getLogger(__name__)

AUDIO_EXTENSIONS = {'.mp3', '.m4b', '.m4a', '.flac', '.ogg', '.opus', '.aac', '.wma'}
COVER_NAMES = ["cover.jpg", "cover.jpeg", "cover.png", "folder.jpg", "folder.png"]
IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp'}

# Subfolders that are parts of one book rather than separate books ("CD1", "Disc 2", "Part 3")
DISC_FOLDER_RE = re.compile(r'^(cd|disc|disk|part)\s*\d+', re.IGNORECASE)
# Folders created by NAS software that never hold books
SKIP_FOLDERS = {"@eadir", "#recycle", "$recycle.bin", ".trash", "lost+found"}

DEFAULT_NAMING_FORMAT = "{Author} - {Series} {SeriesNumber} - {Title}"


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


def normalize(text):
    text = (text or "").lower()
    text = re.sub(r"^(the|a|an)\s+", "", text)
    return re.sub(r"[^a-z0-9]+", "", text)


def primary_author(authors):
    return (authors or "").split(",")[0].split("&")[0].strip()


# "The Expanse 3.5", "The Dresden Files Book 1", "Solo Leveling, Vol. 07", "Darth Bane #1"
_SERIES_WITH_NUMBER = re.compile(
    r'^(?P<series>.+?)[\s,]*(?:book|vol\.?|volume|no\.?|#)?\s*(?P<seq>\d+(?:\.\d+)?)$', re.IGNORECASE)
_BARE_NUMBER = re.compile(r'^(?:book|vol\.?|volume|no\.?|#)?\s*(\d+(?:\.\d+)?)$', re.IGNORECASE)


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
            # "Prelude to Dune - #3": the number belongs to the part before it
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


def _names(value):
    """Audiobookshelf stores people as ["Name"] or [{"name": "Name"}]."""
    if isinstance(value, str):
        return value
    names = []
    for item in value or []:
        name = item.get("name") if isinstance(item, dict) else item
        if name:
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

    series, sequence = "", ""
    raw_series = data.get("series") or []
    if isinstance(raw_series, (str, dict)):
        raw_series = [raw_series]
    if raw_series:
        first = raw_series[0]
        if isinstance(first, dict):
            series, sequence = first.get("name", ""), first.get("sequence", "")
        else:
            # "The Expanse #3.5"
            series, _, sequence = str(first).rpartition(" #")
            if not series:
                series, sequence = str(first), ""

    release_date = data.get("publishedDate") or data.get("publishedYear") or ""
    return {
        "title": str(data["title"]).strip(),
        "authors": _names(data.get("authors")),
        "narrators": _names(data.get("narrators")),
        "series": (series or "").strip(),
        "sequence": format_sequence(sequence),
        "asin": data.get("asin") or "",
        "release_date": str(release_date),
        "description": data.get("description") or "",
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


def describe_files(path):
    files = audio_files(path)
    formats = sorted({os.path.splitext(f)[1].lstrip(".").upper() for f, _ in files})
    return {
        "file_count": len(files),
        "size_bytes": sum(size for _, size in files),
        "format": ", ".join(formats),
    }


def _make_candidate(path, name, cover="", parents=()):
    meta = read_abs_metadata(path) if os.path.isdir(path) else None
    if parents:
        # Nested layouts: Author/Title or Author/Series/Title
        parsed = parse_folder_name(name, author=parents[0])
        if len(parents) >= 2 and not parsed["series"]:
            with_number = _SERIES_WITH_NUMBER.match(parents[-1])
            parsed["series"] = with_number.group("series").strip(" ,") if with_number else parents[-1]
    else:
        parsed = parse_folder_name(name)
    if meta:
        info = meta
        # metadata.json sometimes lacks fields the folder name has
        for key in ("authors", "series", "sequence"):
            if not info.get(key) and parsed.get(key):
                info[key] = parsed[key]
        source = "metadata.json"
    else:
        info = {**parsed, "narrators": "", "asin": "", "release_date": "", "description": ""}
        source = "folder name"
    return {**info, "path": path, "cover": cover, "source": source, **describe_files(path)}


def scan_library(root):
    """Finds books under root. A book is a folder that holds audio files (directly or in
    CD/Disc subfolders), or a single audio file sitting loose in a folder of folders."""
    root = os.path.abspath(root)
    candidates = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d.lower() not in SKIP_FOLDERS)
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


def find_match(library, book):
    """Finds the library entry for a book by path, ASIN, or title + first author."""
    for entry in library:
        if same_path(entry.get("path"), book.get("path")):
            return entry
    asin = book.get("asin")
    if asin:
        for entry in library:
            if entry.get("asin") == asin:
                return entry
    title_key = normalize((book.get("title") or "").split(":")[0])
    author_key = normalize(primary_author(book.get("authors")))
    if not title_key:
        return None
    for entry in library:
        if (normalize((entry.get("title") or "").split(":")[0]) == title_key
                and normalize(primary_author(entry.get("authors"))) == author_key):
            return entry
    return None


def total_duration_min(paths):
    """Total play time of audio files in minutes, or None if any file can't be read."""
    import mutagen  # Only needed for download checks

    total = 0.0
    for path in paths:
        try:
            audio = mutagen.File(path)
        except Exception:
            audio = None
        if audio is None or not getattr(audio.info, "length", 0):
            logger.warning(f"Could not read the duration of {path}")
            return None
        total += audio.info.length
    return round(total / 60)


def _natural_key(path):
    """Sorts 'CD2/01.mp3' before 'CD10/01.mp3' and 'Part 9' before 'Part 10'."""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", path.replace("\\", "/"))]


def safe_filename(text):
    return re.sub(r'[\\/:*?"<>|]', "", text or "").strip(" .")


def plan_import_files(content_path, title, rename=True):
    """Decides where a download's files go inside the book folder.

    Returns (audio, cover): audio is a list of (source, relative destination) in play order,
    cover is one (source, 'cover.ext') pair or None. With rename on, audio files become
    'Title.ext' or 'Title - Part 01.ext'; with it off they keep their names and subfolders,
    so files that share a name on different discs don't collide.
    """
    if os.path.isfile(content_path):
        base = os.path.dirname(content_path)
        name = os.path.basename(content_path)
        audio_rels = [name] if os.path.splitext(name)[1].lower() in AUDIO_EXTENSIONS else []
        image_rels = []
    else:
        base = content_path
        audio_rels, image_rels = [], []
        for root, dirs, files in os.walk(content_path):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d.lower() not in SKIP_FOLDERS]
            for f in files:
                rel = os.path.relpath(os.path.join(root, f), base)
                ext = os.path.splitext(f)[1].lower()
                if ext in AUDIO_EXTENSIONS:
                    audio_rels.append(rel)
                elif ext in IMAGE_EXTENSIONS:
                    image_rels.append(rel)

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
    values = {
        "{Author}": primary_author(book.get("authors")),
        "{Authors}": book.get("authors") or "",
        "{Title}": (book.get("title") or "").strip(),
        "{Series}": book.get("series") or "",
        "{SeriesNumber}": format_sequence(book.get("sequence")),
        "{Year}": (book.get("release_date") or "")[:4],
    }
    name = template or DEFAULT_NAMING_FORMAT
    for token, value in values.items():
        name = name.replace(token, value)
    segments = [re.sub(r"\s+", " ", s).strip() for s in name.split(" - ")]
    name = " - ".join(s for s in segments if s)
    name = re.sub(r'[\\/:*?"<>|]', "", name).strip(" .")
    return name or re.sub(r'[\\/:*?"<>|]', "", book.get("title") or "Unknown")
