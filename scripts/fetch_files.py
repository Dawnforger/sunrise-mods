"""Downloads the files `cargo sunrise-mod registry list` printed into <dir>/<sha256> (README.md, "How it works")."""
import hashlib
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HEX = re.compile(r"[0-9a-f]{64}")
PLAIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


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


def main(repo: str, listing: str, out: str, publishing: bool) -> int:
    files = Path(out)
    files.mkdir(parents=True, exist_ok=True)
    errors = []
    for line in Path(listing).read_text().splitlines():
        parts = line.split()
        if len(parts) != 4:
            continue
        digest, id_, version, name = parts
        tag = f"{id_}-{version}"
        if not (HEX.fullmatch(digest) and PLAIN.fullmatch(tag) and PLAIN.fullmatch(name) and "." in name):
            errors.append(f"{line}: not a plain sha256, id, version and file name")
            continue
        target = files / digest
        if target.is_file() and sha256(target) == digest:
            continue
        with tempfile.TemporaryDirectory() as tmp:
            got = download(repo, tag, name, Path(tmp))
            staged = got is None
            if staged:
                got = download(repo, "staging", f"{digest}.{name.rsplit('.', 1)[1]}", Path(tmp))
            if got is None:
                errors.append(f"{tag} {name}: not in its release, nor in staging as {digest}.<extension>")
                continue
            if sha256(got) != digest:
                errors.append(f"{tag} {name}: the downloaded file isn't the one its sha256 names")
                continue
            if staged and publishing:
                named = Path(tmp) / "out" / name
                named.parent.mkdir()
                shutil.copyfile(got, named)
                failed = publish(repo, tag, f"{id_} {version}", named)
                if failed:
                    errors.append(f"{tag}: couldn't publish it: {failed}")
                    continue
                print(f"published {tag}/{name}")
            shutil.move(got, target)
            print(f"{digest} {tag}/{name}{' (staging)' if staged else ''}")
    for e in errors:
        print(f"error: {e}", file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--publish"]
    if len(args) != 3:
        sys.exit("usage: fetch_files.py <owner/repo> <list.txt> <dir> [--publish]")
    sys.exit(main(*args, "--publish" in sys.argv[1:]))
