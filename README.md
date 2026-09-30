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
  - Audio files are **copied** (not moved) into a folder named like `Author - Series N - Title` (configurable), so qBittorrent keeps seeding.
  - Files are renamed to `Title.m4b`, or `Title - Part 01.mp3`, `Part 02`, … in disc and track order. The release's cover image comes along as `cover.jpg`.
- **Existing library import:** scans the audiobooks you already have and adds them to the library. It reads Audiobookshelf's `metadata.json` when a folder has one, otherwise the folder name. Files are never moved or renamed.
- **Library management:**
  - Filter, sort and search the library, including "Wanted" and "Not matched on Audible" filters.
  - Select many books to change their status, match them on Audible, or remove them at once.
  - Match books imported from folder names to their Audible edition, automatically or by picking from search results.
  - Edit a book's details and status.
  - See its files on disk, search for it on demand, or remove it (files are kept).
  - Books whose folder disappears are flagged as **Missing**.
- **Series monitoring:** monitor a whole Audible series and see which books you're missing. New releases are added automatically.
- **Download checks:** before importing, the downloaded files' real length is compared with Audible's runtime, so a wrong or abridged release is held for review instead of being imported.
- **Activity:** a live download queue with progress from qBittorrent, plus a history of everything grabbed, imported, rejected or flagged.
- **Audiobookshelf:** imported books get a `metadata.json` and Audible cover in Audiobookshelf's format, and Bayarr can start an Audiobookshelf library scan after each import.
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
     - /mnt/media/audiobooks:/audiobooks   # your existing library, for Import Existing
     - /mnt/downloads:/downloads
   ```
4. Start it:
   ```bash
   docker compose up -d
   ```
5. Open `http://<your-server>:8000`.

A prebuilt image is also published to `ghcr.io/epurl/audiobookbay-torznab:latest` on every push to `main`. You can use it instead of `build: .`.

## First-time setup

Open **Settings**. It's split into sections; changes are saved with the **Save Changes** bar at the bottom (it turns orange when something is unsaved).

1. **Security:** set a username and password. Until you do, Bayarr only accepts connections from local network addresses (`192.168.x.x`, `10.x.x.x`, `172.16-31.x.x`, localhost). Your browser will ask you to sign in after you save a login.
2. **Download Client:**
   - Enable qBittorrent and enter its Web UI URL, username and password. From another container this is usually `http://qbittorrent:8080`. **Test Connection** checks them before you save.
   - **Downloads Folder (optional):** only needed when qBittorrent and Bayarr see downloads at different paths. See below.
   - **Stalled Downloads:** a download with no progress for 6 hours (you can change this; 0 turns it off) is removed from qBittorrent along with its partial files, that release is never grabbed again for the book, and the next-best one is grabbed. A torrent you delete from qBittorrent yourself puts the book back to Monitored.
