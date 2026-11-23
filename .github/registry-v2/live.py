"""Retrieve and verify the published v2 catalogue, independent of the new bot build.

Recovery must work even when the new bot does not compile. A missing catalogue
is allowed only on a double 404; partial or unverified live files are not copied.
"""
import argparse
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import quote, unquote

SITE = "https://sunrise-org.github.io/sunrise-mods/v2/"
LIMITS = {
    "index.json": 256 << 20,  # Existing mod tool's bounded::MAX_CATALOG.
    "index.json.minisig": 16 << 20,  # Existing registry fetch_releases.py metadata bound.
}
ICON_LIMIT = 256 << 10  # sunrise_mod_package::icon::MAX_ICON.
SCREENSHOT_LIMIT = 5 << 20  # sunrise_mod_package::page::MAX_SCREENSHOT_BYTES.
PROJECT = re.compile(r"[0-9A-Za-z]{8}")  # Same Modrinth ID rule as registry-bot.
SHA256 = re.compile(r"[0-9a-f]{64}")


def download(name, file, limit):
    file.parent.mkdir(parents=True, exist_ok=True)
    got = subprocess.run(
        ["curl", "--disable", "--silent", "--fail", "--location", "--globoff",
         "--proto", "=https", "--proto-redir", "=https", "--write-out", "%{http_code}",
         "--header", "Cache-Control: no-cache", "--max-filesize", str(limit),
         "--output", str(file), "--", SITE + name],
        capture_output=True,
    )
    if got.returncode == 22 and got.stdout == b"404":
        return False
    if got.returncode or got.stdout != b"200":
        raise ValueError(f"live v2 download failed (curl {got.returncode}, HTTP {got.stdout!r})")
    if file.stat().st_size > limit:
        raise ValueError("live v2 download exceeds the registry limit")
    return True


def images(index):
    refs = {}
    for project in index["projects"]:
        project_id = project["id"]
        if not PROJECT.fullmatch(project_id):
            raise ValueError("invalid live project ID")
        prefix = f"pages/{project_id}/"
        listed = [(project["icon"], ICON_LIMIT)] + [
            (shot["path"], SCREENSHOT_LIMIT) for shot in project["screenshots"]
        ]
        for name, limit in listed:
            if not name.startswith(prefix):
                raise ValueError("live image path is outside its project directory")
            path = unquote(name, errors="strict")
            if (quote(path, safe="/") != name or any(c in path for c in "\\:")
                    or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in path)
                    or any(part in ("", ".", "..") for part in path.split("/"))):
                raise ValueError("invalid encoded live image path")
            refs[name] = (path, min(refs.get(name, (path, limit))[1], limit))
    hashes = index["image_sha256"]
    if hashes.keys() != refs.keys():
        raise ValueError("signed image hashes do not cover exactly the listed images")
    for name, (path, limit) in refs.items():
        digest = hashes[name]
        if not SHA256.fullmatch(digest):
            raise ValueError("invalid live image digest")
        yield name, path, limit, digest


def verify(root, key):
    result = subprocess.run(
        ["minisign", "-Vm", str(root / "index.json"), "-p", str(key)], capture_output=True,
    )
    if result.returncode:
        raise ValueError("index signature verification failed")
    index = json.loads((root / "index.json").read_bytes())
    if index["schema"] != 2:
        raise ValueError("unsupported signed index schema")
    return index


def check_images(root, index, fetch=False):
    for name, path, limit, digest in images(index):
        file = root / path
        if fetch and not download(name, file, limit):
            raise ValueError(f"missing published image: {name!r}")
        if file.stat().st_size > limit or hashlib.sha256(file.read_bytes()).hexdigest() != digest:
            raise ValueError(f"image does not match the signed catalogue: {name!r}")


def restore(out, key, with_images):
    # Never leave an incomplete staging directory behind after an error.
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=out.parent) as directory:
        root = Path(directory) / "v2"
        found = [download(name, root / name, limit) for name, limit in LIMITS.items()]
        if not any(found):
            return
        if not all(found) or not key.is_file():
            raise ValueError("the live index, signature, and trusted public key are not all available")
        index = verify(root, key)
        if with_images:
            check_images(root, index, fetch=True)
        shutil.move(root, out)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--out", type=Path)
    action.add_argument("--check", type=Path)
    parser.add_argument("--key", type=Path, default=Path("registry/index-key.pub"))
    parser.add_argument("--with-images", action="store_true")
    args = parser.parse_args()
    try:
        if args.check is not None:
            check_images(args.check, verify(args.check, args.key))
        else:
            restore(args.out, args.key, args.with_images)
    except (KeyError, OSError, TypeError, ValueError) as error:
        # An error can contain hostile path or metadata text. Do not let it become a CI command.
        reason = ascii(str(error)).replace("::", "%3A%3A").replace("##[", "%23%23%5B")
        parser.exit(1, f"registry v2: {reason}\n")
