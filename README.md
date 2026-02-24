# Audiobookbay Torznab Indexer

This is a lightweight Docker container that acts as a Torznab indexer specifically for Audiobookbay.lu. It translates standard Torznab searches from apps like LazyLibrarian and Listenarr into Audiobookbay website searches and provides the results back as a Torznab RSS feed.

It supports searching by `q` (general query), `author`, and `title`.

## Running the Container

The provided `docker-compose.yml` makes this easy:

```yaml
version: '3.8'

services:
  audiobookbay-torznab:
    build: .
    container_name: audiobookbay-torznab
    ports:
      - "8000:8000"
    environment:
      # Optional but recommended, as Audiobookbay restricts searching unless logged in
      - ABB_COOKIE=${ABB_COOKIE:-} 
      - ABB_USER_AGENT=${ABB_USER_AGENT:-Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36}
    restart: unless-stopped
```

1. (Optional but recommended) Get your Session Cookie.
   - Go to `audiobookbay.lu` in your browser and log in.
   - Open Developer Tools (F12) -> Network tab.
   - Refresh the page and click on the main document request (usually `/`).
   - Find the `Cookie` request header and copy its entire value.
2. In the same directory as `docker-compose.yml`, create a `.env` file and add the cookie (or manually replace it in the yaml).
   ```env
   ABB_COOKIE=your_cookie_string_here
   ```
3. Run `docker-compose up -d`.
4. The API will now be internally available at `http://localhost:8000/api`.

## Setup in LazyLibrarian / Listenarr

1. Add a new **Torznab** or **Jackett** Indexer.
2. Set the **URL** to `http://localhost:8000/api` (or the IP of the machine hosting the docker).
3. The **API Key** can be left blank (or filled with any arbitrary value, the indexer doesn't check it).
4. Select the **Audiobook / Audio** categories (`3030`, `3000`).
5. Test the connection.

## How it works

Apps hit `/api?t=book&q=Sanderson` for example.
The scraper converts this to a standard `audiobookbay.lu/page/1?s=Sanderson` search.
It scrapes the result page for Titles, Authors, and Size, then generates the required XML format.
When the app attempts to "download" the `.torrent` file using the generated link, the server instead goes back to audiobookbay, grabs the `Info Hash` and emits an HTTP Redirect straight to the derived `magnet:` link.
