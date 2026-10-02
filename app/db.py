import json
import os
import re
import shutil
import base64
import datetime
import html
import threading
import tempfile
import logging
import uuid

from app import editions
from app.library import (_CONTRIBUTOR_ROLE, DEFAULT_NAMING_FORMAT, find_import_match, find_match, main_series_fields,
                         merge_series_lists, series_entries, series_key)

logger = logging.getLogger(__name__)

# Persist the database outside the app directory so it survives container rebuilds.
# In Docker this is /config (a mounted volume); locally it defaults to ./config.
def env(name, default=""):
    """A BORGARR_ environment variable, or the same one under the old name (BAYARR_)."""
    return os.environ.get("BORGARR_" + name) or os.environ.get("BAYARR_" + name) or default


CONFIG_DIR = env("CONFIG_DIR", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config"))
DB_FILE = os.path.join(CONFIG_DIR, "database.json")
BACKUP_DIR = os.path.join(CONFIG_DIR, "backups")
BACKUPS_KEPT = 7
_LEGACY_DB_FILE = os.path.join(os.path.dirname(__file__), "database.json")

_lock = threading.RLock()

DEFAULT_SETTINGS = {
    "language": "English",
    "auto_match_narrator": True,
    "format_preference": "prefer_m4b",  # prefer_m4b | m4b_only | any
    "qbt_enabled": False,
    "qbt_host": "http://localhost:8080",
    "qbt_user": "admin",
    "qbt_pass": "adminadmin",
    "root_folder": "",
    "downloads_folder": "",
    "naming_format": DEFAULT_NAMING_FORMAT,
    "rename_files": True,
    "auto_add_folders": True,  # Library scans add folders in the Root Folder no book uses
    "stall_hours": 6,  # 0 = never give up on a stalled download
    "remove_stalled": True,
    "seed_cleanup": False,       # Remove torrents of imported books once they've seeded enough
    "seed_ratio": 0,             # ... at this ratio (0 = no ratio limit)
    "seed_days": 0,              # ... or after this many days of seeding (0 = no time limit)
    "seed_delete_files": True,   # ... with their downloaded files (the library has its own copy)
    # Usenet (NZBGet or SABnzbd), used for releases from Newznab indexers
    "usenet_client": "",            # "" | nzbget | sabnzbd
    "usenet_host": "",
    "usenet_user": "",
    "usenet_pass": "",              # NZBGet
    "usenet_apikey": "",            # SABnzbd
    "usenet_category": "audiobooks",
    "usenet_downloads_folder": "",  # BorgArr's path for the client's finished downloads (when it differs)
    "usenet_remove_completed": True,  # Delete a download once it's imported (nothing to seed)
    "verify_runtime": True,
    "runtime_tolerance": 10,  # percent
    "write_metadata": True,
    "auto_convert_m4b": False,                # Queue imported downloads that aren't a single M4B
    "delete_originals_after_convert": False,  # Delete the .original files once a conversion checks out
    "edition_preference": "narrated",  # narrated (unabridged) | abridged | both: what series monitoring adds
    "ignore_extras": True,  # Series' novellas, short stories, collections... are ignored (app/extras.py)
    # AudiobookBay (the cookie falls back to the ABB_COOKIE environment variable)
    "abb_enabled": True,
    "abb_url": "",
    "abb_user_agent": "",
    "abb_verify_tls": True,  # Check AudiobookBay's certificate
    "torznab_api_key": "",  # When set, Prowlarr & co. must send it to use /api
    "abb_cookie": "",
    "indexers": [],  # Torznab indexers, managed on their own (see indexers.py)
    # Release preferences (scoring of releases); comma-separated lists, 0 = off
    "pref_narrators": "",
    "avoid_narrators": "",
    "preferred_words": "",
    "blocked_words": "",
    "blocked_uploaders": "",
    "min_bitrate": 0,     # kbps
    "max_size_gb": 0,
    "abs_url": "",
    "abs_token": "",
    "abs_library_id": "",
    "auth_username": "",
    "auth_password_hash": "",
}

# Settings the UI is allowed to write directly. Secrets are handled separately.
EDITABLE_SETTINGS = {
    "language", "auto_match_narrator", "format_preference", "qbt_enabled",
    "qbt_host", "qbt_user", "root_folder", "downloads_folder", "naming_format",
    "rename_files", "auto_add_folders", "stall_hours", "remove_stalled", "seed_cleanup", "seed_ratio", "seed_days", "seed_delete_files",
    "usenet_client", "usenet_host", "usenet_user", "usenet_category", "usenet_downloads_folder", "usenet_remove_completed",
    "verify_runtime", "runtime_tolerance", "write_metadata", "edition_preference", "ignore_extras",
    "auto_convert_m4b", "delete_originals_after_convert",
    "pref_narrators", "avoid_narrators", "preferred_words", "blocked_words", "blocked_uploaders",
    "min_bitrate", "max_size_gb", "abb_enabled", "abb_url", "abb_user_agent", "abb_verify_tls", "torznab_api_key",
    "abs_url", "abs_library_id",
}
# Secrets: never sent to the browser, and a blank value from the UI keeps the stored one
SECRET_SETTINGS = {"qbt_pass", "abs_token", "abb_cookie", "usenet_pass", "usenet_apikey"}

# Monitored books are searched for; Unmonitored and Missing ones are left alone.
# Needs Review: the download finished but didn't pass the checks, so it wasn't imported.
STATUSES = ["Unreleased", "Monitored", "Unmonitored", "Downloading", "Downloaded",
            "Needs Review", "Imported", "Missing"]

HISTORY_LIMIT = 1000


def _migrate_legacy_db():
    """Moves a database.json from the old in-app location to the config dir."""
    if os.path.exists(_LEGACY_DB_FILE) and not os.path.exists(DB_FILE):
        os.makedirs(CONFIG_DIR, exist_ok=True)
        shutil.move(_LEGACY_DB_FILE, DB_FILE)
        logger.info(f"Migrated database from {_LEGACY_DB_FILE} to {DB_FILE}")


def _load_db():
    with _lock:
        _migrate_legacy_db()
        db = {}
        if os.path.exists(DB_FILE):
            try:
                with open(DB_FILE, "r", encoding="utf-8") as f:
                    db = json.load(f)
            except json.JSONDecodeError:
                # Keep the damaged file for recovery instead of overwriting it on the next save
                stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")
                kept = os.path.join(CONFIG_DIR, f"database.corrupt-{stamp}.json")
                os.replace(DB_FILE, kept)
                logger.error(f"{DB_FILE} is corrupt; moved it to {kept} and started an empty database. "
                             f"Restore a backup from {BACKUP_DIR} in Settings > Backup.")
        # Fill in anything missing: a new install, a corrupt file, or an older database
        settings = db.setdefault("settings", {})
        for k, v in DEFAULT_SETTINGS.items():
            settings.setdefault(k, v)
        db.setdefault("series", [])
        db.setdefault("history", [])
        db.setdefault("ignored", [])
        db.setdefault("allowed", [])
        changed = False
        for book in db.setdefault("library", []):
            # Books used to be keyed by title; give older entries a stable id
            if not book.get("id"):
                book["id"] = uuid.uuid4().hex
                changed = True
            # Books used to have a single series; keep it as a one-entry series list
            if "series_list" not in book:
                book["series_list"] = series_entries(book)
                changed = True
            # Earlier imports kept translators etc. in the author list
            authors = book.get("authors") or ""
            if _CONTRIBUTOR_ROLE.search(authors):
                book["authors"] = ", ".join(a for a in authors.split(", ") if not _CONTRIBUTOR_ROLE.search(a))
                changed = True
        if changed:
            _save_db(db)
        return db


def _save_db(data):
    with _lock:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        # Write atomically so a crash mid-write can't corrupt the database
        fd, tmp_path = tempfile.mkstemp(dir=CONFIG_DIR, prefix=".database-", suffix=".json")
        try:
            # Compact, in one piece: Python's fast encoder (an indented file takes three
            # times as long to write, and saves happen often)
            text = json.dumps(data)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp_path, DB_FILE)
        except Exception:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            raise


