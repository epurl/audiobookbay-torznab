import asyncio
import base64
import hashlib
import hmac
import ipaddress
import logging
import os
import secrets
import time

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
        candidate = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8", "surrogateescape"), salt.encode(), int(rounds)).hex()
        return hmac.compare_digest(candidate, digest)
    except ValueError:
        return False


def env_credentials_set() -> bool:
    return bool(db.env("USERNAME") and db.env("PASSWORD"))


def credentials_configured() -> bool:
    if env_credentials_set():
        return True
    settings = db.get_settings()
    return bool(settings.get("auth_username") and settings.get("auth_password_hash"))


def _same(a: str, b: str) -> bool:
    """Constant-time comparison that also takes non-ASCII text (compare_digest refuses
    non-ASCII str)."""
    return hmac.compare_digest(a.encode("utf-8", "surrogateescape"), b.encode("utf-8", "surrogateescape"))


def check_credentials(username: str, password: str) -> bool:
    # Environment variables take priority over credentials saved from the UI
    if env_credentials_set():
        user_ok = _same(username, db.env("USERNAME"))
        pass_ok = _same(password, db.env("PASSWORD"))
        return user_ok and pass_ok
    settings = db.get_settings()
    stored_user = settings.get("auth_username", "")
    stored_hash = settings.get("auth_password_hash", "")
    if not stored_user or not stored_hash:
        return False
    # Both checked whatever the username: answering faster for a wrong one would give the
    # username away
    user_ok = _same(username, stored_user)
    pass_ok = _verify_hash(password, stored_hash)
    return user_ok and pass_ok


def _is_local_client(request: Request) -> bool:
    host = request.client.host if request.client else ""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    if getattr(ip, "ipv4_mapped", None):  # "::ffff:203.0.113.5" is that IPv4 address
        ip = ip.ipv4_mapped
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
        headers={"WWW-Authenticate": 'Basic realm="BorgArr", charset="UTF-8"'},
    )


# --- Logins -------------------------------------------------------------------
# Checking a password takes about 200 ms on purpose (PBKDF2), and the browser sends it with
# every request. A login that checked out is remembered for a while (by a hash of it and of
# the stored password, so changing the password forgets it), and checks run off the event
# loop, so neither the UI nor a stream of wrong guesses can stall the server.

LOGIN_REMEMBERED = 600          # seconds
MAX_FAILURES, FAILURE_WINDOW = 10, 300  # wrong logins from one address, and the period
_good_logins = {}               # digest -> when it stops being remembered
_failures = {}                  # client address -> times of recent wrong logins
MAX_TRACKED = 10_000            # addresses with recent wrong logins kept in memory
_hashing = None                 # At most two password checks at once (each takes ~200 ms of CPU)


def _login_key(username, password):
    stored = "env" if env_credentials_set() else db.get_settings().get("auth_password_hash", "")
    raw = "\0".join((username, password, stored)).encode("utf-8", "surrogateescape")
    return hashlib.sha256(raw).hexdigest()


def _remembered(username, password):
    return _good_logins.get(_login_key(username, password), 0) > time.monotonic()


async def _check_login(username, password):
    global _hashing
    if _remembered(username, password):
        return True
    if _hashing is None:
        _hashing = asyncio.Semaphore(2)
    async with _hashing:
        ok = await asyncio.to_thread(check_credentials, username, password)
    if ok:
        now = time.monotonic()
        for key in [k for k, until in _good_logins.items() if until <= now]:
            del _good_logins[key]
        _good_logins[_login_key(username, password)] = now + LOGIN_REMEMBERED
    return ok


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "?"


def _recent_failures(ip):
    now = time.monotonic()
    recent = [t for t in _failures.get(ip, []) if now - t < FAILURE_WINDOW]
    if recent:
        _failures[ip] = recent
    else:
        _failures.pop(ip, None)
    return recent


def _too_many():
    return Response(status_code=429, content="Too many wrong logins from this address; try again in a few minutes.",
                    headers={"Retry-After": str(FAILURE_WINDOW)})


# --- Without a login --------------------------------------------------------------
# Only local addresses are let in, and only under a local name: a web page can point its
# own domain at a local address (DNS rebinding) to drive BorgArr from your browser, but its
# requests then carry its domain in the Host header.
LOCAL_SUFFIXES = (".local", ".lan", ".home", ".internal", ".localdomain", ".home.arpa", ".localhost")


def _local_host_header(request: Request) -> bool:
    host = (request.headers.get("host") or "").strip()
    if host.startswith("["):  # [IPv6]:port
        name = host[1:host.find("]")] if "]" in host else ""
    else:
        name = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        pass
    name = name.lower().rstrip(".")
    return bool(name) and (name == "localhost" or "." not in name or name.endswith(LOCAL_SUFFIXES))


# --- Torznab --------------------------------------------------------------------

