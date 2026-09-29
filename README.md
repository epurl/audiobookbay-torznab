# Bayarr

*****VIBE-CODE PROJECT FOR LEARNING PURPOSES*****

Bayarr is an audiobook manager in the style of Sonarr and Radarr, built around AudiobookBay. You search Audible for a book, add it to your library, and Bayarr finds it on AudiobookBay, sends it to qBittorrent, and copies the finished files into your audiobook folder.

It also still works as a plain **Torznab indexer** for AudiobookBay, so apps like Prowlarr, Listenarr and LazyLibrarian can search it directly.

## Features

- **Audible search:** titles, authors, narrators, series grouping and release dates from Audible's catalog.
- **AudiobookBay search:** shows size, format (M4B, MP3, ...), language and narrator for each release, and highlights releases whose narrator matches Audible's.
- **Library with automatic downloads:**
  - Books move through `Unreleased` → `Monitored` → `Downloading` → `Downloaded` → `Imported`.
  - Adding a released book starts a search straight away.
  - Monitored books are searched again every 6 hours.
  - Unreleased books switch to Monitored on their release date.
- **Picking the right release:**
  - Matches your language setting and prefers M4B (or requires it).
  - Every word of the book's title must appear in the release title.
  - Multi-book collections are skipped.
- **qBittorrent:** downloads go into an `audiobooks` category and are tracked by torrent hash and a `bayarr-` tag.
- **Import:**
  - Finished downloads are checked for every minute.
  - Audio files are **copied** (not moved) into `Root Folder/Author - Title`, so qBittorrent keeps seeding.
- **Docker friendly:**
  - The library and settings live in `/config`.
  - A Downloads Folder setting handles qBittorrent seeing different paths than Bayarr.
  - A folder browser lets you pick folders from inside the container.

## Quick start (Docker)

1. Clone the repo and copy the example environment file:
   ```bash
   cp .env.example .env
   ```
2. (Optional but recommended) Add your AudiobookBay session cookie to `.env`:
   - Go to `audiobookbay.lu` in your browser and log in.
   - Open Developer Tools (F12) → Network tab, refresh, and click the main document request.
   - Copy the whole `Cookie` request header into `.env`, **inside single quotes** (the cookie contains `$` characters that Docker Compose would otherwise strip):
     ```env
     ABB_COOKIE='your_cookie_string_here'
     ```
   `.env` is git-ignored. Never put the cookie in `docker-compose.yml`.
3. In `docker-compose.yml`, mount your audiobook library and qBittorrent's download folder, for example:
   ```yaml
   volumes:
     - ./config:/config
     - /mnt/media/audiobooks:/audiobooks
     - /mnt/downloads:/downloads
   ```
4. Start it:
   ```bash
   docker compose up -d
   ```
5. Open `http://<your-server>:8000`.

A prebuilt image is also published to `ghcr.io/epurl/audiobookbay-torznab:latest` on every push to `main`. You can use it instead of `build: .`.

## First-time setup

Open the **Settings** tab.

1. **Security:** set a username and password. Until you do, Bayarr only accepts connections from local network addresses (`192.168.x.x`, `10.x.x.x`, `172.16-31.x.x`, localhost). Your browser will ask you to sign in after you save a login.
2. **Download Client:** enable qBittorrent and enter its WebUI URL, username and password. From another container this is usually `http://qbittorrent:8080`.
3. **Root Folder:** where imported books go, as Bayarr sees it (e.g. `/audiobooks`). Use **Browse** to pick it.
4. **Downloads Folder (optional):** only needed when qBittorrent and Bayarr see downloads at different paths. See below.
5. **Audio Format:**
   - *Prefer M4B* (default): grab M4B when there's a choice.
   - *M4B only*: never grab anything else.
   - *Any format*: no preference.

### Downloads Folder (remote path mapping)

qBittorrent reports where it saved a torrent using **its own** paths. If Bayarr runs in a different container, that path may not exist for Bayarr.

Set **Downloads Folder** to the path *inside Bayarr* that points to the same place as qBittorrent's save folder for the `audiobooks` category. Bayarr then replaces qBittorrent's save path with that folder and keeps any subfolders below it.

| qBittorrent reports | Downloads Folder | Bayarr reads from |
| --- | --- | --- |
| `/data/torrents/audiobooks/Book Name` | `/downloads/audiobooks` | `/downloads/audiobooks/Book Name` |

If both containers mount the downloads at the same path, leave this empty.

## How it works

1. **Search** Audible from the Search tab and click **Add to Library**, or click a book to open **Manual Search** and pick a specific AudiobookBay release.
2. When a Monitored book is added (and every 6 hours after that), Bayarr searches AudiobookBay and picks the best release:
   - The language must match your setting.
   - All words of the main title must be in the release title.
   - Collections and box sets are skipped.
   - M4B and a matching narrator score higher.
3. The magnet link goes to qBittorrent with the `audiobooks` category and a `bayarr-<title>` tag. The book becomes **Downloading**.
4. Every minute Bayarr asks qBittorrent for finished torrents in that category. When its download finishes, the book becomes **Downloaded**. Its audio files (`.m4b`, `.mp3`, `.m4a`, `.flac`, `.ogg`, `.opus`, `.aac`) are then copied into `Root Folder/Author - Title` and it becomes **Imported**.

## Using it as a Torznab indexer

The Torznab endpoints don't need the Bayarr login, so indexer apps can reach them.

1. Add a new **Torznab** (or Generic Torznab) indexer in Prowlarr, Listenarr or LazyLibrarian.
2. Set the **URL** to `http://<your-server>:8000/api`.
3. The **API Key** can be left blank or set to anything; it isn't checked.
4. Select the **Audio / Audiobook** categories (`3000`, `3030`).
5. Test the connection.

Searches (`t=search` or `t=book` with `q`, `author`, `title`) become AudiobookBay searches. Each result includes a magnet link built from the release's info hash.

## Environment variables

| Variable | Default | Description |
| --- | --- | --- |
| `ABB_COOKIE` | *(empty)* | AudiobookBay session cookie. Wrap it in single quotes in `.env`. |
| `ABB_USER_AGENT` | Chrome user agent | User agent for AudiobookBay requests. Match the browser you took the cookie from. |
| `BAYARR_USERNAME` / `BAYARR_PASSWORD` | *(empty)* | Fixed login for the UI. When set, it overrides the login saved in Settings. |
| `BAYARR_CONFIG_DIR` | `/config` in Docker, `./config` locally | Where `database.json` (library and settings) is stored. |

## Security notes

- The qBittorrent password and the Bayarr login are never sent to the browser. The login is stored as a salted PBKDF2 hash.
- The Torznab endpoints (`/api`, `/api/download`) are public by design. They only fetch AudiobookBay pages.
- Behind a reverse proxy in Docker, requests usually appear to come from the proxy's (local) address, so **set a login** before exposing Bayarr through one.

## Running locally (without Docker)

```bash
pip install -r requirements.txt
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

The library and settings are saved to `./config/database.json`.
