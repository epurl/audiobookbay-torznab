<img src="app/static/logo.svg" width="72" alt="" align="left">

# BorgArr

An audiobook manager in the style of Sonarr and Radarr.

<br clear="left">

> A vibe-coded project, made for learning.

Add a book from Audible and BorgArr finds it on AudiobookBay or your own indexers, sends it to qBittorrent (or NZBGet/SABnzbd), checks the download and copies it into your library, ready for Audiobookshelf. It also works as a Torznab indexer for Prowlarr, Listenarr and LazyLibrarian.

## Features

- **Library:** wanted books are searched for when added or released, then picked up automatically from new uploads.
- **Series and authors:** Sonarr-style series pages with Audible's full book list and what you're missing. Monitor a series or follow an author to get new releases. Novellas, short stories and other extras are recognised and skipped (Settings → General → **Ignore Extras**), and **Ignore** skips any book you don't want.
- **Editions:** unabridged and abridged books (dramatizations such as GraphicAudio included) are separate entries, searched for and imported separately, and Audiobookshelf's Abridged flag is set to match. GraphicAudio releases Audible doesn't list are read from GraphicAudio's store.
- **Careful downloads:** several searches per book, every release scored against it, and only clear matches grabbed. Before import, a download's length and edition are checked against Audible.
- **Bring in what you have:** **Import** adds an existing library (from `metadata.json` or folder names), and **Manual Import** handles a downloads folder, like Sonarr's. Packs of several books are split, and archives are unpacked.
- **Lists:** Goodreads shelves and Listopia lists are watched for new books, and Goodreads or StoryGraph CSV exports can be imported once.
- **Calendar:** your books on their release dates, plus trending Audible releases.
- **Tools:** Convert to M4B (checked before the files are switched over), Organize (rename folders to your format), Split into Books, Rescan, Health and Stats.
- **Audiobookshelf:** `metadata.json` and cover written on import, a library scan after each import, series and Abridged kept in sync.

## Quick start (Docker)

1. Run `cp .env.example .env`.
2. Mount your config, library and downloads in `docker-compose.yml`:
   ```yaml
   volumes:
     - ./config:/config
     - /mnt/media/audiobooks:/audiobooks
     - /mnt/downloads:/downloads
   ```
3. Run `docker compose up -d`, then open `http://<your-server>:8000`.
4. In **Settings**, set a login (Security), qBittorrent (Download Client), your Root Folder (Media Management) and, ideally, your AudiobookBay cookie (Indexers).

A prebuilt image, `ghcr.io/epurl/audiobookbay-torznab:latest`, is published on every push to `main`. Without Docker:

```bash
pip install -r requirements.txt
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## Settings

| Section | What's there |
| --- | --- |
| General | Language, format (*Prefer M4B*, *M4B only*, *Any*), which editions to monitor (unabridged only by default), Ignore Extras. |
| Media Management | Root Folder, Book Folder Format (default `{Author} - {Series} {SeriesNumber} - {Title}`; also `{Authors}`, `{Year}`, `{Edition}`), file renaming, the runtime check (10%), M4B conversion, adding new folders automatically. |
| Download Client | qBittorrent (category `audiobooks`), removal of stalled downloads (after 6 hours), seeding limits, NZBGet or SABnzbd for Usenet, and the downloads path if the client sees it differently. |
| Indexers | AudiobookBay (address, cookie, user agent), Torznab and Newznab indexers, an API key for using BorgArr as an indexer. |
| Releases | Preferred and avoided narrators and words, blocked uploaders, minimum bitrate, maximum size. |
| Lists | Goodreads lists, CSV import, and Exclusions (books nothing adds automatically). |
| Audiobookshelf | Server URL, API token (admin) and library; writing metadata; **Update Audiobookshelf** for books already imported. |
| Security | The login. Until one is set, only local network addresses can connect. |
| Backup | Download or restore the database; a daily copy is kept for 7 days in `config/backups`. |

## How downloads work

1. **Search.** BorgArr searches AudiobookBay several ways (title and author, series and number, title, author), most specific first, plus your indexers, and stops at the first clear match.
2. **Score.** Each release is scored out of 100 on its title and author, series number, part, narrator, format, size against Audible's runtime, and edition. Box sets, other languages, the wrong edition and releases you rejected are ruled out. A release needs 45 to be grabbed. **Manual Search** shows each score and why.
3. **Download.** The release goes to qBittorrent (category `audiobooks`, tag `borgarr-`) or to your Usenet client.
4. **Check and import.** The files' length must be within 10% of Audible's runtime, and their tags and names must match the edition. A download that fails is held in **Activity → Needs Review**, where you can **Import Anyway** or **Reject & Search Again**. Otherwise it's copied (so the torrent keeps seeding), renamed, given `metadata.json` and a cover, and Audiobookshelf is asked to scan.

## Statuses

| Status | Meaning | Colour |
| --- | --- | --- |
| Monitored | Wanted and out; searched for automatically | Red |
| Unreleased | Not out yet; becomes Monitored on its release date | Blue |
| Downloading / Downloaded | In the download client / finished, waiting to import | Purple |
| Needs Review | Failed a check; waiting for you in Activity | Orange |
| Imported | On disk | Green |
| Missing | Its folder is gone; set it to Monitored to get it again | Red |
| Unmonitored | Tracked, but never searched for automatically | Grey |

Colours follow Sonarr and Radarr, and the Calendar uses them too. Series bars work like Sonarr's: green when complete, blue when complete with more books coming, red when missing books, orange when missing books you don't monitor, and purple while one downloads. The logo's green marks only the page you're on.

## Torznab indexer

In Prowlarr, Listenarr or LazyLibrarian, add a **Torznab** indexer with the URL `http://<your-server>:8000/api` and categories `3000` and `3030`. If you've set an API key in **Settings → Indexers**, enter it too; without one, anyone who can reach BorgArr can search through it. These endpoints don't need the login.

## Going easy on AudiobookBay

BorgArr keeps its requests few and spread out, using your own cookie and user agent:

- It reads AudiobookBay's new uploads every 2 hours rather than searching over and over. A book is searched for when it's added or released, or when you click **Search Now**. One that's never found is searched again after 1, 3 and 7 days, then every 14 days. Your other indexers are searched every 6 hours.
- Search pages are cached for an hour and book pages for 30 days.
- Requests are spaced 1 to 4 seconds apart, and automatic work is limited to 300 requests a day.
- A block, rate limit or Cloudflare check pauses all requests, for 5 minutes at first and up to 6 hours if it keeps happening.
- **Settings → Indexers** shows today's requests and any pause.

## Environment variables

| Variable | Default | Description |
| --- | --- | --- |
| `ABB_COOKIE` | *(empty)* | AudiobookBay session cookie, in single quotes in `.env`. One set in Settings takes its place. |
| `ABB_USER_AGENT` | Chrome | The user agent of the browser the cookie came from. |
| `BORGARR_USERNAME` / `BORGARR_PASSWORD` | *(empty)* | A fixed login, overriding the one in Settings. |
| `BORGARR_CONFIG_DIR` | `/config` (Docker), `./config` | Where the database and backups are kept. |

BorgArr used to be called Bayarr. The old `BAYARR_` variable names still work, and torrents tagged `bayarr-` are still recognised.

## Security

- **Set a login.** Without one, BorgArr trusts any local address, and behind a reverse proxy every request looks local.
- Logins are sent in plain HTTP, so use an HTTPS reverse proxy or a VPN to reach BorgArr from outside your home.
- Set a Torznab API key so only your own apps can use `/api`.
- Backups contain your passwords and tokens, so keep them private.
- Passwords are stored as salted hashes and never sent to the browser, and repeated wrong logins are held back.
- The container runs as root by default. To run it as your own user, add `user: "1000:1000"` to the service in `docker-compose.yml`, with your own user and group IDs.
