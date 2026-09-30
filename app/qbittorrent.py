import httpx
import logging
from typing import Optional, Dict

logger = logging.getLogger(__name__)

async def login_qbittorrent(host: str, username: str, password: str) -> Optional[httpx.AsyncClient]:
    """Authenticates with qBittorrent Web API and returns an authenticated httpx client."""
    client = httpx.AsyncClient(base_url=host)
    try:
        data = {"username": username, "password": password}
        response = await client.post("/api/v2/auth/login", data=data, timeout=5.0)
        
        if response.text.strip() == "Ok.":
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
        
        if response.status_code == 200 and response.text.strip() == "Ok.":
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

async def get_completed_torrents(host: str, username: str, password: str):
    """Fetches completed torrents in the audiobooks category."""
    client = await login_qbittorrent(host, username, password)
    if not client:
        return []
        
    try:
        # filter=completed gets seeding/finished torrents
        params = {
            "filter": "completed",
            "category": "audiobooks"
        }
        response = await client.get("/api/v2/torrents/info", params=params, timeout=5.0)
        
        if response.status_code == 200:
            return response.json()
        else:
            logger.error(f"qBittorrent get torrents failed: {response.status_code} - {response.text}")
            return []
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