def is_unreleased(release_date, today=None):
    """True for a release date still to come (Audible dates books without one 2200-01-01).
    Dates that aren't YYYY-MM-DD (a year from a folder's metadata) don't count."""
    try:
        return datetime.datetime.strptime(str(release_date or "")[:10], "%Y-%m-%d").date() > (today or datetime.date.today())
    except ValueError:
        return False


def initial_status(release_date):
    """Books with a future release date start as Unreleased, everything else as Monitored.
    Also the status for monitoring a book again: an unreleased book isn't searched for."""
    return "Unreleased" if is_unreleased(release_date) else "Monitored"


def get_library():
    return _load_db().get("library", [])


def get_book(book_id):
    return next((b for b in get_library() if b.get("id") == book_id), None)


BOOK_FIELDS = {"title", "authors", "narrators", "imageUrl", "release_date", "sequence", "series", "asin",
               "series_asin", "series_list", "runtime_min", "description", "publisher", "language",
               "edition", "edition_reason", "edition_check",
               "ga_url", "runtime_approx", "isbn"}  # GraphicAudio releases Audible doesn't sell
# Fields the book editor may change
EDITABLE_BOOK_FIELDS = {"title", "authors", "narrators", "release_date", "sequence", "series", "asin", "status",
                        "runtime_min", "edition"}


