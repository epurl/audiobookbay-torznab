"""Library statistics for System > Stats."""
import datetime
from collections import Counter, defaultdict

from app import db, editions
from app.library import primary_author, series_entries

DOWNLOAD_EVENTS = ("grabbed", "imported", "needs_review", "approved", "rejected", "stalled", "failed")


def _top(counter, n=10):
    return [{"name": k, "value": v} for k, v in counter.most_common(n) if k]


def compute():
    library = db.get_library()
    on_disk = [b for b in library if b.get("status") == "Imported"]
    minutes = lambda books: sum(b.get("runtime_min") or 0 for b in books)

    author_hours, narrator_hours, series_books = Counter(), Counter(), Counter()
    for b in on_disk:
        hours = (b.get("runtime_min") or 0) / 60
        author_hours[primary_author(b.get("authors"))] += hours
        for n in (b.get("narrators") or "").split(","):
            if n.strip() and "full cast" not in n.lower():
                narrator_hours[n.strip()] += hours
        for e in series_entries(b):
            series_books[e["name"]] += 1

    # Library growth: books added per month (last 24 months), and the running total
    added = Counter((b.get("added") or "")[:7] for b in library if b.get("added"))
    today = datetime.date.today()
    months, total = [], sum(v for k, v in added.items() if k < f"{today.year - 2:04d}-{today.month:02d}")
    for i in range(23, -1, -1):
        y, m = divmod(today.year * 12 + today.month - 1 - i, 12)
        key = f"{y:04d}-{m + 1:02d}"
        total += added.get(key, 0)
        months.append({"month": key, "added": added.get(key, 0), "total": total})

    years = Counter((b.get("release_date") or "")[:4] for b in library
                    if (b.get("release_date") or "")[:4].isdigit() and (b.get("release_date") or "") < "2100")

    history = db.get_history(10000)
    since = (datetime.datetime.now() - datetime.timedelta(days=30)).isoformat()
    downloads = {e: {"recent": 0, "all": 0} for e in DOWNLOAD_EVENTS}
    for h in history:
        if h.get("event") in downloads:
            downloads[h["event"]]["all"] += 1
            if (h.get("time") or "") >= since:
                downloads[h["event"]]["recent"] += 1

    formats = Counter()
    for b in on_disk:
        for f in (b.get("format") or "").split(","):
            if f.strip():
                formats[f.strip()] += 1

    return {
        "totals": {
            "books": len(library),
            "on_disk": len(on_disk),
            "wanted": sum(1 for b in library if b.get("status") in ("Monitored", "Missing", "Downloading", "Downloaded", "Needs Review")),
            "upcoming": sum(1 for b in library if b.get("status") == "Unreleased"),
            "hours": round(minutes(on_disk) / 60),
            "size_bytes": sum(b.get("size_bytes") or 0 for b in on_disk),
            "authors": len({primary_author(b.get("authors")) for b in library if b.get("authors")}),
            "series": len({e["name"] for b in library for e in series_entries(b)}),
            "matched": sum(1 for b in library if b.get("asin")),
        },
        "statuses": _top(Counter(b.get("status") for b in library), 12),
        "editions": _top(Counter(editions.label(editions.edition_of(b)) for b in library), 5),
        "formats": _top(formats, 8),
        "languages": _top(Counter(b.get("language") or "Unknown" for b in library), 8),
        "top_authors": [{"name": k, "value": round(v)} for k, v in author_hours.most_common(10) if k],
        "top_narrators": [{"name": k, "value": round(v)} for k, v in narrator_hours.most_common(10)],
        "top_series": _top(series_books, 10),
        "growth": months,
        "release_years": [{"name": y, "value": years[y]} for y in sorted(years)][-30:],
        "downloads": downloads,
    }
