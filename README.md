# Bayarr

*****VIBE-CODE PROJECT FOR LEARNING PURPOSES*****

An audiobook manager in the style of Sonarr and Radarr, built around AudiobookBay. Search Audible for a book, add it to your library, and Bayarr finds it on AudiobookBay, sends it to qBittorrent, checks the download and copies it into your audiobook folder. It also works as a plain **Torznab indexer** for Prowlarr, Listenarr and LazyLibrarian.

## Features

- **Library:** books from Audible move through `Unreleased` → `Monitored` → `Downloading` → `Imported`. Monitored books are searched when added and every 6 hours.
- **Smart AudiobookBay search:** several searches per book, every release scored against the book. Only clear matches are grabbed; box sets, abridged versions (dramatizations included) and other books with the same title are skipped.
- **Import:** finished downloads are copied (so seeding continues), renamed, and checked against Audible's runtime.
  A download holding several books of a series (`Series 01 - Title, Part 1.m4b`, `Book 2 - Title`...) that's too long for the book: only the grabbed book's files are imported (picked by title, else by number, and they must match Audible's length). The pack's other books are imported too if they're in your library as Monitored or Missing, in the same series and edition, and their length matches.
  Downloads that are archives (`.zip`, `.7z`, or a folder of them) are unpacked into a temporary `.bayarr-unpack` folder in the Root Folder, imported from there, and the unpacked copy is removed; the archive keeps seeding. `.rar` works only if the installed 7-Zip can open it. An archive that can't be unpacked, holds no audio, or contains paths pointing outside its folder goes to Needs Review with the reason.
- **Import** (Search page): add the audiobooks you already have, from Audiobookshelf's `metadata.json` or folder and file names, matched on Audible. Keep the files where they are, or copy or move them into your Root Folder, renamed; the preview shows where each one goes.
- **Series:** a Sonarr-style page with every series, Audible's full book list and what you're missing. Monitor a series to get the books you pick and every new release.
- **Editions:** unabridged and abridged (which includes dramatizations such as GraphicAudio) are told apart, matched, searched for and imported separately, matching Audiobookshelf's Abridged setting.
- **Authors:** an author's page with all their books; follow an author to get their new releases automatically.
- **Calendar:** your books on their release dates, coloured by status like Sonarr's calendar, plus trending Audible releases filtered by trend and genre.
- **Activity:** live download queue, torrents still seeding (removed automatically after a ratio or a number of days, if you like), and history (show 10, 20, 50, 100 or all). Book titles link to the book (`#/book/<id>`) in Activity, Manual Import, series and author pages, the Calendar, System > Health, and Search (**In Library**). **Audiobookshelf:** metadata, covers and library scans.
- **Manual Import** (Search page): like Sonarr's. Scan a folder (your Downloads Folder unless you pick another) and each item is listed: a folder of audio files, a single file, an archive, or one book of a folder holding several. Each gets a guess: a library book waiting for files, else a clear match on Audible. **Change** searches your library and Audible, or uses the item as it's named. Tick the items and **Import** them, either **copied** (the originals stay and keep seeding) or **moved** (the originals are deleted afterwards). Files are renamed and placed like any import; a book already on disk isn't imported twice.

## Quick start (Docker)

1. Clone the repo and run `cp .env.example .env`.
2. Optional but recommended: add your AudiobookBay cookie in **Settings → Indexers** after starting (it explains where to find it). Or put it in `.env`, **in single quotes** (it contains `$`):
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
| Download Client | qBittorrent's URL (often `http://qbittorrent:8080`), username and password; **Test Connection** checks them. **Stalled Downloads:** a download with no progress for 6 hours (0 turns this off) is removed with its partial files, and the next-best release is grabbed. **Seeding:** with **Remove Torrents After Seeding** on, a torrent is removed once its book is imported and it reaches the **Ratio** or has seeded for the number of **Days** (whichever comes first; 0 turns a limit off), optionally with its downloaded files (the library copy stays). Only torrents Bayarr added; Activity lists them under **Seeding** with ratio, upload, seeding time, when each will be removed, and **Remove now**. |
| Downloads Folder | Only when qBittorrent and Bayarr see downloads at different paths: Bayarr's path to qBittorrent's `audiobooks` save folder. If qBittorrent reports `/data/torrents/audiobooks/Book` and this is `/downloads/audiobooks`, Bayarr reads `/downloads/audiobooks/Book`. |
| Media Management | **Root Folder** for imported books. **Book Folder Format**, default `{Author} - {Series} {SeriesNumber} - {Title}` (also `{Authors}`, `{Year}`, `{Edition}`; empty parts are dropped; abridged books (dramatizations included) get "(Abridged)" at the end even without `{Edition}`). **Rename Audio Files:** `Title.m4b`, or `Title - Part 01.mp3`… in disc and track order. |
| Indexers | **AudiobookBay**: on/off, its address (if the domain moves), the session cookie (stored as a secret; **Test** says whether it logs in) and the user agent. **Torznab indexers** (e.g. from Prowlarr or Jackett): name, feed URL, API key and categories; searched alongside AudiobookBay with the same scoring (plus seeders), grabbed by magnet or `.torrent`. |
| Releases | Preferred and avoided narrators, preferred and blocked words, blocked uploaders (ABB's "Shared by"), minimum bitrate and maximum size. Preferences raise or lower a release's score; blocks, the bitrate and the size rule releases out. |
| Lists | **Watched Goodreads Lists:** paste a shelf's link (e.g. your Want to Read list), your profile, the shelf's RSS link or your user number. Bayarr checks it every 6 hours (or **Check Now**) and adds the books new on it as Monitored or Unmonitored, looked up on Audible (only clear matches; the rest are listed with a **Search** link). Optionally the books already on it too. A book you remove from the library isn't added back. The profile must be public, unless you use the RSS link (it has a key). **Import a File:** a one-time import of a Goodreads or StoryGraph export (CSV): choose a shelf, check the matches, add them. |
| Download Checks | Allowed difference from Audible's runtime (10% by default). |
| General | Language; *Prefer M4B*, *M4B only* or *Any format*; whether a matching narrator ranks releases higher; **Editions**: *Unabridged only* (default), *Abridged only* or *Both* for series monitoring (see [Editions](#editions)). |
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
   | Edition | Must be the book's edition: an unabridged book rules out abridged releases, GraphicAudio and other full-cast dramatizations; an abridged book rules out plain unabridged ones. |
   | Ruled out | Box sets (`Books 1-6`, collections), other languages, releases rejected before. |

   A release is grabbed only if nothing rules it out and it scores 45 or more. Among clear matches within 10 points, M4B wins when preferred. **Manual Search** shows each release's score; hover for the reasons, or tick **Show rejected** to see what was ruled out and why.
3. **Download.** The magnet goes to qBittorrent with the `audiobooks` category and a `bayarr-` tag.
4. **Check and import.** Every minute Bayarr looks for finished downloads. The audio files' real length is compared with Audible's runtime, their tags and names must not say they're another edition (e.g. GraphicAudio for a narrated book), and with *M4B only* the format is checked. A release that fails becomes **Needs Review** in Activity, where you can **Import Anyway** or **Reject & Search Again** (that release is never grabbed again). Otherwise it's imported into the Root Folder with `metadata.json` and the cover, and Audiobookshelf is asked to scan.

Searches are spaced a second apart and cached for 15 minutes. If AudiobookBay stops responding, searches pause for 5 minutes.

## Library

**Import** (Search page) scans a folder; each folder with audio files is one book (`CD1`, `Disc 2` subfolders included), and so is each audio file sitting loose in the folder you scan. Details come from Audiobookshelf's `metadata.json` when present, otherwise the folder or file name:

| Folder | Author | Series | # | Title |
| --- | --- | --- | --- | --- |
| `Jane Author - Series Name 3.5 - Book Title` | Jane Author | Series Name | 3.5 | Book Title |
| `Jane Author - Series Name Book 1 - Book Title` | Jane Author | Series Name | 1 | Book Title |
| `Jane Author - Universe - Series Name - #3 - Book Title` | Jane Author | Series Name | 3 | Book Title |
| `Jane Author - Series Name, Vol. 07 - Book Title` | Jane Author | Series Name | 7 | Book Title |
| `Jane Author - Book Title` | Jane Author | | | Book Title |
| `Jane Author/Series Name 1 - Book Title` | Jane Author | Series Name | 1 | Book Title |
| `Book 2 Book Title Part 1 of 2 Series GA.m4b` in `Jane Author - Series GraphicAudio` | Jane Author | | 2 | Book Title (Part 1 of 2), abridged |

A loose file's author and edition can come from the folder it's in.

- **Audible:** each book is looked up on Audible, and clear matches are filled in: the same title and author, the same edition, and for a book sold in parts the same part. **Change** searches Audible yourself, or uses the book as it's named.
- **Files:** **Keep them where they are** (the default when everything is already inside your Root Folder) records the books as they are. **Copy into the Root Folder** (the default for anything outside it) or **Move into the Root Folder** names each book's folder and files from its Audible match, and the **Goes to** column shows each new folder and file name before you import. Moving deletes the originals afterwards (a torrent of them stops seeding).

The preview shows each book as **New**, **Link to library** (a book you already track, matched by ASIN or title and author) or **In library**.

Managing books:
- **Filter and sort.** The **Wanted** and **Not matched on Audible** filters are especially useful. **Edition** shows only unabridged or abridged books. Sorted by series, each series gets a heading, and a series' abridged editions are grouped under their own heading, marked with the scissors icon.
- **Select** several books to change their status, match them on Audible, or remove them.
- **Match on Audible** fills in the ASIN, runtime, narrators and series for books imported from folder names. Matching many books at once accepts only clear-cut matches. Your files are never changed.
- **Book details:** edit, see files, **Search Now**, **Manual Search**, or **Remove**. Removing never deletes files.
- **Convert to M4B** (book details): joins a book's audio files into one `Title.m4b` with a chapter per file, its tags (title, authors, narrators, year) and cover, using ffmpeg (included in the Docker image). AAC files are joined without re-encoding where possible; others become AAC at about the source's bitrate. It runs in the background and is only switched over when the new file checks out: every original's length is read first, then the M4B must be as long as their total (within a minute plus 0.1%), have one chapter per file, and decode from start to end without errors. The originals are kept, renamed to `.original` (Audiobookshelf ignores them), until you click **Delete Originals** (or automatically, with *Delete the Original Files After Converting*).
  - **The queue:** books are converted one at a time in the background; **Activity → Conversions to M4B** and **Settings → Media Management** show the one being converted (with progress and **Cancel**), the queue (each can be removed) and recent results. The queue is kept across restarts.
  - **Adding books:** per book from its details, several with **Select → Convert to M4B**, all of them with **Settings → Media Management → Queue Existing Books**, or automatically with **Convert Downloads to M4B Automatically** (each finished download that isn't a single M4B is queued after it's imported).
- **Organize**: renames existing folders to your Book Folder Format, like Sonarr's Organize. A preview lists every proposed change (current folder struck through, the new one below) with its own **Approve** button, plus **Approve All**; nothing changes until you approve. Books inside the Root Folder end up directly in it (nested `Author/Series/Title` folders are flattened and emptied folders removed); books elsewhere are renamed where they are. Optionally the audio files are renamed too (`Title.m4b`, `Title - Part 01.mp3`). Folders only move on the same drive, so it's instant; clashes with existing folders are shown and skipped.
- **Split into Books** (book details): for a folder holding several books, e.g. `Book 1 Title Part 1 of 2.m4b`, `Book 2 Other Part 1 of 2.m4b`, or a `Book N - Title` subfolder per book. Bayarr matches each to its series on Audible (in the folder's edition; a book Audible sells in parts becomes one book per part when the file names say which part), you check the titles and numbers, and each book gets its own folder next to the original (`Author - Series 1 - Title (Abridged)`), with its files copied in, its own cover and `metadata.json`. The original folder is never changed; remove it yourself (and from Audiobookshelf) when you're happy.
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

A book is **unabridged** (the whole book, read as written by one or a few narrators) or **abridged**: everything else, meaning shortened readings and dramatizations (full-cast productions such as GraphicAudio, radio plays, Audible Original performances), which adapt the book rather than read it. This matches Audiobookshelf's **Abridged** setting. Abridged books and series are marked with a scissors icon (unabridged ones have no mark), and a book's details have an **Abridged** checkbox. Audible labels dramatizations "unabridged", so Bayarr combines several signs: Audible's abridged format, "full cast" or many narrators, GraphicAudio or BBC radio publishers, radio-production and performance listings, and words like "Dramatized Adaptation" or "Abridged" in titles.

- **Separate books:** each edition is its own library entry, matched to its own Audible edition (with its own runtime), searched for in its own edition, and imported into its own folder, e.g. `Author - Series 1 - Title (Abridged)`. Audiobookshelf gets its `abridged` flag set to match.
- **Import:** the edition comes from `metadata.json`, the files' tags, and folder and file names (e.g. "Graphic Audio"). The preview shows it with the reason; change it before importing if it's wrong. Unclear ones (many narrators, or a folder that says GraphicAudio but an ASIN for the unabridged edition) are highlighted.
- **Your library:** books from before editions existed are checked once in the background (Audible's edition by ASIN, plus what the files say); books saved as "dramatized" by older versions are now abridged. Filter the Library by **Edition** or **Check edition**; tick **Abridged** in a book's details, or set many with **Select** → **Mark abridged** / **Mark unabridged** (or **Detect again**).
- **Series:** a series' abridged editions are a series of their own (shown with the scissors icon; "Name (Abridged)" in Audiobookshelf), with **Unabridged / Abridged** tabs on the series page; owning one doesn't count towards the unabridged series. Every release is its own row: a book Audible sells in parts ("Part 1 of 2") is one row (and one library book) per part. In the Library, sorting by series or author puts a series' abridged editions after its unabridged books. The **Editions** setting decides which editions **Monitor Series** offers and series syncs add; the abridged tab always offers its releases.

## Series

The **Series** page lists every series your books are in. A book can be in several, such as #4 of a saga, #1 of a sub-series, and part of a universe. Each series shows posters or a table, and a bar of how many of Audible's books you have (orange when monitored, green when complete). You can filter and sort. Series for books imported from folder names are looked up on Audible in the background.

Open a series to see every release Audible lists, in order (books sold in parts part by part). Books you don't have show an **Add** button. Duplicate regional editions are merged. Abridged editions and dramatizations are on the series' **Abridged** tab.

**Monitor Series** lets you tick which missing books to add. Monitored series are checked every 6 hours, and new releases are added automatically. Books you didn't pick aren't added again.

### GraphicAudio

Audible lists some GraphicAudio dramatizations in a series only as placeholders (no narrators, length or date; "not on Audible" on the series page), and GraphicAudio sells most books in several parts. For a dramatized series like that, Bayarr reads GraphicAudio's own store (graphicaudio.net) and lists its releases instead: every part (e.g. *The Way of Kings (Part 1 of 5)*), linked to its page, with upcoming ones dated (so they're **Unreleased** until they're out). Parts Audible does sell stay Audible's; GraphicAudio fills in the rest, including parts it releases before Audible. **Search** also asks GraphicAudio's store and adds its releases to the matching series in the results once they arrive (a few seconds later; Audible's own GraphicAudio releases are kept). Adding one fetches its page for the release date, approximate length, ISBN, description and cover. GraphicAudio only gives the length in whole hours, so the download check allows 25% for these. The store has no API, so its pages are read gently and cached; a redesign of the site could break this.

## Authors

Click an author's name (in a book's details, Search results, a series page or the Calendar) to see every book Audible lists for them, newest first, with what you have. **Follow Author** lets you tick the books to add now (upcoming ones are ticked); after that their new books are added automatically, checked every 6 hours in the editions your **Editions** setting asks for. **Followed Authors** (on the Series page) lists them; each can be paused, synced now or unfollowed (books stay in your library).

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

## System

**Health** lists books that need attention, grouped by kind, each with a fix:

| Issue | Fix |
| --- | --- |
| Folder gone, no audio files, empty or unreadable files, parts missing (`Part 2 of 3`) | Open the book |
| Length doesn't match Audible, edition to check, same book in two folders | Open the book |
| Several books in one folder | Split into Books |
| Download held for review | Activity |
| Not matched on Audible, no runtime | Match on Audible (one, or all at once) |
| No cover (and none inside the audio files) | Get Audible's cover (one, or all at once) |

**Stats** shows totals (books, on disk, hours, size, wanted, authors, series), library growth over the last 24 months, top authors and narrators by hours, top series, status, edition, format and language breakdowns, books by release year, and downloads (grabbed, imported, held, rejected, stalled) for the last 30 days and all time.

**Deep Check** opens every audio file in the background to find unreadable files, embedded covers, and books whose length is far from Audible's runtime.

## Audiobookshelf

- **Write metadata.json and Cover** (on by default): imported downloads get Audiobookshelf's `metadata.json` and Audible's 1000px cover. Folders imported where they are are never changed.
- **Server URL, API Token, Library:** after each import, Bayarr asks Audiobookshelf to scan that library. The token needs admin rights and is never sent to the browser.
- **Series and editions:** imported books are written so Audiobookshelf matches Bayarr: its **Abridged** setting follows the book's edition, abridged editions (dramatizations included) are a series of their own ("Name (Abridged)"), and a book sold in parts is numbered by part (book 1 in two parts: #1.1 and #1.2; book 5 in three: #5.1–#5.3), so series sort properly. **Update Audiobookshelf** does the same for books already imported: it rewrites only the series and the Abridged flag in each book's existing `metadata.json` (other fields are kept; no file is created where there was none) and, with a server and library set, updates each book in Audiobookshelf through its API (found by ASIN or folder name), so no rescan is needed.

## Torznab indexer

Add a **Torznab** indexer in Prowlarr, Listenarr or LazyLibrarian:
- **URL:** `http://<your-server>:8000/api`.
- **API key:** anything; it isn't checked.
- **Categories:** `3000` and `3030`.

These endpoints don't need the Bayarr login. Searches (`t=search` / `t=book`) return AudiobookBay results with magnet links.

## Environment variables

| Variable | Default | Description |
| --- | --- | --- |
| `ABB_COOKIE` | *(empty)* | AudiobookBay session cookie, in single quotes in `.env`. One saved in Settings → Indexers takes its place. |
| `ABB_USER_AGENT` | Chrome | User agent; match the browser the cookie came from. Settings → Indexers can set it too. |
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
