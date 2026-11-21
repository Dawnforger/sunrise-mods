"""Downloads the files `cargo sunrise-mod registry list` printed into <dir>/<sha256>, and with `--mirror` copies the
ones the registry's releases lack into them (README.md, "How it works")."""
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

HEX = re.compile(r"[0-9a-f]{64}")
# One plain name, as the registry tool's `plain_name` reads it: no folder, no hidden file, at most 128 bytes. A stable
# build's asset carries `+<release>`.
PLAIN = re.compile(r"(?!\.)[A-Za-z0-9._+-]{1,128}")
# An author's release asset. The tag may have `/`-separated parts, each a plain name, as the tool accepts.
ASSET = re.compile(r"https://github\.com/(?P<repo>[A-Za-z0-9-]+/[A-Za-z0-9._-]+)/releases/download/"
                   r"(?P<tag>[^?#\s]+)/(?P<name>[^/?#\s]+)")
# The registry tool's own limits (sunrise-mod-package: index::MAX_FILE, registry::builds::MAX_DESCRIPTOR): nothing
# larger is read, so nothing larger is downloaded. An archive is cut off at the size its verified descriptor gives.
MAX_PACKAGE = 64 << 20
MAX_DESCRIPTOR = 256 << 10


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def asset_url(repo: str, tag: str, name: str) -> str:
    return f"https://github.com/{repo}/releases/download/{quote(tag, safe='/+')}/{quote(name, safe='+')}"


