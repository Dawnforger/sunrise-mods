"""Tests for check_native_rows.py: python3 -m unittest discover -s scripts"""
import tempfile
import unittest
from pathlib import Path

import check_native_rows as cn

ROW = '[[versions]]\nversion = "{}"\nfile = "trail_atlas-{}.smod"\n{}'


def entry(kind: str, *versions: tuple[str, str]) -> str:
    return f'id = "trail_atlas"\nkind = "{kind}"\n' + "".join(ROW.format(v, v, extra) for v, extra in versions)


class NativeRows(unittest.TestCase):
    def errors(self, base: str | None, head: str) -> list[str]:
        with tempfile.TemporaryDirectory() as directory:
            for side, text in (("base", base), ("head", head)):
                mods = Path(directory, side, "mods")
                mods.mkdir(parents=True)
                if text is not None:
                    (mods / "trail_atlas.toml").write_text(text)
            return cn.check(Path(directory, "base"), Path(directory, "head"))

    def test_a_new_native_version_without_a_stable_release_is_sent_to_sources(self):
        old = entry("native-mod", ("0.1.0", ""))
        [error] = self.errors(old, entry("native-mod", ("0.2.0", ""), ("0.1.0", "")))
        self.assertIn("mods/trail_atlas.toml 0.2.0", error)
        self.assertIn("sources/trail_atlas.toml", error)
        [error] = self.errors(None, entry("native-mod", ("0.1.0", "")))
        self.assertIn("mods/trail_atlas.toml 0.1.0", error)

    def test_a_changed_published_row_is_refused_and_an_unchanged_one_is_kept(self):
        old = entry("native-mod", ("0.1.0", ""))
        self.assertEqual(self.errors(old, old), [])
        self.assertEqual(len(self.errors(old, entry("native-mod", ("0.1.0", 'changelog = "x"\n')))), 1)
        # One published row doesn't cover two.
        self.assertEqual(len(self.errors(old, entry("native-mod", ("0.1.0", ""), ("0.1.0", "")))), 1)

    def test_a_stable_native_version_and_other_kinds_pass(self):
        stable = entry("native-mod", ("0.2.0", 'sunrise = "26.3-0.1.0"\n'), ("0.1.0", ""))
        self.assertEqual(self.errors(entry("native-mod", ("0.1.0", "")), stable), [])
        self.assertEqual(self.errors(None, entry("mod", ("0.1.0", ""))), [])

    def test_an_entry_that_does_not_parse_is_left_to_the_registry_tool(self):
        self.assertEqual(self.errors(None, 'kind = "native-mod"\n[[versions'), [])


if __name__ == "__main__":
    unittest.main()
