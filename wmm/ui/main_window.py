"""Main window for Wuchang Mod Manager (§2.1)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QColor, QDragEnterEvent, QDropEvent
from PySide6.QtNetwork import QLocalServer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QFileDialog,
    QHeaderView,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QStatusBar,
    QTableWidget,
    QTableWidgetItem,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from ..cli import NXM_IPC_SERVER_NAME
from ..deploy import DeployError, Manager, UnmanagedMod, scan_unmanaged_mods
from ..extract import ExtractError
from ..game import GameInstall, detect_all_installs, running_game_processes, validate_game_root
from ..i18n import Translator
from ..nexus import NexusClient, NexusError, parse_nxm_uri, update_mod_preserving_configs
from ..paks import PakConflictReport, scan_folder_conflicts
from ..platform import get_platform
from ..store import ModRecord, Profile, Store
from .onboarding import OnboardingDialog
from .settings_dialog import SettingsDialog


class _ResponsiveModTable(QTableWidget):
    """Mod table where all data columns are user-resizable and 'Name' fills remaining width."""

    def __init__(self, rows: int, cols: int, parent: Optional[QWidget] = None):
        super().__init__(rows, cols, parent)
        self._adjusting = False

    def fit_name_column(self) -> None:
        if self._adjusting:
            return
        avail = self.viewport().width()
        if avail <= 0:
            return
        self._adjusting = True
        try:
            other = sum(self.columnWidth(c) for c in range(self.columnCount()) if c != 1)
            target_name = max(140, avail - other)
            if self.columnWidth(1) != target_name:
                self.setColumnWidth(1, target_name)
        finally:
            self._adjusting = False

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.fit_name_column()


class LogDialog(QDialog):
    def __init__(self, log_lines: list[str], title: str, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(680, 380)
        lay = QVBoxLayout(self)
        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setPlainText("\n".join(log_lines))
        lay.addWidget(self.view)


class MainWindow(QMainWindow):
    def __init__(
        self,
        data_dir: Optional[Path] = None,
        game_override: Optional[Path] = None,
        run_onboarding_if_needed: bool = True,
    ):
        super().__init__()
        self.package_root = (
            Path(sys.executable).resolve().parent
            if getattr(sys, "frozen", False)
            else Path(__file__).resolve().parent.parent.parent
        )
        self.store = Store(data_dir or (self.package_root / "data"))
        self.plat = get_platform()
        settings = self.store.load_settings()
        self.t = Translator(self.package_root / "lang", language=settings.get("language", "en"))

        self.game: Optional[GameInstall] = self._resolve_game(game_override)
        self.logs: list[str] = []
        self.conflict_report: Optional[PakConflictReport] = None
        self.unmanaged_mods: dict[str, UnmanagedMod] = {}
        self._suppress_table_signals = False

        self.setWindowTitle(self.t.tr("app.title"))
        self.resize(980, 620)
        self.setAcceptDrops(True)

        self._build_toolbar()
        self._build_body()
        self.setStatusBar(QStatusBar())
        self._start_nxm_ipc_server()

        if run_onboarding_if_needed and (self.game is None or not settings.get("onboarded", False)):
            self._open_onboarding()

        self.refresh_all()

    def _start_nxm_ipc_server(self) -> None:
        self._ipc_server = QLocalServer(self)
        QLocalServer.removeServer(NXM_IPC_SERVER_NAME)
        if self._ipc_server.listen(NXM_IPC_SERVER_NAME):
            self._ipc_server.newConnection.connect(self._on_ipc_connection)

    def _on_ipc_connection(self) -> None:
        while self._ipc_server.hasPendingConnections():
            sock = self._ipc_server.nextPendingConnection()
            if sock is None:
                continue
            if sock.waitForReadyRead(500):
                raw = bytes(sock.readAll()).decode("utf-8", errors="ignore").strip()
                if raw:
                    for line in raw.splitlines():
                        if line.strip().lower().startswith("nxm://"):
                            self.raise_()
                            self.activateWindow()
                            self.handle_nxm_uri(line.strip())
            sock.disconnectFromServer()

    def closeEvent(self, event) -> None:
        if hasattr(self, "_ipc_server") and self._ipc_server.isListening():
            self._ipc_server.close()
        super().closeEvent(event)

    def _log(self, msg: str) -> None:
        self.logs.append(msg)

    def _resolve_game(self, override: Optional[Path]) -> Optional[GameInstall]:
        if override is not None:
            return validate_game_root(override)
        saved = self.store.load_settings().get("game_path")
        if saved:
            inst = validate_game_root(Path(saved))
            if inst is not None:
                return inst
        found = detect_all_installs()
        return found[0] if found else None

    def _manager(self) -> Optional[Manager]:
        if self.game is None:
            return None
        return Manager(self.store, self.game, platform=self.plat, tools_dir=self.package_root / "tools")

    # --- UI construction ---

    def _build_toolbar(self) -> None:
        tb = QToolBar()
        tb.setMovable(False)
        self.addToolBar(tb)

        self.btn_add = QPushButton(self.t.tr("btn.add"))
        self.btn_add.clicked.connect(self._on_add)
        tb.addWidget(self.btn_add)

        self.btn_download = QPushButton(self.t.tr("btn.download"))
        self.btn_download.clicked.connect(self._on_download)
        tb.addWidget(self.btn_download)

        self.btn_play = QPushButton(self.t.tr("btn.play"))
        self.btn_play.setProperty("accent", True)
        self.btn_play.clicked.connect(self._on_play)
        tb.addWidget(self.btn_play)

        tb.addSeparator()

        self.combo_profile = QComboBox()
        self.combo_profile.setMinimumWidth(140)
        self.combo_profile.currentIndexChanged.connect(self._on_profile_changed)
        tb.addWidget(self.combo_profile)

        self.btn_new_profile = QPushButton("+")
        self.btn_new_profile.setFixedWidth(28)
        self.btn_new_profile.setToolTip(self.t.tr("profile.new"))
        self.btn_new_profile.clicked.connect(self._on_new_profile)
        tb.addWidget(self.btn_new_profile)

        tb.addSeparator()

        self.btn_settings = QPushButton(self.t.tr("btn.settings"))
        self.btn_settings.clicked.connect(self._on_settings)
        tb.addWidget(self.btn_settings)

        self.btn_log = QPushButton(self.t.tr("btn.log"))
        self.btn_log.clicked.connect(self._on_show_log)
        tb.addWidget(self.btn_log)

    def _build_body(self) -> None:
        splitter = QSplitter(Qt.Orientation.Horizontal)

        # Mod table: [On, Name, Type, Version, Priority, Status]
        self.table = _ResponsiveModTable(0, 6)
        self.table.setHorizontalHeaderLabels(
            [
                "",
                self.t.tr("col.name"),
                self.t.tr("col.type"),
                self.t.tr("col.version"),
                self.t.tr("col.priority"),
                self.t.tr("col.status"),
            ]
        )
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._on_table_context_menu)
        self.table.itemChanged.connect(self._on_item_changed)
        self.table.itemSelectionChanged.connect(self._on_selection_changed)

        hdr = self.table.horizontalHeader()
        hdr.setStretchLastSection(False)
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(0, 32)
        for col, width in ((1, 240), (2, 85), (3, 70), (4, 65), (5, 105)):
            hdr.setSectionResizeMode(col, QHeaderView.ResizeMode.Interactive)
            self.table.setColumnWidth(col, width)
        hdr.sectionResized.connect(self._on_header_resized)

        # Details pane
        details_box = QWidget()
        dlay = QVBoxLayout(details_box)
        dlay.setContentsMargins(8, 8, 8, 8)
        self.lbl_details_title = QLabel(self.t.tr("details.none"))
        self.lbl_details_title.setStyleSheet("font-weight: 600; font-size: 13px;")
        self.txt_details = QPlainTextEdit()
        self.txt_details.setReadOnly(True)
        dlay.addWidget(self.lbl_details_title)
        dlay.addWidget(self.txt_details, 1)

        splitter.addWidget(self.table)
        splitter.addWidget(details_box)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.splitterMoved.connect(lambda *_: self.table.fit_name_column())
        self.setCentralWidget(splitter)

    def _on_header_resized(self, logical_index: int, _old: int, _new: int) -> None:
        if logical_index != 1:
            self.table.fit_name_column()

    # --- Refresh & state rendering ---

    def _refresh_profiles_combo(self) -> None:
        self.combo_profile.blockSignals(True)
        self.combo_profile.clear()
        active = self.store.load_settings().get("active_profile", "default")
        for pname in self.store.list_profiles():
            self.combo_profile.addItem(pname, pname)
            if pname == active:
                self.combo_profile.setCurrentIndex(self.combo_profile.count() - 1)
        self.combo_profile.blockSignals(False)

    def refresh_all(self) -> None:
        self._refresh_profiles_combo()
        prof = self.store.load_profile()
        enabled_set = set(prof.enabled_mods)
        mods = self.store.list_mods()
        manifest = self.store.load_manifest()

        # Recompute conflicts if game is present
        if self.game is not None and self.game.mods_pak_dir.is_dir():
            rule = self.store.get_pak_order_rule(self.game.build_id)
            aes_key = self.store.load_settings().get("aes_key") or None
            self.conflict_report = scan_folder_conflicts(self.game.mods_pak_dir, rule=rule, aes_key_hex=aes_key)
        else:
            self.conflict_report = None

        deployed_by_mod: dict[str, list[str]] = {}
        for m in manifest:
            deployed_by_mod.setdefault(m.mod_id, []).append(m.deployed_name)

        # Also detect unmanaged mods already in the game folder (~mods, LogicMods, ue4ss/Mods)
        unmanaged_list = scan_unmanaged_mods(self.game, manifest) if self.game is not None else []
        self.unmanaged_mods = {f"unmanaged:{u.folder}:{u.name}": u for u in unmanaged_list}

        self._suppress_table_signals = True
        self.table.setRowCount(len(mods) + len(unmanaged_list))
        for row, mod in enumerate(mods):
            is_on = mod.id in enabled_set

            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            chk.setCheckState(Qt.CheckState.Checked if is_on else Qt.CheckState.Unchecked)
            chk.setData(Qt.ItemDataRole.UserRole, mod.id)

            item_name = QTableWidgetItem(mod.name)
            item_name.setData(Qt.ItemDataRole.UserRole, mod.id)

            item_badge = QTableWidgetItem(mod.badge)
            item_badge.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

            item_ver = QTableWidgetItem(mod.version or "-")
            item_ver.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

            # Priority index
            prio_str = "-"
            for c in mod.components:
                if c.kind == "pak":
                    key = f"{mod.id}:{c.name}"
                    if key in prof.pak_order:
                        prio_str = str(prof.pak_order.index(key) + 1)
                    break
            item_prio = QTableWidgetItem(prio_str)
            item_prio.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

            # Status dot
            dot_text, dot_color = self._compute_mod_status(mod, deployed_by_mod.get(mod.id, []))
            item_status = QTableWidgetItem(dot_text)
            item_status.setForeground(QColor(dot_color))
            item_status.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

            self.table.setItem(row, 0, chk)
            self.table.setItem(row, 1, item_name)
            self.table.setItem(row, 2, item_badge)
            self.table.setItem(row, 3, item_ver)
            self.table.setItem(row, 4, item_prio)
            self.table.setItem(row, 5, item_status)

        base_row = len(mods)
        for idx_u, u in enumerate(unmanaged_list):
            row = base_row + idx_u
            ukey = f"unmanaged:{u.folder}:{u.name}"
            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            chk.setCheckState(Qt.CheckState.Checked)
            chk.setData(Qt.ItemDataRole.UserRole, ukey)

            item_name = QTableWidgetItem(u.name)
            item_name.setForeground(QColor("#9BA3B4"))
            item_name.setData(Qt.ItemDataRole.UserRole, ukey)

            item_badge = QTableWidgetItem(u.badge)
            item_badge.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

            item_ver = QTableWidgetItem("-")
            item_ver.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

            item_prio = QTableWidgetItem("-")
            item_prio.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

            item_status = QTableWidgetItem(f"● {self.t.tr('status.unmanaged')}")
            item_status.setForeground(QColor("#7EA8D9"))
            item_status.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

            self.table.setItem(row, 0, chk)
            self.table.setItem(row, 1, item_name)
            self.table.setItem(row, 2, item_badge)
            self.table.setItem(row, 3, item_ver)
            self.table.setItem(row, 4, item_prio)
            self.table.setItem(row, 5, item_status)

        self._suppress_table_signals = False
        self._update_status_bar(len(enabled_set) + len(unmanaged_list), len(mods) + len(unmanaged_list), prof.name)
        self._on_selection_changed()

    def _compute_mod_status(self, mod: ModRecord, deployed_names: list[str]) -> tuple[str, str]:
        has_cfg = any(bool(c.config_overrides) for c in mod.components)
        if has_cfg:
            return (f"● {self.t.tr('status.conflict')}", "#E05252")
        if self.conflict_report is not None:
            loses = 0
            for dn in deployed_names:
                info = self.conflict_report.per_pak.get(dn)
                if info:
                    loses += info.get("assets_lost", 0)
            if loses > 0:
                return (f"● {self.t.tr('status.overridden')}", "#E5A93C")
        return (f"● {self.t.tr('status.ok')}", "#4CAF7D")

    def _update_status_bar(self, enabled_count: int, total_count: int, profile_name: str) -> None:
        if self.game is None:
            self.statusBar().showMessage(self.t.tr("cli.game_not_found"))
            return
        conflicts = len(self.conflict_report.asset_owners) if self.conflict_report else 0
        self.statusBar().showMessage(
            self.t.tr(
                "statusbar.summary",
                launcher=self.game.launcher,
                build_id=self.game.build_id,
                profile=profile_name,
                enabled=enabled_count,
                total=total_count,
                conflicts=conflicts,
            )
        )

    # --- Actions ---

    def _selected_mod_id(self) -> Optional[str]:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        item = self.table.item(rows[0].row(), 0)
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _on_selection_changed(self) -> None:
        mod_id = self._selected_mod_id()
        if not mod_id:
            self.lbl_details_title.setText(self.t.tr("details.none"))
            self.txt_details.clear()
            return
        if mod_id in self.unmanaged_mods:
            u = self.unmanaged_mods[mod_id]
            self.lbl_details_title.setText(f"{u.name} [{u.badge}] ({self.t.tr('status.unmanaged')})")
            lines = [
                f"Path: {u.path}",
                f"Folder: {u.folder}",
                "",
                "This mod is already in the game folder and not managed by WMM.",
                "Right-click -> Adopt into WMM to manage it with zero duplicate copies.",
            ]
            if self.conflict_report and u.name in self.conflict_report.per_pak:
                info = self.conflict_report.per_pak[u.name]
                if info["assets_won"] or info["assets_lost"]:
                    lines.append("")
                    lines.append(f"Overrides: wins {info['assets_won']} / loses {info['assets_lost']}")
                    if info["wins"]:
                        lines.append(f"  overrides: {', '.join(info['wins'])}")
                    if info["loses"]:
                        lines.append(f"  overridden by: {', '.join(info['loses'])}")
            self.txt_details.setPlainText("\n".join(lines))
            return
        mod = self.store.load_mod(mod_id)
        if mod is None:
            return
        self.lbl_details_title.setText(f"{mod.name} [{mod.badge}]")
        manifest = [e for e in self.store.load_manifest() if e.mod_id == mod_id]
        lines = [
            f"ID: {mod.id}",
            f"Version: {mod.version or '-'}",
            f"Source: {mod.source}",
            "",
            "Components:",
        ]
        for c in mod.components:
            sel = "" if c.selected else " (unselected variant)"
            lines.append(f"  - [{c.kind}] {c.name} -> {c.target_kind}{sel}")
            if c.bp_mod_names:
                lines.append(f"      BP ModActor: {', '.join(c.bp_mod_names)}")
            if c.config_overrides:
                lines.append(f"      WARNING Config override: {', '.join(c.config_overrides)}")
        if manifest:
            lines.append("")
            lines.append("Deployed:")
            for e in manifest:
                lines.append(f"  - {e.deployed_name} ({e.kind})")
                if self.conflict_report and e.deployed_name in self.conflict_report.per_pak:
                    info = self.conflict_report.per_pak[e.deployed_name]
                    if info["assets_won"] or info["assets_lost"]:
                        tag = "" if self.conflict_report.verified else f" [{self.t.tr('status.unverified')}]"
                        lines.append(f"      wins {info['assets_won']} / loses {info['assets_lost']}{tag}")
                        if info["wins"]:
                            lines.append(f"      overrides: {', '.join(info['wins'])}")
                        if info["loses"]:
                            lines.append(f"      overridden by: {', '.join(info['loses'])}")
        self.txt_details.setPlainText("\n".join(lines))

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if self._suppress_table_signals or item.column() != 0:
            return
        mod_id = item.data(Qt.ItemDataRole.UserRole)
        want_on = item.checkState() == Qt.CheckState.Checked
        mgr = self._manager()
        if mgr is None:
            return
        try:
            mgr.set_enabled(mod_id, want_on)
            self._log(f"{'Enabled' if want_on else 'Disabled'} {mod_id}")
        except DeployError as exc:
            QMessageBox.warning(self, self.t.tr("app.title"), str(exc))
        self.refresh_all()

    def _install_path(self, path: Path) -> None:
        mgr = self._manager()
        if mgr is None:
            QMessageBox.warning(self, self.t.tr("app.title"), self.t.tr("cli.game_not_found"))
            return
        try:
            rec = mgr.add_mod(path)
            self._log(f"Added {rec.name} ({rec.id}) [{rec.badge}]")
        except DeployError as exc:
            if "loader DLL" in str(exc):
                ans = QMessageBox.question(self, self.t.tr("app.title"), str(exc))
                if ans == QMessageBox.StandardButton.Yes:
                    rec = mgr.add_mod(path, allow_loaders=True)
                    self._log(f"Added {rec.name} ({rec.id}) [loader confirmed]")
            else:
                QMessageBox.warning(self, self.t.tr("app.title"), str(exc))
        except ExtractError as exc:
            QMessageBox.warning(self, self.t.tr("app.title"), str(exc))
        self.refresh_all()

    def _on_add(self) -> None:
        chosen, _ = QFileDialog.getOpenFileName(
            self,
            self.t.tr("btn.add"),
            "",
            "Archives (*.zip *.7z *.rar *.pak);;All files (*.*)",
        )
        if chosen:
            self._install_path(Path(chosen))

    def handle_nxm_uri(self, uri: str) -> None:
        """Download and install a mod from an nxm:// or https:// URL."""
        uri = uri.strip()
        settings = self.store.load_settings()
        raw_key = settings.get("nexus_api_key", "")
        api_key = self.plat.unprotect_secret(raw_key) if raw_key else ""
        mods_dir_raw = settings.get("mods_archive_dir", "")
        dl_dir = Path(mods_dir_raw) if mods_dir_raw else self.store.downloads_dir
        try:
            if uri.lower().startswith("nxm://"):
                if not api_key:
                    QMessageBox.warning(self, self.t.tr("app.title"), self.t.tr("download.need_api_key"))
                    return
                link = parse_nxm_uri(uri)
                client = NexusClient(api_key)
                links = client.get_download_links(
                    link.mod_id, link.file_id, domain=link.domain, key=link.key, expires=link.expires
                )
                if not links:
                    raise NexusError("No download URL returned by Nexus")
                dl_path = client.download_url(links[0]["URI"], dl_dir)
                self._install_path(dl_path)
            elif uri.lower().startswith(("http://", "https://")):
                client = NexusClient(api_key or "public")
                dl_path = client.download_url(uri, dl_dir)
                self._install_path(dl_path)
            else:
                QMessageBox.warning(self, self.t.tr("app.title"), self.t.tr("download.invalid_url"))
        except Exception as exc:
            QMessageBox.warning(self, self.t.tr("app.title"), str(exc))

    def _on_download(self) -> None:
        uri, ok = QInputDialog.getText(self, self.t.tr("btn.download"), self.t.tr("download.prompt"))
        if not ok or not uri.strip():
            return
        self.handle_nxm_uri(uri.strip())

    def _on_play(self) -> None:
        mgr = self._manager()
        if mgr is None or self.game is None:
            QMessageBox.warning(self, self.t.tr("app.title"), self.t.tr("cli.game_not_found"))
            return
        try:
            mgr.apply_profile()
        except DeployError as exc:
            QMessageBox.warning(self, self.t.tr("app.title"), str(exc))
            return
        kind, target = self.game.launch_command()
        self._log(f"Launching ({kind}): {target}")
        if kind == "uri":
            self.plat.launch_uri(target)
        else:
            self.plat.launch_exe(Path(target))

    def _on_profile_changed(self) -> None:
        pname = self.combo_profile.currentData()
        if not pname:
            return
        settings = self.store.load_settings()
        if settings.get("active_profile") == pname:
            return
        mgr = self._manager()
        if mgr is None:
            return
        try:
            settings["active_profile"] = pname
            self.store.save_settings(settings)
            mgr.apply_profile(self.store.load_profile(pname))
            self._log(f"Switched profile -> {pname}")
        except DeployError as exc:
            QMessageBox.warning(self, self.t.tr("app.title"), str(exc))
        self.refresh_all()

    def _on_new_profile(self) -> None:
        name, ok = QInputDialog.getText(self, self.t.tr("profile.new"), self.t.tr("profile.name_prompt"))
        if not ok or not name.strip():
            return
        prof = Profile(name=name.strip())
        self.store.save_profile(prof)
        self.refresh_all()

    def _on_settings(self) -> None:
        dlg = SettingsDialog(self.store, self.t, self.game, self.package_root, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            if dlg.game is not None:
                self.game = dlg.game
            if dlg.language_changed:
                lang = self.store.load_settings().get("language", "en")
                self.t = Translator(self.package_root / "lang", language=lang)
            self.refresh_all()

    def _open_onboarding(self) -> None:
        dlg = OnboardingDialog(self.store, self.t, game=self.game, package_root=self.package_root, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.game is not None:
            self.game = dlg.game

    def _on_show_log(self) -> None:
        dlg = LogDialog(self.logs or ["(no log entries yet)"], self.t.tr("btn.log"), parent=self)
        dlg.exec()

    # --- Context menu (Open folder, Move up/down, Update, Uninstall) ---

    def _on_table_context_menu(self, pos) -> None:
        mod_id = self._selected_mod_id()
        if not mod_id:
            return
        menu = QMenu(self)
        if mod_id in self.unmanaged_mods:
            u = self.unmanaged_mods[mod_id]
            act_adopt = QAction(self.t.tr("ctx.adopt"), self)
            act_open = QAction(self.t.tr("ctx.open_folder"), self)
            act_adopt.triggered.connect(lambda: self._ctx_adopt(u))
            act_open.triggered.connect(lambda: self._open_path(u.path if u.path.is_dir() else u.path.parent))
            menu.addAction(act_adopt)
            menu.addAction(act_open)
            menu.exec(self.table.viewport().mapToGlobal(pos))
            return

        act_open = QAction(self.t.tr("ctx.open_folder"), self)
        act_up = QAction(self.t.tr("ctx.move_up"), self)
        act_down = QAction(self.t.tr("ctx.move_down"), self)
        act_update = QAction(self.t.tr("btn.update"), self)
        act_uninst = QAction(self.t.tr("btn.uninstall"), self)

        act_open.triggered.connect(lambda: self._ctx_open_folder(mod_id))
        act_up.triggered.connect(lambda: self._ctx_move_pak(mod_id, -1))
        act_down.triggered.connect(lambda: self._ctx_move_pak(mod_id, 1))
        act_update.triggered.connect(lambda: self._ctx_update(mod_id))
        act_uninst.triggered.connect(lambda: self._ctx_uninstall(mod_id))

        menu.addAction(act_open)
        menu.addSeparator()
        menu.addAction(act_up)
        menu.addAction(act_down)
        menu.addSeparator()
        menu.addAction(act_update)
        menu.addAction(act_uninst)
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def _open_path(self, target: Path) -> None:
        if not target.exists():
            return
        if sys.platform == "win32":
            os.startfile(str(target))  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", str(target)])

    def _ctx_adopt(self, u: UnmanagedMod) -> None:
        mgr = self._manager()
        if mgr is None:
            return
        try:
            rec = mgr.adopt_pak(u.path) if u.kind == "pak" else mgr.adopt_ue4ss_mod(u.path)
            self._log(f"Adopted {rec.name} ({rec.id})")
        except DeployError as exc:
            QMessageBox.warning(self, self.t.tr("app.title"), str(exc))
        self.refresh_all()

    def _ctx_open_folder(self, mod_id: str) -> None:
        self._open_path(self.store.mod_dir(mod_id))

    def _ctx_move_pak(self, mod_id: str, delta: int) -> None:
        mgr = self._manager()
        if mgr is None:
            return
        mod = self.store.load_mod(mod_id)
        if mod is None:
            return
        pak_keys = [f"{mod.id}:{c.name}" for c in mod.components if c.kind == "pak"]
        if not pak_keys:
            return
        prof = self.store.load_profile()
        # Ensure all enabled paks are in pak_order
        for m in self.store.list_mods():
            if m.id in prof.enabled_mods:
                for c in m.components:
                    if c.kind == "pak":
                        k = f"{m.id}:{c.name}"
                        if k not in prof.pak_order:
                            prof.pak_order.append(k)
        key = pak_keys[0]
        if key not in prof.pak_order:
            return
        idx = prof.pak_order.index(key)
        new_idx = max(0, min(len(prof.pak_order) - 1, idx + delta))
        if new_idx == idx:
            return
        prof.pak_order.insert(new_idx, prof.pak_order.pop(idx))
        self.store.save_profile(prof)
        try:
            mgr.apply_profile(prof)
        except DeployError as exc:
            QMessageBox.warning(self, self.t.tr("app.title"), str(exc))
        self.refresh_all()

    def _ctx_update(self, mod_id: str) -> None:
        mgr = self._manager()
        if mgr is None:
            return
        chosen, _ = QFileDialog.getOpenFileName(
            self, self.t.tr("btn.update"), "", "Archives (*.zip *.7z *.rar *.pak);;All files (*.*)"
        )
        if not chosen:
            return
        try:
            rec, summaries = update_mod_preserving_configs(mgr, mod_id, Path(chosen))
            self._log(f"Updated {rec.name} (preserved {len(summaries)} config.lua)")
        except Exception as exc:
            QMessageBox.warning(self, self.t.tr("app.title"), str(exc))
        self.refresh_all()

    def _ctx_uninstall(self, mod_id: str) -> None:
        mgr = self._manager()
        if mgr is None:
            return
        ans = QMessageBox.question(self, self.t.tr("app.title"), self.t.tr("ctx.confirm_uninstall", id=mod_id))
        if ans != QMessageBox.StandardButton.Yes:
            return
        try:
            mgr.set_enabled(mod_id, False)
            mdir = self.store.mod_dir(mod_id)
            if mdir.exists():
                shutil.rmtree(mdir)
            self._log(f"Uninstalled {mod_id}")
        except DeployError as exc:
            QMessageBox.warning(self, self.t.tr("app.title"), str(exc))
        self.refresh_all()

    # --- Drag & drop archives/folders onto window ---

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:
        for url in event.mimeData().urls():
            local = url.toLocalFile()
            if local:
                self._install_path(Path(local))
