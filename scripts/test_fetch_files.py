"""fetch_files.py against stubbed downloads and releases: no network, no token."""
import hashlib
import importlib.util
import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

spec = importlib.util.spec_from_file_location("fetch_files", Path(__file__).with_name("fetch_files.py"))
fetch_files = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fetch_files)

REGISTRY = "sunrise-org/sunrise-mods"
PACKAGE = b"a package"
DIGEST = hashlib.sha256(PACKAGE).hexdigest()
THEIRS = "https://github.com/you/example/releases/download/v0.1.0/example-0.1.0.smod"


class Web:
    """urlopen over a table of address to bytes; anything else is a 404."""

    def __init__(self, pages):
        self.pages, self.asked = pages, []

    def __call__(self, request, timeout):
        self.asked.append(request.full_url)
        if request.full_url not in self.pages:
            raise urllib.error.HTTPError(request.full_url, 404, "Not Found", None, None)
        return io.BytesIO(self.pages[request.full_url])


class FetchFiles(unittest.TestCase):
    def setUp(self):
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.files = self.folder / "files"
        self.files.mkdir()

    def listing(self, text):
        path = self.folder / "list"
        path.write_text(text)
        return str(path)

    def fetch(self, text, pages):
        web = Web(pages)
        with mock.patch.object(fetch_files.urllib.request, "urlopen", web):
            return fetch_files.fetch(REGISTRY, self.listing(text), self.files), web.asked

    def test_a_stable_package_comes_from_its_authors_release_until_the_registry_has_it(self):
        mirror = f"https://github.com/{REGISTRY}/releases/download/example-0.1.0/example-0.1.0.smod"
        errors, asked = self.fetch(f"{DIGEST} example 0.1.0 {THEIRS}\n", {THEIRS: PACKAGE})
        self.assertEqual((errors, asked), ([], [mirror, THEIRS]))
        self.assertEqual((self.files / DIGEST).read_bytes(), PACKAGE)
        # Once downloaded and still matching, nothing is asked again.
        self.assertEqual(self.fetch(f"{DIGEST} example 0.1.0 {THEIRS}\n", {}), ([], []))

    def test_an_older_row_still_comes_from_staging(self):
        staging = f"https://github.com/{REGISTRY}/releases/download/staging/{DIGEST}.smod"
        errors, asked = self.fetch(f"{DIGEST} example 0.1.0 example-0.1.0.smod\n", {staging: PACKAGE})
        self.assertEqual(errors, [])
        self.assertEqual(asked[-1], staging)

    def test_a_download_stops_at_its_limit_and_keeps_nothing(self):
        with mock.patch.object(fetch_files, "MAX_PACKAGE", 4):
            errors, _ = self.fetch(f"{DIGEST} example 0.1.0 {THEIRS}\n", {THEIRS: PACKAGE})
        self.assertIn("larger than the 4 bytes allowed", errors[0])
        archive = {"sha256": DIGEST, "id": "example", "version": "0.1.0", "sunrise": "26.3-0.1.1",
                   "url": THEIRS.replace(".smod", "+26.3-0.1.1-x86_64-unknown-linux-gnu.tar.xz"),
                   "name": "example-0.1.0+26.3-0.1.1-x86_64-unknown-linux-gnu.tar.xz", "size": len(PACKAGE) - 1}
        errors, _ = self.fetch(json.dumps([archive]), {archive["url"]: PACKAGE})
        self.assertIn(f"larger than the {len(PACKAGE) - 1} bytes allowed", errors[0])
        with mock.patch.object(fetch_files, "MAX_DESCRIPTOR", 4):
            errors, _ = self.fetch(json.dumps([archive | {"size": None}]), {archive["url"]: PACKAGE})
        self.assertIn("larger than the 4 bytes allowed", errors[0])
        self.assertEqual(list(self.files.iterdir()), [])

    def test_wrong_bytes_and_missing_assets_are_refused(self):
        errors, _ = self.fetch(f"{DIGEST} example 0.1.0 {THEIRS}\n", {THEIRS: b"other"})
        self.assertIn("isn't the one its sha256 names", errors[0])
        errors, _ = self.fetch(f"{DIGEST} example 0.1.0 {THEIRS}\n", {})
        self.assertIn("nor in you/example's release v0.1.0", errors[0])
        self.assertEqual(list(self.files.iterdir()), [])

    def test_release_tags_follow_the_tools_rule(self):
        # A build asset's tag may have `/`-separated plain parts; a package's is one plain name.
        nested = THEIRS.replace("/v0.1.0/", "/mods/example/v0.1.0/")
        row = {"sha256": DIGEST, "id": "example", "version": "0.1.0", "sunrise": "26.3-0.1.1", "url": nested,
               "name": "example-0.1.0.smod", "size": None}
        self.assertEqual(self.fetch(json.dumps([row]), {nested: PACKAGE})[0], [])
        (self.files / DIGEST).unlink()
        for bad in (nested, THEIRS.replace("/v0.1.0/", "/../v0.1.0/"), THEIRS.replace("/v0.1.0/", "/.hidden/"),
                    "https://example.test/example-0.1.0.smod", "../example-0.1.0.smod",
                    THEIRS.replace("example-0.1.0.smod", ".smod")):
            with self.subTest(file=bad):
                errors, asked = self.fetch(f"{DIGEST} example 0.1.0 {bad}\n", {bad: PACKAGE})
                self.assertIn("not a plain sha256", errors[0])
                self.assertEqual(asked, [])
        for bad in (THEIRS.replace("/v0.1.0/", "/a//b/"), THEIRS.replace("/v0.1.0/", "/a/../b/")):
            with self.subTest(build=bad):
                errors, asked = self.fetch(json.dumps([row | {"url": bad}]), {bad: PACKAGE})
                self.assertIn("not a plain sha256", errors[0])
                self.assertEqual(asked, [])

    def test_mirroring_adds_only_what_the_release_lacks_from_the_checked_files(self):
        (self.files / DIGEST).write_bytes(PACKAGE)
        other = hashlib.sha256(b"descriptor").hexdigest()
        (self.files / other).write_bytes(b"descriptor")
        missing = hashlib.sha256(b"never downloaded").hexdigest()
        text = (f"{DIGEST} example 0.1.0 {THEIRS}\n{other} listed 1.0.0 listed-1.0.0.smod\n"
                f"{missing} example 0.1.0 {THEIRS.replace('.smod', '.zip')}\n")
        published = []

        def publish(repo, tag, title, file, exists):
            published.append((tag, file.name, file.read_bytes(), exists))

        with mock.patch.object(fetch_files, "release_assets", side_effect=lambda repo, tag:
                               {"listed-1.0.0.smod": other} if tag == "listed-1.0.0" else None), \
                mock.patch.object(fetch_files, "publish", side_effect=publish):
            errors = fetch_files.mirror(REGISTRY, self.listing(text), self.files)
        self.assertEqual(published, [("example-0.1.0", "example-0.1.0.smod", PACKAGE, False)])
        self.assertEqual(len(errors), 1)
        self.assertIn("example-0.1.0.zip: not among the downloaded files", errors[0])

    def test_a_mirrored_asset_with_other_bytes_stops_publication(self):
        (self.files / DIGEST).write_bytes(PACKAGE)
        mirror = f"https://github.com/{REGISTRY}/releases/download/example-0.1.0/example-0.1.0.smod"
        text = f"{DIGEST} example 0.1.0 {THEIRS}\n"
        refusal = f"holds a different file than the entry's sha256 {DIGEST}"
        # GitHub's recorded digest decides; an asset without one is downloaded and hashed.
        for held, pages, wrong in ((DIGEST, {}, False), ("0" * 64, {}, True), (None, {mirror: PACKAGE}, False),
                                   (None, {mirror: b"other one"}, True), (None, {mirror: PACKAGE + b"+"}, True),
                                   (None, {}, True)):
            with self.subTest(held=held, pages=pages), \
                    mock.patch.object(fetch_files, "release_assets", return_value={"example-0.1.0.smod": held}), \
                    mock.patch.object(fetch_files, "publish", side_effect=AssertionError("uploaded")), \
                    mock.patch.object(fetch_files.urllib.request, "urlopen", Web(pages)):
                errors = fetch_files.mirror(REGISTRY, self.listing(text), self.files)
                self.assertEqual(len(errors), int(wrong))
                if wrong:
                    self.assertIn(refusal, errors[0])
        self.assertEqual([f.name for f in self.files.iterdir()], [DIGEST])

    def test_release_assets_reads_githubs_digests(self):
        answer = json.dumps({"assets": [{"name": "a.smod", "digest": "sha256:" + DIGEST},
                                        {"name": "old.smod", "digest": None}]})
        with mock.patch.object(fetch_files, "gh", return_value=mock.Mock(returncode=0, stdout=answer)):
            self.assertEqual(fetch_files.release_assets(REGISTRY, "a-1"), {"a.smod": DIGEST, "old.smod": None})
        with mock.patch.object(fetch_files, "gh", return_value=mock.Mock(returncode=1, stdout="")):
            self.assertIsNone(fetch_files.release_assets(REGISTRY, "a-1"))


if __name__ == "__main__":
    unittest.main()
