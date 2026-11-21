"""Onboarding & Mod Base setup dialog (§5).

One clean screen with per-step immediate CTA buttons and green ✔ / red ✖ badges:
  1. Game folder    -> [Change] (validates Shipping exe; if invalid, other rows greyed out)
  2. Mods folder    -> [Create] / [Change] (immediately creates/saves)
  3. Engine.ini     -> [Start game once] or [Preset dropdown] + [Apply] (immediate write)
  4. Modding setup  -> [Full / Paks only / Skip] + [Apply] (immediate install/confirm)
  Footer row        -> [Status / Warning] [Cancel] [Finish] (Finish enabled only when all 4 steps are validated)
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..base import (
    ModBaseStatus,
    OptionalPackages,
    detect_mod_base,
    enable_unsigned_paks_in_ue4ss_ini,
    find_optional_packages,
    install_ue4ss_base,
    install_wmm_lua_bploader,
    install_wmm_pak_enabler,
)
from ..engineini import apply_perf_preset, backup_engine_ini, read_perf_preset_mode
from ..extract import extract_archive
from ..game import GameInstall, detect_all_installs, running_game_processes, validate_game_root
from ..i18n import Translator
from ..platform import get_platform
from ..store import Store

_BADGE_OK = "<span style='color:#4CAF7D; font-weight:700; font-size:15px;'>✔</span>"
_BADGE_FAIL = "<span style='color:#E05252; font-weight:700; font-size:15px;'>✖</span>"
_BADGE_WARN = "<span style='color:#E5A93C; font-weight:700; font-size:15px;'>!</span>"


class OnboardingDialog(QDialog):
    def __init__(
        self,
        store: Store,
        translator: Translator,
        game: Optional[GameInstall] = None,
        package_root: Optional[Path] = None,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self.store = store
        self.t = translator
        self.game = game
        self.package_root = package_root or Path(__file__).resolve().parent.parent.parent
        self.base_status: Optional[ModBaseStatus] = None
        self.opt_pkgs: OptionalPackages = OptionalPackages()

        settings = self.store.load_settings()
        saved_mods_dir = settings.get("mods_archive_dir", "")
        self.mods_archive_dir = Path(saved_mods_dir) if saved_mods_dir else (self.package_root / "mods")
        self._skip_applied = settings.get("modding_mode") == "skip"
        self._last_feedback = ""

        self.setWindowTitle(self.t.tr("onboarding.title"))
        self.setMinimumWidth(740)
        self._build_ui()
        self._refresh_state()

    def _make_row(self, title: str) -> tuple[QWidget, QLabel, QLabel, QHBoxLayout]:
        box = QWidget()
        lay = QHBoxLayout(box)
        lay.setContentsMargins(0, 6, 0, 6)
        lay.setSpacing(10)

        lbl_badge = QLabel(_BADGE_FAIL)
        lbl_badge.setFixedWidth(20)
        lbl_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)

        lbl_title = QLabel(f"<b>{title}</b>")
        lbl_title.setFixedWidth(115)

        lbl_state = QLabel("")
        lbl_state.setWordWrap(True)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(6)

        lay.addWidget(lbl_badge)
        lay.addWidget(lbl_title)
        lay.addWidget(lbl_state, 1)
        lay.addLayout(btn_row)
        return box, lbl_badge, lbl_state, btn_row

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setSpacing(10)
        root.setContentsMargins(18, 18, 18, 16)

        # 1. Game folder
        self.row_game, self.badge_game, self.lbl_game, b_game = self._make_row(self.t.tr("onboarding.step_game"))
        self.btn_change_game = QPushButton(self.t.tr("btn.change"))
        self.btn_change_game.clicked.connect(self._pick_game)
        b_game.addWidget(self.btn_change_game)
        root.addWidget(self.row_game)

        # 2. Mods folder
        self.row_mods, self.badge_mods, self.lbl_mods, b_mods = self._make_row(self.t.tr("onboarding.step_mods_dir"))
        self.btn_create_mods = QPushButton(self.t.tr("btn.create"))
        self.btn_create_mods.clicked.connect(self._create_mods_dir)
        self.btn_change_mods = QPushButton(self.t.tr("btn.change"))
        self.btn_change_mods.clicked.connect(self._pick_mods_dir)
        b_mods.addWidget(self.btn_create_mods)
        b_mods.addWidget(self.btn_change_mods)
        root.addWidget(self.row_mods)

        # 3. Engine.ini + immediate Apply CTA
        self.row_ini, self.badge_ini, self.lbl_ini, b_ini = self._make_row(self.t.tr("onboarding.step_ini"))
        self.combo_perf = QComboBox()
        self.combo_perf.addItem(self.t.tr("onboarding.perf_none"), "none")
        self.combo_perf.addItem(self.t.tr("onboarding.perf_vrr"), "vrr")
        self.combo_perf.addItem(self.t.tr("onboarding.perf_no_vrr"), "no_vrr")
        self.btn_apply_ini = QPushButton(self.t.tr("btn.apply"))
        self.btn_apply_ini.clicked.connect(self._apply_ini_step)
        self.btn_start_once = QPushButton(self.t.tr("btn.start_game"))
        self.btn_start_once.clicked.connect(self._start_game_once)
        b_ini.addWidget(self.combo_perf)
        b_ini.addWidget(self.btn_apply_ini)
        b_ini.addWidget(self.btn_start_once)
        root.addWidget(self.row_ini)

        # 4. Merged Modding setup + immediate Apply CTA
        self.row_setup, self.badge_setup, self.lbl_setup, b_setup = self._make_row(self.t.tr("onboarding.step_modding"))
        self.combo_modding = QComboBox()
        self.combo_modding.addItem(self.t.tr("onboarding.mode_full"), "full")
        self.combo_modding.addItem(self.t.tr("onboarding.mode_paks_only"), "paks_only")
        self.combo_modding.addItem(self.t.tr("onboarding.mode_skip"), "skip")
        self.combo_modding.currentIndexChanged.connect(lambda _: self._refresh_modding_status())
        self.btn_apply_modding = QPushButton(self.t.tr("btn.apply"))
        self.btn_apply_modding.clicked.connect(self._apply_modding_step)
        b_setup.addWidget(self.combo_modding)
        b_setup.addWidget(self.btn_apply_modding)
        root.addWidget(self.row_setup)

        # Footer row: status/warning on left, Cancel + Finish on right
        footer = QHBoxLayout()
        footer.setSpacing(8)
        self.lbl_status = QLabel("")
        self.lbl_status.setWordWrap(True)
        self.lbl_status.setStyleSheet("color: #AEB3BE;")
        footer.addWidget(self.lbl_status, 1)

        self.btn_cancel = QPushButton(self.t.tr("btn.cancel"))
        self.btn_cancel.clicked.connect(self.reject)
        self.btn_finish = QPushButton(self.t.tr("btn.finish"))
        self.btn_finish.setProperty("accent", True)
        self.btn_finish.clicked.connect(self._finish)
        footer.addWidget(self.btn_cancel)
        footer.addWidget(self.btn_finish)
        root.addLayout(footer)

    def _set_status(self, text: str, warning: bool = False) -> None:
        color = "#E5A93C" if warning else "#AEB3BE"
        self.lbl_status.setStyleSheet(f"color: {color};")
        self.lbl_status.setText(text)

    def _set_dependent_rows_enabled(self, enabled: bool) -> None:
        self.row_mods.setEnabled(enabled)
        self.row_ini.setEnabled(enabled)
        self.row_setup.setEnabled(enabled)
        if not enabled:
            self.btn_finish.setEnabled(False)

    def _refresh_state(self) -> None:
        if self.game is None:
            found = detect_all_installs()
            self.game = found[0] if found else None

        game_valid = self.game is not None and self.game.exe_path.is_file()
        if not game_valid:
            self.badge_game.setText(_BADGE_FAIL)
            self.lbl_game.setText(self.t.tr("onboarding.game_missing"))
            self.badge_mods.setText(_BADGE_FAIL)
            self.lbl_mods.setText("-")
            self.badge_ini.setText(_BADGE_FAIL)
            self.lbl_ini.setText("-")
            self.badge_setup.setText(_BADGE_FAIL)
            self.lbl_setup.setText("-")
            self._set_dependent_rows_enabled(False)
            self._set_status(self.t.tr("onboarding.need_game_first"), warning=True)
            return

        assert self.game is not None
        self._set_dependent_rows_enabled(True)
        self.badge_game.setText(_BADGE_OK)
        self.lbl_game.setText(f"{self.game.root} ({self.game.launcher}, build {self.game.build_id})")

        # Step 2: Mods folder
        self._mods_ok = self.mods_archive_dir.is_dir()
        self.badge_mods.setText(_BADGE_OK if self._mods_ok else _BADGE_FAIL)
        self.lbl_mods.setText(
            str(self.mods_archive_dir)
            if self._mods_ok
            else self.t.tr("onboarding.mods_dir_missing", path=str(self.mods_archive_dir))
        )
        self.btn_create_mods.setVisible(not self._mods_ok)

        # Step 3: Engine.ini
        self.base_status = detect_mod_base(self.game)
        st = self.base_status
        self._ini_ok = bool(st.engine_ini_exists and self.game.engine_ini_path is not None)
        if self._ini_ok and self.game.engine_ini_path is not None:
            self.badge_ini.setText(_BADGE_OK)
            cur_mode = read_perf_preset_mode(self.game.engine_ini_path)
            idx = self.combo_perf.findData(cur_mode)
            if idx >= 0:
                self.combo_perf.blockSignals(True)
                self.combo_perf.setCurrentIndex(idx)
                self.combo_perf.blockSignals(False)
            mode_str = {"vrr": "VRR", "no_vrr": "No VRR", "none": "Off"}[cur_mode]
            self.lbl_ini.setText(self.t.tr("onboarding.ini_found_preset", preset=mode_str))
            self.combo_perf.setVisible(True)
            self.btn_apply_ini.setVisible(True)
            self.btn_start_once.setVisible(False)
        else:
            self.badge_ini.setText(_BADGE_FAIL)
            self.lbl_ini.setText(self.t.tr("onboarding.ini_missing"))
            self.combo_perf.setVisible(False)
            self.btn_apply_ini.setVisible(False)
            self.btn_start_once.setVisible(True)

        # Step 4: Modding setup
        self.opt_pkgs = find_optional_packages(self.package_root, self.mods_archive_dir if self._mods_ok else None)
        self._refresh_modding_status()

    def _refresh_modding_status(self) -> None:
        if self.base_status is None:
            return
        st = self.base_status
        mode = self.combo_modding.currentData() or "full"

        ue_txt = "OK" if st.ue4ss.installed else "missing"
        pak_txt = "OK" if st.enabler.present else "missing"
        bp_txt = "WMM Lua" if st.bp_loader.kind == "wmm_lua" else ("WFF" if st.bp_loader.kind == "wff_pak" else "none")
        self.lbl_setup.setText(f"UE4SS: {ue_txt}  ·  Pak enabler: {pak_txt}  ·  BP loader: {bp_txt}")

        modding_ok = False
        warning_text = ""

        if mode == "full":
            modding_ok = st.ue4ss.installed and st.enabler.present
            self.badge_setup.setText(_BADGE_OK if modding_ok else _BADGE_FAIL)
            if not modding_ok and not st.ue4ss.installed and self.opt_pkgs.ue4ss_archive is None:
                warning_text = self.t.tr("onboarding.warn_need_ue4ss_zip")
        elif mode == "paks_only":
            modding_ok = st.enabler.present
            self.badge_setup.setText(_BADGE_OK if modding_ok else _BADGE_FAIL)
            warning_text = self.t.tr("onboarding.warn_paks_only")
        else:  # skip
            modding_ok = self._skip_applied
            self.badge_setup.setText(_BADGE_WARN if self._skip_applied else _BADGE_FAIL)
            warning_text = self.t.tr("onboarding.warn_skip_inline")

        all_valid = bool(self.game and self.game.exe_path.is_file() and self._mods_ok and self._ini_ok and modding_ok)
        self.btn_finish.setEnabled(all_valid)

        if warning_text:
            self._set_status(warning_text, warning=True)
        elif self._last_feedback:
            self._set_status(self._last_feedback, warning=False)
        elif not all_valid:
            self._set_status(self.t.tr("onboarding.complete_all_steps"), warning=False)
        else:
            self._set_status(self.t.tr("onboarding.ready_to_finish"), warning=False)

    # --- Per-step immediate CTAs ---

    def _pick_game(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, self.t.tr("onboarding.step_game"))
        if not chosen:
            return
        inst = validate_game_root(Path(chosen))
        if inst is None:
            self.game = None
            self._refresh_state()
            QMessageBox.warning(self, self.t.tr("app.title"), self.t.tr("onboarding.invalid_game"))
            return
        self.game = inst
        settings = self.store.load_settings()
        settings["game_path"] = str(inst.root)
        self.store.save_settings(settings)
        self._last_feedback = self.t.tr("onboarding.saved_step", step=self.t.tr("onboarding.step_game"))
        self._refresh_state()

    def _create_mods_dir(self) -> None:
        try:
            self.mods_archive_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            QMessageBox.warning(self, self.t.tr("app.title"), str(exc))
            return
        settings = self.store.load_settings()
        settings["mods_archive_dir"] = str(self.mods_archive_dir)
        self.store.save_settings(settings)
        self._last_feedback = self.t.tr("onboarding.saved_step", step=self.t.tr("onboarding.step_mods_dir"))
        self._refresh_state()

    def _pick_mods_dir(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, self.t.tr("onboarding.step_mods_dir"))
        if not chosen:
            return
        self.mods_archive_dir = Path(chosen)
        settings = self.store.load_settings()
        settings["mods_archive_dir"] = str(self.mods_archive_dir)
        self.store.save_settings(settings)
        self._last_feedback = self.t.tr("onboarding.saved_step", step=self.t.tr("onboarding.step_mods_dir"))
        self._refresh_state()

    def _start_game_once(self) -> None:
        if self.game is None:
            return
        kind, target = self.game.launch_command()
        plat = get_platform()
        if kind == "uri":
            plat.launch_uri(target)
        else:
            plat.launch_exe(Path(target))

    def _apply_ini_step(self) -> None:
        if self.game is None or self.game.engine_ini_path is None or not self.game.engine_ini_path.is_file():
            return
        procs = running_game_processes()
        if procs:
            QMessageBox.warning(self, self.t.tr("app.title"), self.t.tr("err.game_running", procs=", ".join(procs)))
            return
        perf_mode = self.combo_perf.currentData() or "none"
        try:
            bak_path = backup_engine_ini(self.game.engine_ini_path, self.store.backup_dir)
            apply_perf_preset(self.game.engine_ini_path, perf_mode, backup_dir=None)
            bak_name = bak_path.parent.name if bak_path else "backup"
            self._last_feedback = self.t.tr("onboarding.ini_applied_backup", preset=perf_mode, backup=bak_name)
        except Exception as exc:
            QMessageBox.critical(self, self.t.tr("app.title"), str(exc))
        self._refresh_state()

    def _apply_modding_step(self) -> None:
        if self.game is None:
            return
        procs = running_game_processes()
        if procs:
            QMessageBox.warning(self, self.t.tr("app.title"), self.t.tr("err.game_running", procs=", ".join(procs)))
            return

        mode = self.combo_modding.currentData() or "full"
        if mode == "paks_only":
            ans = QMessageBox.warning(
                self,
                self.t.tr("app.title"),
                self.t.tr("onboarding.confirm_paks_only"),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if ans != QMessageBox.StandardButton.Yes:
                return
        elif mode == "skip":
            ans = QMessageBox.warning(
                self,
                self.t.tr("app.title"),
                self.t.tr("onboarding.skip_warning"),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if ans != QMessageBox.StandardButton.Yes:
                return
            self._skip_applied = True
            settings = self.store.load_settings()
            settings["modding_mode"] = "skip"
            self.store.save_settings(settings)
            self._refresh_state()
            return

        try:
            if mode == "full":
                if self.opt_pkgs.ue4ss_archive is not None:
                    tmp = Path(tempfile.mkdtemp(prefix="wmm_ue4ss_"))
                    try:
                        extract_archive(self.opt_pkgs.ue4ss_archive, tmp, tools_dir=self.package_root / "tools")
                        payload_root = tmp
                        for cand in tmp.rglob("dwmapi.dll"):
                            payload_root = cand.parent
                            break
                        install_ue4ss_base(self.game, self.store, payload_root)
                    finally:
                        shutil.rmtree(tmp, ignore_errors=True)

                st_after = detect_mod_base(self.game)
                if st_after.ue4ss.settings_ini is not None and (st_after.ue4ss.is_fork or self.opt_pkgs.ue4ss_archive is not None):
                    enable_unsigned_paks_in_ue4ss_ini(st_after.ue4ss.settings_ini)
                elif not st_after.enabler.present and self.opt_pkgs.pak_enabler_dll is not None:
                    install_wmm_pak_enabler(self.game, self.store, self.opt_pkgs.pak_enabler_dll)

                if st_after.ue4ss.installed and st_after.bp_loader.kind != "wmm_lua":
                    install_wmm_lua_bploader(self.game, self.store)
                self._skip_applied = False
            elif mode == "paks_only":
                st_cur = detect_mod_base(self.game)
                if not st_cur.enabler.present and self.opt_pkgs.pak_enabler_dll is not None:
                    install_wmm_pak_enabler(self.game, self.store, self.opt_pkgs.pak_enabler_dll)
                self._skip_applied = False

            settings = self.store.load_settings()
            settings["modding_mode"] = mode
            self.store.save_settings(settings)
            self._last_feedback = self.t.tr("onboarding.saved_step", step=self.t.tr("onboarding.step_modding"))
        except Exception as exc:
            QMessageBox.critical(self, self.t.tr("app.title"), str(exc))
        self._refresh_state()

    def _finish(self) -> None:
        if self.game is None or not self.game.exe_path.is_file():
            return
        settings = self.store.load_settings()
        settings["game_path"] = str(self.game.root)
        settings["mods_archive_dir"] = str(self.mods_archive_dir)
        settings["onboarded"] = True
        self.store.save_settings(settings)
        self.accept()
