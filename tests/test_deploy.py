"""End-to-end tests for one-copy-per-file deployment, junctions, profiles, adopt, and remove-all (§4)."""

from __future__ import annotations

import os
import tempfile
import unittest
import zipfile
from pathlib import Path

from tests.test_game import _make_fake_exe
from tests.test_pakindex import build_synthetic_v3_pak, build_synthetic_v11_pak
from wmm.deploy import DeployError, Manager
from wmm.engineini import WMM_BP_SECTION, read_section_entries
from wmm.game import validate_game_root
from wmm.platform import get_platform
from wmm.store import Profile, Store
from wmm.ue4ss import parse_mods_txt


class _RunningGamePlatformWrapper:
    """Wraps real platform but pretends the game is running."""

    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def is_process_running(self, exe_names):
        return ["Project_Plague-Win64-Shipping.exe"]


class TestDeploy(unittest.TestCase):
    def _make_env(self, td: str):
        root = Path(td)
        game_root = root / "Game"
        _make_fake_exe(game_root / "Project_Plague" / "Binaries" / "Win64" / "Project_Plague-Win64-Shipping.exe")
        (game_root / "Project_Plague" / "Content" / "Paks" / "~mods").mkdir(parents=True)
        (game_root / "Project_Plague" / "Binaries" / "Win64" / "ue4ss" / "Mods").mkdir(parents=True)
        cfg_dir = root / "Config"
        cfg_dir.mkdir()
        (cfg_dir / "Engine.ini").write_text("[Core.System]\nPaths=Base\n", encoding="utf-8")

        game = validate_game_root(game_root)
        assert game is not None
        game.config_dir = cfg_dir
        store = Store(root / "WMM_data")
        mgr = Manager(store, game)
        return root, game, store, mgr

    def test_one_copy_pak_and_ue4ss_junction_and_remove_all(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root, game, store, mgr = self._make_env(td)

            # Place an unmanaged file in ~mods to verify remove-all never deletes it
            unmanaged = game.mods_pak_dir / "Unmanaged_P.pak"
            build_synthetic_v11_pak(unmanaged, "../../../", {"Project_Plague/Content/": ["U.uasset"]})

            # Create a source mod folder with a BP pak + UE4SS mod
            src_mod = root / "SrcMod"
            src_mod.mkdir()
            build_synthetic_v3_pak(
                src_mod / "SkipIntroMessages.pak",
                "../../../",
                ["Project_Plague/Content/Mods/SkipIntroMessages/ModActor.uasset"],
            )
            lua_main = src_mod / "MyLuaMod" / "Scripts" / "main.lua"
            lua_main.parent.mkdir(parents=True)
            lua_main.write_text("print('ok')\n", encoding="utf-8")
            (src_mod / "MyLuaMod" / "config.lua").write_text("return { Val = 1 }\n", encoding="utf-8")

            rec = mgr.add_mod(src_mod, name="ComboMod")
            mgr.set_enabled(rec.id, True)

            # 1. Pak deployed via hardlink on same volume (same inode = zero duplicate bytes!)
            deployed_pak = game.mods_pak_dir / "SkipIntroMessages.pak"
            store_pak = store.mod_dir(rec.id) / "paks" / "SkipIntroMessages.pak"
            self.assertTrue(deployed_pak.is_file())
            self.assertEqual(os.stat(deployed_pak).st_ino, os.stat(store_pak).st_ino)

            # 2. UE4SS mod deployed via junction/symlink; writes inside it persist in store after disable!
            deployed_lua_dir = game.ue4ss_mods_dir / "MyLuaMod"
            self.assertTrue(get_platform().is_dir_link(deployed_lua_dir))
            (deployed_lua_dir / "crumb.txt").write_text("runtime write", encoding="utf-8")

            # 3. mods.txt is LF-only and contains MyLuaMod : 1 and Keybinds : 1 last
            self.assertNotIn(b"\r\n", game.ue4ss_mods_txt.read_bytes())
            self.assertIn("MyLuaMod : 1", game.ue4ss_mods_txt.read_text(encoding="utf-8"))

            # 4. Engine.ini has SkipIntroMessages in WMM BP section
            self.assertEqual(read_section_entries(game.engine_ini_path, WMM_BP_SECTION), ["SkipIntroMessages"])

            # 5. Disable: junction removed, runtime write still in store!
            mgr.set_enabled(rec.id, False)
            self.assertFalse(deployed_pak.exists())
            self.assertFalse(deployed_lua_dir.exists())
            self.assertTrue((store.mod_dir(rec.id) / "ue4ss" / "MyLuaMod" / "crumb.txt").is_file())

            # 6. Re-enable and then remove-all: unmanaged file stays untouched
            mgr.set_enabled(rec.id, True)
            removed = mgr.remove_all()
            self.assertGreaterEqual(removed, 2)
            self.assertTrue(unmanaged.is_file())
            self.assertFalse(deployed_pak.exists())
            self.assertFalse(deployed_lua_dir.exists())

    def test_adopt_unmanaged_pak_and_ue4ss(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            _root, game, store, mgr = self._make_env(td)

            raw_pak = game.mods_pak_dir / "Existing_P.pak"
            build_synthetic_v11_pak(raw_pak, "../../../", {"Project_Plague/Content/": ["X.uasset"]})
            rec1 = mgr.adopt_pak(raw_pak)
            self.assertTrue(rec1.adopted)
            self.assertTrue(raw_pak.is_file())

            raw_lua = game.ue4ss_mods_dir / "ExistingLua"
            (raw_lua / "Scripts").mkdir(parents=True)
            (raw_lua / "Scripts" / "main.lua").write_text("-- hi\n", encoding="utf-8")
            rec2 = mgr.adopt_ue4ss_mod(raw_lua)
            self.assertTrue(rec2.adopted)
            self.assertTrue(get_platform().is_dir_link(raw_lua))
            self.assertTrue((store.mod_dir(rec2.id) / "ue4ss" / "ExistingLua" / "Scripts" / "main.lua").is_file())

    def test_refuses_changes_while_game_running(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root, game, store, _mgr = self._make_env(td)
            mgr = Manager(store, game, platform=_RunningGamePlatformWrapper(get_platform()))
            with self.assertRaises(DeployError):
                mgr.remove_all()

    def test_refuses_unconfirmed_loader_dll(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root, _game, _store, mgr = self._make_env(td)
            ldr = root / "LoaderMod"
            ldr.mkdir()
            (ldr / "dsound.dll").write_bytes(b"MZ")
            with self.assertRaises(DeployError):
                mgr.add_mod(ldr)

    def test_profile_switch_removes_inactive_ue4ss_from_mods_txt(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root, game, store, mgr = self._make_env(td)

            # Seed an unmanaged UE4SS entry in mods.txt to ensure it is preserved
            game.ue4ss_mods_txt.parent.mkdir(parents=True, exist_ok=True)
            game.ue4ss_mods_txt.write_bytes(b"UnmanagedLua : 1\nKeybinds : 1\n")

            # Add a managed UE4SS mod on the default profile
            src = root / "MyManagedLua.zip"
            with zipfile.ZipFile(src, "w") as zf:
                zf.writestr("MyManagedLua/Scripts/main.lua", "-- test\n")
            rec = mgr.add_mod(src)
            mgr.set_enabled(rec.id, True)
            self.assertTrue((game.ue4ss_mods_dir / "MyManagedLua").exists())
            names_default = [n for n, _ in parse_mods_txt(game.ue4ss_mods_txt)]
            self.assertIn("MyManagedLua", names_default)
            self.assertIn("UnmanagedLua", names_default)

            # Switch to a clean profile with no mods enabled
            test_prof = Profile(name="test")
            store.save_profile(test_prof)
            mgr.apply_profile(test_prof)

            self.assertFalse((game.ue4ss_mods_dir / "MyManagedLua").exists())
            names_test = [n for n, _ in parse_mods_txt(game.ue4ss_mods_txt)]
            self.assertNotIn("MyManagedLua", names_test)
            self.assertIn("UnmanagedLua", names_test)
            self.assertEqual(names_test[-1], "Keybinds")


if __name__ == "__main__":
    unittest.main()


class StoreLayoutTests(unittest.TestCase):
    def test_no_data_downloads_and_tmp_on_demand(self) -> None:
        import tempfile
        from pathlib import Path

        from wmm.store import Store

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = Store(root / "data")
            self.assertFalse((root / "data" / "downloads").exists())
            self.assertEqual(store.downloads_dir, root / "downloads")
            self.assertFalse(store.tmp_dir.exists())
            t = store.new_tmp_dir()
            self.assertTrue(t.is_dir() and t.parent == store.tmp_dir)


class AppLogTests(unittest.TestCase):
    def test_entries_timestamped_and_persisted(self) -> None:
        import tempfile
        from pathlib import Path

        from wmm.applog import AppLog

        with tempfile.TemporaryDirectory() as td:
            log = AppLog(Path(td) / "logs" / "wmm.log")
            log.add("hello")
            log.add("boom", "error")
            self.assertEqual([e.level for e in log.entries], ["INFO", "ERROR"])
            self.assertRegex(log.entries[0].line(), r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d  INFO   hello$")
            self.assertEqual(len((Path(td) / "logs" / "wmm.log").read_text(encoding="utf-8").splitlines()), 2)
