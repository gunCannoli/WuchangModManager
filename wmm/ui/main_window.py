"""Main window for Wuchang Mod Manager (§2.1)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QAction, QColor, QDragEnterEvent, QDropEvent, QFont
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
    QProgressBar,
    QPushButton,
    QSplitter,
    QStatusBar,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from ..base import detect_mod_base
from ..cli import NXM_IPC_SERVER_NAME
from ..deploy import DeployError, Manager, UnmanagedMod, scan_unmanaged_mods
from ..extract import ExtractError
from ..game import GameInstall, detect_all_installs, running_game_processes, validate_game_root
from ..i18n import Translator
from ..nexus import NexusClient, NexusError, parse_nxm_uri, update_mod_preserving_configs
from ..paks import PakConflictReport, parse_pak_priority, scan_folder_conflicts
from ..platform import get_platform
from ..store import ModRecord, Profile, Store
from ..ue4ss import BASE_UE4SS_MODS, LAST_UE4SS_MOD, parse_mods_txt, write_mods_txt
from .onboarding import OnboardingDialog
from .profiles_dialog import ProfilesDialog
from .settings_dialog import SettingsDialog

_CORE_UE4SS_LOWER = {n.lower() for n in BASE_UE4SS_MODS} | {LAST_UE4SS_MOD.lower(), "wmm_bploader"}


def _format_speed(bps: float) -> str:
    if bps >= 1024 * 1024:
        return f"{bps / (1024 * 1024):.1f} MB/s"
    if bps >= 1024:
        return f"{bps / 1024:.0f} KB/s"
    return f"{int(bps)} B/s"


def _bold_italic_font(base: QFont) -> QFont:
    f = QFont(base)
    f.setBold(True)
    f.setItalic(True)
    return f


class _DownloadWorker(QThread):
    """Background worker for Nexus nxm:// and https:// downloads with live progress + speed."""

    progress = Signal(int, str, int, str)  # dl_id, filename, percent (-1 if unknown), speed_str
    finished_ok = Signal(int, str, object, str)  # dl_id, downloaded_path, mod_name_or_None, mod_version
    failed = Signal(int, str)

    def __init__(self, dl_id: int, uri: str, api_key: str, dl_dir: Path, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.dl_id = dl_id
        self.uri = uri
        self.api_key = api_key
        self.dl_dir = dl_dir

    def run(self) -> None:
        def _on_prog(fname: str, done: int, total: int, bps: float) -> None:
            pct = int(done * 100 / total) if total > 0 else -1
            self.progress.emit(self.dl_id, fname, pct, _format_speed(bps))

        try:
            if self.uri.lower().startswith("nxm://"):
                link = parse_nxm_uri(self.uri)
                client = NexusClient(self.api_key)
                links = client.get_download_links(
                    link.mod_id, link.file_id, domain=link.domain, key=link.key, expires=link.expires
                )
                if not links:
                    raise NexusError("No download URL returned by Nexus")
                mod_name: Optional[str] = None
                mod_ver = ""
                try:
                    info = client.get_mod_info(link.mod_id, domain=link.domain)
                    mod_name = info.get("name") or None
                    mod_ver = str(info.get("version") or "")
                except Exception:
                    pass
                dl_path = client.download_url(links[0]["URI"], self.dl_dir, progress_cb=_on_prog)
                self.finished_ok.emit(self.dl_id, str(dl_path), mod_name, mod_ver)
            else:
                client = NexusClient(self.api_key or "public")
                dl_path = client.download_url(self.uri, self.dl_dir, progress_cb=_on_prog)
                self.finished_ok.emit(self.dl_id, str(dl_path), None, "")
        except Exception as exc:
            self.failed.emit(self.dl_id, str(exc))


class _ResponsiveTable(QTableWidget):
    """Table where all data columns are user-resizable and flex_col fills remaining width."""

    def __init__(self, rows: int, cols: int, flex_col: int = 1, parent: Optional[QWidget] = None):
        super().__init__(rows, cols, parent)
        self.flex_col = flex_col
        self._adjusting = False
        self.horizontalHeader().sectionResized.connect(self._on_col_resized)

    def _on_col_resized(self, logical_index: int, _old: int, _new: int) -> None:
        if logical_index != self.flex_col:
            self.fit_flex_column()

    def fit_flex_column(self) -> None:
        if self._adjusting:
            return
        avail = self.viewport().width()
        if avail <= 0:
            return
        self._adjusting = True
        try:
            other = sum(self.columnWidth(c) for c in range(self.columnCount()) if c != self.flex_col)
            target = max(120, avail - other)
            if self.columnWidth(self.flex_col) != target:
                self.setColumnWidth(self.flex_col, target)
        finally:
            self._adjusting = False

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.fit_flex_column()


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
        self._active_downloads: list[_DownloadWorker] = []
        self._dl_progress: dict[int, dict] = {}  # dl_id -> {name, pct, speed}
        self._dl_seq = 0
        self._suppress_table_signals = False
        self._suppress_right_signals = False

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

        self.btn_profiles = QPushButton(self.t.tr("btn.profiles"))
        self.btn_profiles.clicked.connect(self._on_manage_profiles)
        tb.addWidget(self.btn_profiles)

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
        self.table = _ResponsiveTable(0, 6, flex_col=1)
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

        # Right pane: MO2-style bottom-tabbed live view ([UE4SS] [Paks] [Downloads])
        self.right_tabs = QTabWidget()
        self.right_tabs.setTabPosition(QTabWidget.TabPosition.South)

        # Tab 1: UE4SS ([On, Installed, State])
        self.tab_ue4ss = _ResponsiveTable(0, 3, flex_col=1)
        self.tab_ue4ss.setHorizontalHeaderLabels(["", self.t.tr("tabcol.item"), self.t.tr("tabcol.state")])
        self._style_right_table(self.tab_ue4ss, col_widths={0: (30, True), 1: (180, False), 2: (100, False)})
        self.tab_ue4ss.itemChanged.connect(self._on_ue4ss_item_changed)
        self.tab_ue4ss.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tab_ue4ss.customContextMenuRequested.connect(self._on_ue4ss_context_menu)
        self.right_tabs.addTab(self.tab_ue4ss, self.t.tr("tab.ue4ss"))

        # Tab 2: Paks ([On, Order, Name, Overrides])
        self.tab_paks = _ResponsiveTable(0, 4, flex_col=2)
        self.tab_paks.setHorizontalHeaderLabels(
            ["", self.t.tr("tabcol.load_order"), self.t.tr("col.name"), self.t.tr("tabcol.overrides")]
        )
        self._style_right_table(
            self.tab_paks, col_widths={0: (30, True), 1: (46, False), 2: (180, False), 3: (95, False)}
        )
        self.tab_paks.itemChanged.connect(self._on_pak_item_changed)
        self.tab_paks.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tab_paks.customContextMenuRequested.connect(self._on_paks_context_menu)
        self.right_tabs.addTab(self.tab_paks, self.t.tr("tab.paks"))

        # Tab 3: Downloads ([File, Size, State/Progress])
        self.tab_downloads = _ResponsiveTable(0, 3, flex_col=0)
        self.tab_downloads.setHorizontalHeaderLabels(
            [self.t.tr("tabcol.file"), self.t.tr("tabcol.size"), self.t.tr("tabcol.state")]
        )
        self._style_right_table(self.tab_downloads, col_widths={0: (180, False), 1: (68, False), 2: (135, False)})
        self.tab_downloads.cellDoubleClicked.connect(self._on_download_row_activated)
        self.tab_downloads.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tab_downloads.customContextMenuRequested.connect(self._on_downloads_context_menu)
        self.right_tabs.addTab(self.tab_downloads, self.t.tr("tab.downloads"))
        self.right_tabs.currentChanged.connect(self._on_right_tab_changed)

        splitter.addWidget(self.table)
        splitter.addWidget(self.right_tabs)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.splitterMoved.connect(self._on_splitter_moved)
        self.setCentralWidget(splitter)

    def _on_right_tab_changed(self, _index: int) -> None:
        for tbl in (self.tab_ue4ss, self.tab_paks, self.tab_downloads):
            tbl.clearSelection()
            tbl.setCurrentCell(-1, -1)
            tbl.fit_flex_column()

    def _on_splitter_moved(self, *_args) -> None:
        self.table.fit_flex_column()
        self.tab_ue4ss.fit_flex_column()
        self.tab_paks.fit_flex_column()
        self.tab_downloads.fit_flex_column()

    def _style_right_table(self, tbl: _ResponsiveTable, col_widths: dict[int, tuple[int, bool]]) -> None:
        tbl.setAlternatingRowColors(True)
        tbl.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        tbl.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        tbl.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        tbl.verticalHeader().setVisible(False)
        hdr = tbl.horizontalHeader()
        hdr.setStretchLastSection(False)
        for c, (w, is_fixed) in col_widths.items():
            hdr.setSectionResizeMode(c, QHeaderView.ResizeMode.Fixed if is_fixed else QHeaderView.ResizeMode.Interactive)
            tbl.setColumnWidth(c, w)

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
        unmanaged_tip = self.t.tr("status.unmanaged_tooltip")
        bi_font = _bold_italic_font(self.table.font())
        for idx_u, u in enumerate(unmanaged_list):
            row = base_row + idx_u
            ukey = f"unmanaged:{u.folder}:{u.name}"
            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            chk.setCheckState(Qt.CheckState.Checked)
            chk.setData(Qt.ItemDataRole.UserRole, ukey)
            chk.setToolTip(unmanaged_tip)

            item_name = QTableWidgetItem(u.name)
            item_name.setFont(bi_font)
            item_name.setForeground(QColor("#B0B8C8"))
            item_name.setToolTip(unmanaged_tip)
            item_name.setData(Qt.ItemDataRole.UserRole, ukey)

            item_badge = QTableWidgetItem(u.badge)
            item_badge.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            item_badge.setToolTip(unmanaged_tip)

            item_ver = QTableWidgetItem("-")
            item_ver.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

            item_prio = QTableWidgetItem("-")
            item_prio.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

            item_status = QTableWidgetItem(f"● {self.t.tr('status.unmanaged')}")
            item_status.setFont(bi_font)
            item_status.setForeground(QColor("#7EA8D9"))
            item_status.setToolTip(unmanaged_tip)
            item_status.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

            self.table.setItem(row, 0, chk)
            self.table.setItem(row, 1, item_name)
            self.table.setItem(row, 2, item_badge)
            self.table.setItem(row, 3, item_ver)
            self.table.setItem(row, 4, item_prio)
            self.table.setItem(row, 5, item_status)

        self._suppress_table_signals = False
        self._update_status_bar(len(enabled_set) + len(unmanaged_list), len(mods) + len(unmanaged_list), prof.name)
        self._refresh_right_tabs(mods, manifest)

    def _refresh_right_tabs(self, mods: list[ModRecord], manifest) -> None:
        self._suppress_right_signals = True
        try:
            self._refresh_ue4ss_tab(mods, manifest)
            self._refresh_paks_tab(mods, manifest)
            self._refresh_downloads_tab(mods)
            for tbl in (self.tab_ue4ss, self.tab_paks, self.tab_downloads):
                tbl.clearSelection()
                tbl.setCurrentCell(-1, -1)
        finally:
            self._suppress_right_signals = False

    def _refresh_ue4ss_tab(self, mods: list[ModRecord], manifest) -> None:
        prof = self.store.load_profile()
        enabled_ids = set(prof.enabled_mods)

        # Track ALL managed UE4SS mod names (including disabled/other-profile mods)
        # so a disabled managed mod never gets misclassified as Unmanaged.
        all_managed_lower: set[str] = set()
        # Map ue4ss mod folder name (lower) -> (mod_id, orig_name, store_folder_path, comp_selected)
        managed_ue4ss_map: dict[str, tuple[str, str, Path, bool]] = {}
        for m in mods:
            mdir = self.store.mod_dir(m.id)
            for c in m.components:
                if c.kind == "ue4ss_mod":
                    all_managed_lower.add(c.name.lower())
                    if m.id in enabled_ids:
                        managed_ue4ss_map[c.name.lower()] = (m.id, c.name, mdir / c.store_rel, c.selected)

        unmanaged_tip = self.t.tr("status.unmanaged_tooltip")
        bi_font = _bold_italic_font(self.tab_ue4ss.font())
        keybinds_tip = "Core UE4SS hotkey engine required by Lua mods (cannot be disabled)."

        # Row tuple: (mode, checked, payload, label, state, text_color, state_color, bold_italic, tooltip, folder_path)
        # mode: "kit" (no checkbox), "locked_core" (disabled hard-checked checkbox), "mod" (interactive checkbox)
        rows: list[tuple[str, bool, object, str, str, str, str, bool, str, Optional[Path]]] = []
        if self.game is not None:
            st = detect_mod_base(self.game)
            if st.ue4ss.installed:
                ver = st.ue4ss.version_line or "installed"
                rows.append(("kit", True, None, f"[Modding Kit] UE4SS ({ver})", "Active", "#E2E4E9", "#4CAF7D", False, "", self.game.ue4ss_dir))
            if st.enabler.present:
                rows.append(("kit", True, None, f"[Modding Kit] Pak Enabler ({st.enabler.kind})", "Active", "#E2E4E9", "#4CAF7D", False, "", self.game.binaries_dir))
            if st.bp_loader.kind != "none":
                bp_lbl = "WMM Lua BP Loader" if st.bp_loader.kind == "wmm_lua" else "WFF_BPLoader"
                bp_dir = (self.game.ue4ss_mods_dir / "WMM_BPLoader") if st.bp_loader.kind == "wmm_lua" else self.game.mods_pak_dir
                rows.append(("kit", True, None, f"[Modding Kit] {bp_lbl}", "Active", "#E2E4E9", "#4CAF7D", False, "", bp_dir))

            # Keybinds is always the first mod row: greyed-out hard-checked, bold italic, state 'Core'
            if st.ue4ss.installed or self.game.ue4ss_mods_txt.is_file():
                rows.append(("locked_core", True, None, LAST_UE4SS_MOD, "Core", "#7A808C", "#7A808C", True, keybinds_tip, self.game.ue4ss_mods_dir))

            txt_entries = parse_mods_txt(self.game.ue4ss_mods_txt)
            seen: set[str] = {LAST_UE4SS_MOD.lower()}

            # 1. Managed UE4SS mods from enabled WMM mods (whether currently linked or individually unchecked)
            for low, (mid, orig_name, store_folder, is_sel) in sorted(managed_ue4ss_map.items()):
                seen.add(low)
                live_dir = self.game.ue4ss_mods_dir / orig_name
                folder_path = live_dir if live_dir.exists() else store_folder
                rows.append((
                    "mod",
                    is_sel,
                    ("managed", mid, orig_name),
                    orig_name,
                    "Enabled" if is_sel else "Disabled",
                    "#E2E4E9" if is_sel else "#7A808C",
                    "#4CAF7D" if is_sel else "#7A808C",
                    False,
                    "",
                    folder_path,
                ))

            # 2. Remaining entries from mods.txt (built-in UE4SS core mods or unmanaged user mods)
            for name, en in txt_entries:
                low = name.lower()
                if low in seen or low in all_managed_lower:
                    continue
                seen.add(low)
                mod_path = self.game.ue4ss_mods_dir / name
                if low in _CORE_UE4SS_LOWER:
                    tip = "Built-in UE4SS core component."
                    rows.append(("mod", en, ("unmanaged", name), name, "Core" if en else "Disabled", "#7A808C", "#7A808C", True, tip, mod_path if mod_path.is_dir() else self.game.ue4ss_mods_dir))
                else:
                    rows.append((
                        "mod",
                        en,
                        ("unmanaged", name),
                        name,
                        self.t.tr("status.unmanaged"),
                        "#B0B8C8",
                        "#7EA8D9",
                        True,
                        unmanaged_tip,
                        mod_path if mod_path.is_dir() else self.game.ue4ss_mods_dir,
                    ))

            # 3. Unmanaged folders in ue4ss/Mods/ not listed in mods.txt
            if self.game.ue4ss_mods_dir.is_dir():
                for d in sorted(self.game.ue4ss_mods_dir.iterdir()):
                    low = d.name.lower()
                    if not d.is_dir() or low in seen or low == "shared" or low in _CORE_UE4SS_LOWER:
                        continue
                    if (d / "Scripts" / "main.lua").is_file() or (d / "dlls" / "main.dll").is_file():
                        rows.append((
                            "mod",
                            (d / "enabled.txt").is_file(),
                            ("unmanaged", d.name),
                            d.name,
                            self.t.tr("status.unmanaged"),
                            "#B0B8C8",
                            "#7EA8D9",
                            True,
                            unmanaged_tip,
                            d,
                        ))

        self.tab_ue4ss.setRowCount(len(rows))
        for r, (mode, checked, payload, label, state, txt_col, st_col, is_bi, tip, fpath) in enumerate(rows):
            chk = QTableWidgetItem()
            if mode == "mod":
                chk.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                chk.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
                chk.setData(Qt.ItemDataRole.UserRole, payload)
            elif mode == "locked_core":
                # Greyed-out, hard-checked checkbox (cannot be unchecked)
                chk.setFlags(Qt.ItemFlag.ItemIsUserCheckable)
                chk.setCheckState(Qt.CheckState.Checked)
            else:
                chk.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)

            it_name = QTableWidgetItem(label)
            it_name.setForeground(QColor(txt_col))
            it_name.setData(Qt.ItemDataRole.UserRole, str(fpath) if fpath else "")
            it_st = QTableWidgetItem(state)
            it_st.setForeground(QColor(st_col))
            if is_bi:
                it_name.setFont(bi_font)
                it_st.setFont(bi_font)
            if tip:
                chk.setToolTip(tip)
                it_name.setToolTip(tip)
                it_st.setToolTip(tip)
            self.tab_ue4ss.setItem(r, 0, chk)
            self.tab_ue4ss.setItem(r, 1, it_name)
            self.tab_ue4ss.setItem(r, 2, it_st)

    def _on_ue4ss_item_changed(self, item: QTableWidgetItem) -> None:
        if self._suppress_right_signals or item.column() != 0 or self.game is None:
            return
        payload = item.data(Qt.ItemDataRole.UserRole)
        if not payload:
            return
        procs = running_game_processes(self.plat)
        if procs:
            QMessageBox.warning(self, self.t.tr("app.title"), self.t.tr("err.game_running", procs=", ".join(procs)))
            self.refresh_all()
            return
        want_on = item.checkState() == Qt.CheckState.Checked

        if payload[0] == "managed":
            _, mod_id, orig_name = payload
            mod = self.store.load_mod(mod_id)
            mgr = self._manager()
            if mod is not None and mgr is not None:
                for c in mod.components:
                    if c.kind == "ue4ss_mod" and c.name == orig_name:
                        c.selected = want_on
                self.store.save_mod(mod)
                try:
                    # Re-applying profile removes the junction when unchecked and recreates it when checked
                    mgr.apply_profile()
                    self._log(f"[UE4SS] {orig_name}: {'linked & enabled' if want_on else 'unlinked & disabled'}")
                except DeployError as exc:
                    QMessageBox.warning(self, self.t.tr("app.title"), str(exc))
        else:
            _, mod_name = payload
            write_mods_txt(self.game.ue4ss_mods_txt, [(str(mod_name), want_on)], preserve_unmanaged=True)
            self._log(f"[UE4SS] {mod_name} : {1 if want_on else 0}")
        self.refresh_all()

    def _find_mod_config_file(self, folder: Path) -> Optional[Path]:
        if not folder.is_dir():
            return None
        for name in ("config.lua", "config.ini", "config.json", "settings.ini", "UE4SS-settings.ini"):
            cand = folder / name
            if cand.is_file():
                return cand
        return None

    def _on_ue4ss_context_menu(self, pos) -> None:
        row = self.tab_ue4ss.currentRow()
        if row < 0:
            return
        item = self.tab_ue4ss.item(row, 1)
        if not item:
            return
        raw_path = item.data(Qt.ItemDataRole.UserRole)
        if not raw_path:
            return
        folder = Path(raw_path)
        if not folder.exists():
            return
        menu = QMenu(self)
        act_open = QAction(self.t.tr("ctx.open_folder"), self)
        act_open.triggered.connect(lambda: self._open_path(folder))
        menu.addAction(act_open)

        cfg = self._find_mod_config_file(folder)
        if cfg is not None:
            act_cfg = QAction(self.t.tr("ctx.open_config"), self)
            act_cfg.triggered.connect(lambda: self._open_path(cfg))
            menu.addAction(act_cfg)

        menu.exec(self.tab_ue4ss.viewport().mapToGlobal(pos))

    def _on_paks_context_menu(self, pos) -> None:
        row = self.tab_paks.currentRow()
        if row < 0:
            return
        item = self.tab_paks.item(row, 2)
        if not item:
            return
        raw_path = item.data(Qt.ItemDataRole.UserRole)
        if not raw_path:
            return
        target = Path(raw_path)
        folder = target.parent if target.is_file() else target
        if not folder.exists():
            return
        menu = QMenu(self)
        act_open = QAction(self.t.tr("ctx.open_folder"), self)
        act_open.triggered.connect(lambda: self._open_path(folder))
        menu.addAction(act_open)
        menu.exec(self.tab_paks.viewport().mapToGlobal(pos))

    def _refresh_paks_tab(self, mods: list[ModRecord], manifest) -> None:
        unmanaged_tip = self.t.tr("status.unmanaged_tooltip")
        bi_font = _bold_italic_font(self.tab_paks.font())

        # Map deployed pak filename -> (mod_id, original_comp_name)
        deployed_pak_map: dict[str, tuple[str, str]] = {}
        for e in manifest:
            if e.kind.startswith("pak_") and e.deployed_name.lower().endswith(".pak"):
                deployed_pak_map[e.deployed_name.lower()] = (e.mod_id, e.original_name)

        # Gather all active paks on disk + disabled paks of enabled mods so user can re-enable them
        prof = self.store.load_profile()
        enabled_ids = set(prof.enabled_mods)
        entries: list[tuple[str, str, bool, Optional[tuple[str, str]], bool]] = []
        # (pak_name, folder_lbl, checked, (mod_id, orig_name) or None, is_unmanaged)
        seen_names: set[str] = set()

        if self.game is not None:
            for folder_lbl, pdir in (("~mods", self.game.mods_pak_dir), ("LogicMods", self.game.logic_mods_dir)):
                if pdir.is_dir():
                    for p in sorted(pdir.glob("*.pak")):
                        if p.is_file():
                            low = p.name.lower()
                            seen_names.add(low)
                            owner = deployed_pak_map.get(low)
                            entries.append((p.name, folder_lbl, True, owner, owner is None))

        # Include individually unchecked paks belonging to currently enabled mods
        for m in mods:
            if m.id not in enabled_ids:
                continue
            for c in m.components:
                if c.kind == "pak" and not c.selected and c.name.lower() not in seen_names:
                    entries.append((c.name, c.target_kind, False, (m.id, c.name), False))

        rule = self.store.get_pak_order_rule(self.game.build_id) if self.game else None
        entries.sort(key=lambda item: parse_pak_priority(item[0], item[1], rule).sort_key)

        self.tab_paks.setRowCount(len(entries))
        for r, (pname, folder_lbl, checked, owner, is_unmanaged) in enumerate(entries):
            chk = QTableWidgetItem()
            if owner is not None:
                chk.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                chk.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
                chk.setData(Qt.ItemDataRole.UserRole, owner)
            else:
                chk.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                chk.setCheckState(Qt.CheckState.Checked)

            it_rank = QTableWidgetItem(str(r + 1) if checked else "-")
            it_rank.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

            display = pname if folder_lbl == "~mods" else f"[LogicMods] {pname}"
            it_name = QTableWidgetItem(display)
            if checked and self.game is not None:
                p_folder = self.game.logic_mods_dir if folder_lbl == "LogicMods" else self.game.mods_pak_dir
                it_name.setData(Qt.ItemDataRole.UserRole, str(p_folder / pname))
            elif owner is not None:
                it_name.setData(Qt.ItemDataRole.UserRole, str(self.store.mod_dir(owner[0])))

            if is_unmanaged:
                ov_txt = self.t.tr("status.unmanaged")
                color = "#7EA8D9"
            elif not checked:
                ov_txt = "Disabled"
                color = "#7A808C"
            else:
                ov_txt = "OK"
                color = "#4CAF7D"
                if self.conflict_report and pname in self.conflict_report.per_pak:
                    info = self.conflict_report.per_pak[pname]
                    if info["assets_lost"] > 0:
                        ov_txt = f"wins {info['assets_won']} / loses {info['assets_lost']}"
                        color = "#E5A93C"
                    elif info["assets_won"] > 0:
                        ov_txt = f"wins {info['assets_won']}"
                        color = "#7EA8D9"

            it_ov = QTableWidgetItem(ov_txt)
            it_ov.setForeground(QColor(color))
            if is_unmanaged:
                it_name.setFont(bi_font)
                it_ov.setFont(bi_font)
                for it in (chk, it_rank, it_name, it_ov):
                    it.setToolTip(unmanaged_tip)

            self.tab_paks.setItem(r, 0, chk)
            self.tab_paks.setItem(r, 1, it_rank)
            self.tab_paks.setItem(r, 2, it_name)
            self.tab_paks.setItem(r, 3, it_ov)

    def _on_pak_item_changed(self, item: QTableWidgetItem) -> None:
        if self._suppress_right_signals or item.column() != 0:
            return
        owner = item.data(Qt.ItemDataRole.UserRole)
        if not owner:
            return
        mod_id, orig_name = owner
        want_on = item.checkState() == Qt.CheckState.Checked
        mod = self.store.load_mod(mod_id)
        mgr = self._manager()
        if mod is None or mgr is None:
            return
        for c in mod.components:
            if c.kind == "pak" and c.name == orig_name:
                c.selected = want_on
        self.store.save_mod(mod)
        try:
            mgr.apply_profile()
            self._log(f"[Pak] {orig_name}: {'enabled' if want_on else 'disabled'}")
        except DeployError as exc:
            QMessageBox.warning(self, self.t.tr("app.title"), str(exc))
        self.refresh_all()

    def _refresh_downloads_tab(self, mods: list[ModRecord]) -> None:
        settings = self.store.load_settings()
        mods_dir_raw = settings.get("mods_archive_dir", "")
        dl_dir = Path(mods_dir_raw) if mods_dir_raw else (self.package_root / "mods")

        installed_sources = {Path(m.source).name.lower() for m in mods if m.source}
        archives: list[Path] = []
        if dl_dir.is_dir():
            for p in sorted(dl_dir.iterdir(), key=lambda x: x.stat().st_mtime if x.is_file() else 0, reverse=True):
                if p.is_file() and p.suffix.lower() in (".zip", ".7z", ".rar", ".pak"):
                    archives.append(p)

        active_items = list(self._dl_progress.items())
        self.tab_downloads.clearContents()
        self.tab_downloads.setRowCount(len(active_items) + len(archives))

        # Active downloads with live progress bar + speed at top
        for r, (dl_id, info) in enumerate(active_items):
            it_file = QTableWidgetItem(info.get("name") or "Downloading...")
            it_spd = QTableWidgetItem(info.get("speed") or "")
            it_spd.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.tab_downloads.setItem(r, 0, it_file)
            self.tab_downloads.setItem(r, 1, it_spd)
            bar = QProgressBar()
            pct = info.get("pct", -1)
            if pct < 0:
                bar.setRange(0, 0)  # indeterminate
                bar.setFormat("Downloading...")
            else:
                bar.setRange(0, 100)
                bar.setValue(pct)
                bar.setFormat(f"{pct}%")
            bar.setTextVisible(True)
            bar.setFixedHeight(20)
            self.tab_downloads.setCellWidget(r, 2, bar)

        offset = len(active_items)
        for idx, arc in enumerate(archives):
            r = offset + idx
            self.tab_downloads.removeCellWidget(r, 2)
            is_inst = arc.name.lower() in installed_sources
            size_mb = arc.stat().st_size / (1024 * 1024)
            size_str = f"{size_mb:.1f} MB" if size_mb >= 0.1 else f"{arc.stat().st_size // 1024} KB"
            it_file = QTableWidgetItem(arc.name)
            it_file.setData(Qt.ItemDataRole.UserRole, str(arc))
            it_size = QTableWidgetItem(size_str)
            it_size.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            it_state = QTableWidgetItem("✔ Installed" if is_inst else "Unpacked: No")
            it_state.setForeground(QColor("#4CAF7D" if is_inst else "#AEB3BE"))
            self.tab_downloads.setItem(r, 0, it_file)
            self.tab_downloads.setItem(r, 1, it_size)
            self.tab_downloads.setItem(r, 2, it_state)

    def _on_download_row_activated(self, row: int, _col: int) -> None:
        item = self.tab_downloads.item(row, 0)
        if item and item.data(Qt.ItemDataRole.UserRole):
            p = Path(item.data(Qt.ItemDataRole.UserRole))
            if p.is_file():
                self._install_path(p)

    def _on_downloads_context_menu(self, pos) -> None:
        row = self.tab_downloads.currentRow()
        if row < 0:
            return
        item = self.tab_downloads.item(row, 0)
        if not item or not item.data(Qt.ItemDataRole.UserRole):
            return
        p = Path(item.data(Qt.ItemDataRole.UserRole))
        menu = QMenu(self)
        act_inst = QAction(self.t.tr("ctx.install_download"), self)
        act_open = QAction(self.t.tr("ctx.open_folder"), self)
        act_del = QAction(self.t.tr("ctx.delete_download"), self)
        act_inst.triggered.connect(lambda: self._install_path(p))
        act_open.triggered.connect(lambda: self._open_path(p.parent))
        act_del.triggered.connect(lambda: self._delete_download(p))
        menu.addAction(act_inst)
        menu.addAction(act_open)
        menu.addSeparator()
        menu.addAction(act_del)
        menu.exec(self.tab_downloads.viewport().mapToGlobal(pos))

    def _delete_download(self, archive_path: Path) -> None:
        ans = QMessageBox.question(
            self,
            self.t.tr("app.title"),
            self.t.tr("ctx.confirm_delete_download", name=archive_path.name),
        )
        if ans != QMessageBox.StandardButton.Yes:
            return
        try:
            archive_path.unlink(missing_ok=True)
            self._log(f"Deleted download: {archive_path.name}")
        except OSError as exc:
            QMessageBox.warning(self, self.t.tr("app.title"), str(exc))
        self.refresh_all()

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
        # Right pane now shows live installed UE4SS / Paks / Downloads tabs;
        # per-mod details are shown via right-click -> Details...
        pass

    def _show_mod_details(self, mod_id: str) -> None:
        if mod_id in self.unmanaged_mods:
            u = self.unmanaged_mods[mod_id]
            lines = [
                f"Name: {u.name} [{u.badge}] ({self.t.tr('status.unmanaged')})",
                f"Path: {u.path}",
                f"Folder: {u.folder}",
            ]
            LogDialog(lines, u.name, parent=self).exec()
            return
        mod = self.store.load_mod(mod_id)
        if mod is None:
            return
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
                        lines.append(f"      wins {info['assets_won']} / loses {info['assets_lost']}")
        LogDialog(lines, f"{mod.name} [{mod.badge}]", parent=self).exec()

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

    def _install_path(self, path: Path, name: Optional[str] = None, version: str = "") -> None:
        mgr = self._manager()
        if mgr is None:
            QMessageBox.warning(self, self.t.tr("app.title"), self.t.tr("cli.game_not_found"))
            return
        try:
            rec = mgr.add_mod(path, name=name, version=version)
            self._log(f"Added {rec.name} ({rec.id}) [{rec.badge}]")
        except DeployError as exc:
            if "loader DLL" in str(exc):
                ans = QMessageBox.question(self, self.t.tr("app.title"), str(exc))
                if ans == QMessageBox.StandardButton.Yes:
                    rec = mgr.add_mod(path, name=name, version=version, allow_loaders=True)
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
        """Download and install a mod asynchronously from an nxm:// or https:// URL (non-blocking UI)."""
        uri = uri.strip()
        if not uri.lower().startswith(("nxm://", "http://", "https://")):
            QMessageBox.warning(self, self.t.tr("app.title"), self.t.tr("download.invalid_url"))
            return

        settings = self.store.load_settings()
        raw_key = settings.get("nexus_api_key", "")
        api_key = self.plat.unprotect_secret(raw_key) if raw_key else ""
        if uri.lower().startswith("nxm://") and not api_key:
            QMessageBox.warning(self, self.t.tr("app.title"), self.t.tr("download.need_api_key"))
            return

        mods_dir_raw = settings.get("mods_archive_dir", "")
        dl_dir = Path(mods_dir_raw) if mods_dir_raw else self.store.downloads_dir
        auto_extract = bool(settings.get("auto_extract", False))

        self._dl_seq += 1
        dl_id = self._dl_seq
        self._dl_progress[dl_id] = {"name": uri.split("?")[0], "pct": -1, "speed": ""}
        self.right_tabs.setCurrentWidget(self.tab_downloads)
        self._refresh_downloads_tab(self.store.list_mods())
        self.statusBar().showMessage(self.t.tr("download.in_progress", name=uri.split("?")[0]))
        self._log(f"Downloading: {uri.split('?')[0]}")

        worker = _DownloadWorker(dl_id, uri, api_key, dl_dir, parent=self)
        self._active_downloads.append(worker)

        def _on_prog(did: int, fname: str, pct: int, spd: str) -> None:
            if did in self._dl_progress:
                self._dl_progress[did] = {"name": fname, "pct": pct, "speed": spd}
                self._refresh_downloads_tab(self.store.list_mods())
                self.statusBar().showMessage(f"Downloading {fname} ({pct}% — {spd})" if pct >= 0 else f"Downloading {fname} ({spd})")

        def _on_ok(did: int, dl_path_str: str, mod_name, mod_ver: str) -> None:
            self._dl_progress.pop(did, None)
            if worker in self._active_downloads:
                self._active_downloads.remove(worker)
            dl_path = Path(dl_path_str)
            self._log(f"Downloaded: {dl_path.name}")
            if auto_extract:
                self._install_path(dl_path, name=mod_name, version=mod_ver)
            else:
                self.refresh_all()
            self.right_tabs.setCurrentWidget(self.tab_downloads)

        def _on_fail(did: int, err_msg: str) -> None:
            self._dl_progress.pop(did, None)
            if worker in self._active_downloads:
                self._active_downloads.remove(worker)
            self.refresh_all()
            QMessageBox.warning(self, self.t.tr("app.title"), err_msg)

        worker.progress.connect(_on_prog)
        worker.finished_ok.connect(_on_ok)
        worker.failed.connect(_on_fail)
        worker.start()

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

    def _on_manage_profiles(self) -> None:
        dlg = ProfilesDialog(self.store, self.t, parent=self)
        dlg.exec()
        if dlg.active_profile_changed:
            mgr = self._manager()
            if mgr is not None:
                try:
                    mgr.apply_profile()
                except DeployError as exc:
                    QMessageBox.warning(self, self.t.tr("app.title"), str(exc))
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

        act_details = QAction(self.t.tr("ctx.details"), self)
        act_open = QAction(self.t.tr("ctx.open_folder"), self)
        act_up = QAction(self.t.tr("ctx.move_up"), self)
        act_down = QAction(self.t.tr("ctx.move_down"), self)
        act_update = QAction(self.t.tr("btn.update"), self)
        act_uninst = QAction(self.t.tr("btn.uninstall"), self)

        act_details.triggered.connect(lambda: self._show_mod_details(mod_id))
        act_open.triggered.connect(lambda: self._ctx_open_folder(mod_id))
        act_up.triggered.connect(lambda: self._ctx_move_pak(mod_id, -1))
        act_down.triggered.connect(lambda: self._ctx_move_pak(mod_id, 1))
        act_update.triggered.connect(lambda: self._ctx_update(mod_id))
        act_uninst.triggered.connect(lambda: self._ctx_uninstall(mod_id))

        menu.addAction(act_details)
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
