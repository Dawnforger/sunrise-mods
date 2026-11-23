"""Privileged release PR publication/reporting and read-only PR shape checks.

Never checks out a PR or runs code from one. PR inputs go to Git/GitHub as data.
"""
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import quote

PROJECT = r"[A-Za-z0-9]{8}"
# Rust's semver parser owns the version; here constrain independent Git/path inputs.
VERSION = r"[0-9][0-9A-Za-z.+-]*"
RELEASE = re.compile(rf"release/(?P<project>{PROJECT})/(?P<version>{VERSION})", re.ASCII)
ROW = re.compile(rf"registry/projects/(?P<project>{PROJECT})/releases/(?P<version>{VERSION})\.toml", re.ASCII)
SHA = re.compile(r"[0-9a-f]{40}", re.ASCII)
PR = re.compile(r"[1-9][0-9]*", re.ASCII)
RUN = re.compile(rf"Registry release PR ({PR.pattern}) at ({SHA.pattern})", re.ASCII)
BOT = "github-actions[bot]"
STATUS = "registry-v2/release-check"
WORKFLOW = "release-check.yml"
MAIN = "refs/heads/main"
ROOT = Path(__file__).resolve().parents[2]


class CommandError(ValueError):
    pass


def command(*args, cwd=ROOT, data=None):
    result = subprocess.run(args, cwd=cwd, input=data, capture_output=True)
    if result.returncode:
        name = " ".join(map(str, args[:2]))
        raise CommandError(f"{name}: {result.stderr.decode('utf-8', errors='replace').strip()}")
    return result.stdout


def gh(*args, data=None):
    return command("gh", *args, data=None if data is None else json.dumps(data).encode())


def api(endpoint, *args, data=None):
    return json.loads(gh("api", "--hostname", "github.com", *args, "repos/" + os.environ["GITHUB_REPOSITORY"] + "/" + endpoint,
                         *(('--input', '-') if data is not None else ()), data=data))


def rows(endpoint):
    return [item for page in api(endpoint, "--paginate", "--slurp") for item in page]


def git(*args, cwd=ROOT):
    return command("git", *args, cwd=cwd)


def require(condition, detail):
    if not condition:
        raise ValueError(detail)


def safe(text):
    return str(text).encode("unicode_escape").decode("ascii").replace("::", "%3A%3A").replace("##[", "%23%23%5B")


def own_release(pr):
    repo = os.environ["GITHUB_REPOSITORY"].lower()
    head_repo = pr["head"]["repo"]
    return (head_repo and head_repo["full_name"].lower() == repo and pr["base"]["ref"] == "main"
            and pr["user"]["login"] == BOT and RELEASE.fullmatch(pr["head"]["ref"]))


def verify(out):
    number, sha = os.environ["PR_NUMBER"], os.environ["PR_HEAD"]
    require(PR.fullmatch(number) and SHA.fullmatch(sha), "dispatch requires a positive PR number and full lowercase head SHA")
    require(os.environ["GITHUB_REF"] == MAIN, "release checks run from main")
    pr = api(f"pulls/{number}")
    branch = own_release(pr)
    require(branch and pr["state"] == "open" and pr["head"]["sha"] == sha,
            "dispatch does not name the current head of an open registry release bot PR")
    git("fetch", "--no-tags", "origin", f"refs/pull/{number}/head")
    require(git("rev-parse", "FETCH_HEAD").decode().strip() == sha, "PR changed during fetch")
    base = git("merge-base", "HEAD", sha).decode().strip()
    project = branch["project"]
    root = f"registry/projects/{project}"
    row = f"{root}/releases/{branch['version']}.toml"
    page = f"{root}/page"
    changed = git("diff", "--name-only", "-z", "--no-renames", "--no-ext-diff", "--no-textconv", base, sha, "--").decode().split("\0")[:-1]
    require(row in changed and all(name == row or name.startswith(page + "/") for name in changed),
            "PR may only add its own release row and replace its own page")
    require(not git("ls-tree", "-z", base, "--", row), "PR may not edit an existing release row")
    actual = {}
    for entry in git("ls-tree", "-rz", sha, "--", row, page).split(b"\0")[:-1]:
        header, name = entry.split(b"\t", 1)
        mode, kind, blob = header.decode().split()
        require((mode, kind) == ("100644", "blob"), "release and page may only contain regular non-executable files")
        actual[name.decode()] = blob
    require(row in actual, "release row is missing from the PR head")

    out.mkdir()
    claim = out / "claim.toml"
    claim.write_bytes(git("cat-file", "blob", actual[row]))
    subprocess.run([os.environ["REGISTRY_BOT"], "verify-release", ROOT, os.environ["GITHUB_REPOSITORY"], project, claim, out],
                   cwd=ROOT, check=True)
    regenerated = out / "expected"
    expected = {p.relative_to(regenerated).as_posix(): git("hash-object", "--no-filters", p).decode().strip()
                for p in regenerated.rglob("*") if p.is_file()}
    require(actual == expected, "PR files are not exactly the release row and page regenerated from the attested assets")
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
        print("previous=" + str((out / "previous").is_dir()).lower(), file=stream)
    with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as stream:
        print("Verified the PR file scope, both attestations, hashes, package manifest, pinned build and resolved page.", file=stream)


