"""Usenet downloads through NZBGet or SABnzbd (Settings > Download Client > Usenet).

A grabbed release's NZB is fetched from its indexer and handed to the client, which
downloads, repairs and unpacks it. Bayarr tracks the job by the client's id, kept on the
book as "nzbget:<id>" or "sab:<id>", imports it when it's done (copying it like any
import), then deletes the download: nothing needs to seed."""
import base64
import logging

import httpx

logger = logging.getLogger(__name__)

PREFIXES = ("nzbget:", "sab:")


def client(settings):
    """"nzbget", "sabnzbd" or "" (no Usenet client set up)."""
    kind = settings.get("usenet_client") or ""
    return kind if kind in ("nzbget", "sabnzbd") and settings.get("usenet_host") else ""


def is_usenet_id(download_id):
    return str(download_id or "").startswith(PREFIXES)


# --- NZBGet: JSON-RPC --------------------------------------------------------

async def _nzbget(settings, method, *params):
    url = settings["usenet_host"].rstrip("/") + "/jsonrpc"
    auth = (settings.get("usenet_user") or "", settings.get("usenet_pass") or "")
    async with httpx.AsyncClient(timeout=30.0, auth=auth if auth[0] else None) as http:
        res = await http.post(url, json={"method": method, "params": list(params), "id": 1})
    if res.status_code == 401:
        raise ValueError("NZBGet refused the username or password.")
    res.raise_for_status()
    data = res.json()
    if data.get("error"):
        raise ValueError(str(data["error"].get("message") if isinstance(data["error"], dict) else data["error"]))
    return data.get("result")


def _nzbget_size(item, prefix="FileSize"):
    """NZBGet gives sizes as MB and as two 32-bit halves of a byte count."""
    lo, hi = item.get(prefix + "Lo"), item.get(prefix + "Hi")
    if lo is not None and hi is not None:
        return (hi << 32) + lo
    return int((item.get(prefix + "MB") or 0) * 1024 * 1024)


# --- SABnzbd: its HTTP API --------------------------------------------------

async def _sab(settings, mode, files=None, **params):
    url = settings["usenet_host"].rstrip("/") + "/api"
    params = {"mode": mode, "output": "json", "apikey": settings.get("usenet_apikey") or "", **params}
    async with httpx.AsyncClient(timeout=30.0) as http:
        if files:
            res = await http.post(url, params=params, files=files)
        else:
            res = await http.get(url, params=params)
    res.raise_for_status()
    data = res.json()
    if isinstance(data, dict) and data.get("status") is False:
        raise ValueError(data.get("error") or "SABnzbd refused the request.")
    return data


# --- What Bayarr uses ---------------------------------------------------------

async def test(settings):
    """The client's version, or raises ValueError with a readable reason."""
    kind = client(settings)
    if not kind:
        raise ValueError("Choose NZBGet or SABnzbd and enter its URL.")
    try:
        if kind == "nzbget":
            return f"NZBGet {await _nzbget(settings, 'version')}"
        data = await _sab(settings, "version")
        await _sab(settings, "queue", limit=1)  # The version doesn't need the API key; this does
        return f"SABnzbd {data.get('version', '')}".strip()
    except httpx.HTTPError as e:
        raise ValueError(f"Couldn't reach it: {e}")


async def add(settings, nzb, name):
    """Hands an NZB to the client. Returns the job's id ("nzbget:12", "sab:SABnzbd_nzo_x")."""
    kind = client(settings)
    category = settings.get("usenet_category") or "audiobooks"
    filename = (name or "audiobook").replace("/", " ").strip()[:200] + ".nzb"
    if kind == "nzbget":
        content = base64.b64encode(nzb).decode()
        # append(NZBFilename, Content, Category, Priority, AddToTop, AddPaused, DupeKey, DupeScore, DupeMode, PPParameters)
        nzb_id = await _nzbget(settings, "append", filename, content, category, 0, False, False, "", 0, "SCORE", [])
        if not nzb_id or int(nzb_id) <= 0:
            raise ValueError("NZBGet didn't accept the NZB.")
        return f"nzbget:{nzb_id}"
    if kind == "sabnzbd":
        data = await _sab(settings, "addfile", files={"name": (filename, nzb, "application/x-nzb")},
                          cat=category, nzbname=name or "")
        ids = data.get("nzo_ids") or []
        if not ids:
            raise ValueError("SABnzbd didn't accept the NZB.")
        return f"sab:{ids[0]}"
    raise ValueError("No Usenet client is set up.")


