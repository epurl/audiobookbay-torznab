import httpx
import logging
from typing import Optional

logger = logging.getLogger(__name__)


def _succeeded(response) -> bool:
    """qBittorrent 4.x answers 200 "Ok.", newer versions 204 with no body; failures are
    200 "Fails." or a 4xx status."""
    return 200 <= response.status_code < 300 and response.text.strip() != "Fails."

def normalize_host(host: str) -> str:
    """ "192.168.1.5:8080" -> "http://192.168.1.5:8080" (the scheme is easy to leave out)."""
    host = (host or "").strip().rstrip("/")
    if host and "://" not in host:
        host = "http://" + host
    return host


async def login_qbittorrent(host: str, username: str, password: str) -> Optional[httpx.AsyncClient]:
    """Authenticates with qBittorrent Web API and returns an authenticated httpx client."""
    client = httpx.AsyncClient(base_url=normalize_host(host))
    try:
        data = {"username": username, "password": password}
        response = await client.post("/api/v2/auth/login", data=data, timeout=5.0)
        
        if _succeeded(response):
            return client
        else:
            logger.error(f"qBittorrent login failed: {response.text}")
            await client.aclose()
            return None
    except Exception as e:
        logger.error(f"Error connecting to qBittorrent at {host}: {e}")
        await client.aclose()
        return None

async def send_to_qbittorrent(host: str, username: str, password: str, magnet_url: str, title: str = "") -> bool:
    """Sends a magnet link to qBittorrent with a tag for tracking."""
    client = await login_qbittorrent(host, username, password)
    if not client:
        return False
        
    try:
        data = {
            "urls": magnet_url,
            "category": "audiobooks"
        }
        if title:
            safe_title = title.replace(",", "").strip()
            data["tags"] = f"bayarr-{safe_title}"
            
        response = await client.post("/api/v2/torrents/add", data=data, timeout=5.0)
        
        if _succeeded(response):
            logger.info("Successfully added torrent to qBittorrent")
            return True
        else:
            logger.error(f"qBittorrent add torrent failed: {response.status_code} - {response.text}")
            return False
    except Exception as e:
        logger.error(f"Error adding torrent to qBittorrent: {e}")
        return False
    finally:
        await client.aclose()

async def send_torrent_file(host: str, username: str, password: str, torrent: bytes, title: str = "") -> bool:
    """Uploads a .torrent file to qBittorrent (for indexers that don't give magnet links)."""
    client = await login_qbittorrent(host, username, password)
    if not client:
        return False
    try:
        data = {"category": "audiobooks"}
        if title:
            data["tags"] = f"bayarr-{title.replace(',', '').strip()}"
        response = await client.post("/api/v2/torrents/add", data=data, timeout=15.0,
                                     files={"torrents": ("release.torrent", torrent, "application/x-bittorrent")})
        if _succeeded(response):
            logger.info("Successfully added torrent file to qBittorrent")
            return True
        logger.error(f"qBittorrent add torrent failed: {response.status_code} - {response.text}")
        return False
    except Exception as e:
        logger.error(f"Error adding torrent to qBittorrent: {e}")
        return False
    finally:
        await client.aclose()


async def get_completed_torrents(host: str, username: str, password: str, hashes=()):
    """Fetches completed torrents: the given ones (whatever their category: one qBittorrent
    already had, or one moved to another category) and those in the audiobooks category."""
    client = await login_qbittorrent(host, username, password)
    if not client:
        return []

    try:
        # filter=completed gets seeding/finished torrents
        queries = [{"filter": "completed", "category": "audiobooks"}]
        if hashes:
            queries.append({"filter": "completed", "hashes": "|".join(hashes)})
        found = {}
        for params in queries:
            response = await client.get("/api/v2/torrents/info", params=params, timeout=5.0)
            if response.status_code != 200:
                logger.error(f"qBittorrent get torrents failed: {response.status_code} - {response.text}")
                return []
            for torrent in response.json():
                found[(torrent.get("hash") or "").lower()] = torrent
        return list(found.values())
    except Exception as e:
        logger.error(f"Error fetching torrents from qBittorrent: {e}")
        return []
    finally:
        await client.aclose()


async def get_torrents(host: str, username: str, password: str, hashes):
    """Fetches the given torrents (for download progress). Returns None if qBittorrent can't be reached."""
    if not hashes:
        return []
    client = await login_qbittorrent(host, username, password)
    if not client:
        return None
    try:
        response = await client.get("/api/v2/torrents/info", params={"hashes": "|".join(hashes)}, timeout=5.0)
        if response.status_code == 200:
            return response.json()
        logger.error(f"qBittorrent get torrents failed: {response.status_code} - {response.text}")
        return None
    except Exception as e:
        logger.error(f"Error fetching torrents from qBittorrent: {e}")
        return None
    finally:
        await client.aclose()


async def test_connection(host: str, username: str, password: str):
    """Logs in and returns qBittorrent's version, or raises with a readable message."""
    async with httpx.AsyncClient(base_url=normalize_host(host), timeout=5.0) as client:
        try:
            login = await client.post("/api/v2/auth/login", data={"username": username, "password": password})
        except httpx.HTTPError as e:
            raise ConnectionError(f"Couldn't reach qBittorrent at {host} ({type(e).__name__}). Check the URL and that it's running.")
        if login.status_code == 403:
            raise ConnectionError("qBittorrent blocked this address after too many failed logins. Wait a while, or restart it.")
        if not _succeeded(login):
            raise ConnectionError("qBittorrent rejected the username or password.")
        version = await client.get("/api/v2/app/version")
        version.raise_for_status()
        return version.text.strip()


async def delete_torrents(host: str, username: str, password: str, hashes, delete_files: bool) -> bool:
    """Removes torrents from qBittorrent, optionally with their downloaded data."""
    client = await login_qbittorrent(host, username, password)
    if not client:
        return False
    try:
        response = await client.post("/api/v2/torrents/delete", timeout=10.0,
                                     data={"hashes": "|".join(hashes), "deleteFiles": "true" if delete_files else "false"})
        return response.status_code == 200
    except Exception as e:
        logger.error(f"Error deleting torrents from qBittorrent: {e}")
        return False
    finally:
        await client.aclose()