def install_tree(generated, work, branch):
    if match := RELEASE.fullmatch(branch):
        root = Path("registry/projects") / match["project"]
        row, page = root / "releases" / (match["version"] + ".toml"), root / "page"
        found = {p.relative_to(generated).as_posix() for p in generated.rglob("*") if p.is_file()}
        require(row.as_posix() in found and (page / "page.toml").as_posix() in found
                and all(name == row.as_posix() or name.startswith(page.as_posix() + "/") for name in found),
                "watch artifact is outside its release row and page")
        require(not (work / row).exists(), "cannot alter a release already on main")
        (work / row).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(generated / row, work / row)
        if (work / page).exists():
            shutil.rmtree(work / page)
        shutil.copytree(generated / page, work / page)
        git("add", "--", row, page, cwd=work)
    else:
        row = Path("registry/watched.toml")
        require((generated / row).is_file(), "watch rename artifact is missing watched.toml")
        shutil.copyfile(generated / row, work / row)
        git("add", "--", row, cwd=work)


def open_pr(generated, branch, base, title, body):
    with tempfile.TemporaryDirectory() as folder:
        work = Path(folder) / "registry"
        git("worktree", "add", "--detach", work, base)
        try:
            install_tree(generated, work, branch)
            if not git("diff", "--cached", "--name-only", cwd=work):
                return
            git("-c", "core.hooksPath=/dev/null", "-c", "user.name=github-actions[bot]",
                "-c", "user.email=41898282+github-actions[bot]@users.noreply.github.com", "commit", "-s", "-m", title, cwd=work)
            # Never overwrite a branch. A run interrupted after push but before opening the
            # PR can reuse it only if its base and complete tree are exactly the ones just generated.
            cred = ("-c", "credential.helper=", "-c", "credential.helper=!gh auth git-credential")
            try:
                git(*cred, "push", "origin", f"HEAD:refs/heads/{branch}", cwd=work)
            except CommandError:
                git("fetch", "--no-tags", "origin", f"refs/heads/{branch}", cwd=work)
                require(git("rev-parse", "FETCH_HEAD^", "FETCH_HEAD^{tree}", cwd=work)
                        == git("rev-parse", "HEAD^", "HEAD^{tree}", cwd=work),
                        "an existing branch does not match the generated release; not replacing it")
            api("pulls", "-X", "POST", data={"base": "main", "head": branch, "title": title, "body": body})
        finally:
            git("worktree", "remove", "--force", work)


def publish(generated):
    base = git("rev-parse", "HEAD").decode().strip()
    live = api("git/ref/heads/main")["object"]["sha"]
    if live != base:
        # Do not commit stale generated pages or registry names. The next watch uses new main.
        print("Main changed during observation; no generated files were published.")
        return
    repo = os.environ["GITHUB_REPOSITORY"]
    open_prs = rows("pulls?state=open&base=main&per_page=100")
    open_branches = {pr["head"]["ref"] for pr in open_prs
                     if pr["head"]["repo"] and pr["head"]["repo"]["full_name"].lower() == repo.lower()}
    if (generated / "rename").exists() and "registry/refresh-repository-names" not in open_branches:
        open_pr(generated / "rename", "registry/refresh-repository-names", base, "Refresh watched repository names",
                "Updates the display names returned by GitHub for registered numeric repository IDs. No package or release identity changes.")
    for path in sorted((generated / "releases").glob("*/*")):
        branch = f"release/{path.parent.name}/{path.name}"
        match = RELEASE.fullmatch(branch)
        require(match, "watch artifact has an invalid release path")
        prefix = f"release/{match['project']}/"
        if any(other.startswith(prefix) for other in open_branches):
            continue
        open_pr(path, branch, base, f"Publish {match['project']} {match['version']}",
                "## Verified release\n\nBoth GitHub attestations match the registered repository ID, tag and commit, and the shared signer workflow. "
                "The asset sizes and SHA-256 hashes, package manifest, exact Sunrise build and resolved page were checked. "
                "The release check will reverify those inputs, build the package and run the scanner before review.")
    # A dispatch retry after a failed request, without redispatching runs already queued or completed.
    for pr in rows("pulls?state=open&base=main&per_page=100"):
        if not own_release(pr):
            continue
        number, sha = pr["number"], pr["head"]["sha"]
        require(PR.fullmatch(str(number)) and SHA.fullmatch(sha), "invalid GitHub PR identity")
        title = f"Registry release PR {number} at {sha}"
        runs = api(f"actions/workflows/{WORKFLOW}/runs?event=workflow_dispatch&branch=main&per_page=100",
                   "--paginate", "--slurp")
        if not any(run["display_title"] == title for page in runs for run in page["workflow_runs"]):
            gh("workflow", "run", WORKFLOW, "--repo", repo, "--ref", "main", "-f", f"pr={number}", "-f", f"head={sha}")