def _state(progress=0.0, state="downloading", size=0, speed=0, eta=None, path="", message=""):
    return {"state": state, "progress": progress, "size": size, "speed": speed, "eta": eta, "path": path, "message": message}


async def status(settings, ids):
    """{id: state} for the given jobs: state is "queued", "downloading" (including repairing
    and unpacking), "completed" (with its path), "failed" (with a message) or "missing".
    None when the client can't be reached."""
    kind = client(settings)
    ids = [i for i in ids if is_usenet_id(i)]
    if not kind or not ids:
        return {}
    try:
        return await (_nzbget_status(settings, ids) if kind == "nzbget" else _sab_status(settings, ids))
    except (httpx.HTTPError, ValueError) as e:
        logger.warning(f"Usenet client unreachable: {e}")
        return None


async def _nzbget_status(settings, ids):
    queue = {f"nzbget:{g['NZBID']}": g for g in await _nzbget(settings, "listgroups", 0) or []}
    history = {f"nzbget:{h['NZBID']}": h for h in await _nzbget(settings, "history", False) or []}
    rate = ((await _nzbget(settings, "status")) or {}).get("DownloadRate", 0)
    found = {}
    for i in ids:
        if i in queue:
            g = queue[i]
            total, left = _nzbget_size(g), _nzbget_size(g, "RemainingSize")
            progress = (total - left) / total if total else 0.0
            state = "queued" if g.get("Status") in ("QUEUED", "PAUSED") else "downloading"
            eta = int(left / rate) if rate and state == "downloading" else None
            found[i] = _state(progress, state, total, rate if state == "downloading" else 0, eta, message=g.get("Status", ""))
        elif i in history:
            h = history[i]
            result = (h.get("Status") or "").upper()  # "SUCCESS/UNPACK", "FAILURE/PAR", "DELETED/MANUAL"...
            if result.startswith(("SUCCESS", "WARNING")):
                found[i] = _state(1.0, "completed", _nzbget_size(h), path=h.get("FinalDir") or h.get("DestDir") or "")
            elif result.startswith("DELETED"):
                found[i] = _state(state="missing")
            else:
                found[i] = _state(state="failed", message=f"NZBGet: {result.lower()}")
        else:
            found[i] = _state(state="missing")
    return found


async def _sab_status(settings, ids):
    queue = (await _sab(settings, "queue", limit=500)).get("queue") or {}
    slots = {f"sab:{s['nzo_id']}": s for s in queue.get("slots") or []}
    speed = float(queue.get("kbpersec") or 0) * 1024
    history = (await _sab(settings, "history", limit=500)).get("history") or {}
    done = {f"sab:{s['nzo_id']}": s for s in history.get("slots") or []}
    found = {}
    for i in ids:
        if i in slots:
            s = slots[i]
            total = float(s.get("mb") or 0) * 1024 * 1024
            progress = float(s.get("percentage") or 0) / 100
            state = "queued" if s.get("status") in ("Queued", "Paused", "Grabbing", "Propagating") else "downloading"
            found[i] = _state(progress, state, int(total), speed if state == "downloading" else 0, message=s.get("status", ""))
        elif i in done:
            s = done[i]
            if s.get("status") == "Completed":
                found[i] = _state(1.0, "completed", int(s.get("bytes") or 0), path=s.get("storage") or "")
            elif s.get("status") == "Failed":
                found[i] = _state(state="failed", message=f"SABnzbd: {s.get('fail_message') or 'failed'}")
            else:  # Verifying, repairing, extracting, moving...
                found[i] = _state(0.99, "downloading", int(s.get("bytes") or 0), message=s.get("status", ""))
        else:
            found[i] = _state(state="missing")
    return found


async def forget(settings, download_id):
    """Removes a finished job from the client's history (its files are dealt with by Bayarr)."""
    kind = client(settings)
    try:
        if kind == "nzbget" and download_id.startswith("nzbget:"):
            await _nzbget(settings, "editqueue", "HistoryDelete", "", [int(download_id.split(":", 1)[1])])
        elif kind == "sabnzbd" and download_id.startswith("sab:"):
            await _sab(settings, "history", name="delete", value=download_id.split(":", 1)[1])
    except Exception as e:
        logger.info(f"Couldn't remove {download_id} from the Usenet client's history: {e}")
