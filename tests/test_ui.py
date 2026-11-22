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
            self.assertEqual(win.tab_paks.item(0, 1).text(), "PreExisting_P.pak")
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


@unittest.skipUnless(HAS_PYSIDE6, "PySide6 not installed")
class TestOrdering(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_left_order_drives_paks_but_not_vice_versa(self) -> None:
        from wmm.ui.main_window import MainWindow

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            game_root = root / "Game"
            _make_fake_exe(game_root / "Project_Plague" / "Binaries" / "Win64" / "Project_Plague-Win64-Shipping.exe")
            (game_root / "Project_Plague" / "Content" / "Paks" / "~mods").mkdir(parents=True)
            win = MainWindow(data_dir=root / "data", game_override=game_root, run_onboarding_if_needed=False)
            for name in ("Aaa_P.pak", "Bbb_P.pak", "Ccc_P.pak"):
                src = root / name
                build_synthetic_v11_pak(src, "../../../", {"Project_Plague/Content/": [name + ".uasset"]})
                win._install_path(src)

            prof = win.store.load_profile()
            prof.enabled_mods = [win.table.item(r, 0).data(0x0100) for r in range(3)]
            win.store.save_profile(prof)
            win._manager().apply_profile(prof)
            win.refresh_all()

            self.assertEqual([win.table.item(r, 4).text() for r in range(3)], ["0", "1", "2"])
            names = [win.table.item(r, 1).text() for r in range(3)]
            self.assertEqual(names, ["Aaa_P", "Bbb_P", "Ccc_P"])

            # Left panel: move the last mod to the top -> right panel follows
            win._move_mod_to(win.table.item(2, 0).data(0x0100), 0)
            self.assertEqual([win.table.item(r, 1).text() for r in range(3)], ["Ccc_P", "Aaa_P", "Bbb_P"])
            right = [win.tab_paks.item(r, 1).text().split("_")[0] for r in range(3)]
            self.assertEqual(right, ["Ccc", "Aaa", "Bbb"])
            self.assertEqual([win.tab_paks.item(r, 2).text() for r in range(3)], ["0", "1", "2"])

            # Original names are shown even though the deployed files are renamed for load order
            deployed = {p.name for p in (game_root / "Project_Plague" / "Content" / "Paks" / "~mods").glob("*.pak")}
            shown = [win.tab_paks.item(r, 1).text() for r in range(3)]
            self.assertEqual(shown, ["Ccc_P.pak", "Aaa_P.pak", "Bbb_P.pak"])
            self.assertNotEqual(deployed, set(shown))

            # Right panel: move row 0 to row 2 -> left panel order unchanged
            win._on_pak_row_moved(0, 2)
            right = [win.tab_paks.item(r, 1).text().split("_")[0] for r in range(3)]
            self.assertEqual(right, ["Aaa", "Bbb", "Ccc"])
            self.assertEqual([win.table.item(r, 1).text() for r in range(3)], ["Ccc_P", "Aaa_P", "Bbb_P"])


@unittest.skipUnless(HAS_PYSIDE6, "PySide6 not installed")
class TestSortFilter(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_sort_and_filter(self) -> None:
        from wmm.ui.main_window import MainWindow

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            game_root = root / "Game"
            _make_fake_exe(game_root / "Project_Plague" / "Binaries" / "Win64" / "Project_Plague-Win64-Shipping.exe")
            (game_root / "Project_Plague" / "Content" / "Paks" / "~mods").mkdir(parents=True)
            win = MainWindow(data_dir=root / "data", game_override=game_root, run_onboarding_if_needed=False)
            for name in ("Aaa_P.pak", "Bbb_P.pak", "Ccc_P.pak"):
                src = root / name
                build_synthetic_v11_pak(src, "../../../", {"Project_Plague/Content/": [name + ".uasset"]})
                win._install_path(src)
            names = lambda: [win.table.item(r, 1).text() for r in range(3)]
            self.assertEqual(names(), ["Aaa_P", "Bbb_P", "Ccc_P"])
            win._on_header_clicked(1)
            self.assertEqual(names(), ["Aaa_P", "Bbb_P", "Ccc_P"])
            win._on_header_clicked(1)
            self.assertEqual(names(), ["Ccc_P", "Bbb_P", "Aaa_P"])
            self.assertEqual(win.table.item(0, 4).text(), "2")  # true order is kept
            win._on_header_clicked(1)
            self.assertIsNone(win._sort_col)
            mid = win.table.item(0, 0).data(0x0100)
            win._update_cache[mid] = {"v": "99.0", "f": 999}
            win.refresh_all()
            self.assertFalse(win.table.item(0, 3).icon().isNull())
            self.assertIn("99.0", win.table.item(0, 3).toolTip())
            self.assertTrue(win.table.item(1, 3).icon().isNull())
            win.edit_filter.setText("bbb")
            self.assertEqual([win.table.isRowHidden(r) for r in range(3)], [True, False, True])
            win.edit_filter.setText("")
            self.assertFalse(any(win.table.isRowHidden(r) for r in range(3)))


@unittest.skipUnless(HAS_PYSIDE6, "PySide6 not installed")
class TestDisable(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_checkbox_click_and_bulk_disable(self) -> None:
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest
        from wmm.ui.main_window import MainWindow

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            game_root = root / "Game"
            _make_fake_exe(game_root / "Project_Plague" / "Binaries" / "Win64" / "Project_Plague-Win64-Shipping.exe")
            (game_root / "Project_Plague" / "Content" / "Paks" / "~mods").mkdir(parents=True)
            win = MainWindow(data_dir=root / "data", game_override=game_root, run_onboarding_if_needed=False)
            for name in ("Aaa_P.pak", "Bbb_P.pak", "Ccc_P.pak"):
                src = root / name
                build_synthetic_v11_pak(src, "../../../", {"Project_Plague/Content/": [name + ".uasset"]})
                win._install_path(src)
            ids = [win.table.item(r, 0).data(0x0100) for r in range(3)]
            prof = win.store.load_profile()
            prof.enabled_mods = list(ids)
            win.store.save_profile(prof)
            win._manager().apply_profile(prof)
            win.resize(900, 400)
            win.show()
            win.refresh_all()

            # A real click on the centred checkbox disables the mod
            rect = win.table.visualItemRect(win.table.item(0, 0))
            QTest.mouseClick(win.table.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, rect.center())
            self.assertNotIn(ids[0], win.store.load_profile().enabled_mods)
            self.assertEqual(len(win.store.load_profile().enabled_mods), 2)

            # Multi-select (extended) + bulk disable, then disable all
            win.table.selectRow(1)
            win.table.selectionModel().select(win.table.model().index(2, 1), win.table.selectionModel().SelectionFlag.Select | win.table.selectionModel().SelectionFlag.Rows)
            self.assertEqual(len(win._selected_managed_ids()), 2)
            win._disable_mods(win._selected_managed_ids())
            self.assertEqual(win.store.load_profile().enabled_mods, [])
            self.assertEqual(win.tab_paks.rowCount(), 0)
            win._enable_mods(ids)
            self.assertEqual(set(win.store.load_profile().enabled_mods), set(ids))
            self.assertEqual(win.tab_paks.rowCount(), 3)


@unittest.skipUnless(HAS_PYSIDE6, "PySide6 not installed")
class TestRenameAndNames(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_manual_download_name_and_rename(self) -> None:
        import zipfile
        from wmm.ui.details_dialog import ModDetailsDialog
        from wmm.ui.main_window import MainWindow

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            game_root = root / "Game"
            _make_fake_exe(game_root / "Project_Plague" / "Binaries" / "Win64" / "Project_Plague-Win64-Shipping.exe")
            (game_root / "Project_Plague" / "Content" / "Paks" / "~mods").mkdir(parents=True)
            win = MainWindow(data_dir=root / "data", game_override=game_root, run_onboarding_if_needed=False)
            pak = root / "Thing_P.pak"
            build_synthetic_v11_pak(pak, "../../../", {"Project_Plague/Content/": ["T.uasset"]})
            arc = root / "UncensorMod 411 4 2026-10-11T21-53Z AMLpw5goK.zip"
            with zipfile.ZipFile(arc, "w") as z:
                z.write(pak, "Thing_P.pak")
            win._install_path(arc)
            mod = win.store.list_mods()[0]
            self.assertEqual((mod.name, mod.version, mod.nexus_mod_id), ("UncensorMod", "4", 411))

            dlg = ModDetailsDialog(mod, [], None, win.t, on_rename=lambda n: win._rename_mod(mod.id, n))
            dlg.edit_name.setText("Uncensor (mine)")
            self.assertTrue(dlg.btn_rename.isEnabled())
            dlg.btn_rename.click()
            self.assertEqual(win.store.load_mod(mod.id).name, "Uncensor (mine)")
            self.assertEqual(win.table.item(0, 1).text(), "Uncensor (mine)")


@unittest.skipUnless(HAS_PYSIDE6, "PySide6 not installed")
class TestRightPanelActions(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_space_double_click_bulk_and_downloads(self) -> None:
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest
        from wmm.ui.main_window import MainWindow

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            game_root = root / "Game"
            _make_fake_exe(game_root / "Project_Plague" / "Binaries" / "Win64" / "Project_Plague-Win64-Shipping.exe")
            (game_root / "Project_Plague" / "Content" / "Paks" / "~mods").mkdir(parents=True)
            win = MainWindow(data_dir=root / "data", game_override=game_root, run_onboarding_if_needed=False)
            for name in ("Aaa_P.pak", "Bbb_P.pak"):
                src = root / name
                build_synthetic_v11_pak(src, "../../../", {"Project_Plague/Content/": [name + ".uasset"]})
                win._install_path(src)
            ids = [win.table.item(r, 0).data(0x0100) for r in range(2)]
            prof = win.store.load_profile()
            prof.enabled_mods = list(ids)
            win.store.save_profile(prof)
            win._manager().apply_profile(prof)
            win.show()
            win.refresh_all()

            # Space on the left list toggles the selected mod
            win.table.selectRow(0)
            win.table.setFocus()
            QTest.keyClick(win.table, Qt.Key.Key_Space)
            self.assertEqual(win.store.load_profile().enabled_mods, [ids[1]])
            QTest.keyClick(win.table, Qt.Key.Key_Space)
            self.assertIn(ids[0], win.store.load_profile().enabled_mods)

            # Double-click opens details (not on the checkbox column)
            opened: list[str] = []
            win._show_mod_details = lambda mid: opened.append(mid)  # type: ignore[method-assign]
            win._on_mod_row_activated(0, 1)
            win._on_mod_row_activated(0, 0)
            self.assertEqual(len(opened), 1)

            # Paks tab: space toggles the selected pak, bulk disable/enable
            win.right_tabs.setCurrentWidget(win.tab_paks)
            win.tab_paks.selectRow(0)
            win.tab_paks.setFocus()
            QTest.keyClick(win.tab_paks, Qt.Key.Key_Space)
            self.assertEqual([c for _, c in win._pak_items()].count(False), 1)  # toggled pak is now off
            win._set_paks_enabled([k for k, _ in win._pak_items()], True)
            self.assertTrue(all(c for _, c in win._pak_items()))
            win._set_paks_enabled([k for k, _ in win._pak_items()], False)
            self.assertFalse(any(c for _, c in win._pak_items()))

            # Downloads: delete several / all
            dl = win.package_root / "downloads"
            dl.mkdir(exist_ok=True)
            made = []
            for n in ("a.zip", "b.zip", "c.zip"):
                (dl / n).write_bytes(b"PK")
                made.append(dl / n)
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)  # type: ignore[method-assign]
            win._delete_downloads(made[:2])
            self.assertFalse(made[0].exists() or made[1].exists())
            self.assertTrue(made[2].exists())
            for m in made:
                m.unlink(missing_ok=True)
