import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile

import build_release
import release_pr as release

SHA = "a" * 40
REPO = "sunrise-org/sunrise-mods"


def pull(branch="release/Ab3dE9xQ/1.0.0", number=10, state="open"):
    return {"number": number, "state": state, "user": {"login": release.BOT},
            "base": {"ref": "main"}, "head": {"repo": {"full_name": REPO}, "ref": branch, "sha": SHA}}


class ReleasePRTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, GITHUB_REPOSITORY=REPO, GITHUB_REF=release.MAIN,
                                 PR_NUMBER="10", PR_HEAD=SHA, GITHUB_RUN_ID="1", GITHUB_RUN_ATTEMPT="1",
                                 REPORT_DOWNLOADED="true")
        environment.start()
        self.addCleanup(environment.stop)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.folder = Path(directory.name)

    def test_dispatch_rejects_wrong_author_base_sha_or_path(self):
        good = pull()
        cases = (("user", "login", "author"), ("base", "ref", "dev"), ("head", "sha", "b" * 40),
                 ("head", "ref", "not-a-release"), ("head", "repo", {"full_name": "someone/fork"}))
        for group, key, value in cases:
            with self.subTest(key=key):
                pr = copy.deepcopy(good)
                pr[group][key] = value
                with patch.object(release, "api", return_value=pr), patch.object(release, "git") as git:
                    with self.assertRaisesRegex(ValueError, "open registry release bot PR"):
                        release.verify(self.folder / "out")
                    git.assert_not_called()
        with patch.object(release, "api", return_value=good), patch.object(release, "git") as git:
            git.side_effect = [b"", SHA.encode(), b"b" * 40,
                               b"registry/projects/Ab3dE9xQ/releases/1.0.0.toml\x00.github/workflows/evil.yml\x00"]
            with self.assertRaisesRegex(ValueError, "may only add its own release row"):
                release.verify(self.folder / "out")

    def test_rejection_and_publish_error_do_not_block_another_project_or_dispatch(self):
        rejected = pull(state="closed")
        open_pr = pull("release/Zz91kLmN/1.0.0", number=11)
        for branch in (rejected["head"]["ref"], "release/Bb3dE9xQ/1.0.0", "release/Cb3dE9xQ/1.0.0"):
            (self.folder / "releases" / branch.removeprefix("release/")).mkdir(parents=True)
        summary = self.folder / "summary.md"
        api = Mock(side_effect=[{"object": {"sha": SHA}}, [{"workflow_runs": []}]])
        with patch.dict(os.environ, GITHUB_STEP_SUMMARY=str(summary)), patch.object(release, "api", api), \
             patch.object(release, "rows", side_effect=[[rejected, open_pr], [open_pr]]), \
             patch.object(release, "git", return_value=SHA.encode()), patch.object(release, "gh") as gh, \
             patch.object(release, "open_pr", side_effect=[release.CommandError("::error\n##[warning]"), None]) as publish:
            release.publish(self.folder)
        self.assertEqual([call.args[1] for call in publish.call_args_list],
                         ["release/Bb3dE9xQ/1.0.0", "release/Cb3dE9xQ/1.0.0"])
        gh.assert_called_once_with("workflow", "run", release.WORKFLOW, "--repo", REPO,
                                   "--ref", "main", "-f", "pr=11", "-f", f"head={SHA}")
        text = summary.read_text()
        self.assertIn("Could not publish release/Bb3dE9xQ/1.0.0", text)
        self.assertNotIn("::error", text)
        self.assertNotIn("##[", text)

    def test_a_rename_refresh_only_rewrites_repository_files_already_on_main(self):
        generated, work = self.folder / "rename", self.folder / "work"
        row = Path("registry/repositories/123456789.toml")
        for root, text in ((generated, 'name = "example/renamed"\n'), (work, 'name = "example/wayfinder"\n')):
            (root / row).parent.mkdir(parents=True)
            (root / row).write_text(text)
        with patch.object(release, "git") as git:
            release.install_tree(generated, work, "registry/refresh-repository-names")
        self.assertEqual((work / row).read_text(), 'name = "example/renamed"\n')
        git.assert_called_once_with("add", "--", row, cwd=work)
        for stray in ("registry/repositories/987654321.toml", "registry/repositories/0123.toml",
                      "registry/projects/Ab3dE9xQ/project.toml"):
            with self.subTest(stray=stray):
                (generated / stray).parent.mkdir(parents=True, exist_ok=True)
                (generated / stray).write_text("")
                with self.assertRaisesRegex(ValueError, "not a set of registered repository files"):
                    release.install_tree(generated, work, "registry/refresh-repository-names")
                (generated / stray).unlink()

    def test_a_maintenance_pr_is_sent_to_the_non_release_classifier_once(self):
        rename = pull("registry/refresh-repository-names", number=12)
        summary = self.folder / "summary.md"
        for statuses, expected in (([], 1), ([{"context": release.STATUS}], 0)):
            api = Mock(side_effect=[{"object": {"sha": "0" * 40}}, statuses])
            with patch.dict(os.environ, GITHUB_STEP_SUMMARY=str(summary)), patch.object(release, "api", api), \
                 patch.object(release, "rows", return_value=[rename]), \
                 patch.object(release, "git", return_value=SHA.encode()), patch.object(release, "gh") as gh:
                release.publish(self.folder)
            self.assertEqual(gh.call_count, expected)
            if expected:
                gh.assert_called_once_with("workflow", "run", release.NOT_A_RELEASE, "--repo", REPO,
                                           "--ref", "main", "-f", "pr=12")

    def test_a_burst_of_open_release_prs_lists_the_dispatched_runs_once(self):
        first, second = pull("release/Ab3dE9xQ/1.0.0", number=10), pull("release/Bb3dE9xQ/1.0.0", number=11)
        api = Mock(return_value={"object": {"sha": "0" * 40}})
        runs = Mock(return_value=[{"display_title": f"Registry release PR 10 at {SHA}"}])
        with patch.dict(os.environ, GITHUB_STEP_SUMMARY=str(self.folder / "summary.md")), \
             patch.object(release, "api", api), patch.object(release, "rows", return_value=[first, second]), \
             patch.object(release, "git", return_value=SHA.encode()), patch.object(release, "gh") as gh, \
             patch.object(release, "dispatch_runs", runs):
            release.publish(self.folder)
        runs.assert_called_once_with()
        gh.assert_called_once_with("workflow", "run", release.WORKFLOW, "--repo", REPO,
                                   "--ref", "main", "-f", "pr=11", "-f", f"head={SHA}")

    def report_jobs(self, attempt="1"):
        path = self.folder / "summary.zip"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("summary.md", "A trusted scan report.\n")
        return {"pending": {"result": "success"}, "build": {"result": "success"},
                "verify": {"result": "success", "outputs": {"attempt": attempt}},
                "scanner": {"result": "success", "outputs": {"attempt": attempt, "summary-id": "84",
                            "summary-digest": hashlib.sha256(path.read_bytes()).hexdigest()}}}

    def run_report(self, jobs, run_id="1", attempt="1", downloaded="true", newest=None):
        calls = Mock(return_value=pull())
        matching_runs = [{"id": int(run_id), "run_attempt": int(attempt),
                          "display_title": f"Registry release PR 10 at {SHA}"}]
        if newest is not None:
            matching_runs.append({**matching_runs[0], "id": int(newest)})
        with patch.dict(os.environ, JOB_RESULTS=json.dumps(jobs), GITHUB_RUN_ID=run_id,
                        GITHUB_RUN_ATTEMPT=attempt, REPORT_DOWNLOADED=downloaded), \
             patch.object(release, "api", calls), patch.object(release, "rows", return_value=[]), \
             patch.object(release, "dispatch_runs", return_value=matching_runs):
            if newest is None:
                release.pending()
            else:
                with self.assertRaisesRegex(ValueError, "not the latest run"):
                    release.pending()
            release.report(self.folder)
        posts = [call for call in calls.call_args_list if call.args[1:] == ("-X", "POST")]
        return [call.kwargs["data"] for call in posts]

    def test_older_success_cannot_overwrite_newer_failure(self):
        jobs = self.report_jobs()
        first = self.run_report(jobs, run_id="111")
        jobs["build"]["result"] = "failure"
        second = self.run_report(jobs, run_id="112")
        self.assertEqual([item["state"] for item in (first[0], first[-1], second[0], second[-1])],
                         ["pending", "success", "pending", "failure"])
        self.assertIn("/112/attempts/1", second[-1]["target_url"])
        jobs["build"]["result"] = "success"
        self.assertEqual(self.run_report(jobs, run_id="111", attempt="2", newest="112"), [])

    def test_skipped_cancelled_and_timed_out_jobs_fail(self):
        for result in ("skipped", "cancelled", "timed_out"):
            with self.subTest(result=result):
                jobs = self.report_jobs()
                jobs["build"]["result"] = result
                self.assertEqual(self.run_report(jobs)[-1]["state"], "failure")

    def test_partial_rerun_never_renders_an_earlier_summary(self):
        jobs = self.report_jobs()
        posts = self.run_report(jobs, attempt="2", downloaded="false")
        self.assertNotIn("A trusted scan report.", posts[-2]["body"])
        self.assertIn("Rerun all jobs", posts[-2]["body"])
        self.assertEqual((posts[-1]["state"], posts[-1]["description"]), ("failure", "Rerun all jobs"))

    def test_scanner_report_digest_is_from_trusted_job_output(self):
        jobs = self.report_jobs()
        with zipfile.ZipFile(self.folder / "summary.zip", "w") as archive:
            archive.writestr("summary.md", "different report")
        api = Mock(return_value=pull())
        with patch.dict(os.environ, JOB_RESULTS=json.dumps(jobs)), patch.object(release, "api", api), \
             patch.object(release, "current_run", return_value=True):
            with self.assertRaisesRegex(ValueError, "does not match the digest"):
                release.report(self.folder)
        self.assertEqual(api.call_args.kwargs["data"]["state"], "failure")

    def test_release_contract_refuses_old_or_noninteger_tool_descriptors(self):
        for meta in ({}, {"release_contract": 0}, {"release_contract": True}, {"release_contract": "1"}):
            with self.subTest(meta=meta), self.assertRaisesRegex(ValueError, "release_contract >= 1"):
                build_release.require_release_contract(meta)
        build_release.require_release_contract({"release_contract": 1})
        build_release.require_release_contract({"release_contract": 2})
