"""Offline fixtures for the signed Pages publish and recovery decisions."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from unittest.mock import patch

import live

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
PORTS = REPO / "bot/tests/fixtures/two-ports"
FIXTURES = HERE / "fixtures"
KEY = FIXTURES / "index-key.pub"
OLDER = (PORTS / "index.json").read_bytes()
NEWER = OLDER.replace(b'"serial": 41,', b'"serial": 42,', 1)
BODIES = {"older": OLDER, "newer": NEWER, "equal-different": OLDER + b"\n"}


class Publish(unittest.TestCase):
    def setUp(self):
        output = Path(os.environ["CARGO_TARGET_DIR"])
        scratch = tempfile.TemporaryDirectory(dir=output)
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.bot = Path(os.environ["REGISTRY_BOT"])
        self.verifier = Path(os.environ["REGISTRY_VERIFIER"])
        self.site = self.root / "site"
        self.site.mkdir()
        self.legacy = {"index.json": b"new legacy index\n", "sources.json": b"new legacy sources\n",
                       "icons/example.png": b"legacy icon"}
        for name, body in self.legacy.items():
            path = self.site / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)

    def snapshot(self, root, variant):
        root.mkdir(exist_ok=True)
        (root / "index.json").write_bytes(BODIES[variant])
        shutil.copyfile(FIXTURES / f"{variant}.minisig", root / "index.json.minisig")
        for project in json.loads(OLDER)["projects"]:
            source = PORTS / "registry/projects" / project["id"] / "page/icon.png"
            # The golden fixture's screenshots are byte-identical to each project's icon.
            file = root / project["icon"]
            file.parent.mkdir(parents=True)
            shutil.copyfile(source, file)
        return root

    def live_from(self, root):
        def download(name, file, _limit):
            source = root / name
            if not source.is_file():
                return False
            file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, file)
            return True
        return patch.object(live, "download", side_effect=download)

    def assert_site(self, variant=None):
        for name, bytes_ in self.legacy.items():
            self.assertEqual((self.site / name).read_bytes(), bytes_)
        if variant is None:
            self.assertFalse((self.site / "v2").exists())
        else:
            published = self.site / "v2"
            index = live.verify(published, KEY, self.verifier)
            live.check_images(published, index)
            self.assertEqual(index.body, BODIES[variant])

    def test_old_main_commit_cannot_sign_or_deploy(self):
        workflow = (REPO / ".github/workflows/publish.yml").read_text()
        old_sha, current_sha = "a" * 40, "b" * 40
        gh = self.root / "gh"
        gh.write_text("#!/bin/sh\n"
                      "[ \"$1\" = api ] && [ \"$2\" = repos/example/mods/git/ref/heads/main ] || exit 2\n"
                      "printf '%s\\n' \"$CURRENT_SHA\"\n")
        gh.chmod(0o755)
        for job in ("sign", "deploy"):
            with self.subTest(job=job):
                section = workflow.split(f"\n  {job}:\n", 1)[1]
                first = section.split("    steps:\n", 1)[1]
                self.assertTrue(first.startswith("      - name: Require the current main tip\n"))
                block = first.split("        run: |\n", 1)[1].split("\n      - ", 1)[0]
                command = textwrap.dedent(block)
                env = {**os.environ, "PATH": f"{self.root}:{os.environ['PATH']}",
                       "GITHUB_REPOSITORY": "example/mods", "GITHUB_REF": "refs/heads/main",
                       "CURRENT_SHA": current_sha, "GITHUB_SHA": old_sha}
                stale = subprocess.run(["bash", "-e", "-c", command], env=env, capture_output=True)
                self.assertEqual(stale.returncode, 1, stale.stderr)
                self.assertIn(b"current main tip", stale.stdout)
                env["GITHUB_SHA"] = current_sha
                current = subprocess.run(["bash", "-e", "-c", command], env=env, capture_output=True)
                self.assertEqual(current.returncode, 0, current.stderr)

    def test_deploy_retry_refuses_an_older_signed_candidate(self):
        candidate = self.snapshot(self.root / "candidate", "older")
        published = self.snapshot(self.root / "published", "newer")
        with self.live_from(published), patch.object(live, "read_deployed", return_value=None):
            with self.assertRaisesRegex(ValueError, "old or different equal serial"):
                live.guard_deploy(candidate, KEY, self.verifier, carried=False)

    def test_deploy_retry_uses_last_deployment_when_public_pages_lags(self):
        candidate = self.snapshot(self.root / "candidate", "older")
        published = self.snapshot(self.root / "published", "newer")
        verified = live.verify(published, KEY, self.verifier)
        with self.live_from(candidate), patch.object(live, "read_deployed", return_value=verified):
            with self.assertRaisesRegex(ValueError, "old or different equal serial"):
                live.guard_deploy(candidate, KEY, self.verifier, carried=True)

    def test_carry_only_reuses_equal_serial_for_identical_index_bytes(self):
        published = self.snapshot(self.root / "published", "older")
        equal = self.snapshot(self.root / "different", "equal-different")
        with self.live_from(published), patch.object(live, "read_deployed", return_value=None):
            live.guard_deploy(published, KEY, self.verifier, carried=True)
            with self.assertRaisesRegex(ValueError, "old or different equal serial"):
                live.guard_deploy(equal, KEY, self.verifier, carried=True)
            with self.assertRaisesRegex(ValueError, "old or different equal serial"):
                live.guard_deploy(published, KEY, self.verifier, carried=False)

    def test_legacy_publish_carries_live_snapshot_after_generation_failure(self):
        self.assert_failed_candidate_carries_live("generation")

    def test_legacy_publish_carries_live_snapshot_after_signing_failure(self):
        self.assert_failed_candidate_carries_live("signing")

    def test_legacy_publish_carries_live_snapshot_after_verification_failure(self):
        self.assert_failed_candidate_carries_live("verification")

    def assert_failed_candidate_carries_live(self, phase):
        candidate = self.root / "candidate"
        pages = self.root / "v2-pages.tar"
        published = self.snapshot(self.root / "published", "older")
        if phase in ("signing", "verification"):
            candidate.mkdir()
            (candidate / "index.json").write_bytes(NEWER)
            subprocess.run(["tar", "-C", published, "-cf", pages, "pages"], check=True)
        if phase == "verification":
            shutil.copyfile(FIXTURES / "older.minisig", candidate / "index.json.minisig")
        with self.live_from(published), patch.object(live, "read_deployed") as archived:
            self.assertTrue(live.assemble(self.site, candidate, pages, KEY, self.verifier))
            archived.assert_not_called()
        self.assert_site("older")

    def test_live_read_failure_uses_last_successful_deploy_run_artifact(self):
        published = self.snapshot(self.root / "previous", "older")
        env = {"GITHUB_SERVER_URL": "https://github.com", "GITHUB_REPOSITORY": "example/mods"}
        sha = "c" * 40
        log_url = f"{env['GITHUB_SERVER_URL']}/{env['GITHUB_REPOSITORY']}/actions/runs/70/job/80"
        api = {
            "deployments?environment=github-pages&per_page=100&page=1": [{"id": 10}, {"id": 9, "ref": "main", "sha": sha}],
            "deployments/10/statuses?per_page=100": [{"state": "in_progress"}],
            "deployments/9/statuses?per_page=100": [{"state": "success", "log_url": log_url}],
            "actions/runs/70": {"head_branch": "main", "path": ".github/workflows/publish.yml", "head_sha": sha,
                                "conclusion": "failure"},  # Pages succeeded before v2 was reported red.
            "actions/jobs/80": {"run_id": 70, "name": "deploy", "run_attempt": 2, "head_sha": sha},
            "actions/runs/70/artifacts?per_page=100&page=1": {"artifacts": [
                {"name": "v2-snapshot-3", "expired": False},  # A later assembly did not deploy.
                {"name": "v2-snapshot-1", "expired": False},  # Attempt 2 retried only deploy.
            ]},
            "actions/runs/70/artifacts?per_page=100&page=2": {"artifacts": []},
        }
        run = subprocess.run
        downloads = []

        def run_with_artifact(command, **kwargs):
            if command[:3] == ["gh", "run", "download"]:
                self.assertEqual(command[3:-1], ["70", "--repo", "example/mods", "--name", "v2-snapshot-1", "--dir"])
                shutil.copytree(published, command[-1])
                downloads.append(command[3])
                return subprocess.CompletedProcess(command, 0)
            return run(command, **kwargs)

        with (patch.dict(os.environ, env), patch.object(live, "gh_api", side_effect=api.__getitem__),
              patch.object(live, "download", side_effect=ValueError("live Pages returned HTTP 503")),
              patch.object(subprocess, "run", side_effect=run_with_artifact)):
            self.assertTrue(live.assemble(self.site, self.root / "none", self.root / "none.tar", KEY, self.verifier))
            live.guard_deploy(self.site / "v2", KEY, self.verifier, carried=True)
        self.assertEqual(downloads, ["70", "70"])
        self.assert_site("older")

    def test_no_key_never_reads_v2_and_keeps_legacy_site(self):
        with patch.object(live, "download") as download, patch.object(live, "gh_api") as api:
            no_key = self.root / "no-key"
            self.assertFalse(live.assemble(self.site, self.root / "none", self.root / "no.tar", no_key, self.verifier))
            live.guard_deploy(self.root / "none", no_key, self.verifier, carried=False)
            download.assert_not_called()
            api.assert_not_called()
        self.assert_site()

    def test_missing_authenticated_snapshot_has_a_named_failure(self):
        with (patch.object(live, "read_live", side_effect=ValueError("live read failed")),
              patch.object(live, "read_deployed", return_value=None)):
            with self.assertRaisesRegex(ValueError, "NO_AUTHENTICATED_V2_SNAPSHOT"):
                live.assemble(self.site, self.root / "none", self.root / "no.tar", KEY, self.verifier)
        self.assert_site()

    def test_author_dot_github_screenshot_survives_the_pages_archive_rule(self):
        source = PORTS / "registry/projects/Zz91kLmN/page/.github/map #1 %.png"
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        relative = f"pages/Zz91kLmN/{digest}.png"
        candidate = self.root / "candidate"
        subprocess.run([self.bot, "index", PORTS, "--serial", "41", "--out", candidate], check=True)
        generated = json.loads((candidate / "index.json").read_bytes())
        self.assertEqual(generated["projects"][1]["screenshots"][0]["path"], relative)
        pages = self.root / "v2-pages.tar"
        subprocess.run(["tar", "-C", candidate, "-cf", pages, "pages"], check=True)
        shutil.rmtree(candidate / "pages")
        (candidate / "index.json").write_bytes(OLDER)
        shutil.copyfile(FIXTURES / "older.minisig", candidate / "index.json.minisig")
        self.assertFalse(live.assemble(self.site, candidate, pages, KEY, self.verifier))
        self.assert_site("older")
        # Also prove the guard would reject an otherwise staged image that Pages strips.
        stripped = "pages/Zz91kLmN/.github/shot.png"
        file = self.site / "v2" / stripped
        file.parent.mkdir()
        file.write_bytes(source.read_bytes())
        with self.assertRaisesRegex(ValueError, "Pages archive would omit signed v2 files"):
            live.check_archive(self.site, live.Index(41, OLDER, {stripped: (live.SCREENSHOT_LIMIT, digest)}))


if __name__ == "__main__":
    unittest.main()