def _new_entry(book, status):
    return {**book, "id": uuid.uuid4().hex, "status": status,
            "added": datetime.datetime.now().isoformat(timespec="seconds")}


def _clean_description(text):
    """Audible summaries are HTML; keep plain text."""
    text = re.sub(r"<br\s*/?>|</p>", "\n", str(text or ""), flags=re.IGNORECASE)
    return html.unescape(re.sub(r"<[^>]+>", "", text)).strip()


def add_to_library(book, status=None):
    """Adds a book if it isn't tracked yet. Returns the stored entry."""
    # Known fields only, and no structured values where text or numbers belong
    book = {k: v for k, v in book.items() if k in BOOK_FIELDS and (k == "series_list" or not isinstance(v, (dict, list)))}
    if not isinstance(book.get("series_list"), list):
        book.pop("series_list", None)
    book["series_list"] = merge_series_lists(series_entries(book))
    if book.get("description"):
        book["description"] = _clean_description(book["description"])
    book["edition"] = editions.stored_edition(book.get("edition")) or book.get("edition")  # Old "dramatized"
    if book.get("edition") not in editions.EDITIONS:
        # Added from somewhere that didn't say (e.g. the Search page): judge by its details
        found = editions.classify_fields(book) or {"edition": editions.NARRATED, "reason": "No signs of an abridgement"}
        book.update(edition=found["edition"], edition_reason=found["reason"])
    with _lock:
        db = _load_db()
        existing = find_match(db["library"], book)
        if existing:
            return existing
        entry = _new_entry(book, status or initial_status(book.get("release_date")))
        db["library"].append(entry)
        _save_db(db)
        return entry


def import_books(books):
    """Adds books found on disk as Imported. Books already in the library (e.g. ones being
    monitored) are linked to their folder instead of duplicated. Returns (added, linked)."""
    added = linked = 0
    with _lock:
        db = _load_db()
        for book in books:
            existing = find_import_match(db["library"], book)
            if existing:
                existing.update(path=book["path"], cover=book.get("cover", ""), status="Imported")
                for key in ("asin", "series", "sequence", "narrators", "edition", "edition_reason"):
                    if book.get(key) and not existing.get(key):
                        existing[key] = book[key]
                existing["series_list"] = merge_series_lists(series_entries(existing), series_entries(book))
                linked += 1
            else:
                db["library"].append(_new_entry(book, "Imported"))
                added += 1
        _save_db(db)
    return added, linked


def update_book(book_id, **fields):
    with _lock:
        db = _load_db()
        for b in db["library"]:
            if b.get("id") == book_id:
                b.update(fields)
                _save_db(db)
                return True
        return False


# Fields taken from Audible when a book is matched; status, path and files stay as they are
AUDIBLE_FIELDS = ("title", "authors", "narrators", "asin", "series", "series_asin", "sequence", "runtime_min",
                  "description", "publisher", "language", "release_date", "imageUrl", "edition", "edition_reason",
                  "ga_url", "runtime_approx", "isbn")  # The last ones for GraphicAudio releases


