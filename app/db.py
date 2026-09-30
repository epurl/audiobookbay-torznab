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

from app.library import DEFAULT_NAMING_FORMAT, find_match

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
    "verify_runtime", "runtime_tolerance", "write_metadata",
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
        if not os.path.exists(DB_FILE):
            return {"library": [], "settings": dict(DEFAULT_SETTINGS)}
        with open(DB_FILE, "r", encoding="utf-8") as f:
            try:
                db = json.load(f)
            except json.JSONDecodeError:
                logger.error(f"{DB_FILE} is corrupt; starting with an empty database.")
                return {"library": [], "settings": dict(DEFAULT_SETTINGS)}
        # Ensure new keys exist
        settings = db.setdefault("settings", {})
        for k, v in DEFAULT_SETTINGS.items():
            settings.setdefault(k, v)
        db.setdefault("series", [])
        db.setdefault("history", [])
        # Books used to be keyed by title; give older entries a stable id
        missing_ids = False
        for book in db.setdefault("library", []):
            if not book.get("id"):
                book["id"] = uuid.uuid4().hex
                missing_ids = True
        if missing_ids:
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
               "series_asin", "runtime_min", "description", "publisher", "language"}
# Fields the book editor may change
EDITABLE_BOOK_FIELDS = {"title", "authors", "narrators", "release_date", "sequence", "series", "asin", "status",
                        "runtime_min"}


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
    if book.get("description"):
        book["description"] = _clean_description(book["description"])
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
            existing = find_match(db["library"], book)
            if existing:
                existing.update(path=book["path"], cover=book.get("cover", ""), status="Imported")
                for key in ("asin", "series", "sequence", "narrators"):
                    if book.get(key) and not existing.get(key):
                        existing[key] = book[key]
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
                  "description", "publisher", "language", "release_date", "imageUrl")


def apply_audible_match(book_id, audible_book):
    fields = {k: audible_book.get(k) for k in AUDIBLE_FIELDS if audible_book.get(k)}
    if fields.get("description"):
        fields["description"] = _clean_description(fields["description"])
    return update_book(book_id, **fields)


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


def add_series(asin, title, author, mode):
    """Starts tracking an Audible series. mode: 'all' (want every missing book) or 'future'."""
    with _lock:
        db = _load_db()
        existing = next((s for s in db["series"] if s.get("asin") == asin), None)
        if existing:
            existing.update(monitored=True, mode=mode)
            _save_db(db)
            return existing
        entry = {"id": uuid.uuid4().hex, "asin": asin, "title": title, "author": author,
                 "monitored": True, "mode": mode,
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
