# Bayarr

*****VIBE-CODE PROJECT FOR LEARNING PURPOSES*****

An audiobook manager in the style of Sonarr and Radarr, built around AudiobookBay. Search Audible for a book, add it to your library, and Bayarr finds it on AudiobookBay, sends it to qBittorrent, checks the download and copies it into your audiobook folder. It also works as a plain **Torznab indexer** for Prowlarr, Listenarr and LazyLibrarian.

## Features

- **Library:** books from Audible move through `Unreleased` → `Monitored` → `Downloading` → `Imported`. Monitored books are searched when added and every 6 hours.
- **Smart AudiobookBay search:** several searches per book, every release scored against the book. Only clear matches are grabbed; box sets, dramatized and abridged versions and other books with the same title are skipped.
- **Import:** finished downloads are hardlinked or copied (so seeding continues), renamed, and checked against Audible's runtime.
- **Existing library:** import the audiobooks you already have, from Audiobookshelf's `metadata.json` or folder names. Files are never moved.
- **Series:** a Sonarr-style page with every series, Audible's full book list and what you're missing. Monitor a series to get the books you pick and every new release.
- **Activity:** live download queue and history. **Audiobookshelf:** metadata, covers and library scans.

## Quick start (Docker)

1. Clone the repo and run `cp .env.example .env`.
2. Optional but recommended: log in to `audiobookbay.lu`, open Developer Tools (F12) → Network, refresh, click the page request and copy its `Cookie` header into `.env`, **in single quotes** (it contains `$`):
   ```env
   ABB_COOKIE='your_cookie_string_here'
   ```
3. Mount your config, library and downloads in `docker-compose.yml`:
   ```yaml
   volumes:
     - ./config:/config
     - /mnt/media/audiobooks:/audiobooks
     - /mnt/downloads:/downloads
   ```
4. `docker compose up -d`, then open `http://<your-server>:8000`.

A prebuilt image, `ghcr.io/epurl/audiobookbay-torznab:latest`, is published on every push to `main`.

## Settings

Changes are saved with the **Save Changes** bar at the bottom.

