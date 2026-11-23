from pathlib import Path
import tempfile
import unittest

from cache_guard import guard


class CacheGuardTests(unittest.TestCase):
    def test_rejects_cache_actions_and_setup_options_through_callees(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workflows = root / ".github/workflows"
            workflows.mkdir(parents=True)
            (workflows / "release-check.yml").write_text("""
 jobs:
   build:
     cache-mode: none
     steps:
       - run: echo build
   scanner:
     uses: ./.github/workflows/scan.yml
 """)
            scan = workflows / "scan.yml"
            scan.write_text("""
 jobs:
   scan:
     cache-mode: none
     steps:
       - uses: ./.github/actions/setup
 """)
            action = root / ".github/actions/setup/action.yml"
            action.parent.mkdir(parents=True)
            action.write_text("runs: {using: composite, steps: [{run: echo safe}]}\n")
            guard(root)
            for step in ("{uses: actions/cache@v4}", "{uses: actions/cache/restore@v4}",
                         "{uses: actions/setup-node@v6, with: {package-manager-cache: true}}"):
                with self.subTest(step=step):
                    action.write_text(f"runs: {{using: composite, steps: [{step}]}}\n")
                    with self.assertRaisesRegex(ValueError, "contains a cache step or option"):
                        guard(root)

    def test_author_jobs_must_deny_cache_access(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workflow = root / ".github/workflows/release-check.yml"
            workflow.parent.mkdir(parents=True)
            workflow.write_text("jobs: {build: {cache-mode: read, steps: []}, scanner: {steps: []}}\n")
            with self.assertRaisesRegex(ValueError, "build must set cache-mode: none"):
                guard(root)
