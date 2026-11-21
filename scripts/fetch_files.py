"""Downloads the files `cargo sunrise-mod registry list` printed into <dir>/<sha256> (README.md, "How it works")."""
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HEX = re.compile(r"[0-9a-f]{64}")
PLAIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
# A release asset's name: a stable build's carries `+<release>`. No pattern character of `gh release download -p`.
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,159}")
ASSET = re.compile(r"https://github\.com/(?P<repo>[A-Za-z0-9-]+/[A-Za-z0-9._-]+)/releases/download/"
                   r"(?P<tag>[A-Za-z0-9][A-Za-z0-9._+-]{0,127})/(?P<name>[^/?#\s]+)")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def gh(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["gh", *args], capture_output=True, text=True)


def download(repo: str, tag: str, asset: str, into: Path) -> Path | None:
    done = gh("release", "download", tag, "-R", repo, "-p", asset, "-D", str(into), "--clobber")
    path = into / asset
    return path if done.returncode == 0 and path.is_file() else None


def publish(repo: str, tag: str, title: str, file: Path) -> str | None:
    if gh("release", "view", tag, "-R", repo).returncode == 0:
        done = gh("release", "upload", tag, "-R", repo, str(file))
    else:
        done = gh("release", "create", tag, "-R", repo, "--title", title, "--notes", "", "--latest=false", str(file))
    return None if done.returncode == 0 else done.stderr.strip()


def rows(listing: str):
    """Each listed file as (sha256, id, version, file, size): the lines of `registry list`, where `file` is a plain
    name or an author's release asset, or the JSON of `registry list --build-assets`."""
    text = Path(listing).read_text()
    if text.lstrip().startswith("["):
        for row in json.loads(text):
            yield row["sha256"], row["id"], row["version"], row["url"], row["size"]
        return
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 4:
            yield *parts, None


def main(repo: str, listing: str, out: str, publishing: bool) -> int:
    files = Path(out)
    files.mkdir(parents=True, exist_ok=True)
    errors = []
    for digest, id_, version, file, size in rows(listing):
        tag = f"{id_}-{version}"
        # A stable row names the author's own release asset; the registry keeps a copy under the same name.
        theirs = ASSET.fullmatch(file)
        name = theirs["name"] if theirs else file
        if not (HEX.fullmatch(digest) and PLAIN.fullmatch(tag) and NAME.fullmatch(name) and "." in name
                and (theirs or PLAIN.fullmatch(file))):
            errors.append(f"{digest} {tag} {file}: not a plain sha256, id, version and a file name or GitHub release "
                          "asset")
            continue
        target = files / digest
        if target.is_file() and sha256(target) == digest:
            continue
        with tempfile.TemporaryDirectory() as tmp:
            got = download(repo, tag, name, Path(tmp))
            mirrored = got is not None
            if got is None and theirs:
                got = download(theirs["repo"], theirs["tag"], name, Path(tmp))
                where = f"nor in {theirs['repo']}'s release {theirs['tag']}"
            elif got is None:
                got = download(repo, "staging", f"{digest}.{name.rsplit('.', 1)[1]}", Path(tmp))
                where = f"nor in staging as {digest}.<extension>"
            if got is None:
                errors.append(f"{tag} {name}: not in its release, {where}")
                continue
            if (size is not None and got.stat().st_size != size) or sha256(got) != digest:
                errors.append(f"{tag} {name}: the downloaded file isn't the one its sha256 names")
                continue
            if not mirrored and publishing:
                named = Path(tmp) / "out" / name
                named.parent.mkdir()
                shutil.copyfile(got, named)
                failed = publish(repo, tag, f"{id_} {version}", named)
                if failed:
                    errors.append(f"{tag}: couldn't publish it: {failed}")
                    continue
                print(f"published {tag}/{name}")
            shutil.move(got, target)
            source = "" if mirrored else f" ({theirs['repo']})" if theirs else " (staging)"
            print(f"{digest} {tag}/{name}{source}")
    for e in errors:
        print(f"error: {e}", file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--publish"]
    if len(args) != 3:
        sys.exit("usage: fetch_files.py <owner/repo> <list.txt> <dir> [--publish]")
    sys.exit(main(*args, "--publish" in sys.argv[1:]))