def torznab_key_ok(request: Request) -> bool:
    """With a Torznab API key set (Settings > Indexers), Prowlarr must send it."""
    key = (db.get_settings().get("torznab_api_key") or "").strip()
    if not key:
        return True
    given = request.query_params.get("apikey") or ""
    return hmac.compare_digest(given.encode("utf-8", "surrogateescape"), key.encode("utf-8", "surrogateescape"))


# Torznab is reachable without the login: per address at most TORZNAB_PER_MINUTE
# requests, and TORZNAB_AT_ONCE in progress overall, so nobody can queue up endless
# AudiobookBay requests made with your cookie
TORZNAB_PER_MINUTE, TORZNAB_AT_ONCE = 60, 4
_torznab_recent = {}  # client address -> times of its requests in the last minute
_torznab_running = 0


def _torznab_slow_down(retry_after):
    return Response(status_code=429, media_type="application/xml", headers={"Retry-After": str(retry_after)},
                    content='<?xml version="1.0" encoding="UTF-8"?><error code="429" description="Too many requests; try again shortly"/>')


async def _torznab(request: Request, call_next):
    global _torznab_running
    now, ip = time.monotonic(), _client_ip(request)
    recent = [t for t in _torznab_recent.get(ip, []) if now - t < 60]
    if len(_torznab_recent) > MAX_TRACKED:  # Many addresses: start afresh
        _torznab_recent.clear()
    if len(recent) >= TORZNAB_PER_MINUTE:
        _torznab_recent[ip] = recent
        return _torznab_slow_down(60)
    if _torznab_running >= TORZNAB_AT_ONCE:
        return _torznab_slow_down(10)
    _torznab_recent[ip] = recent + [now]
    _torznab_running += 1
    try:
        return await call_next(request)
    finally:
        _torznab_running -= 1


# --- Headers ----------------------------------------------------------------------
# No framing by other sites (clickjacking), and only BorgArr's own script runs
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; img-src 'self' data: https: http:; connect-src 'self'; "
        "object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'"),
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


def _secured(response):
    for name, value in SECURITY_HEADERS.items():
        response.headers.setdefault(name, value)
    return response


async def auth_middleware(request: Request, call_next):
    return _secured(await _authorize(request, call_next))


async def _authorize(request: Request, call_next):
    # The path the router dispatches on. Not request.url.path: that's rebuilt from the Host
    # header, which a client can craft (e.g. "host/api?x=") to make any path look like an
    # open Torznab path (Starlette GHSA-86qp-5c8j-p5mr)
    path = request.scope.get("path", "").rstrip("/") or "/"

    if path in TORZNAB_PATHS:
        if not torznab_key_ok(request):
            return Response(status_code=401, media_type="application/xml",
                            content='<?xml version="1.0" encoding="UTF-8"?><error code="100" description="Incorrect user credentials"/>')
        return await _torznab(request, call_next)

    # Cross-site pages can't send application/json without a CORS preflight (which
    # we never approve), so requiring it blocks CSRF against the JSON API.
    if request.method in ("POST", "PUT", "PATCH"):
        content_type = request.headers.get("content-type", "")
        if not content_type.startswith("application/json"):
            return JSONResponse({"detail": "Content-Type must be application/json"}, status_code=415)

    if credentials_configured():
        creds = _parse_basic_auth(request)
        if not creds:
            return _challenge()
        ip = _client_ip(request)
        # A login already known to be right gets in even while an address is held back
        if not _remembered(*creds):
            if len(_recent_failures(ip)) >= MAX_FAILURES:
                return _too_many()
            if not await _check_login(*creds):
                if len(_failures) >= MAX_TRACKED:  # Many addresses: forget the stale ones
                    for address in list(_failures):
                        _recent_failures(address)  # Drops it when its failures are old
                    if len(_failures) >= MAX_TRACKED:
                        _failures.clear()
                _failures.setdefault(ip, []).append(time.monotonic())
                if len(_failures[ip]) == MAX_FAILURES:
                    logger.warning(f"{MAX_FAILURES} wrong logins from {ip}; refusing it for {FAILURE_WINDOW // 60} minutes")
                return _challenge()
            _failures.pop(ip, None)
        return await call_next(request)

    # No credentials yet: only allow clients on the local network, under a local address, so
    # the owner can reach the Settings page and set a username and password.
    if not _is_local_client(request):
        logger.warning(f"Rejected request from non-local address {_client_ip(request)}: no credentials configured")
        return Response(
            status_code=403,
            content="BorgArr has no login configured, so it only accepts connections from the local network. "
                    "Set BORGARR_USERNAME and BORGARR_PASSWORD, or set a login from Settings on your local network.",
        )
    if not _local_host_header(request):
        logger.warning(f"Rejected request for host {request.headers.get('host')!r}: no credentials configured")
        return Response(
            status_code=403,
            content="BorgArr has no login configured, so it only answers at its local address (e.g. "
                    "http://192.168.1.10:8085), not at a domain name. Open it that way and set a login in "
                    "Settings > Security (or set BORGARR_USERNAME and BORGARR_PASSWORD) to use this address.",
        )
    return await call_next(request)
