from pathlib import Path
import unittest

from scan import module_in_release


class ModuleFileTest(unittest.TestCase):
    files = {Path(name) for name in ("src/lib.rs", "src/terrain.rs", "src/pieces/mod.rs", "src/pieces/rooms.rs",
                                     "src/terrain/tiles.rs", "tools/entry.rs", "tools/helper.rs")}

    def present(self, declaring, text, targets=()):
        return module_in_release(self.files, {Path(target) for target in targets}, Path(declaring), text.splitlines())

    def test_crate_root_and_mod_rs_look_beside_themselves(self):
        self.assertTrue(self.present("src/lib.rs", "pub mod terrain;"))
        self.assertTrue(self.present("src/lib.rs", "mod r#pieces;"))
        self.assertTrue(self.present("src/pieces/mod.rs", "mod rooms;"))
        self.assertTrue(self.present("tools/entry.rs", "mod helper;", targets=["tools/entry.rs"]))

    def test_other_files_look_in_their_own_folder(self):
        self.assertTrue(self.present("src/terrain.rs", "mod tiles;"))
        self.assertFalse(self.present("src/terrain.rs", "mod pieces;"))
        self.assertFalse(self.present("tools/entry.rs", "mod helper;"))

    def test_missing_or_unreadable_declarations_stay_flagged(self):
        self.assertFalse(self.present("src/lib.rs", "mod generated;"))
        self.assertFalse(self.present("src/lib.rs", "mod /* note */ terrain;"))
        self.assertFalse(self.present("src/lib.rs", "mod terrain; mod pieces;"))
