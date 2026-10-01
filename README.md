# Bayarr

*****VIBE-CODE PROJECT FOR LEARNING PURPOSES*****

An audiobook manager in the style of Sonarr and Radarr, built around AudiobookBay. Search Audible for a book, add it to your library, and Bayarr finds it on AudiobookBay, sends it to qBittorrent, checks the download and copies it into your audiobook folder. It also works as a plain **Torznab indexer** for Prowlarr, Listenarr and LazyLibrarian.

## Features

- **Library:** books from Audible move through `Unreleased` → `Monitored` → `Downloading` → `Imported`. Monitored books are searched when added and every 6 hours.
- **Smart AudiobookBay search:** several searches per book, every release scored against the book. Only clear matches are grabbed; box sets, dramatized and abridged versions and other books with the same title are skipped.
- **Import:** finished downloads are hardlinked or copied (so seeding continues), renamed, and checked against Audible's runtime.
- **Existing library:** import the audiobooks you already have, from Audiobookshelf's `metadata.json` or folder names. Files are never moved.
- **Series:** a Sonarr-style page with every series, Audible's full book list and what you're missing. Monitor a series to get the books you pick and every new release.
- **Editions:** narrated, dramatized (full cast, GraphicAudio) and abridged editions are told apart, matched, searched for and imported separately.
- **Calendar:** your books on their release dates, coloured by status like Sonarr's calendar, plus trending Audible releases filtered by trend and genre.
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
| Media Management | **Root Folder** for imported books. **Book Folder Format**, default `{Author} - {Series} {SeriesNumber} - {Title}` (also `{Authors}`, `{Year}`, `{Edition}`; empty parts are dropped; dramatized and abridged books get "(Dramatized)" / "(Abridged)" at the end even without `{Edition}`). **Rename Audio Files:** `Title.m4b`, or `Title - Part 01.mp3`… in disc and track order. **Use Hardlinks:** works when downloads and the Root Folder are in one mounted volume (e.g. `/data/downloads` and `/data/audiobooks`); otherwise files are copied. Don't run Audiobookshelf's "embed metadata" on hardlinked books. |
| Download Checks | Allowed difference from Audible's runtime (10% by default). |
| General | Language; *Prefer M4B*, *M4B only* or *Any format*; whether a matching narrator ranks releases higher; **Editions**: *Narrated only* (default), *Dramatized only* or *Both* for series monitoring (see [Editions](#editions)). |
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
   | Edition | Must be the book's edition: a narrated book rules out GraphicAudio/full-cast/dramatized and abridged releases, a dramatized book rules out plain ones. |
   | Ruled out | Box sets (`Books 1-6`, collections), other languages, releases rejected before. |

   A release is grabbed only if nothing rules it out and it scores 45 or more. Among clear matches within 10 points, M4B wins when preferred. **Manual Search** shows each release's score; hover for the reasons, or tick **Show rejected** to see what was ruled out and why.
3. **Download.** The magnet goes to qBittorrent with the `audiobooks` category and a `bayarr-` tag.
4. **Check and import.** Every minute Bayarr looks for finished downloads. The audio files' real length is compared with Audible's runtime, their tags and names must not say they're another edition (e.g. GraphicAudio for a narrated book), and with *M4B only* the format is checked. A release that fails becomes **Needs Review** in Activity, where you can **Import Anyway** or **Reject & Search Again** (that release is never grabbed again). Otherwise it's imported into the Root Folder with `metadata.json` and the cover, and Audiobookshelf is asked to scan.

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
- **Organize**: renames existing folders to your Book Folder Format, like Sonarr's Organize. A preview lists every proposed change (current folder struck through, the new one below) with its own **Approve** button, plus **Approve All**; nothing changes until you approve. Books inside the Root Folder end up directly in it (nested `Author/Series/Title` folders are flattened and emptied folders removed); books elsewhere are renamed where they are. Optionally the audio files are renamed too (`Title.m4b`, `Title - Part 01.mp3`). Folders only move on the same drive, so it's instant and hardlinks are kept; clashes with existing folders are shown and skipped.
- **Split into Books** (book details): for a folder holding several books, e.g. `Book 1 Title Part 1 of 2.m4b`, `Book 2 Other Part 1 of 2.m4b`, or a `Book N - Title` subfolder per book. Bayarr matches each to its series on Audible (in the folder's edition), you check the titles and numbers, and each book gets its own folder next to the original (`Author - Series 1 - Title (Dramatized)`), with its files hardlinked in (copied if the drive doesn't allow it), its own cover and `metadata.json`. The original folder is never changed; remove it yourself (and from Audiobookshelf) when you're happy.
- **A second copy** of a book that's already on disk in another folder is imported as its own entry; only books not on disk yet (e.g. Monitored) are linked to an imported folder.
- **Rescan** (also every 6 hours) refreshes files and flags deleted folders as **Missing**. An unreachable drive is left alone.

| Status | Meaning |
| --- | --- |
| Monitored | Wanted; searched when added and every 6 hours. |
| Unmonitored | Tracked, never searched automatically. |
| Unreleased | Becomes Monitored on its release date. |
| Downloading / Downloaded | In qBittorrent / finished, waiting for import. |
| Needs Review | Files failed a check; waiting for you in Activity. |
| Imported / Missing | On disk / folder gone (set to Monitored to download again). |

## Editions

A book can be **narrated** (one or a few narrators), **dramatized** (full-cast productions such as GraphicAudio, radio plays, Audible Original performances) or **abridged**. Audible labels dramatizations "unabridged" too, so Bayarr combines several signs: "full cast" or many narrators, GraphicAudio or BBC radio publishers, radio-production and performance listings, and words like "Dramatized Adaptation" in titles.

- **Separate books:** each edition is its own library entry, matched to its own Audible edition (with its own runtime), searched for in its own edition, and imported into its own folder, e.g. `Author - Series 1 - Title (Dramatized)`. Audiobookshelf gets a `Dramatized` tag and its `abridged` flag.
- **Import Existing:** the edition comes from `metadata.json`, the files' tags, and folder and file names (e.g. "Graphic Audio"). The preview shows it with the reason; change it before importing if it's wrong. Unclear ones (many narrators, or a folder that says GraphicAudio but an ASIN for the narrated edition) are highlighted.
- **Your library:** books from before editions existed are checked once in the background (Audible's edition by ASIN, plus what the files say). Filter the Library by **Dramatized**, **Abridged** or **Check edition**; change one in a book's details, or many with **Select** → **Set edition** (or **Detect again**).
- **Series:** the main list shows each book once (its narrated edition where there is one). **Other editions** below lists dramatized and abridged versions; editions Audible sells in parts ("Part 1 of 2") are one book. Having any edition of a book counts it as had. The **Editions** setting decides which editions **Monitor Series** offers and series syncs add.

## Series

The **Series** page lists every series your books are in. A book can be in several, such as #4 of a saga, #1 of a sub-series, and part of a universe. Each series shows posters or a table, and a bar of how many of Audible's books you have (orange when monitored, green when complete). You can filter and sort. Series for books imported from folder names are looked up on Audible in the background.

Open a series to see every book Audible lists, in order. Books you don't have show an **Add** button. Duplicate editions are merged, and dramatized versions are listed only when there's no regular one.

**Monitor Series** lets you tick which missing books to add. Monitored series are checked every 6 hours, and new releases are added automatically. Books you didn't pick aren't added again.

## Calendar

Month, week and agenda views, laid out like Sonarr's calendar. Your library's books appear on their release dates, coloured by status:

| Colour | Status |
| --- | --- |
| Green | On disk |
| Purple | Downloading |
| Red | Missing (wanted and released, not on disk) |
| Orange | Needs review |
| Blue | Upcoming |
| Grey | Unmonitored |

With **Trending** on, recent and upcoming Audible releases you don't have are added with a dashed outline. They come from Audible's top 500 best sellers (pre-orders included), the top 100 of each genre, and new books by the authors in your library. They're refreshed in the background every 12 hours; **Refresh** reloads them.

- **Trend filter:** all trending, top 100 best sellers, top in their genre, from your authors, in your series, series starts (#1), or highly rated.
- **Genre filter:** Audible's 24 genres.

Click a trending book for its details, then **Add to Library** (upcoming books become Unreleased and are downloaded on release day) or **View Series**.

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