def report():
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_bytes())
    run = event["workflow_run"]
    repo = os.environ["GITHUB_REPOSITORY"]
    if (run["event"] != "workflow_dispatch" or run["path"] != ".github/workflows/" + WORKFLOW
            or run["head_branch"] != "main" or run["head_repository"]["id"] != event["repository"]["id"]):
        return
    match = RUN.fullmatch(run["display_title"])
    if not match:
        return  # Dispatch rejected before it could name a release PR.
    number, sha = match.groups()
    pr = api(f"pulls/{number}")
    if not own_release(pr) or pr["head"]["sha"] != sha:
        return  # A stale completion never writes to a replacement head.
    latest = api(f"actions/runs/{run['id']}")
    if latest["run_attempt"] != run["run_attempt"]:
        return  # A newer rerun owns the result.
    conclusion = run["conclusion"]
    target = f"https://github.com/{repo}/actions/runs/{run['id']}/attempts/{run['run_attempt']}"
    marker = "<!-- registry-v2-release-check -->"
    body = f"{marker}\n## Registry release check\n\nResult: **{safe(conclusion)}**. [Check run]({target}).\n"
    success = False
    try:
        jobs = api(f"actions/runs/{run['id']}/attempts/{run['run_attempt']}/jobs?per_page=100", "--paginate", "--slurp")
        jobs = [job for page in jobs for job in page["jobs"]]
        names = {job["name"] for job in jobs}
        expected = {"Verify release", "Build / Linux", "Build / macOS", "Build / Windows"}
        complete = (expected <= names and any(name.endswith(" / scan") for name in names)
                    and all(job["conclusion"] == "success" for job in jobs))
        body += "\n" + "\n".join(f"- {safe(job['name'])}: {safe(job['conclusion'])}" for job in jobs) + "\n"
        artifacts = api(f"actions/runs/{run['id']}/artifacts?name=scan-summary&per_page=100")["artifacts"]
        reports = [a for a in artifacts if a["name"] == "scan-summary" and not a["expired"]]
        if len(reports) == 1:
            raw = gh("api", "--hostname", "github.com", f"repos/{repo}/actions/artifacts/{reports[0]['id']}/zip")
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                require(archive.namelist() == ["summary.md"], "unexpected scanner report artifact shape")
                with archive.open("summary.md") as summary:
                    # GitHub's issue/PR comment body is limited to 65,536 characters. Keep whole lines.
                    text = summary.read(60_000)
                    truncated = len(text) == 60_000
                    if truncated:
                        text = text.rsplit(b"\n", 1)[0]
                    body += "\n" + text.decode("utf-8", errors="replace").replace("::", "%3A%3A").replace("##[", "%23%23%5B")
                    if truncated:
                        body += "\n\nReport excerpt; see the full `scan-summary` artifact linked above.\n"
        else:
            body += "\nNo scanner report was available for this run.\n"
        comments = rows(f"issues/{number}/comments?per_page=100")
        current = next((c for c in comments if c["user"]["login"] == BOT and c["body"].startswith(marker)), None)
        if current:
            api(f"issues/comments/{current['id']}", "-X", "PATCH", data={"body": body})
        else:
            api(f"issues/{number}/comments", "-X", "POST", data={"body": body})
        success = conclusion == "success" and complete and len(reports) == 1
    finally:
        api(f"statuses/{sha}", "-X", "POST", data={"context": STATUS, "state": "success" if success else "failure",
                                                     "description": "All release checks passed" if success else "Release checks failed or did not complete",
                                                     "target_url": target})


if __name__ == "__main__":
    try:
        match sys.argv[1:]:
            case ["publish", folder]:
                publish(Path(folder))
            case ["verify", folder]:
                verify(Path(folder))
            case ["report"]:
                report()
            case _:
                raise ValueError("usage: release_pr.py publish|verify <folder>, or report")
    except (OSError, ValueError, KeyError, TypeError, subprocess.CalledProcessError, zipfile.BadZipFile) as error:
        sys.exit("registry release: " + safe(error))