def apply_audible_match(book_id, audible_book):
    fields = {k: audible_book.get(k) for k in AUDIBLE_FIELDS if audible_book.get(k)}
    if fields.get("description"):
        fields["description"] = _clean_description(fields["description"])
    book = get_book(book_id) or {}
    # Matched to Audible or to a GraphicAudio release: what identified the other goes
    if audible_book.get("asin"):
        fields.update(ga_url="", runtime_approx=False)
    elif audible_book.get("ga_url"):
        fields.update(asin="")
    # Keep every series from both sides; the main one is what Audible matching chose. A
    # different book than before (a wrong match corrected) drops the Audible series the
    # previous match brought in, unless the new one is in them too
    kept = series_entries(book)
    old_id, new_id = book.get("asin") or book.get("ga_url"), audible_book.get("asin") or audible_book.get("ga_url")
    if old_id and new_id and old_id != new_id:
        new_entries = series_entries(audible_book)
        listed = {e["asin"] for e in new_entries if e.get("asin")}
        names = {series_key(e["name"]) for e in new_entries}
        kept = [e for e in kept if not e.get("asin") or e["asin"] in listed or series_key(e["name"]) in names]
    entries = merge_series_lists(series_entries(audible_book), kept)
    fields["series_list"] = entries
    if fields.get("edition"):
        fields["edition_check"] = False  # Audible's edition for the chosen match
    if not fields.get("series"):
        fields.update({k: v for k, v in main_series_fields(entries).items() if k != "series_list"})
    return update_book(book_id, **fields)


def add_series_to_books(entry, book_ids):
    """Records that these books belong to a series (e.g. after a series sync)."""
    add_series_entries({book_id: [entry] for book_id in book_ids})


def add_series_entries(entries_by_book):
    """Records series memberships for many books in one save: {book id: [series entry]}."""
    with _lock:
        db = _load_db()
        changed = False
        for b in db["library"]:
            if b.get("id") not in entries_by_book:
                continue
            merged = merge_series_lists(series_entries(b), entries_by_book[b["id"]])
            if merged != b.get("series_list"):
                b["series_list"] = merged
                if not b.get("series"):
                    b.update({k: v for k, v in main_series_fields(merged).items() if k != "series_list"})
                else:
                    # Fill in the id and number of the main series once Audible confirms them
                    main = next((e for e in merged if series_key(e["name"]) == series_key(b["series"])), None)
                    if main:
                        b["series_asin"] = b.get("series_asin") or main.get("asin", "")
                        b["sequence"] = b.get("sequence") or main.get("sequence", "")
                changed = True
        if changed:
            _save_db(db)


def set_series_asin(name, asin):
    """Fills in the Audible id for a series that books only know by name."""
    key = series_key(name)
    with _lock:
        db = _load_db()
        changed = False
        for b in db["library"]:
            for e in b.get("series_list") or []:
                if not e.get("asin") and series_key(e.get("name")) == key:
                    e["asin"] = asin
                    if series_key(b.get("series")) == key and not b.get("series_asin"):
                        b["series_asin"] = asin
                    changed = True
        if changed:
            _save_db(db)


def update_books(changes):
    """Applies {book_id: {field: value}} in a single write."""
    with _lock:
        db = _load_db()
        for b in db["library"]:
            if b.get("id") in changes:
                b.update(changes[b["id"]])
        _save_db(db)


def update_library_status(book_id, status):
    return update_book(book_id, status=status)


def remove_from_library(book_id):
    with _lock:
        db = _load_db()
        db["library"] = [b for b in db["library"] if b.get("id") != book_id]
        _save_db(db)
        return True


# --- History ---------------------------------------------------------------

def add_history(event, book=None, message=""):
    """Records something that happened (grabbed, imported, needs review...)."""
    entry = {
        "time": datetime.datetime.now().isoformat(timespec="seconds"),
        "event": event,
        "book_id": (book or {}).get("id", ""),
        "title": (book or {}).get("title", ""),
        "message": message,
    }
    with _lock:
        db = _load_db()
        db["history"].append(entry)
        del db["history"][:-HISTORY_LIMIT]
        _save_db(db)


def get_history(limit=200, offset=0):
    """The newest events first: `limit` of them, skipping the `offset` newest."""
    history = _load_db()["history"]
    end = max(0, len(history) - max(0, offset))
    return list(reversed(history[max(0, end - limit):end]))


def history_count():
    return len(_load_db()["history"])


# --- Ignored books -------------------------------------------------------------
# Books never added automatically, by a monitored series, a followed author or a watched
# list, even when one of them lists the book. Ignored from a series page (or a list review);
# kept by the ids the release is known by, so it's recognised wherever it turns up.

