"""Headless PySide6 UI smoke tests (runs offscreen via QT_QPA_PLATFORM=offscreen)."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication
    HAS_PYSIDE6 = True
except ImportError:
    HAS_PYSIDE6 = False

from tests.test_game import _make_fake_exe
from tests.test_pakindex import build_synthetic_v11_pak
from wmm.i18n import Translator
from wmm.store import Store


@unittest.skipUnless(HAS_PYSIDE6, "PySide6 not installed")
class TestUI(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_main_window_and_dialogs_render(self) -> None:
        from wmm.ui.main_window import MainWindow
        from wmm.ui.onboarding import OnboardingDialog
        from wmm.ui.settings_dialog import SettingsDialog
        from wmm.ui.theme import apply_dark_theme

        apply_dark_theme(self.app)

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            game_root = root / "Game"
            _make_fake_exe(game_root / "Project_Plague" / "Binaries" / "Win64" / "Project_Plague-Win64-Shipping.exe")
            (game_root / "Project_Plague" / "Content" / "Paks" / "~mods").mkdir(parents=True)

            # Seed one unmanaged pak directly in ~mods + one managed mod
            unmanaged_pak = game_root / "Project_Plague" / "Content" / "Paks" / "~mods" / "PreExisting_P.pak"
            build_synthetic_v11_pak(unmanaged_pak, "../../../", {"Project_Plague/Content/": ["B.uasset"]})

            win = MainWindow(data_dir=root / "data", game_override=game_root, run_onboarding_if_needed=False)
            src_pak = root / "Sample_P.pak"
            build_synthetic_v11_pak(src_pak, "../../../", {"Project_Plague/Content/": ["A.uasset"]})
            win._install_path(src_pak)

            self.assertEqual(win.table.rowCount(), 2)
            self.assertEqual(win.table.item(0, 1).text(), "Sample_P")
            self.assertEqual(win.table.item(0, 2).text(), "PAK")
            self.assertEqual(win.table.item(1, 1).text(), "PreExisting_P.pak")
            self.assertIn("Unmanaged", win.table.item(1, 5).text())
            # MO2-style bottom tabs (UE4SS, Paks, Downloads) populated
            self.assertEqual(win.right_tabs.count(), 3)
            self.assertEqual(win.tab_paks.rowCount(), 1)
            self.assertEqual(win.tab_paks.item(0, 2).text(), "PreExisting_P.pak")
            self.assertTrue(win.table.item(1, 1).font().bold())
            self.assertTrue(win.table.item(1, 1).font().italic())

            # Smoke-construct Onboarding & Settings dialogs
            pkg_root = Path(__file__).resolve().parent.parent
            t = Translator(pkg_root / "lang", language="en")
            ob = OnboardingDialog(win.store, t, game=win.game, package_root=pkg_root)
            self.assertIn("✔", ob.badge_game.text())
            self.assertTrue(ob.row_mods.isEnabled())
            self.assertTrue(ob.row_ini.isEnabled())
            self.assertTrue(ob.row_setup.isEnabled())
            # Finish is greyed out until all 4 steps (incl. Engine.ini & Modding setup) are validated
            self.assertFalse(ob.btn_finish.isEnabled())

            # Switching to Paks-only shows the UE4SS warning in bottom status
            paks_idx = ob.combo_modding.findData("paks_only")
            self.assertGreaterEqual(paks_idx, 0)
            ob.combo_modding.setCurrentIndex(paks_idx)
            self.assertIn("UE4SS", ob.lbl_status.text())

            # Applying Skip validates Modding setup while keeping the warning in bottom status
            cfg_dir = root / "Cfg"
            cfg_dir.mkdir()
            (cfg_dir / "Engine.ini").write_text("[Core.System]\n", encoding="utf-8")
            win.game.config_dir = cfg_dir
            ob.mods_archive_dir = root / "ModsDir"
            ob.mods_archive_dir.mkdir()
            ob._skip_applied = True
            skip_idx = ob.combo_modding.findData("skip")
            ob.combo_modding.setCurrentIndex(skip_idx)
            ob._refresh_state()
            self.assertTrue(ob.btn_finish.isEnabled())
            self.assertIn("know what you are doing", ob.lbl_status.text())

            # When game is invalid/None, dependent rows and Finish must be greyed out
            ob.game = None
            ob._set_dependent_rows_enabled(False)
            self.assertFalse(ob.row_mods.isEnabled())
            self.assertFalse(ob.row_ini.isEnabled())
            self.assertFalse(ob.row_setup.isEnabled())
            self.assertFalse(ob.btn_finish.isEnabled())

            from wmm.ui.details_dialog import ModDetailsDialog
            from wmm.ui.profiles_dialog import ProfilesDialog

            sd = SettingsDialog(win.store, t, game=win.game, package_root=pkg_root)
            self.assertGreaterEqual(sd.combo_lang.count(), 1)
            self.assertEqual(sd.edit_game.cursorPosition(), 0)

            pd = ProfilesDialog(win.store, t)
            self.assertGreaterEqual(pd.list_profiles.count(), 1)
            pd._on_create()
            pd._commit_pending("boss_rush")
            self.assertIn("boss_rush", win.store.list_profiles())
            win.store.clone_profile("boss_rush", "boss_rush_copy")
            self.assertIn("boss_rush_copy", win.store.list_profiles())
            self.assertTrue(win.store.delete_profile("boss_rush_copy"))
            self.assertNotIn("boss_rush_copy", win.store.list_profiles())

            first_mod = win.store.list_mods()[0]
            dd = ModDetailsDialog(first_mod, win.store.load_manifest(), win.conflict_report, t)
            self.assertIn("No asset overrides", dd.overrides_view.toPlainText())

            dd.close()
            pd.close()
            ob.close()
            sd.close()
            win.close()


if __name__ == "__main__":
    unittest.main()
