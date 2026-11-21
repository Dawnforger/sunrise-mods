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


NAMES = ("sdk.json", "sunrise-release.json", "SHA256SUMS", "SHA256SUMS.v2.sig")


def runner(release, sdk, missing=False, title="Nightly 2026-11-20 · 9425651", rotated=False):
    """subprocess.run for the tool's stable list, the Nightly's one release record, its assets by id and the
    signature check."""
    files = {"sdk.json": json.dumps(sdk).encode(), "sunrise-release.json": json.dumps(release).encode(),
             "SHA256SUMS": b"sums", "SHA256SUMS.v2.sig": b"signature"}
    lookups = []

    def run(command, **_):
        if command[0] == "cargo-sunrise-mod":
            assert command[1:4] == ["sunrise-mod", "registry", "releases"]
            Path(command[command.index("--out") + 1]).write_text(json.dumps([{"tag": "26.3-0.1.0", "commit": STABLE}]))
        elif command[:2] == ["gh", "api"] and command[-1].endswith("/releases/tags/nightly"):
            lookups.append(command)
            assert len(lookups) == 1, "the moving tag is looked up once"
            assets = [{"name": name, "id": 100 + i, "size": len(files[name]) + int(rotated)}
                      for i, name in enumerate(NAMES)]
            return mock.Mock(returncode=int(missing), stderr="gh: Not Found (HTTP 404)" if missing else "",
                             stdout=json.dumps({"id": 7, "name": title, "assets": assets}))
        elif command[:2] == ["gh", "api"]:
            # An asset is fetched by the id its release record gave, never through the tag again.
            number = int(command[-1].removeprefix(f"repos/{fetch_releases.REPO}/releases/assets/"))
            return mock.Mock(returncode=0, stdout=files[NAMES[number - 100]])
        else:
            assert command[0] == "sunrise-release-sign"
            # Both files are covered by the signature for the build the release's title names.
            value = lambda flag: [command[i + 1] for i, a in enumerate(command) if a == flag]
            assert value("--only") == ["sdk.json", "sunrise-release.json"]
            assert value("--signature") == ["SHA256SUMS.v2.sig"]
            assert sorted(f.name for f in Path(command[-1]).iterdir()) == sorted(NAMES)
            if value("--tag") != ["nightly-2026-11-20-9425651"]:
                raise fetch_releases.subprocess.CalledProcessError(1, command)
        return mock.Mock(returncode=0)
    return run


class Nightly(unittest.TestCase):
    def nightly(self, release=RELEASE, sdk=SDK, missing=False, **more):
        with mock.patch.object(fetch_releases.subprocess, "run", runner(release, sdk, missing, **more)):
            return fetch_releases.nightly()

    def test_an_older_nightlys_files_do_not_pass_as_the_current_one(self):
        # The title names another build than the files are signed for; then files about another build than it names.
        with self.assertRaises(fetch_releases.subprocess.CalledProcessError):
            self.nightly(title="Nightly 2026-11-21 · 1111111")
        for title in ("Nightly", "Snapshot 2026-11-20 · 9425651", "Nightly 2026-11-20 - 9425651", None):
            with self.subTest(title=title), self.assertRaises(SystemExit):
                self.nightly(title=title)
        with self.assertRaises(SystemExit):
            self.nightly(RELEASE | {"build": "nightly-2026-11-19-9425651"})

    def test_the_date_is_the_signed_releases_not_the_commits(self):
        self.assertEqual(self.nightly(), {"commit": COMMIT, "date": "2026-11-20"})

    def test_files_about_different_builds_are_refused(self):
        for release, sdk in ((RELEASE, SDK | {"build": {"commit": STABLE, "date": "2026-11-19"}}),
                             (RELEASE | {"build": "nightly-2026-11-20-1111111"}, SDK),
                             (RELEASE | {"channel": "stable"}, SDK), (RELEASE | {"build": "26.3-0.1.0"}, SDK),
                             (RELEASE, SDK | {"schema": 2})):
            with self.subTest(release=release, sdk=sdk), self.assertRaises(SystemExit):
                self.nightly(release, sdk)

    def test_an_asset_that_is_not_the_listed_one_is_refused(self):
        with self.assertRaises(SystemExit):
            self.nightly(rotated=True)

    def test_an_absent_nightly_leaves_only_the_nightly_out_when_optional(self):
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        with mock.patch.object(fetch_releases.subprocess, "run", runner(RELEASE, SDK, missing=True)):
            with self.assertRaises(SystemExit):
                fetch_releases.main(folder / "required", False)
        with mock.patch.object(fetch_releases.subprocess, "run", runner(RELEASE, SDK, missing=True)):
            fetch_releases.main(folder, True)
        self.assertEqual(json.loads((folder / "releases.json").read_text()), [{"tag": "26.3-0.1.0", "commit": STABLE}])
        self.assertEqual((folder / "sdk-commits.txt").read_text(), STABLE + "\n")
        self.assertFalse((folder / "nightly-build.json").exists())

    def test_the_inputs_name_the_stable_and_nightly_commits(self):
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        with mock.patch.object(fetch_releases.subprocess, "run", runner(RELEASE, SDK)):
            fetch_releases.main(folder, False)
        self.assertEqual(json.loads((folder / "nightly-build.json").read_text()),
                         {"commit": COMMIT, "date": "2026-11-20"})
        self.assertEqual((folder / "sdk-commits.txt").read_text(), f"{STABLE}\n{COMMIT}\n")

if __name__ == "__main__":
    unittest.main()