def release_ids(book):
    """The ids a release is known by: its ASIN (any part's, for a book sold in parts) and
    its GraphicAudio page."""
    book = book or {}
    ids = [book.get("asin"), book.get("ga_url")] + list(book.get("part_asins") or [])
    return {i for i in ids if isinstance(i, str) and i}


def get_ignored():
    return _load_db().get("ignored", [])


def ignored_ids():
    return {i for e in get_ignored() for i in e.get("ids") or []}


def is_ignored(book, ids=None):
    return bool(release_ids(book) & (ignored_ids() if ids is None else ids))


def ignore(book, status_before=""):
    """Ignores a release; status_before is the library status it had (put back when it's
    no longer ignored). None when the book has no id to recognise it by."""
    ids = release_ids(book)
    if not ids:
        return None
    entry = {"ids": sorted(ids), "title": book.get("title") or "", "authors": book.get("authors") or "",
             "series": book.get("series") or "", "sequence": str(book.get("sequence") or book.get("catalog_sequence") or ""),
             "status_before": status_before, "when": datetime.datetime.now().isoformat(timespec="seconds")}
    with _lock:
        db = _load_db()
        db["ignored"] = [e for e in db["ignored"] if not set(e.get("ids") or []) & ids] + [entry]
        _save_db(db)
    return entry


def allowed_ids():
    """Extras you want after all: not ignored automatically (Settings > General > Ignore Extras)."""
    return set(_load_db().get("allowed", []))


def allow(book, allowed=True):
    """Lets an extra be added like a main book (allowed=False: ignored automatically again)."""
    ids = release_ids(book)
    if not ids:
        return
    with _lock:
        db = _load_db()
        before = set(db["allowed"])
        after = before | ids if allowed else before - ids
        if after != before:
            db["allowed"] = sorted(after)
            _save_db(db)


def unignore(book):
    """Stops ignoring a release. Returns the entries removed."""
    ids = release_ids(book)
    if not ids:
        return []
    with _lock:
        db = _load_db()
        removed = [e for e in db["ignored"] if set(e.get("ids") or []) & ids]
        if removed:
            db["ignored"] = [e for e in db["ignored"] if e not in removed]
            _save_db(db)
    return removed


# --- Series ----------------------------------------------------------------

def get_series_list():
    return _load_db()["series"]


def get_series(series_id):
    return next((s for s in get_series_list() if s.get("id") == series_id), None)


# --- Audible series lists ------------------------------------------------------
# Every book Audible lists for a series, so the Series page can show totals and the
# books you don't have. Refreshed when older than CATALOG_MAX_AGE_DAYS.

CATALOG_MAX_AGE_DAYS = 7
# Raised when the way series lists are built changes, so saved ones are fetched again
CATALOG_VERSION = 9
_CATALOG_BOOK_FIELDS = ("title", "authors", "narrators", "imageUrl", "release_date", "asin", "series", "series_asin",
                        "sequence", "series_list", "runtime_min", "publisher", "language", "catalog_sequence",
                        "edition", "edition_reason", "part_asins", "part", "part_count", "placeholder", "ga_url",
                        "subtitle", "content_type", "summary_hint")  # The last three tell extras apart


CATALOG_FILE = os.path.join(CONFIG_DIR, "series_catalog.json")
_catalogs = None  # Kept in memory; this file can grow large and rarely changes


def get_catalogs():
    global _catalogs
    with _lock:
        if _catalogs is None:
            try:
                with open(CATALOG_FILE, "r", encoding="utf-8") as f:
                    _catalogs = json.load(f)
            except (OSError, ValueError):
                _catalogs = {}
        return _catalogs


def get_catalog(asin):
    return get_catalogs().get(asin)


def catalog_is_fresh(catalog):
    if not catalog or not catalog.get("fetched") or catalog.get("version") != CATALOG_VERSION:
        return False
    age = datetime.datetime.now() - datetime.datetime.fromisoformat(catalog["fetched"])
    return age < datetime.timedelta(days=CATALOG_MAX_AGE_DAYS)