3. **Media Management:**
   - **Root Folder:** where imported books go, as Bayarr sees it (e.g. `/audiobooks`). Use **Browse** to pick it.
   - **Book Folder Format:** how new downloads are named inside the Root Folder. The default is `{Author} - {Series} {SeriesNumber} - {Title}`, for example `Jim Butcher - The Dresden Files 4 - Summer Knight`. Books without a series drop that part: `Jaysea Lynn - For Whom the Belle Tolls`. Available tokens: `{Author}`, `{Authors}`, `{Series}`, `{SeriesNumber}`, `{Title}`, `{Year}`.
   - **Rename Audio Files** (on by default): a single file becomes `Summer Knight.m4b`; several files become `Summer Knight - Part 01.mp3`, `Part 02`, … numbered in disc-then-track order (`CD1/01`, `CD1/02`, `CD2/01`, …). Turn it off to keep the release's own file names and subfolders.
   - **Use Hardlinks** (on by default): when the downloads and the Root Folder are on the same drive, imported files are hardlinked instead of copied, so a seeding book doesn't take up space twice. If they're on different drives Bayarr copies instead. In Docker, hardlinks only work when both folders are inside **one** mounted volume (e.g. mount `/mnt/data` as `/data`, and use `/data/downloads` and `/data/audiobooks`). Don't use Audiobookshelf's "embed metadata" tool on hardlinked books: it would change the files qBittorrent is seeding.
   - **Download Checks:** see [Download checks](#download-checks).
4. **General:** preferred language, audio format (*Prefer M4B*, *M4B only* or *Any format*), and whether a matching narrator ranks releases higher.
5. **Audiobookshelf:** see [Audiobookshelf](#audiobookshelf).
6. **Backup:** download a backup of the database, or restore one. Bayarr also keeps a daily copy of the last 7 days in `config/backups`. A backup includes your qBittorrent password and Audiobookshelf token, so keep it private. Restoring keeps your current login, and saves the current database to `config/backups` first.

### Downloads Folder (remote path mapping)

qBittorrent reports where it saved a torrent using **its own** paths. If Bayarr runs in a different container, that path may not exist for Bayarr.

Set **Downloads Folder** to the path *inside Bayarr* that points to the same place as qBittorrent's save folder for the `audiobooks` category. Bayarr then replaces qBittorrent's save path with that folder and keeps any subfolders below it.

| qBittorrent reports | Downloads Folder | Bayarr reads from |
| --- | --- | --- |
| `/data/torrents/audiobooks/Book Name` | `/downloads/audiobooks` | `/downloads/audiobooks/Book Name` |

If both containers mount the downloads at the same path, leave this empty.

## Importing your existing library

On the **Library** tab, click **Import Existing**. The folder defaults to your Root Folder; you can pick another with **Browse**. Then click **Scan**.

Bayarr treats every folder that holds audio files as one book. Audio files in `CD1`, `Disc 2` or `Part 3` subfolders count as part of the same book. Details come from:

1. **Audiobookshelf's `metadata.json`**, when the folder has one. This is the most accurate source: it has the real authors, narrators, series and ASIN.
2. **Otherwise, the folder name.** These layouts are recognized:

| Folder | Author | Series | # | Title |
| --- | --- | --- | --- | --- |
| `James S. A. Corey - The Expanse 3.5 - The Vital Abyss` | James S. A. Corey | The Expanse | 3.5 | The Vital Abyss |
| `Jim Butcher - The Dresden Files Book 1 - Storm Front` | Jim Butcher | The Dresden Files | 1 | Storm Front |
| `Frank Herbert - Dune - Prelude to Dune - #3 - House Corrino` | Frank Herbert | Prelude to Dune | 3 | House Corrino |
| `Chugong - Solo Leveling, Vol. 07 - Solo Leveling, Book 07` | Chugong | Solo Leveling | 7 | Solo Leveling, Book 07 |
| `Jaysea Lynn - For Whom the Belle Tolls` | Jaysea Lynn | | | For Whom the Belle Tolls |
| `Brandon Sanderson/Mistborn 1 - The Final Empire` (nested) | Brandon Sanderson | Mistborn | 1 | The Final Empire |

The preview lists everything found before anything is imported. Each book is marked:
- **New**: added to the library as **Imported**.
- **Link to library**: you already track this book (for example, it's Monitored). It is matched by ASIN, or by title and first author, and linked to these files instead of being added twice.
- **In library**: already imported from this folder; skipped.

`cover.jpg` (or `folder.jpg`, or another image in the folder) is used as the cover. Books you own also show as **In Library** in Search results.

## Managing the library

- **Filter and sort:** filter by text or status; sort by author, title, series or date added. **Wanted** shows books not on disk yet; **Not matched on Audible** shows books without an ASIN.
- **Select:** click **Select**, pick books (or **Select all shown**), then set their status, match them on Audible, or remove them.
- **Match on Audible:** books imported from folder names don't have Audible's details. Matching fills in the ASIN, runtime, narrators, series and description, which makes the download length check, series monitoring and "In Library" detection work for them.
  - **For many books:** filter by *Not matched on Audible*, **Select all shown**, **Match on Audible**. This runs in the background and only accepts clear-cut matches: the title and first author must match, dramatized versions are skipped, and a matching series number wins. Books it isn't sure about are left for you.
  - **For one book:** open it and click **Match on Audible** to search Audible and pick the right edition yourself.
  - Your files, folder and status are never changed by a match.
- **Book details:** click a book to:
  - edit its title, authors, narrators, series, number, ASIN or status
  - see its folder and audio files
  - use **Search Now**, which grabs the best release from AudiobookBay
  - use **Manual Search**, where you pick a release yourself
  - use **Match on Audible**, to fill in its details from Audible
  - **Remove** it from Bayarr (files on disk are never deleted)
- **Statuses:**

| Status | Meaning |
| --- | --- |
| Monitored | Wanted. Searched when added and every 6 hours. |
| Unmonitored | Kept in the library but never searched automatically. |
| Unreleased | Release date is in the future. Becomes Monitored on release day. |
| Downloading / Downloaded | Sent to qBittorrent / finished, waiting for import. |
| Needs Review | Finished, but the files failed a check (see Download checks). Waiting for you in Activity. |
| Imported | On disk. |
| Missing | Was on disk, but its folder is gone. Not searched automatically; set it to Monitored to download it again. |

- **Rescan:** refreshes file counts and sizes, and marks books whose folders were deleted as **Missing**. Books whose folders come back return to **Imported**. This also runs every 6 hours. If a whole drive or mount is unreachable, its books are left alone rather than flagged.

## Series

Click **Monitor Series** on a series in Search results, or in a book's details in the Library. This also works for books imported from folder names: Bayarr looks the book up on Audible to find its series. Then choose which books to download:

- **All books I don't have:** every missing book becomes Monitored.
- **Only new releases:** books already out are added as Unmonitored, so the gaps are visible but nothing is downloaded. Future books are downloaded when they come out.

Series are checked against Audible every 6 hours, and newly announced books are added. Bayarr keeps one entry per book: dramatized adaptations (GraphicAudio), box sets and duplicate UK/US editions are left out. Books you already have are matched and filled in with Audible's details (ASIN, runtime, narrators), which makes the download check below work for them too.

The **Series** page shows each series with how many books are on disk, and lists every book with its status. From there you can sync a series now, pause it by unticking Monitored, or remove it. Removing a series keeps its books in your library.

## Download checks

AudiobookBay listings are typed in by uploaders, so the format, narrator and even the book can be wrong. Bayarr checks the files it actually downloaded:

- **Length:** the total play time of the audio files is compared with Audible's runtime. If it's off by more than the allowed difference (10% by default), the book is set to **Needs Review** instead of being imported. This catches wrong books and abridged editions. It can't tell a dramatized version from the original, since those often run about the same length; dramatized releases are skipped by title instead. Books without an Audible runtime, such as ones imported from folder names that haven't been matched, aren't checked.
- **Format:** with *M4B only*, a release listed as M4B that turns out to contain MP3 files is held for review.

Books waiting for review show a badge on **Activity**. For each one you can:
- **Import Anyway:** it's imported within a minute.
- **Reject & Search Again:** that release is never grabbed again for this book, and a new search starts. The torrent stays in qBittorrent for you to remove.

## Activity

- **Queue:** books on their way in, with progress, speed, time left and seeders from qBittorrent. Books waiting for review can be approved or rejected here.
- **History:** the last 1000 events: grabbed, imported, needs review, approved, rejected, stalled, failed, missing, matched, series additions and releases.

## Audiobookshelf

Under **Settings → Audiobookshelf**:

- **Write metadata.json and Cover** (on by default): each imported download gets a `metadata.json` in Audiobookshelf's format, and Audible's cover at 1000px as `cover.jpg`. Audiobookshelf then shows the right title, authors, narrators, series and description. This only applies to downloads; folders brought in with Import Existing are never changed.
- **Server URL, API Token and Library:** enter your Audiobookshelf address and an API token, click **Test & Load Libraries**, choose a library and save. After every import, Bayarr asks Audiobookshelf to scan that library so the book appears right away. The token needs admin rights and is never sent back to the browser.

## How it works

1. **Search** Audible from the Search tab and click **Add to Library**, or click a book to open **Manual Search** and pick a specific AudiobookBay release.
2. When a Monitored book is added (and every 6 hours after that), Bayarr searches AudiobookBay and picks the best release:
   - The language must match your setting.
   - All words of the main title must be in the release title.
   - Collections and box sets are skipped.
   - Dramatized versions and releases rejected before (by you, or because they stalled) are skipped.
   - M4B and a matching narrator score higher.
3. The magnet link goes to qBittorrent with the `audiobooks` category and a `bayarr-<title>` tag. The book becomes **Downloading**.
4. Every minute Bayarr asks qBittorrent for finished torrents in that category. When its download finishes, the book becomes **Downloaded**. Its audio files (`.m4b`, `.mp3`, `.m4a`, `.flac`, `.ogg`, `.opus`, `.aac`) are then copied into a new folder inside the Root Folder, named using the Book Folder Format and renamed (unless Rename Audio Files is off). If the files fail a check, the book becomes **Needs Review** instead (see Download checks). Otherwise `metadata.json` and the cover are written, Audiobookshelf is asked to scan, and the book becomes **Imported**.

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
