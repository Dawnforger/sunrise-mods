"""Read public Nightly source manifests at the exact default-branch head, as data only."""
import argparse
import base64
import json
import re
import subprocess
import sys
import tomllib
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

REPO = re.compile(r"(?!\.{1,2}/)[A-Za-z0-9_.-]{1,39}/(?!\.{1,2}$)[A-Za-z0-9_.-]{1,100}")
ID = re.compile(r"[a-z0-9_]{1,64}")
SHA = re.compile(r"[0-9a-f]{40}")
DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
# A Git branch name: ordinary ref characters, with no special components or sequences.
BRANCH = re.compile(r"(?![-/])(?!.*(?:\.\.|@\{|//))(?!.*(?:^|/)\.)(?!.*\.lock(?:/|$))"
                    r"[^\x00-\x20\x7f~^:?*\[\\]+(?<![/.])")
PATH = re.compile(r"(?!.*(?:^|/)\.{1,2}(?:/|$))[^/\\:\x00-\x1f\x7f]+"
                  r"(?:/[^/\\:\x00-\x1f\x7f]+)*")


def read_api(endpoint: str, problem: str) -> dict:
    result = subprocess.run(["gh", "api", endpoint], capture_output=True, text=True)
    if result.returncode:
        raise ValueError(problem)
    data = json.loads(result.stdout)
    if not isinstance(data, dict):
        raise ValueError(problem)
    return data


def metadata(project: str, source: dict) -> dict:
    if not isinstance(source, dict):
        raise ValueError("nightly must be a table")
    repo, folder = source.get("repo"), source.get("path")
    if not isinstance(repo, str) or not REPO.fullmatch(repo):
        raise ValueError("nightly.repo must be GitHub owner/name")
    if folder is not None and (not isinstance(folder, str) or not PATH.fullmatch(folder)):
        raise ValueError("nightly.path must be a relative directory in the repository")
    root = f"repos/{repo}"
    details = read_api(root, f"{repo} is not a readable public repository")
    if details.get("visibility") != "public":
        raise ValueError(f"{repo} is not public")
    branch = details.get("default_branch")
    if not isinstance(branch, str) or not BRANCH.fullmatch(branch) or branch == "@":
        raise ValueError(f"{repo} has no valid default branch")
    head = read_api(f"{root}/branches/{quote(branch, safe='')}", f"could not read {repo}'s default branch")["commit"]
    sha, updated = head["sha"], head["commit"]["committer"]["date"]
    if not isinstance(sha, str) or not SHA.fullmatch(sha):
        raise ValueError(f"{repo}'s default branch did not return a full commit SHA")
    if not isinstance(updated, str) or not DATE.fullmatch(updated):
        raise ValueError(f"{repo}'s head commit did not return a UTC timestamp")
    datetime.fromisoformat(updated)
    path = f"{folder}/sunrise-mod.toml" if folder is not None else "sunrise-mod.toml"
    missing = f"{repo} has no readable {path!r} at {sha}"
    file = read_api(f"{root}/contents/{quote(path)}?ref={sha}", missing)
    if (file.get("type") != "file" or file.get("encoding") != "base64"
            or not isinstance(file.get("content"), str)):
        raise ValueError(missing)
    text = base64.b64decode(file["content"].replace("\n", ""), validate=True).decode("utf-8")
    manifest = tomllib.loads(text)
    if manifest.get("id") != project:
        raise ValueError(f"{repo}'s manifest id is {manifest.get('id')!r}, expected {project!r}")
    result = {"branch": branch, "commit": sha, "updated": updated}
    if "native" in manifest:
        native = manifest["native"]
        pin = native.get("sunrise") if isinstance(native, dict) else None
        if not isinstance(pin, str) or not SHA.fullmatch(pin):
            raise ValueError(f"{repo}'s manifest has no full [native] sunrise pin")
        result["pin"] = pin
    return result


def main(checkout: Path, output: Path, warn: bool) -> int:
    mods = checkout / "mods"
    if mods.is_symlink():
        sys.exit("mods must not be a symlink")
    rows = {}
    failed = False
    for file in sorted(mods.glob("*.toml")):
        project = file.stem
        try:
            if file.is_symlink() or not file.is_file():
                raise ValueError("entry is not a plain file")
            with file.open("rb") as data:
                entry = tomllib.load(data)
            if "nightly" in entry:
                if not ID.fullmatch(project) or entry.get("id") != project:
                    raise ValueError("entry id must match the file name")
                rows[project] = metadata(project, entry["nightly"])
        except (KeyError, OSError, TypeError, ValueError) as error:
            # GitHub and the entries only supply data, not workflow commands.
            print(f"{'warning' if warn else 'error'}: {project!r}: {str(error)!r}", file=sys.stderr)
            failed = True
    output.write_text(json.dumps(rows) + "\n")
    return int(failed and not warn)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checkout", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--warn", action="store_true", help="skip unavailable sources instead of failing")
    sys.exit(main(**vars(parser.parse_args())))