def save_catalog(asin, title, books, alternates=()):
    """books: one edition per book (narrated where there is one); alternates: the other
    editions (dramatized, abridged) of the same books."""
    with _lock:
        catalogs = get_catalogs()
        catalogs[asin] = {
            "title": title,
            "version": CATALOG_VERSION,
            "fetched": datetime.datetime.now().isoformat(timespec="seconds"),
            "books": [{k: b.get(k) for k in _CATALOG_BOOK_FIELDS} for b in books],
            "alternates": [{k: b.get(k) for k in _CATALOG_BOOK_FIELDS} for b in alternates],
        }
        os.makedirs(CONFIG_DIR, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=CONFIG_DIR, prefix=".catalog-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(catalogs, f)
        os.replace(tmp_path, CATALOG_FILE)


def add_series(asin, title, author, known_asins=None):
    """Starts monitoring an Audible series. known_asins are the books that existed when it
    was added; later syncs only add books that weren't among them (new releases)."""
    with _lock:
        db = _load_db()
        existing = next((s for s in db["series"] if s.get("asin") == asin), None)
        if existing:
            existing["monitored"] = True
            if known_asins is not None:
                existing["known_asins"] = sorted(set(existing.get("known_asins") or []) | set(known_asins))
            _save_db(db)
            return existing
        entry = {"id": uuid.uuid4().hex, "asin": asin, "title": title, "author": author,
                 "monitored": True, "known_asins": sorted(set(known_asins or [])),
                 "added": datetime.datetime.now().isoformat(timespec="seconds"), "last_sync": ""}
        db["series"].append(entry)
        _save_db(db)
        return entry


def update_series(series_id, **fields):
    with _lock:
        db = _load_db()
        for s in db["series"]:
            if s.get("id") == series_id:
                s.update(fields)
                _save_db(db)
                return True
        return False


def remove_series(series_id):
    with _lock:
        db = _load_db()
        db["series"] = [s for s in db["series"] if s.get("id") != series_id]
        _save_db(db)


def get_settings():
    return _load_db().get("settings", {})


def get_public_settings():
    """Settings safe to send to the browser: no passwords or hashes."""
    settings = get_settings()
    public = {k: settings.get(k) for k in EDITABLE_SETTINGS}
    for key in SECRET_SETTINGS:
        public[f"{key}_set"] = bool(settings.get(key))
    public["auth_username"] = settings.get("auth_username", "")
    public["abb_cookie_env"] = bool(os.environ.get("ABB_COOKIE"))
    return public


def _typed(key, value):
    """A setting's value as the type its default has (text, yes/no), so a malformed one
    from the browser or a backup can't break the code that uses it. Numbers are checked
    below; settings without a default are kept as they are."""
    default = DEFAULT_SETTINGS.get(key)
    if isinstance(default, bool):
        return value if isinstance(value, bool) else str(value).strip().lower() in ("1", "true", "yes", "on")
    if isinstance(default, str):
        return value if isinstance(value, str) else ("" if value is None or isinstance(value, (dict, list)) else str(value))
    return value


def update_settings(new_settings):
    """Merges whitelisted keys into the stored settings."""
    with _lock:
        db = _load_db()
        settings = db["settings"]
        for k in EDITABLE_SETTINGS:
            if k in new_settings:
                settings[k] = _typed(k, new_settings[k])
        # An empty password field means "keep the current one"
        for key in SECRET_SETTINGS:
            if new_settings.get(key):
                settings[key] = str(new_settings[key]).strip()
        if new_settings.get("abb_cookie_clear"):
            settings["abb_cookie"] = ""
        if "stall_hours" in new_settings:
            try:
                settings["stall_hours"] = max(0, min(168, int(new_settings["stall_hours"])))
            except (TypeError, ValueError):
                settings["stall_hours"] = DEFAULT_SETTINGS["stall_hours"]
        for key, kind, top in (("min_bitrate", int, 1000), ("max_size_gb", float, 1000),
                               ("seed_ratio", float, 100), ("seed_days", float, 3650)):
            try:
                settings[key] = max(0, min(top, kind(settings.get(key) or 0)))
            except (TypeError, ValueError):
                settings[key] = 0
        if settings.get("edition_preference") == "dramatized":
            settings["edition_preference"] = "abridged"  # Dramatizations count as abridged now
        if settings.get("edition_preference") not in ("narrated", "abridged", "both"):
            settings["edition_preference"] = DEFAULT_SETTINGS["edition_preference"]
        if "runtime_tolerance" in new_settings:
            try:
                settings["runtime_tolerance"] = max(1, min(50, int(new_settings["runtime_tolerance"])))
            except (TypeError, ValueError):
                settings["runtime_tolerance"] = DEFAULT_SETTINGS["runtime_tolerance"]
        _save_db(db)
        return True


def set_setting(key, value):
    """Stores one setting directly (for values the settings form doesn't edit, e.g. indexers)."""
    with _lock:
        db = _load_db()
        db["settings"][key] = value
        _save_db(db)


def set_auth_credentials(username, password_hash):
    with _lock:
        db = _load_db()
        db["settings"]["auth_username"] = username
        db["settings"]["auth_password_hash"] = password_hash
        _save_db(db)


def extract_infohash(magnet):
    """A magnet's info hash as lowercase hex, which is how qBittorrent reports it. Some
    magnets give it in base32 (32 characters) instead of hex (40)."""
    match = re.search(r"btih:([0-9a-zA-Z]+)", magnet or "")
    if not match:
        return None
    value = match.group(1)
    if len(value) == 32 and re.fullmatch(r"[A-Za-z2-7]+", value):
        try:
            return base64.b32decode(value.upper()).hex()
        except ValueError:
            pass
    return value.lower()


# --- Backups -----------------------------------------------------------------

def _prune_backups():
    backups = sorted(f for f in os.listdir(BACKUP_DIR) if f.startswith("database-") and f.endswith(".json"))
    for old in backups[:-BACKUPS_KEPT]:
        os.remove(os.path.join(BACKUP_DIR, old))


def backup_now(label=None):
    """Copies database.json into the backups folder. Returns the backup's path or None."""
    with _lock:
        if not os.path.exists(DB_FILE):
            return None
        os.makedirs(BACKUP_DIR, exist_ok=True)
        stamp = label or datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")
        path = os.path.join(BACKUP_DIR, f"database-{stamp}.json")
        shutil.copy2(DB_FILE, path)
        _prune_backups()
        return path


def daily_backup():
    """Keeps one backup per day (the last 7 days)."""
    today = datetime.date.today().isoformat()
    if os.path.isdir(BACKUP_DIR) and any(f.startswith(f"database-{today}") for f in os.listdir(BACKUP_DIR)):
        return None
    return backup_now()


def restore(data):
    """Replaces the database with a backup. The current login is kept, so a restore can't
    lock you out, and the current database is backed up first."""
    if not isinstance(data, dict) or not isinstance(data.get("library"), list) or not isinstance(data.get("settings"), dict):
        raise ValueError("This isn't a BorgArr backup (expected library and settings).")
    if not all(isinstance(b, dict) and b.get("title") for b in data["library"]):
        raise ValueError("The backup's library has entries without a title.")
    # A crafted backup could point books at system folders (which Organize would then move)
    from app.library import is_unsafe_folder
    unsafe = next((b for b in data["library"] if isinstance(b.get("path"), str) and is_unsafe_folder(b["path"])), None)
    if unsafe or is_unsafe_folder(str(data["settings"].get("root_folder") or "")):
        where = unsafe["path"] if unsafe else data["settings"].get("root_folder")
        raise ValueError(f"The backup points at a system folder ({where}); it wasn't restored.")
    with _lock:
        backup_now(datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S") + "-before-restore")
        current = _load_db()["settings"]
        restored = {
            "library": data["library"],
            "settings": {**{k: _typed(k, v) for k, v in data["settings"].items()},
                         "auth_username": current.get("auth_username", ""),
                         "auth_password_hash": current.get("auth_password_hash", "")},
            "series": data.get("series") if isinstance(data.get("series"), list) else [],
            "history": data.get("history") if isinstance(data.get("history"), list) else [],
            "allowed": [i for i in (data.get("allowed") if isinstance(data.get("allowed"), list) else []) if isinstance(i, str) and i],
            "ignored": [{**e, "ids": [i for i in e["ids"] if isinstance(i, str) and i]}
                        for e in (data.get("ignored") if isinstance(data.get("ignored"), list) else [])
                        if isinstance(e, dict) and isinstance(e.get("ids"), list)],
        }
        _save_db(restored)
    return len(restored["library"])
