import json
import os
import re
import shutil
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
CONFIG_DIR = os.environ.get(
    "BAYARR_CONFIG_DIR",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config"),
)
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
    "use_hardlinks": True,
    "stall_hours": 6,  # 0 = never give up on a stalled download
    "remove_stalled": True,
    "verify_runtime": True,
    "runtime_tolerance": 10,  # percent
    "write_metadata": True,
    "edition_preference": "narrated",  # narrated | dramatized | both: what series monitoring adds
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
    "rename_files", "use_hardlinks", "stall_hours", "remove_stalled",
    "verify_runtime", "runtime_tolerance", "write_metadata", "edition_preference",
    "pref_narrators", "avoid_narrators", "preferred_words", "blocked_words", "blocked_uploaders",
    "min_bitrate", "max_size_gb",
    "abs_url", "abs_library_id",
}
# Secrets: never sent to the browser, and a blank value from the UI keeps the stored one
SECRET_SETTINGS = {"qbt_pass", "abs_token"}

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
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=4)
            os.replace(tmp_path, DB_FILE)
        except Exception:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            raise


def initial_status(release_date):
    """Books with a future release date start as Unreleased, everything else as Monitored."""
    if release_date:
        try:
            # Audible dates are usually YYYY-MM-DD
            dt = datetime.datetime.strptime(release_date, "%Y-%m-%d").date()
            if dt > datetime.date.today():
                return "Unreleased"
        except ValueError:
            pass
    return "Monitored"


def get_library():
    return _load_db().get("library", [])


def get_book(book_id):
    return next((b for b in get_library() if b.get("id") == book_id), None)


BOOK_FIELDS = {"title", "authors", "narrators", "imageUrl", "release_date", "sequence", "series", "asin",
               "series_asin", "series_list", "runtime_min", "description", "publisher", "language",
               "edition", "edition_reason", "edition_check"}
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
    book = {k: v for k, v in book.items() if k in BOOK_FIELDS}
    if not isinstance(book.get("series_list"), list):
        book.pop("series_list", None)
    book["series_list"] = merge_series_lists(series_entries(book))
    if book.get("description"):
        book["description"] = _clean_description(book["description"])
    if book.get("edition") not in editions.EDITIONS:
        # Added from somewhere that didn't say (e.g. the Search page): judge by its details
        found = editions.classify_fields(book) or {"edition": editions.NARRATED, "reason": "No signs of a dramatization"}
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
                  "description", "publisher", "language", "release_date", "imageUrl", "edition", "edition_reason")


def apply_audible_match(book_id, audible_book):
    fields = {k: audible_book.get(k) for k in AUDIBLE_FIELDS if audible_book.get(k)}
    if fields.get("description"):
        fields["description"] = _clean_description(fields["description"])
    book = get_book(book_id) or {}
    # Keep every series from both sides; the main one is what Audible matching chose
    entries = merge_series_lists(series_entries(audible_book), series_entries(book))
    fields["series_list"] = entries
    if fields.get("edition"):
        fields["edition_check"] = False  # Audible's edition for the chosen match
    if not fields.get("series"):
        fields.update({k: v for k, v in main_series_fields(entries).items() if k != "series_list"})
    return update_book(book_id, **fields)


def add_series_to_books(entry, book_ids):
    """Records that these books belong to a series (e.g. after a series sync)."""
    with _lock:
        db = _load_db()
        changed = False
        for b in db["library"]:
            if b.get("id") not in book_ids:
                continue
            merged = merge_series_lists(series_entries(b), [entry])
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


def get_history(limit=200):
    return list(reversed(_load_db()["history"][-limit:]))


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
CATALOG_VERSION = 4
_CATALOG_BOOK_FIELDS = ("title", "authors", "narrators", "imageUrl", "release_date", "asin", "series", "series_asin",
                        "sequence", "series_list", "runtime_min", "publisher", "language", "catalog_sequence",
                        "edition", "edition_reason", "part_asins")


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
    return public


def update_settings(new_settings):
    """Merges whitelisted keys into the stored settings."""
    with _lock:
        db = _load_db()
        settings = db["settings"]
        for k in EDITABLE_SETTINGS:
            if k in new_settings:
                settings[k] = new_settings[k]
        # An empty password field means "keep the current one"
        for key in SECRET_SETTINGS:
            if new_settings.get(key):
                settings[key] = new_settings[key]
        if "stall_hours" in new_settings:
            try:
                settings["stall_hours"] = max(0, min(168, int(new_settings["stall_hours"])))
            except (TypeError, ValueError):
                settings["stall_hours"] = DEFAULT_SETTINGS["stall_hours"]
        for key, kind, top in (("min_bitrate", int, 1000), ("max_size_gb", float, 1000)):
            try:
                settings[key] = max(0, min(top, kind(settings.get(key) or 0)))
            except (TypeError, ValueError):
                settings[key] = 0
        if settings.get("edition_preference") not in ("narrated", "dramatized", "both"):
            settings["edition_preference"] = DEFAULT_SETTINGS["edition_preference"]
        if "runtime_tolerance" in new_settings:
            try:
                settings["runtime_tolerance"] = max(1, min(50, int(new_settings["runtime_tolerance"])))
            except (TypeError, ValueError):
                settings["runtime_tolerance"] = DEFAULT_SETTINGS["runtime_tolerance"]
        _save_db(db)
        return True


def set_auth_credentials(username, password_hash):
    with _lock:
        db = _load_db()
        db["settings"]["auth_username"] = username
        db["settings"]["auth_password_hash"] = password_hash
        _save_db(db)


def extract_infohash(magnet):
    match = re.search(r"btih:([0-9a-zA-Z]+)", magnet or "")
    return match.group(1).lower() if match else None


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
        raise ValueError("This isn't a Bayarr backup (expected library and settings).")
    if not all(isinstance(b, dict) and b.get("title") for b in data["library"]):
        raise ValueError("The backup's library has entries without a title.")
    with _lock:
        backup_now(datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S") + "-before-restore")
        current = _load_db()["settings"]
        restored = {
            "library": data["library"],
            "settings": {**data["settings"],
                         "auth_username": current.get("auth_username", ""),
                         "auth_password_hash": current.get("auth_password_hash", "")},
            "series": data.get("series") if isinstance(data.get("series"), list) else [],
            "history": data.get("history") if isinstance(data.get("history"), list) else [],
        }
        _save_db(restored)
    return len(restored["library"])
