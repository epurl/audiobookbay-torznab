import base64
import hashlib
import hmac
import ipaddress
import logging
import os
import secrets

from fastapi import Request
from fastapi.responses import JSONResponse, Response

from app import db

logger = logging.getLogger(__name__)

# Paths used by Prowlarr/Listenarr as a Torznab indexer. They only proxy public
# AudiobookBay searches, so they stay reachable without the UI credentials.
TORZNAB_PATHS = {"/api", "/api/download"}

_PBKDF2_ROUNDS = 200_000


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), _PBKDF2_ROUNDS).hex()
    return f"pbkdf2_sha256${_PBKDF2_ROUNDS}${salt}${digest}"


def _verify_hash(password: str, stored: str) -> bool:
    try:
        _, rounds, salt, digest = stored.split("$")
        candidate = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(rounds)).hex()
        return hmac.compare_digest(candidate, digest)
    except ValueError:
        return False


def env_credentials_set() -> bool:
    return bool(os.environ.get("BAYARR_USERNAME") and os.environ.get("BAYARR_PASSWORD"))


def credentials_configured() -> bool:
    if env_credentials_set():
        return True
    settings = db.get_settings()
    return bool(settings.get("auth_username") and settings.get("auth_password_hash"))


def check_credentials(username: str, password: str) -> bool:
    # Environment variables take priority over credentials saved from the UI
    if env_credentials_set():
        user_ok = hmac.compare_digest(username, os.environ["BAYARR_USERNAME"])
        pass_ok = hmac.compare_digest(password, os.environ["BAYARR_PASSWORD"])
        return user_ok and pass_ok
    settings = db.get_settings()
    stored_user = settings.get("auth_username", "")
    stored_hash = settings.get("auth_password_hash", "")
    if not stored_user or not stored_hash:
        return False
    return hmac.compare_digest(username, stored_user) and _verify_hash(password, stored_hash)


def _is_local_client(request: Request) -> bool:
    host = request.client.host if request.client else ""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private


def _parse_basic_auth(request: Request):
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("basic "):
        return None
    try:
        decoded = base64.b64decode(header[6:]).decode("utf-8")
    except Exception:
        return None
    username, sep, password = decoded.partition(":")
    return (username, password) if sep else None


def _challenge():
    return Response(
        status_code=401,
        content="Authentication required",
        headers={"WWW-Authenticate": 'Basic realm="Bayarr", charset="UTF-8"'},
    )


async def auth_middleware(request: Request, call_next):
    path = request.url.path.rstrip("/") or "/"

    if path in TORZNAB_PATHS:
        return await call_next(request)

    # Cross-site pages can't send application/json without a CORS preflight (which
    # we never approve), so requiring it blocks CSRF against the JSON API.
    if request.method in ("POST", "PUT", "PATCH"):
        content_type = request.headers.get("content-type", "")
        if not content_type.startswith("application/json"):
            return JSONResponse({"detail": "Content-Type must be application/json"}, status_code=415)

    if credentials_configured():
        creds = _parse_basic_auth(request)
        if not creds or not check_credentials(*creds):
            return _challenge()
        return await call_next(request)

    # No credentials yet: only allow clients on the local network so the owner
    # can reach the Settings page and set a username and password.
    if not _is_local_client(request):
        logger.warning(f"Rejected request from non-local address {request.client.host if request.client else '?'}: no credentials configured")
        return Response(
            status_code=403,
            content="Bayarr has no login configured, so it only accepts connections from the local network. "
                    "Set BAYARR_USERNAME and BAYARR_PASSWORD, or set a login from Settings on your local network.",
        )
    return await call_next(request)
