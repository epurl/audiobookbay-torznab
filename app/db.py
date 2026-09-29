import json
import os
import re
import shutil
import datetime
import threading
import tempfile
import logging

logger = logging.getLogger(__name__)

# Persist the database outside the app directory so it survives container rebuilds.
# In Docker this is /config (a mounted volume); locally it defaults to ./config.
CONFIG_DIR = os.environ.get(
    "BAYARR_CONFIG_DIR",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config"),
)
DB_FILE = os.path.join(CONFIG_DIR, "database.json")
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
    "auth_username": "",
    "auth_password_hash": "",
}

# Settings the UI is allowed to write directly. Secrets are handled separately.
EDITABLE_SETTINGS = {
    "language", "auto_match_narrator", "format_preference", "qbt_enabled",
    "qbt_host", "qbt_user", "root_folder", "downloads_folder",
}


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
        db.setdefault("library", [])
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


def get_book(title):
    return next((b for b in get_library() if b.get("title") == title), None)


BOOK_FIELDS = {"title", "authors", "narrators", "imageUrl", "release_date", "sequence"}


def add_to_library(book):
    """Adds a book if it isn't tracked yet. Returns the stored entry."""
    book = {k: v for k, v in book.items() if k in BOOK_FIELDS}
    with _lock:
        db = _load_db()
        existing = next((b for b in db["library"] if b.get("title") == book.get("title")), None)
        if existing:
            return existing
        book["status"] = initial_status(book.get("release_date"))
        db["library"].append(book)
        _save_db(db)
        return book


def update_book(title, **fields):
    with _lock:
        db = _load_db()
        for b in db["library"]:
            if b.get("title") == title:
                b.update(fields)
                _save_db(db)
                return True
        return False


def update_library_status(title, status):
    return update_book(title, status=status)


def remove_from_library(title):
    with _lock:
        db = _load_db()
        db["library"] = [b for b in db["library"] if b.get("title") != title]
        _save_db(db)
        return True


def get_settings():
    return _load_db().get("settings", {})


def get_public_settings():
    """Settings safe to send to the browser: no passwords or hashes."""
    settings = get_settings()
    public = {k: settings.get(k) for k in EDITABLE_SETTINGS}
    public["qbt_pass_set"] = bool(settings.get("qbt_pass"))
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
        if new_settings.get("qbt_pass"):
            settings["qbt_pass"] = new_settings["qbt_pass"]
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