def download(url: str, into: Path, limit: int) -> str | None:
    """Streams `url` into `into`, stopping at `limit` bytes. Why it failed, or None; "" when the asset isn't there."""
    request = urllib.request.Request(url, headers={"User-Agent": "sunrise-mods-registry"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response, into.open("wb") as out:
            size = 0
            while chunk := response.read(1 << 20):
                size += len(chunk)
                if size > limit:
                    out.close()
                    into.unlink()
                    return f"is larger than the {limit} bytes allowed"
                out.write(chunk)
    except urllib.error.HTTPError as error:
        into.unlink(missing_ok=True)
        return "" if error.code == 404 else f"could not be downloaded (HTTP {error.code})"
    except OSError as error:
        into.unlink(missing_ok=True)
        return f"could not be downloaded ({error})"
    return None


def gh(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["gh", *args], capture_output=True, text=True)


def release_assets(repo: str, tag: str) -> dict[str, str | None] | None:
    """Each asset of the registry's release `tag` with the SHA-256 GitHub recorded for it (None for an asset older
    than GitHub's digests), or None when it has no such release."""
    done = gh("api", f"repos/{repo}/releases/tags/{quote(tag, safe='')}")
    if done.returncode:
        return None
    return {asset["name"]: (asset.get("digest") or "").removeprefix("sha256:") or None
            for asset in json.loads(done.stdout)["assets"]}


def publish(repo: str, tag: str, title: str, file: Path, exists: bool) -> str | None:
    if exists:
        done = gh("release", "upload", tag, "-R", repo, str(file))
    else:
        done = gh("release", "create", tag, "-R", repo, "--title", title, "--notes", "", "--latest=false", str(file))
    return None if done.returncode == 0 else done.stderr.strip()


def rows(listing: str):
    """Each listed file as (sha256, id, version, file, size): the lines of `registry list`, where `file` is a plain
    name or an author's release asset, or the JSON of `registry list --build-assets`, whose descriptors have no size."""
    text = Path(listing).read_text()
    if text.lstrip().startswith("["):
        for row in json.loads(text):
            yield row["sha256"], row["id"], row["version"], row["url"], row["size"], True
        return
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 4:
            yield *parts, None, False


def parse(digest: str, id_: str, version: str, file: str):
    """The registry's release tag, the asset name and the author's (repo, tag) for a listed file, or None."""
    tag = f"{id_}-{version}"
    theirs = ASSET.fullmatch(file)
    name = theirs["name"] if theirs else file
    if not (HEX.fullmatch(digest) and PLAIN.fullmatch(tag) and PLAIN.fullmatch(name) and "." in name):
        return None
    if theirs and not all(PLAIN.fullmatch(part) for part in theirs["tag"].split("/")):
        return None
    return tag, name, (theirs["repo"], theirs["tag"]) if theirs else None


def fetch(repo: str, listing: str, files: Path) -> list[str]:
    errors = []
    for digest, id_, version, file, size, build in rows(listing):
        if not (parsed := parse(digest, id_, version, file)):
            errors.append(f"{digest} {id_}-{version} {file}: not a plain sha256, id, version and a file name or "
                          "GitHub release asset")
            continue
        tag, name, theirs = parsed
        target = files / digest
        if target.is_file() and sha256(target) == digest:
            continue
        if build and size is not None and not (isinstance(size, int) and 0 < size <= MAX_PACKAGE):
            errors.append(f"{tag} {name}: its descriptor gives no usable size")
            continue
        limit = size if size is not None else MAX_DESCRIPTOR if build else MAX_PACKAGE
        # The registry's own copy first; then the author's release for a stable row, `staging` for an older one.
        sources = [(asset_url(repo, tag, name), "")]
        if theirs:
            sources.append((asset_url(*theirs, name), f" ({theirs[0]})"))
        else:
            sources.append((asset_url(repo, "staging", f"{digest}.{name.rsplit('.', 1)[1]}"), " (staging)"))
        part = files / f"{digest}.part"
        problem = ""
        for url, source in sources:
            problem = download(url, part, limit)
            if problem != "":
                break
        if problem == "":
            where = f"{theirs[0]}'s release {theirs[1]}" if theirs else f"staging as {digest}.<extension>"
            errors.append(f"{tag} {name}: not in its release, nor in {where}")
        elif problem:
            errors.append(f"{tag} {name}: {problem}")
        elif (size is not None and part.stat().st_size != size) or sha256(part) != digest:
            part.unlink()
            errors.append(f"{tag} {name}: the downloaded file isn't the one its sha256 names")
        else:
            part.replace(target)
            print(f"{digest} {tag}/{name}{source}")
    return errors


def mirror(repo: str, listing: str, files: Path) -> list[str]:
    """Copies each listed file the registry's `<id>-<version>` release lacks into it, under its own name, from the
    downloaded and checked files: run only after `registry build` accepted them. An asset already there must be
    the listed file, by GitHub's recorded SHA-256 or, without one, by downloading it."""
    errors = []
    releases: dict[str, dict[str, str | None] | None] = {}
    for digest, id_, version, file, _, _ in rows(listing):
        if not (parsed := parse(digest, id_, version, file)):
            errors.append(f"{digest} {id_}-{version} {file}: not a plain sha256, id, version and a file name or "
                          "GitHub release asset")
            continue
        tag, name, _ = parsed
        if tag not in releases:
            releases[tag] = release_assets(repo, tag)
        checked = files / digest
        if not checked.is_file() or sha256(checked) != digest:
            errors.append(f"{tag} {name}: not among the downloaded files")
            continue
        if name in (releases[tag] or ()):
            held = releases[tag][name]
            if held is None:
                part = files / f"{digest}.part"
                problem = download(asset_url(repo, tag, name), part, checked.stat().st_size)
                held = None if problem is not None else sha256(part)
                part.unlink(missing_ok=True)
            if held != digest:
                errors.append(f"{tag} {name}: the registry's release holds a different file than the entry's "
                              f"sha256 {digest}")
            continue
        with tempfile.TemporaryDirectory() as folder:
            named = Path(folder) / name
            shutil.copyfile(checked, named)
            failed = publish(repo, tag, f"{id_} {version}", named, releases[tag] is not None)
        if failed:
            errors.append(f"{tag}: couldn't publish {name}: {failed}")
            continue
        releases[tag] = (releases[tag] or {}) | {name: digest}
        print(f"published {tag}/{name}")
    return errors


def main(repo: str, listing: str, out: str, mirroring: bool) -> int:
    files = Path(out)
    files.mkdir(parents=True, exist_ok=True)
    errors = (mirror if mirroring else fetch)(repo, listing, files)
    for e in errors:
        print(f"error: {e}", file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--mirror"]
    if len(args) != 3:
        sys.exit("usage: fetch_files.py <owner/repo> <list> <dir> [--mirror]")
    sys.exit(main(*args, "--mirror" in sys.argv[1:]))
