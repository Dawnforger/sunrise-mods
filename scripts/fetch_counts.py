"""Writes GitHub's counts for the index (docs/modding/src/reference/registry-index.md, `downloads` and `stars`):
every release asset's download_count of the repositories the index's files are released in, by its download
address, and every GitHub source repository's stargazers_count, by `owner/repo`.

    python3 -I scripts/fetch_counts.py site/index.json > counts.json

`cargo sunrise-mod registry build --counts counts.json` sums them per project. A repository GitHub doesn't answer
for counts nothing; the build still publishes.
"""
import json
import re
import subprocess
import sys

RELEASE = re.compile(r"https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/releases/download/")
SOURCE = re.compile(r"https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?(?:/.*)?$")


def gh_json(path: str):
    done = subprocess.run(["gh", "api", "--paginate", "--slurp", path], capture_output=True, text=True)
    if done.returncode != 0:
        print(f"::warning::gh api {path}: {done.stderr.strip()}", file=sys.stderr)
        return None
    return json.loads(done.stdout)


def files(index: dict):
    for project in index.get("projects", []):
        for version in project.get("versions", []):
            yield version["file"]["url"]
            for binary in version.get("native_binaries", []):
                yield binary["file"]["url"]


def main(index_path: str) -> int:
    with open(index_path, encoding="utf-8") as f:
        index = json.load(f)
    downloads = {}
    for owner, repo in sorted({m.groups() for m in map(RELEASE.match, files(index)) if m}):
        pages = gh_json(f"repos/{owner}/{repo}/releases?per_page=100")
        for release in (r for page in pages or [] for r in page):
            for asset in release.get("assets", []):
                downloads[asset["browser_download_url"]] = asset["download_count"]
    stars = {}
    for owner, repo in sorted({m.groups() for p in index.get("projects", []) if (m := SOURCE.match(p.get("source", "")))}):
        pages = gh_json(f"repos/{owner}/{repo}")
        if pages:
            stars[f"{owner}/{repo}"] = pages[0]["stargazers_count"]
    json.dump({"downloads": downloads, "stars": stars}, sys.stdout, indent=1, sort_keys=True)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
