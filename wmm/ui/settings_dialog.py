"""Settings dialog: game path, language, Nexus API key, nxm:// handler, Mod base, Remove all."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..cli import create_desktop_shortcut, steam_launch_option_string
from ..deploy import DeployError, Manager
from ..engineini import apply_perf_preset, backup_engine_ini, read_perf_preset_mode
from ..game import GameInstall, running_game_processes, validate_game_root
from ..i18n import Translator
from ..platform import get_platform
from ..store import Store
from .onboarding import OnboardingDialog


class SettingsDialog(QDialog):
    def __init__(
        self,
        store: Store,
        translator: Translator,
        game: Optional[GameInstall],
        package_root: Path,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self.store = store
        self.t = translator
        self.game = game
        self.package_root = package_root
        self.plat = get_platform()
        self.language_changed = False
        self.removed_all = False

        self.setWindowTitle(self.t.tr("settings.title"))
        self.setMinimumWidth(520)
        self._build_ui()

    def _build_ui(self) -> None:
        settings = self.store.load_settings()
        root = QVBoxLayout(self)
        form = QFormLayout()
        form.setSpacing(10)

        # Game path
        game_row = QHBoxLayout()
        self.edit_game = QLineEdit(str(self.game.root) if self.game else settings.get("game_path", ""))
        self.edit_game.setReadOnly(True)
        btn_browse = QPushButton(self.t.tr("btn.change"))
        btn_browse.clicked.connect(self._browse_game)
        game_row.addWidget(self.edit_game, 1)
        game_row.addWidget(btn_browse)
        form.addRow(self.t.tr("settings.game_path"), game_row)

        # Language (populated from lang/*.json; users drop <code >.json to add languages)
        self.combo_lang = QComboBox()
        current_lang = settings.get("language", "en")
        for code, display in self.t.available_languages():
            self.combo_lang.addItem(f"{display} ({code})", code)
            if code == current_lang:
                self.combo_lang.setCurrentIndex(self.combo_lang.count() - 1)
        form.addRow(self.t.tr("settings.language"), self.combo_lang)

        # Nexus API key (stored encrypted via platform.protect_secret)
        self.edit_api = QLineEdit()
        self.edit_api.setEchoMode(QLineEdit.EchoMode.Password)
        raw_token = settings.get("nexus_api_key", "")
        if raw_token:
            try:
                self.edit_api.setText(self.plat.unprotect_secret(raw_token))
            except Exception:
                pass
        form.addRow(self.t.tr("settings.nexus_key"), self.edit_api)

        # Options
        self.chk_nxm = QCheckBox(self.t.tr("settings.nxm_handler"))
        self.chk_nxm.setChecked(bool(settings.get("nxm_registered", False)))
        form.addRow("", self.chk_nxm)

        self.chk_keep = QCheckBox(self.t.tr("settings.keep_archives"))
        self.chk_keep.setChecked(bool(settings.get("keep_archives", False)))
        form.addRow("", self.chk_keep)

        # Engine.ini performance preset (Restore original / Default / VRR)
        ini_row = QHBoxLayout()
        self.combo_perf = QComboBox()
        self.combo_perf.addItem(self.t.tr("settings.perf_restore"), "none")
        self.combo_perf.addItem(self.t.tr("onboarding.perf_default"), "default")
        self.combo_perf.addItem(self.t.tr("onboarding.perf_vrr"), "vrr")
        if self.game is not None and self.game.engine_ini_path is not None and self.game.engine_ini_path.is_file():
            cur_mode = read_perf_preset_mode(self.game.engine_ini_path)
            idx = self.combo_perf.findData(cur_mode)
            if idx >= 0:
                self.combo_perf.setCurrentIndex(idx)
            ini_enabled = True
        else:
            ini_enabled = False
        self.combo_perf.setEnabled(ini_enabled)
        btn_apply_ini = QPushButton(self.t.tr("btn.apply"))
        btn_apply_ini.setEnabled(ini_enabled)
        btn_apply_ini.clicked.connect(self._apply_engine_ini)
        ini_row.addWidget(self.combo_perf, 1)
        ini_row.addWidget(btn_apply_ini)
        form.addRow(self.t.tr("onboarding.step_ini"), ini_row)

        # Steam Launch Options command (lets Steam launch through WMM)
        launch_row = QHBoxLayout()
        self.edit_steam_launch = QLineEdit(steam_launch_option_string())
        self.edit_steam_launch.setReadOnly(True)
        btn_copy_launch = QPushButton(self.t.tr("btn.copy"))
        btn_copy_launch.clicked.connect(self._copy_steam_launch)
        launch_row.addWidget(self.edit_steam_launch, 1)
        launch_row.addWidget(btn_copy_launch)
        form.addRow(self.t.tr("settings.steam_launch"), launch_row)

        root.addLayout(form)

        # Tools row: Run onboarding again + Desktop shortcut
        tools_row = QHBoxLayout()
        btn_base = QPushButton(self.t.tr("settings.run_onboarding"))
        btn_base.clicked.connect(self._open_mod_base)
        btn_shortcut = QPushButton(self.t.tr("btn.desktop_shortcut"))
        btn_shortcut.clicked.connect(self._create_shortcut)
        tools_row.addWidget(btn_base)
        tools_row.addWidget(btn_shortcut)
        tools_row.addStretch(1)
        root.addLayout(tools_row)

        # Save / Cancel
        footer = QHBoxLayout()
        footer.addStretch(1)
        btn_cancel = QPushButton(self.t.tr("btn.cancel"))
        btn_cancel.clicked.connect(self.reject)
        btn_save = QPushButton(self.t.tr("btn.apply"))
        btn_save.setProperty("accent", True)
        btn_save.clicked.connect(self._save)
        footer.addWidget(btn_cancel)
        footer.addWidget(btn_save)
        root.addLayout(footer)

    def _browse_game(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, self.t.tr("settings.game_path"))
        if not chosen:
            return
        inst = validate_game_root(Path(chosen))
        if inst is None:
            QMessageBox.warning(self, self.t.tr("app.title"), self.t.tr("onboarding.invalid_game"))
            return
        self.game = inst
        self.edit_game.setText(str(inst.root))

    def _apply_engine_ini(self) -> None:
        if self.game is None or self.game.engine_ini_path is None or not self.game.engine_ini_path.is_file():
            return
        procs = running_game_processes()
        if procs:
            QMessageBox.warning(self, self.t.tr("app.title"), self.t.tr("err.game_running", procs=", ".join(procs)))
            return
        mode = self.combo_perf.currentData() or "none"
        try:
            bak = backup_engine_ini(self.game.engine_ini_path, self.store.backup_dir)
            apply_perf_preset(self.game.engine_ini_path, mode, backup_dir=None)
            bak_name = bak.parent.name if bak else "backup"
            QMessageBox.information(
                self,
                self.t.tr("app.title"),
                self.t.tr("onboarding.ini_applied_backup", preset=mode, backup=bak_name),
            )
        except Exception as exc:
            QMessageBox.critical(self, self.t.tr("app.title"), str(exc))

    def _copy_steam_launch(self) -> None:
        QApplication.clipboard().setText(self.edit_steam_launch.text())

    def _create_shortcut(self) -> None:
        try:
            sc = create_desktop_shortcut(self.game)
            QMessageBox.information(self, self.t.tr("app.title"), self.t.tr("settings.shortcut_created", path=str(sc)))
        except Exception as exc:
            QMessageBox.warning(self, self.t.tr("app.title"), str(exc))

    def _open_mod_base(self) -> None:
        dlg = OnboardingDialog(self.store, self.t, game=self.game, package_root=self.package_root, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.game is not None:
            self.game = dlg.game
            self.edit_game.setText(str(dlg.game.root))

    def _remove_all(self) -> None:
        if self.game is None:
            return
        ans = QMessageBox.question(self, self.t.tr("app.title"), self.t.tr("settings.confirm_remove_all"))
        if ans != QMessageBox.StandardButton.Yes:
            return
        mgr = Manager(self.store, self.game, tools_dir=self.package_root / "tools")
        try:
            count = mgr.remove_all()
            self.removed_all = True
            QMessageBox.information(self, self.t.tr("app.title"), self.t.tr("cli.removed_all", count=count))
        except DeployError as exc:
            QMessageBox.warning(self, self.t.tr("app.title"), str(exc))

    def _save(self) -> None:
        settings = self.store.load_settings()
        if self.game is not None:
            settings["game_path"] = str(self.game.root)

        new_lang = self.combo_lang.currentData() or "en"
        if new_lang != settings.get("language", "en"):
            self.language_changed = True
            settings["language"] = new_lang

        api_key = self.edit_api.text().strip()
        settings["nexus_api_key"] = self.plat.protect_secret(api_key) if api_key else ""
        settings["keep_archives"] = self.chk_keep.isChecked()

        want_nxm = self.chk_nxm.isChecked()
        was_nxm = bool(settings.get("nxm_registered", False))
        if want_nxm and not was_nxm:
            prev = self.plat.register_nxm(Path(sys.executable))
            settings["nxm_previous_handler"] = prev or ""
            settings["nxm_registered"] = True
        elif not want_nxm and was_nxm:
            self.plat.unregister_nxm(settings.get("nxm_previous_handler") or None)
            settings["nxm_registered"] = False

        self.store.save_settings(settings)
        self.accept()
