"""Write the stable release tags and the current Nightly's build for the registry. With `--nightly-optional` a
missing `nightly` release is a warning and no `nightly-build.json` is written; without it, an error."""
import json
import re
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path

REPO = "sunrise-org/sunrise"
SHA = re.compile(r"[0-9a-f]{40}")
DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
# A Nightly release's title, as Sunrise's verifier reads it (sunrise-release-sign: build_title_version).
TITLE = re.compile(rf"Nightly (?P<date>{DATE.pattern}) · (?P<short>[0-9a-f]{{7,40}})")
MAX_ASSET = 16 << 20  # The Nightly's metadata files are far smaller.


def asset(row: dict) -> bytes:
    """One small release asset by its id, so it is the asset of the release record that listed it."""
    if type(row["id"]) is not int or type(row["size"]) is not int or not 0 < row["size"] <= MAX_ASSET:
        sys.exit(f"nightly: {row['name']} has no usable id and size")
    data = subprocess.run(["gh", "api", "-H", "Accept: application/octet-stream",
                           f"repos/{REPO}/releases/assets/{row['id']}"], capture_output=True, check=True).stdout
    if len(data) != row["size"]:
        sys.exit(f"nightly: {row['name']} is not the size its release lists")
    return data


def nightly() -> dict | None:
    """The current Nightly's commit and date, or None while the moving `nightly` release is absent: Sunrise renames
    the old one away before it creates the next. The release's title names the build (`Nightly <date> · <commit>`),
    and its files are verified with the signature for exactly that build, so an older Nightly's files do not pass
    as the current one's."""
    found = subprocess.run(["gh", "api", f"repos/{REPO}/releases/tags/nightly"], capture_output=True, text=True)
    if found.returncode:
        if "HTTP 404" in found.stderr:
            return None
        sys.exit(f"nightly: {found.stderr.strip()}")
    # One release record gives the title and the assets: a rotation between two lookups of the moving tag could
    # pair one Nightly's title with another's files.
    record = json.loads(found.stdout)
    title = record["name"]
    named = TITLE.fullmatch(title) if isinstance(title, str) else None
    if not named:
        sys.exit("nightly: the release's title does not name its date and commit")
    build = f"nightly-{named['date']}-{named['short']}"
    date.fromisoformat(named["date"])
    listed = {row["name"]: row for row in record["assets"]}
    with tempfile.TemporaryDirectory() as folder:
        for name in ("sdk.json", "sunrise-release.json", "SHA256SUMS", "SHA256SUMS.v2.sig"):
            if name not in listed:
                sys.exit(f"nightly: the release has no {name}")
            (Path(folder) / name).write_bytes(asset(listed[name]))
        subprocess.run(["sunrise-release-sign", "verify", "--repository", REPO, "--tag", build,
                        "--signature", "SHA256SUMS.v2.sig", "--only", "sdk.json", "--only", "sunrise-release.json",
                        folder], check=True)
        index = json.loads((Path(folder) / "sdk.json").read_text())
        release = json.loads((Path(folder) / "sunrise-release.json").read_text())
    # Both signed files must be about that build: the release file by its name and commit, sdk.json by its commit.
    sha = release["commit"]
    if not (release["channel"] == "nightly" and release["build"] == build
            and isinstance(sha, str) and SHA.fullmatch(sha) and sha.startswith(named["short"])):
        sys.exit(f"nightly: sunrise-release.json is not about {build}")
    if type(index["schema"]) is not int or index["schema"] != 1 or index["build"]["commit"] != sha:
        sys.exit("nightly: sdk.json and sunrise-release.json name different builds")
    return {"commit": sha, "date": named["date"]}


def main(out: Path, optional: bool) -> None:
    out.mkdir(parents=True, exist_ok=True)
    # The pinned tool lists the stable releases and resolves their tags: it stops on pagination that shifts under
    # it instead of returning part of the list.
    subprocess.run(["cargo-sunrise-mod", "sunrise-mod", "registry", "releases", "--out", str(out / "releases.json")],
                   check=True)
    releases = json.loads((out / "releases.json").read_text())
    current = nightly()
    # The cache key follows the commits, not the release names or their dates.
    commits = {r["commit"] for r in releases}
    if current:
        (out / "nightly-build.json").write_text(json.dumps(current) + "\n")
        commits.add(current["commit"])
    elif optional:
        print("::warning::Sunrise has no `nightly` release right now: the Nightly build and its fit verdicts are "
              "left out of this run")
    else:
        sys.exit("waiting for Sunrise's `nightly` tag: it is absent while Sunrise publishes the next Nightly. Nothing "
                 "is deployed and the last good index stays live; the next run publishes once the tag is back")
    (out / "sdk-commits.txt").write_text("".join(f"{sha}\n" for sha in sorted(commits)))


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--nightly-optional"]
    if len(args) != 1:
        sys.exit("usage: fetch_releases.py <output-dir> [--nightly-optional]")
    try:
        main(Path(args[0]), len(args) < len(sys.argv) - 1)
    except (subprocess.CalledProcessError, OSError, ValueError, KeyError, TypeError) as error:
        sys.exit(f"could not read Sunrise releases: {error}")