| Section | What to set |
| --- | --- |
| Security | A username and password. Until one is set, only local network addresses can connect. |
| Download Client | qBittorrent's URL (often `http://qbittorrent:8080`), username and password; **Test Connection** checks them. **Stalled Downloads:** a download with no progress for 6 hours (0 turns this off) is removed with its partial files, and the next-best release is grabbed. |
| Downloads Folder | Only when qBittorrent and Bayarr see downloads at different paths: Bayarr's path to qBittorrent's `audiobooks` save folder. If qBittorrent reports `/data/torrents/audiobooks/Book` and this is `/downloads/audiobooks`, Bayarr reads `/downloads/audiobooks/Book`. |
| Media Management | **Root Folder** for imported books. **Book Folder Format**, default `{Author} - {Series} {SeriesNumber} - {Title}` (also `{Authors}`, `{Year}`; empty parts are dropped). **Rename Audio Files:** `Title.m4b`, or `Title - Part 01.mp3`… in disc and track order. **Use Hardlinks:** works when downloads and the Root Folder are in one mounted volume (e.g. `/data/downloads` and `/data/audiobooks`); otherwise files are copied. Don't run Audiobookshelf's "embed metadata" on hardlinked books. |
| Download Checks | Allowed difference from Audible's runtime (10% by default). |
| General | Language; *Prefer M4B*, *M4B only* or *Any format*; whether a matching narrator ranks releases higher. |
| Audiobookshelf | See [Audiobookshelf](#audiobookshelf). |
| Backup | Download or restore the database. A daily copy of the last 7 days is kept in `config/backups`. Backups include your passwords and tokens. |

## How downloads work

1. **Search.** AudiobookBay's search matches words anywhere in a post, so Bayarr runs several searches, most specific first, and stops at a clear match:
   1. title and surname
   2. title and full name
   3. series and number
   4. the title alone
   5. the author

   Search results (including the ones the site sends encoded) and the pages of the best ones are read in full.
2. **Score.** Each release is scored out of 100 against the book:

   | Checked | Rule |
   | --- | --- |
   | Title, author | Every title word and the author's surname must be there. Exact titles score higher; extra words lower. |
   | Series number | `[Series 7]`, `Book 7`, `#7` must match. |
   | Narrator, format | A matching narrator and M4B (when preferred) score higher. |
   | Size | Compared with Audible's runtime: far too small or large is ruled out. |
   | Ruled out | Box sets (`Books 1-6`, collections), dramatized/full-cast/GraphicAudio, abridged, other languages, releases rejected before. |

   A release is grabbed only if nothing rules it out and it scores 45 or more. Among clear matches within 10 points, M4B wins when preferred. **Manual Search** shows each release's score; hover for the reasons, or tick **Show rejected** to see what was ruled out and why.
3. **Download.** The magnet goes to qBittorrent with the `audiobooks` category and a `bayarr-` tag.
4. **Check and import.** Every minute Bayarr looks for finished downloads. The audio files' real length is compared with Audible's runtime, and with *M4B only* the format is checked. A release that fails becomes **Needs Review** in Activity, where you can **Import Anyway** or **Reject & Search Again** (that release is never grabbed again). Otherwise it's imported into the Root Folder with `metadata.json` and the cover, and Audiobookshelf is asked to scan.

Searches are spaced a second apart and cached for 15 minutes. If AudiobookBay stops responding, searches pause for 5 minutes.

## Library

**Import Existing** (Library tab) scans a folder; each folder with audio files is one book (`CD1`, `Disc 2` subfolders included). Details come from Audiobookshelf's `metadata.json` when present, otherwise the folder name:

| Folder | Author | Series | # | Title |
| --- | --- | --- | --- | --- |
| `Jane Author - Series Name 3.5 - Book Title` | Jane Author | Series Name | 3.5 | Book Title |
| `Jane Author - Series Name Book 1 - Book Title` | Jane Author | Series Name | 1 | Book Title |
| `Jane Author - Universe - Series Name - #3 - Book Title` | Jane Author | Series Name | 3 | Book Title |
| `Jane Author - Series Name, Vol. 07 - Book Title` | Jane Author | Series Name | 7 | Book Title |
| `Jane Author - Book Title` | Jane Author | | | Book Title |
| `Jane Author/Series Name 1 - Book Title` | Jane Author | Series Name | 1 | Book Title |

The preview shows each book as **New**, **Link to library** (a book you already track, matched by ASIN or title and author) or **In library**.

Managing books:
- **Filter and sort.** The **Wanted** and **Not matched on Audible** filters are especially useful.
- **Select** several books to change their status, match them on Audible, or remove them.
- **Match on Audible** fills in the ASIN, runtime, narrators and series for books imported from folder names. Matching many books at once accepts only clear-cut matches. Your files are never changed.
- **Book details:** edit, see files, **Search Now**, **Manual Search**, or **Remove**. Removing never deletes files.
- **Rescan** (also every 6 hours) refreshes files and flags deleted folders as **Missing**. An unreachable drive is left alone.

| Status | Meaning |
| --- | --- |
| Monitored | Wanted; searched when added and every 6 hours. |
| Unmonitored | Tracked, never searched automatically. |
| Unreleased | Becomes Monitored on its release date. |
| Downloading / Downloaded | In qBittorrent / finished, waiting for import. |
| Needs Review | Files failed a check; waiting for you in Activity. |
| Imported / Missing | On disk / folder gone (set to Monitored to download again). |

## Series

The **Series** page lists every series your books are in. A book can be in several, such as #4 of a saga, #1 of a sub-series, and part of a universe. Each series shows posters or a table, and a bar of how many of Audible's books you have (orange when monitored, green when complete). You can filter and sort. Series for books imported from folder names are looked up on Audible in the background.

Open a series to see every book Audible lists, in order. Books you don't have show an **Add** button. Duplicate editions are merged, and dramatized versions are listed only when there's no regular one.

**Monitor Series** lets you tick which missing books to add. Monitored series are checked every 6 hours, and new releases are added automatically. Books you didn't pick aren't added again.

## Audiobookshelf

- **Write metadata.json and Cover** (on by default): imported downloads get Audiobookshelf's `metadata.json` and Audible's 1000px cover. Folders from Import Existing are never changed.
- **Server URL, API Token, Library:** after each import, Bayarr asks Audiobookshelf to scan that library. The token needs admin rights and is never sent to the browser.

## Torznab indexer

Add a **Torznab** indexer in Prowlarr, Listenarr or LazyLibrarian:
- **URL:** `http://<your-server>:8000/api`.
- **API key:** anything; it isn't checked.
- **Categories:** `3000` and `3030`.

These endpoints don't need the Bayarr login. Searches (`t=search` / `t=book`) return AudiobookBay results with magnet links.

## Environment variables

| Variable | Default | Description |
| --- | --- | --- |
| `ABB_COOKIE` | *(empty)* | AudiobookBay session cookie, in single quotes in `.env`. |
| `ABB_USER_AGENT` | Chrome | User agent; match the browser the cookie came from. |
| `BAYARR_USERNAME` / `BAYARR_PASSWORD` | *(empty)* | Fixed UI login, overriding the one in Settings. |
| `BAYARR_CONFIG_DIR` | `/config` (Docker), `./config` | Where the database and backups are stored. |

## Security

- Passwords are never sent to the browser; the login is stored as a salted PBKDF2 hash.
- `/api` and `/api/download` (Torznab) are public by design and only fetch AudiobookBay pages.
- Behind a reverse proxy, requests can look local, so **set a login** before exposing Bayarr.

## Running without Docker

```bash
pip install -r requirements.txt
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```
