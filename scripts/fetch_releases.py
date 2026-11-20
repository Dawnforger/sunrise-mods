"""Write the stable release tags and the current Nightly's build for the registry."""
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


def nightly() -> dict:
    with tempfile.TemporaryDirectory() as folder:
        subprocess.run(["gh", "release", "download", "nightly", "-R", REPO, "-D", folder,
                        "-p", "sdk.json", "-p", "SHA256SUMS", "-p", "SHA256SUMS.sig"], check=True)
        subprocess.run(["sunrise-release-sign", "verify", "--repository", REPO, "--tag", "nightly",
                        "--only", "sdk.json", folder], check=True)
        index = json.loads((Path(folder) / "sdk.json").read_text())
    build = index["build"]
    sha, day = build["commit"], build["date"]
    if not (type(index["schema"]) is int and index["schema"] == 1
            and isinstance(sha, str) and SHA.fullmatch(sha)
            and isinstance(day, str) and DATE.fullmatch(day)):
        sys.exit("nightly: sdk.json has no known schema, build commit or date")
    date.fromisoformat(day)
    return {"commit": sha, "date": day}


def main(out: Path) -> None:
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
    (out / "nightly-build.json").write_text(json.dumps(current) + "\n")
    # The cache key follows the commits, not the release names or their dates.
    commits = {r["commit"] for r in releases} | {current["commit"]}
    (out / "sdk-commits.txt").write_text("".join(f"{sha}\n" for sha in sorted(commits)))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: fetch_releases.py <output-dir>")
    try:
        main(Path(sys.argv[1]))
    except (subprocess.CalledProcessError, OSError, ValueError, KeyError, TypeError) as error:
        sys.exit(f"could not read Sunrise releases: {error}")
