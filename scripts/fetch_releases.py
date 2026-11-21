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
TIMESTAMP = re.compile(DATE.pattern + r"T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
NUMBER = r"(0|[1-9][0-9]{0,19})"
NIGHTLY = re.compile(rf"nightly-(?P<date>{DATE.pattern})-(?P<short>[0-9a-f]{{7,40}})")
STABLE = re.compile(rf"[0-9]+(?:\.[0-9]+)+-{NUMBER}\.{NUMBER}\.{NUMBER}")


def api(path: str, *flags: str):
    return json.loads(subprocess.check_output(["gh", "api", *flags, f"repos/{REPO}/{path}"]))


def commit_of(tag: str) -> str:
    # A release's target_commitish is not its tag. Peel annotated tags as well as lightweight ones.
    target = api(f"git/ref/tags/{tag}")["object"]
    seen = set()
    while True:
        kind, sha = target["type"], target["sha"]
        if not isinstance(sha, str) or not SHA.fullmatch(sha):
            sys.exit(f"{tag}: GitHub did not return a full commit or tag SHA")
        if kind == "commit":
            return sha
        if kind != "tag" or sha in seen:
            sys.exit(f"{tag}: tag does not point to a commit")
        seen.add(sha)
        target = api(f"git/tags/{sha}")["object"]


def nightly() -> dict | None:
    """The current Nightly's commit and date from its signed files, or None while the moving `nightly` release is
    absent: Sunrise renames the old one away before it creates the next."""
    found = subprocess.run(["gh", "api", f"repos/{REPO}/releases/tags/nightly"], capture_output=True, text=True)
    if found.returncode:
        if "HTTP 404" in found.stderr:
            return None
        sys.exit(f"nightly: {found.stderr.strip()}")
    with tempfile.TemporaryDirectory() as folder:
        subprocess.run(["gh", "release", "download", "nightly", "-R", REPO, "-D", folder, "-p", "sdk.json",
                        "-p", "sunrise-release.json", "-p", "SHA256SUMS", "-p", "SHA256SUMS.sig"], check=True)
        subprocess.run(["sunrise-release-sign", "verify", "--repository", REPO, "--tag", "nightly",
                        "--only", "sdk.json", "--only", "sunrise-release.json", folder], check=True)
        index = json.loads((Path(folder) / "sdk.json").read_text())
        release = json.loads((Path(folder) / "sunrise-release.json").read_text())
    # The release file names the build `nightly-<date>-<short commit>`: that date is the Nightly's own, where
    # sdk.json records the commit's. Both files must be about one commit.
    sha, build = release["commit"], release["build"]
    name = NIGHTLY.fullmatch(build) if isinstance(build, str) else None
    if not (release["channel"] == "nightly" and isinstance(sha, str) and SHA.fullmatch(sha)
            and name and sha.startswith(name["short"])):
        sys.exit("nightly: sunrise-release.json has no Nightly build name and commit")
    if type(index["schema"]) is not int or index["schema"] != 1 or index["build"]["commit"] != sha:
        sys.exit("nightly: sdk.json and sunrise-release.json name different builds")
    date.fromisoformat(name["date"])
    return {"commit": sha, "date": name["date"]}


def main(out: Path, optional: bool) -> None:
    tags = set()
    for page in api("releases?per_page=100", "--paginate", "--slurp"):
        for release in page:
            tag, published = release["tag_name"], release["published_at"]
            match = STABLE.fullmatch(tag) if isinstance(tag, str) else None
            if (match and release["draft"] is False and release["prerelease"] is False
                    and isinstance(published, str) and TIMESTAMP.fullmatch(published)
                    and all(int(n) < 1 << 64 for n in match.groups())):
                tags.add(tag)
    releases = [{"tag": tag, "commit": commit_of(tag)} for tag in sorted(tags)]
    current = nightly()
    out.mkdir(parents=True, exist_ok=True)
    (out / "releases.json").write_text(json.dumps(releases) + "\n")
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
