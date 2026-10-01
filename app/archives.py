"""Unpacking downloads that arrive as archives (.zip, .7z, .rar).

Zips are read with Python's zipfile. Everything else goes through 7-Zip (7zz / 7z / 7za),
which the Docker image includes; whether it opens .rar depends on the 7-Zip build.
Archives are always unpacked into a separate folder: the download itself isn't touched,
so the client keeps seeding it.
"""
import os
import re
import shutil
import subprocess
import zipfile

ARCHIVE_EXTENSIONS = {".zip", ".7z", ".rar"}
SEVEN_ZIP_NAMES = ("7zz", "7z", "7za")
# Later volumes of a split RAR (name.part2.rar); 7-Zip reads them through the first one
LATER_VOLUME_RE = re.compile(r"\.part0*(?:[2-9]|[1-9]\d+)\.rar$", re.IGNORECASE)


def is_archive(path):
    return os.path.splitext(path)[1].lower() in ARCHIVE_EXTENSIONS and not LATER_VOLUME_RE.search(path)


def find_archives(path):
    """The archives a download consists of: the download itself if it's one file, otherwise
    the archives in its folder and subfolders (first volumes only), in name order."""
    if os.path.isfile(path):
        return [path] if is_archive(path) else []
    found = []
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        found += [os.path.join(root, f) for f in files if is_archive(f)]
    return sorted(found)


def seven_zip():
    for name in SEVEN_ZIP_NAMES:
        tool = shutil.which(name)
        if tool:
            return tool
    return ""


def _inside(dest, member):
    """True if the archive member unpacks to somewhere inside dest (no '../' or absolute paths)."""
    dest = os.path.realpath(dest)
    target = os.path.realpath(os.path.join(dest, member))
    return target == dest or target.startswith(dest + os.sep)


def _extract_zip(archive, dest):
    with zipfile.ZipFile(archive) as z:
        for member in z.namelist():
            if not _inside(dest, member):
                raise ValueError(f"it contains a file outside its folder ({member})")
        z.extractall(dest)


def _extract_7z(archive, dest):
    tool = seven_zip()
    if not tool:
        raise ValueError("7-Zip isn't installed (it's included in the Docker image)")
    # List first, so nothing is written if a path points outside the folder
    listing = subprocess.run([tool, "l", "-slt", "-ba", archive], capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=300)
    if listing.returncode != 0:
        raise ValueError(_last_line(listing.stderr or listing.stdout) or "7-Zip can't open it")
    for line in listing.stdout.splitlines():
        # Older 7-Zip versions also list the archive itself
        if line.startswith("Path = ") and line[7:] != archive and not _inside(dest, line[7:]):
            raise ValueError(f"it contains a file outside its folder ({line[7:]})")
    result = subprocess.run([tool, "x", "-y", "-bd", f"-o{dest}", archive], capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=3600)
    if result.returncode != 0:
        raise ValueError(_last_line(result.stderr or result.stdout) or f"7-Zip failed (exit {result.returncode})")


def _last_line(text):
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    return lines[-1] if lines else ""


def extract(archive, dest):
    """Unpacks one archive into dest. Raises ValueError with a readable reason if it can't."""
    os.makedirs(dest, exist_ok=True)
    try:
        if os.path.splitext(archive)[1].lower() == ".zip":
            _extract_zip(archive, dest)
        else:
            _extract_7z(archive, dest)
    except zipfile.BadZipFile as e:
        raise ValueError(f"it isn't a valid zip ({e})")
    except subprocess.TimeoutExpired:
        raise ValueError("unpacking took too long")
    except OSError as e:
        raise ValueError(str(e))


def extract_all(archives, dest):
    """Unpacks each archive into its own subfolder of dest (so files that share a name in
    different archives don't overwrite each other)."""
    for i, archive in enumerate(archives, 1):
        sub = os.path.splitext(os.path.basename(archive))[0] if len(archives) > 1 else ""
        target = os.path.join(dest, f"{i:02d} {sub}".strip()) if sub else dest
        try:
            extract(archive, target)
        except ValueError as e:
            raise ValueError(f"Couldn't unpack {os.path.basename(archive)}: {e}")
