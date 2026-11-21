"""fetch_releases.py's reading of the Nightly, against stubbed `gh` and release verification."""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

spec = importlib.util.spec_from_file_location("fetch_releases", Path(__file__).with_name("fetch_releases.py"))
fetch_releases = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fetch_releases)

COMMIT = "94256510f74e6843754e0a93fd9e4da7e45b1056"
STABLE = "1" * 40
RELEASE = {"contract": 1, "channel": "nightly", "build": "nightly-2026-11-20-9425651", "commit": COMMIT}
SDK = {"schema": 1, "build": {"commit": COMMIT, "date": "2026-11-19"}}


def runner(release, sdk, missing=False):
    """subprocess.run for the Nightly lookup, its download and its signature check."""
    def run(command, **_):
        if command[:2] == ["gh", "api"]:
            return mock.Mock(returncode=int(missing), stderr="gh: Not Found (HTTP 404)" if missing else "")
        if command[:3] == ["gh", "release", "download"]:
            folder = Path(command[command.index("-D") + 1])
            (folder / "sunrise-release.json").write_text(json.dumps(release))
            (folder / "sdk.json").write_text(json.dumps(sdk))
        else:
            # Both files are covered by the signature check.
            assert [command[i + 1] for i, a in enumerate(command) if a == "--only"] == ["sdk.json", "sunrise-release.json"]
        return mock.Mock(returncode=0)
    return run


class Nightly(unittest.TestCase):
    def nightly(self, release=RELEASE, sdk=SDK, missing=False):
        with mock.patch.object(fetch_releases.subprocess, "run", runner(release, sdk, missing)):
            return fetch_releases.nightly()

    def test_the_date_is_the_signed_releases_not_the_commits(self):
        self.assertEqual(self.nightly(), {"commit": COMMIT, "date": "2026-11-20"})

    def test_files_about_different_builds_are_refused(self):
        for release, sdk in ((RELEASE, SDK | {"build": {"commit": STABLE, "date": "2026-11-19"}}),
                             (RELEASE | {"build": "nightly-2026-11-20-1111111"}, SDK),
                             (RELEASE | {"channel": "stable"}, SDK), (RELEASE | {"build": "26.3-0.1.0"}, SDK),
                             (RELEASE, SDK | {"schema": 2})):
            with self.subTest(release=release, sdk=sdk), self.assertRaises(SystemExit):
                self.nightly(release, sdk)

    def test_an_absent_nightly_leaves_only_the_nightly_out_when_optional(self):
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        stable = [[{"tag_name": "26.3-0.1.0", "published_at": "2026-11-20T00:00:00Z", "draft": False,
                    "prerelease": False}]]
        with mock.patch.object(fetch_releases, "api", return_value=stable), \
                mock.patch.object(fetch_releases, "commit_of", return_value=STABLE), \
                mock.patch.object(fetch_releases.subprocess, "run", runner(RELEASE, SDK, missing=True)):
            with self.assertRaises(SystemExit):
                fetch_releases.main(folder / "required", False)
            fetch_releases.main(folder, True)
        self.assertEqual(json.loads((folder / "releases.json").read_text()), [{"tag": "26.3-0.1.0", "commit": STABLE}])
        self.assertEqual((folder / "sdk-commits.txt").read_text(), STABLE + "\n")
        self.assertFalse((folder / "nightly-build.json").exists())


if __name__ == "__main__":
    unittest.main()
