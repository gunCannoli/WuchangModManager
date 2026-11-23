"""Main window for Wuchang Mod Manager (§2.1)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QEvent, QRect, QSize, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QAction, QBrush, QColor, QDragEnterEvent, QDropEvent, QFont, QPalette
from PySide6.QtNetwork import QLocalServer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QSplitter,
    QStatusBar,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from ..applog import AppLog, LogEntry
from ..base import detect_mod_base
from ..cli import NXM_IPC_SERVER_NAME
from ..deploy import DeployError, Manager, UnmanagedMod, scan_unmanaged_mods
from ..extract import ExtractError
from ..game import GameInstall, detect_all_installs, running_game_processes, validate_game_root
from ..i18n import Translator
from ..nexus import (
    NexusClient,
    NexusError,
    nexus_mod_url,
    parse_nexus_archive_name,
    parse_nexus_mod_id_from_filename,
    parse_nxm_uri,
    is_update_available,
    latest_main_file,
    rank_update_targets,
    update_mod_preserving_configs,
    version_key,
)
from ..paks import PakConflictReport, parse_pak_priority, scan_folder_conflicts
from ..platform import get_platform
from ..store import ModRecord, Profile, Store
from ..ue4ss import BASE_UE4SS_MODS, LAST_UE4SS_MOD, parse_mods_txt, write_mods_txt
from .details_dialog import ModDetailsDialog
from .icons import mdi_icon
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
    finished_ok = Signal(int, str, object, str, object)  # dl_id, path, mod_name_or_None, version, nexus_mod_id_or_None
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
                file_name: Optional[str] = None
                try:
                    info = client.get_mod_info(link.mod_id, domain=link.domain)
                    mod_name = info.get("name") or None
                    mod_ver = str(info.get("version") or "")
                except Exception:
                    pass
                try:
                    finfo = client.get_mod_file_info(link.mod_id, link.file_id, domain=link.domain)
                    file_name = finfo.get("file_name") or None
                    if not mod_ver and finfo.get("version"):
                        mod_ver = str(finfo.get("version"))
                except Exception:
                    pass
                dl_path = client.download_url(
                    links[0]["URI"], self.dl_dir, filename=file_name, progress_cb=_on_prog
                )
                self.finished_ok.emit(self.dl_id, str(dl_path), mod_name, mod_ver, link.mod_id)
            else:
                client = NexusClient(self.api_key or "public")
                dl_path = client.download_url(self.uri, self.dl_dir, progress_cb=_on_prog)
                self.finished_ok.emit(self.dl_id, str(dl_path), None, "", parse_nexus_mod_id_from_filename(dl_path.name))
        except Exception as exc:
            self.failed.emit(self.dl_id, str(exc))


def _ordered_mods(mods: list[ModRecord], prof: Profile) -> list[ModRecord]:
    """Managed mods in the profile's mod order (unlisted mods follow in store order)."""
    idx = {mid: i for i, mid in enumerate(prof.mod_order)}
    return sorted(mods, key=lambda m: idx.get(m.id, len(idx)))  # stable: keeps store order for ties


class _RowDelegate(QStyledItemDelegate):
    """Selected rows only change background (text keeps its own colour); checkboxes are centred."""

    def initStyleOption(self, option, index) -> None:  # noqa: N802
        super().initStyleOption(option, index)
        fg = index.data(Qt.ItemDataRole.ForegroundRole)
        if fg is not None:
            color = fg.color() if isinstance(fg, QBrush) else QColor(fg)
        else:
            color = option.palette.color(QPalette.ColorRole.Text)
        option.palette.setBrush(QPalette.ColorGroup.All, QPalette.ColorRole.HighlightedText, QBrush(color))

    def _check_rect(self, option):
        style = option.widget.style() if option.widget else QApplication.style()
        base = style.subElementRect(QStyle.SubElement.SE_ItemViewItemCheckIndicator, option, option.widget)
        r = QRect(base)
        r.moveCenter(option.rect.center())
        return r

    def paint(self, painter, option, index) -> None:
        if index.column() != 0 or index.data(Qt.ItemDataRole.CheckStateRole) is None:
            super().paint(painter, option, index)
            return
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        checked = opt.checkState == Qt.CheckState.Checked
        opt.features &= ~QStyleOptionViewItem.ViewItemFeature.HasCheckIndicator
        style = opt.widget.style() if opt.widget else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, opt.widget)
        ind = QStyleOptionViewItem(option)
        self.initStyleOption(ind, index)
        ind.rect = self._check_rect(ind)
        ind.state = (ind.state & ~(QStyle.StateFlag.State_On | QStyle.StateFlag.State_Off)) | (
            QStyle.StateFlag.State_On if checked else QStyle.StateFlag.State_Off
        )
        style.drawPrimitive(QStyle.PrimitiveElement.PE_IndicatorItemViewItemCheck, ind, painter, opt.widget)

    def editorEvent(self, event, model, option, index) -> bool:  # noqa: N802
        if index.column() == 0 and index.data(Qt.ItemDataRole.CheckStateRole) is not None:
            flags = index.flags()
            if event.type() in (QEvent.Type.MouseButtonRelease, QEvent.Type.MouseButtonDblClick):
                if event.type() == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton:
                    if (flags & Qt.ItemFlag.ItemIsUserCheckable) and (flags & Qt.ItemFlag.ItemIsEnabled):
                        cur = index.data(Qt.ItemDataRole.CheckStateRole)  # int or enum depending on the binding
                        is_on = cur == Qt.CheckState.Checked or cur == Qt.CheckState.Checked.value
                        new = Qt.CheckState.Unchecked if is_on else Qt.CheckState.Checked
                        model.setData(index, new, Qt.ItemDataRole.CheckStateRole)
                return True
        return super().editorEvent(event, model, option, index)


class _UpdateCheckWorker(QThread):
    """Asks Nexus for the newest main file of each managed mod (one files.json call per mod)."""

    done = Signal(dict, str)  # {mod_id: {"v": version, "f": file_id}}, error text ("" if none)

    def __init__(self, api_key: str, targets: list[tuple[str, int]], parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.api_key = api_key
        self.targets = targets

    def run(self) -> None:
        result: dict[str, dict] = {}
        err = ""
        client = NexusClient(self.api_key)
        for mod_id, nexus_id in self.targets:
            try:
                latest = latest_main_file(client.get_mod_files(nexus_id))
            except Exception as exc:  # noqa: BLE001
                err = str(exc)
                break  # rate limit or network problem: stop, keep what we have
            if latest is not None:
                result[mod_id] = {"v": str(latest.get("version") or ""), "f": int(latest.get("file_id", 0))}
        self.done.emit(result, err)


class _ResponsiveTable(QTableWidget):
    """Table where all data columns are user-resizable with zero dead space.

    Resize rules:
    - Shrinking any column expands the biggest other resizable column to fill the space.
    - Shrinking the biggest column expands the second-biggest resizable column.
    - On ties, the State/Status column (last column) is expanded.
    """

    rowMoved = Signal(int, int)  # (source row, destination row) after a drag & drop
    spacePressed = Signal()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Space and event.modifiers() == Qt.KeyboardModifier.NoModifier:
            self.spacePressed.emit()
            event.accept()
            return
        super().keyPressEvent(event)

    def enable_row_drag(self) -> None:
        """Allow dragging a row onto another row; the owner reorders its own model on rowMoved."""
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802
        if self.dragDropMode() != QAbstractItemView.DragDropMode.InternalMove or event.source() is not self:
            super().dropEvent(event)
            return
        src = self.currentRow()
        dst = self.rowAt(int(event.position().y()))
        if dst < 0:
            dst = self.rowCount() - 1
        # Never let Qt move/delete the cells itself; the owner rebuilds the table.
        event.setDropAction(Qt.DropAction.IgnoreAction)
        event.accept()
        if src >= 0 and dst >= 0 and src != dst:
            QTimer.singleShot(0, lambda: self.rowMoved.emit(src, dst))

    def __init__(self, rows: int, cols: int, flex_col: int = 1, parent: Optional[QWidget] = None):
        super().__init__(rows, cols, parent)
        self.flex_col = flex_col
        self.status_col = cols - 1
        self._adjusting = False
        self.horizontalHeader().sectionResized.connect(self._on_col_resized)
        self.setItemDelegate(_RowDelegate(self))

    def _interactive_cols(self) -> list[int]:
        hdr = self.horizontalHeader()
        return [
            c
            for c in range(self.columnCount())
            if hdr.sectionResizeMode(c) != QHeaderView.ResizeMode.Fixed
        ]

    def _min_col_width(self, col: int) -> int:
        return 44 if col != self.flex_col else 80

    def _pick_absorber(self, exclude_col: Optional[int] = None) -> Optional[int]:
        cands = [c for c in self._interactive_cols() if c != exclude_col]
        if not cands:
            return None
        max_w = max(self.columnWidth(c) for c in cands)
        tied = [c for c in cands if self.columnWidth(c) == max_w]
        if len(tied) > 1 and self.status_col in tied:
            return self.status_col
        if self.flex_col in tied:
            return self.flex_col
        return tied[0]

    def _on_col_resized(self, logical_index: int, _old: int, _new: int) -> None:
        if self._adjusting:
            return
        avail = self.viewport().width()
        if avail <= 0:
            return
        total = sum(self.columnWidth(c) for c in range(self.columnCount()))
        gap = avail - total
        if gap == 0:
            return
        absorber = self._pick_absorber(exclude_col=logical_index)
        if absorber is None:
            return
        self._adjusting = True
        try:
            cur_w = self.columnWidth(absorber)
            min_w = self._min_col_width(absorber)
            if gap > 0:
                self.setColumnWidth(absorber, cur_w + gap)
            else:
                shrink = min(-gap, max(0, cur_w - min_w))
                if shrink > 0:
                    self.setColumnWidth(absorber, cur_w - shrink)
        finally:
            self._adjusting = False

    def fit_flex_column(self) -> None:
        if self._adjusting:
            return
        avail = self.viewport().width()
        if avail <= 0:
            return
        total = sum(self.columnWidth(c) for c in range(self.columnCount()))
        gap = avail - total
        if gap == 0:
            return
        self._adjusting = True
        try:
            if gap > 0:
                absorber = self._pick_absorber(exclude_col=None)
                if absorber is not None:
                    self.setColumnWidth(absorber, self.columnWidth(absorber) + gap)
            else:
                rem = -gap
                # Shrink from largest interactive column down to min width so columns stay flush
                ordered = sorted(
                    self._interactive_cols(),
                    key=lambda c: (self.columnWidth(c), 1 if c == self.status_col else 0),
                    reverse=True,
                )
                for col in ordered:
                    if rem <= 0:
                        break
                    cur_w = self.columnWidth(col)
                    min_w = self._min_col_width(col)
                    take = min(rem, max(0, cur_w - min_w))
                    if take > 0:
                        self.setColumnWidth(col, cur_w - take)
                        rem -= take
        finally:
            self._adjusting = False

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.fit_flex_column()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        QTimer.singleShot(0, self.fit_flex_column)


class LogDialog(QDialog):
    """Log viewer: one row per event (Time | Level | Event); right-click copies a line or the whole log."""

    def __init__(
        self,
        entries: list[LogEntry],
        title: str,
        parent: Optional[QWidget] = None,
        translator: Optional[Translator] = None,
    ):
        super().__init__(parent)
        self._entries = list(entries)
        self._tr = translator.tr if translator is not None else (lambda k, **kw: k)
        self.setWindowTitle(title)
        self.resize(900, 460)
        lay = QVBoxLayout(self)
        self.view = QTableWidget(len(self._entries), 3)
        self.view.setHorizontalHeaderLabels(
            [self._tr("log.col.time"), self._tr("log.col.level"), self._tr("log.col.event")]
        )
        self.view.verticalHeader().setVisible(False)
        self.view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.view.setShowGrid(False)
        self.view.setWordWrap(True)
        hh = self.view.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        for r, e in enumerate(self._entries):
            self.view.setItem(r, 0, QTableWidgetItem(e.stamp))
            lvl = QTableWidgetItem(e.level)
            if e.level in ("ERROR", "WARN"):
                lvl.setForeground(QColor("#E5646E" if e.level == "ERROR" else "#E0B050"))
            self.view.setItem(r, 1, lvl)
            self.view.setItem(r, 2, QTableWidgetItem(e.message))
        self.view.resizeRowsToContents()
        self.view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self._on_menu)
        lay.addWidget(self.view)
        if self._entries:
            self.view.scrollToBottom()

    def _selected_rows(self) -> list[int]:
        return sorted({i.row() for i in self.view.selectedIndexes()})

    def _on_menu(self, pos) -> None:
        row = self.view.rowAt(pos.y())
        if row >= 0 and row not in self._selected_rows():
            self.view.selectRow(row)
        menu = QMenu(self)
        rows = self._selected_rows()
        if rows:
            act = QAction(self._tr("log.copy_line"), self)
            act.triggered.connect(
                lambda: QApplication.clipboard().setText("\n".join(self._entries[r].line() for r in rows))
            )
            menu.addAction(act)
        act_all = QAction(self._tr("log.copy_all"), self)
        act_all.triggered.connect(lambda: QApplication.clipboard().setText("\n".join(e.line() for e in self._entries)))
        menu.addAction(act_all)
        menu.exec(self.view.viewport().mapToGlobal(pos))


class _GameLockDialog(QDialog):
    """Centered WindowModal overlay locking WMM UI while the game is running (background downloads keep running)."""

    def __init__(self, translator: Translator, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle(translator.tr("app.title"))
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.setWindowFlags(
            Qt.WindowType.Dialog
            | Qt.WindowType.CustomizeWindowHint
            | Qt.WindowType.WindowTitleHint
        )
        self.setFixedWidth(400)
        lay = QVBoxLayout(self)
        lay.setSpacing(12)
        lay.setContentsMargins(20, 20, 20, 18)

        lbl_title = QLabel(f"<b>{translator.tr('lock.title')}</b>")
        lbl_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl_title.setWordWrap(True)
        lay.addWidget(lbl_title)

        lbl_body = QLabel(translator.tr("lock.body"))
        lbl_body.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl_body.setWordWrap(True)
        lbl_body.setStyleSheet("color: #AEB3BE;")
        lay.addWidget(lbl_body)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        self.btn_unlock = QPushButton(translator.tr("btn.unlock"))
        self.btn_unlock.setFixedWidth(100)
        self.btn_unlock.setProperty("accent", True)
        self.btn_unlock.clicked.connect(self.accept)
        btn_row.addWidget(self.btn_unlock)
        btn_row.addStretch(1)
        lay.addLayout(btn_row)

    def reject(self) -> None:
        # Ignore Escape while locked; user must close the game or click Unlock
        pass


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
        self.applog = AppLog(self.store.logs_dir / "wmm.log")
        self._install_excepthook()
        self.conflict_report: Optional[PakConflictReport] = None
        self.unmanaged_mods: dict[str, UnmanagedMod] = {}
        self._active_downloads: list[_DownloadWorker] = []
        self._dl_progress: dict[int, dict] = {}  # dl_id -> {name, pct, speed}
        self._dl_seq = 0
        self._suppress_table_signals = False
        self._suppress_right_signals = False
        self._sort_col: Optional[int] = None  # None = default (profile order); else left-table column
        self._sort_desc = False
        self._pak_rows: list[tuple[Optional[tuple[str, str]], str, bool]] = []
        self._update_cache: dict[str, dict] = dict(settings.get("update_cache", {}).get("latest", {}))
        self._update_worker: Optional[_UpdateCheckWorker] = None
        self._game_unlocked = False
        self._lock_dialog: Optional[_GameLockDialog] = None

        self.setWindowTitle(self.t.tr("app.title"))
        self.resize(980, 620)
        self.setAcceptDrops(True)

        self._build_toolbar()
        self._build_body()
        self.setStatusBar(QStatusBar())
        self._start_nxm_ipc_server()

        if run_onboarding_if_needed and (self.game is None or not settings.get("onboarded", False)):
            self._open_onboarding()

        if self.game is not None:
            self._log(f"Game: {self.game.root} ({self.game.launcher}, build {self.game.build_id})")
        self.refresh_all()

        self._game_poll_timer = QTimer(self)
        self._game_poll_timer.setInterval(2000)
        self._game_poll_timer.timeout.connect(self._poll_game_running)
        self._game_poll_timer.start()
        self._poll_game_running()
        QTimer.singleShot(1500, self._start_update_check)

    def _start_update_check(self, force: bool = False) -> None:
        """Background Nexus update check (needs an API key; cached 6 h unless forced)."""
        if self._update_worker is not None:
            return
        settings = self.store.load_settings()
        raw_key = settings.get("nexus_api_key", "")
        api_key = self.plat.unprotect_secret(raw_key) if raw_key else ""
        if not api_key:
            return
        age = time.time() - float(settings.get("update_cache", {}).get("ts", 0))
        if not force and age < 6 * 3600:
            return
        targets = []
        for m in self.store.list_mods():
            nid = self._resolve_nexus_mod_id(m)
            if nid:
                targets.append((m.id, nid))
        if not targets:
            return
        worker = _UpdateCheckWorker(api_key, targets, parent=self)
        worker.done.connect(self._on_update_check_done)
        self._update_worker = worker
        worker.start()

    def _on_update_check_done(self, result: dict, err: str) -> None:
        self._update_worker = None
        if err:
            self._log(f"[Nexus] update check stopped: {err}")
        if result:
            self._update_cache.update(result)
            settings = self.store.load_settings()
            settings["update_cache"] = {"ts": time.time(), "latest": self._update_cache}
            self.store.save_settings(settings)
            self.refresh_all()

    def _on_refresh_clicked(self) -> None:
        self.refresh_all()
        self._start_update_check(force=True)

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
        if hasattr(self, "_game_poll_timer"):
            self._game_poll_timer.stop()
        if self._lock_dialog is not None:
            self._lock_dialog.hide()
            self._lock_dialog.deleteLater()
            self._lock_dialog = None
        if hasattr(self, "_ipc_server") and self._ipc_server.isListening():
            self._ipc_server.close()
        super().closeEvent(event)

    def _poll_game_running(self) -> None:
        running = bool(running_game_processes(self.plat))
        if running and not self._game_unlocked:
            if self._lock_dialog is None:
                dlg = _GameLockDialog(self.t, parent=self)
                dlg.accepted.connect(self._on_manual_unlock)
                self._lock_dialog = dlg
                dlg.show()
        elif not running:
            self._game_unlocked = False
            if self._lock_dialog is not None:
                dlg = self._lock_dialog
                self._lock_dialog = None
                dlg.hide()
                dlg.deleteLater()

    def _on_manual_unlock(self) -> None:
        self._game_unlocked = True
        if self._lock_dialog is not None:
            dlg = self._lock_dialog
            self._lock_dialog = None
            dlg.deleteLater()
        self._log("WMM manually unlocked while game is running.")

    def _notify_restart_if_game_running(self) -> None:
        if running_game_processes(self.plat):
            QMessageBox.information(
                self,
                self.t.tr("app.title"),
                self.t.tr("lock.restart_required"),
            )

    def _log(self, msg: str, level: str = "INFO") -> None:
        self.applog.add(msg, level)

    def _install_excepthook(self) -> None:
        """Route uncaught exceptions in Qt slots into the log instead of failing silently."""
        import traceback

        prev = sys.excepthook

        def hook(exc_type, exc, tb) -> None:
            try:
                self._log("Unhandled error: " + "".join(traceback.format_exception(exc_type, exc, tb)).strip(), "ERROR")
            except Exception:  # noqa: BLE001
                pass
            prev(exc_type, exc, tb)

        sys.excepthook = hook

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
        return Manager(
            self.store,
            self.game,
            platform=self.plat,
            tools_dir=self.package_root / "tools",
            allow_while_running=self._game_unlocked,
            log=self._log,
        )

    # --- UI construction ---

    def _build_toolbar(self) -> None:
        tb = QToolBar()
        tb.setMovable(False)
        tb.setIconSize(QSize(20, 20))
        self.addToolBar(tb)

        def icon_button(icon: str, tip: str, slot, accent: bool = False) -> QPushButton:
            b = QPushButton()
            b.setIcon(mdi_icon(icon))
            b.setIconSize(QSize(20, 20))
            b.setToolTip(tip)
            b.setFixedSize(36, 30)
            if accent:
                b.setProperty("accent", True)
            b.clicked.connect(slot)
            tb.addWidget(b)
            return b

        self.btn_refresh = icon_button("refresh", self.t.tr("btn.refresh.tip"), self._on_refresh_clicked)
        self.btn_add = icon_button("plus", self.t.tr("btn.add"), self._on_add)
        self.btn_play = icon_button("play", self.t.tr("btn.play"), self._on_play, accent=True)

        tb.addSeparator()

        self.combo_profile = QComboBox()
        self.combo_profile.setMinimumWidth(140)
        self.combo_profile.currentIndexChanged.connect(self._on_profile_changed)
        tb.addWidget(self.combo_profile)

        self.btn_profiles = icon_button("account", self.t.tr("btn.profiles"), self._on_manage_profiles)

        tb.addSeparator()
        self.edit_filter = QLineEdit()
        self.edit_filter.setPlaceholderText(self.t.tr("filter.placeholder"))
        self.edit_filter.setToolTip(self.t.tr("filter.tip"))
        self.edit_filter.setClearButtonEnabled(True)
        self.edit_filter.setMinimumWidth(120)
        self.edit_filter.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.edit_filter.textChanged.connect(self._apply_filter)
        tb.addWidget(self.edit_filter)
        tb.addSeparator()

        self.btn_log = icon_button("log", self.t.tr("btn.log"), self._on_show_log)
        self.btn_settings = icon_button("cog", self.t.tr("btn.settings"), self._on_settings)

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
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)  # shift / ctrl
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._on_table_context_menu)
        self.table.itemChanged.connect(self._on_item_changed)
        self.table.itemSelectionChanged.connect(self._on_selection_changed)
        self.table.enable_row_drag()
        self.table.rowMoved.connect(self._on_mod_row_moved)
        self.table.cellDoubleClicked.connect(self._on_mod_row_activated)
        self.table.spacePressed.connect(self._on_mods_space)

        hdr = self.table.horizontalHeader()
        hdr.setStretchLastSection(False)
        hdr.setSectionsClickable(True)
        hdr.setHighlightSections(False)
        hdr.sectionClicked.connect(self._on_header_clicked)
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(0, 32)
        for col, width in ((1, 230), (2, 85), (3, 90), (4, 65), (5, 95)):
            hdr.setSectionResizeMode(col, QHeaderView.ResizeMode.Interactive)
            self.table.setColumnWidth(col, width)

        # Right pane: MO2-style bottom-tabbed live view ([UE4SS] [Paks] [Downloads])
        self.right_tabs = QTabWidget()
        self.right_tabs.setTabPosition(QTabWidget.TabPosition.South)

        # Tab 1: UE4SS ([On, Mod name, State])
        self.tab_ue4ss = _ResponsiveTable(0, 3, flex_col=1)
        self.tab_ue4ss.setHorizontalHeaderLabels(["", self.t.tr("tabcol.item"), self.t.tr("tabcol.state")])
        self._style_right_table(self.tab_ue4ss, col_widths={0: (30, True), 1: (180, False), 2: (100, False)})
        self.tab_ue4ss.itemChanged.connect(self._on_ue4ss_item_changed)
        self.tab_ue4ss.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tab_ue4ss.customContextMenuRequested.connect(self._on_ue4ss_context_menu)
        self.tab_ue4ss.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tab_ue4ss.spacePressed.connect(self._on_ue4ss_space)
        self.right_tabs.addTab(self.tab_ue4ss, self.t.tr("tab.ue4ss"))

        # Tab 2: Paks ([On, Mod name, Order, State])
        self.tab_paks = _ResponsiveTable(0, 4, flex_col=1)
        self.tab_paks.setHorizontalHeaderLabels(
            ["", self.t.tr("col.name"), self.t.tr("tabcol.load_order"), self.t.tr("tabcol.state")]
        )
        self._style_right_table(
            self.tab_paks, col_widths={0: (30, True), 1: (180, False), 2: (46, False), 3: (95, False)}
        )
        self.tab_paks.itemChanged.connect(self._on_pak_item_changed)
        self.tab_paks.enable_row_drag()
        self.tab_paks.rowMoved.connect(self._on_pak_row_moved)
        self.tab_paks.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tab_paks.customContextMenuRequested.connect(self._on_paks_context_menu)
        self.tab_paks.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tab_paks.spacePressed.connect(self._on_paks_space)
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
        self.tab_downloads.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
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
        mods = _ordered_mods(self.store.list_mods(), prof)  # true profile order (also feeds the right tabs)
        order_of = {m.id: i for i, m in enumerate(mods)}
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

        status_of = {m.id: self._compute_mod_status(m, deployed_by_mod.get(m.id, [])) for m in mods}
        display_mods = mods
        if self._sort_col is not None:
            col = self._sort_col
            keys = {
                0: lambda m: m.id in enabled_set,
                1: lambda m: m.name.lower(),
                2: lambda m: (m.badge or "").lower(),
                3: lambda m: version_key(m.version or ""),
                4: lambda m: order_of[m.id],
                5: lambda m: status_of[m.id][0],
            }
            display_mods = sorted(mods, key=keys[col], reverse=self._sort_desc)

        self._suppress_table_signals = True
        self.table.setRowCount(len(mods) + len(unmanaged_list))
        for row, mod in enumerate(display_mods):
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
            upd = self._update_cache.get(mod.id)
            if upd and is_update_available(upd.get("v", ""), upd.get("f"), mod.version or "", mod.nexus_file_id):
                item_ver.setForeground(QColor("#F2C94C"))
                item_ver.setIcon(mdi_icon("alert", "#F2C94C"))
                item_ver.setToolTip(self.t.tr("update.available_tip", version=upd.get("v") or "?"))

            # Order (0-based position in the profile's mod order)
            item_prio = QTableWidgetItem(str(order_of[mod.id]))
            item_prio.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

            # Status dot
            dot_text, dot_color = status_of[mod.id]
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
        self._apply_filter()

    def _on_header_clicked(self, col: int) -> None:
        """Click a title: ascending, click again: descending, third click: back to profile order."""
        if self._sort_col != col:
            self._sort_col, self._sort_desc = col, False
        elif not self._sort_desc:
            self._sort_desc = True
        else:
            self._sort_col, self._sort_desc = None, False
        hdr = self.table.horizontalHeader()
        if self._sort_col is None:
            hdr.setSortIndicatorShown(False)
        else:
            hdr.setSortIndicatorShown(True)
            hdr.setSortIndicator(
                col, Qt.SortOrder.DescendingOrder if self._sort_desc else Qt.SortOrder.AscendingOrder
            )
        self.refresh_all()
        self.table.clearSelection()

    def _apply_filter(self, *_args) -> None:
        """Hide rows not matching the filter text: left list by mod name, right tabs by their mod."""
        needle = self.edit_filter.text().strip().lower() if hasattr(self, "edit_filter") else ""
        visible_ids: set[str] = set()
        for r in range(self.table.rowCount()):
            chk, name = self.table.item(r, 0), self.table.item(r, 1)
            show = not needle or (name is not None and needle in name.text().lower())
            self.table.setRowHidden(r, not show)
            if show and chk is not None:
                visible_ids.add(str(chk.data(Qt.ItemDataRole.UserRole)))

        for r in range(self.tab_ue4ss.rowCount()):
            chk, name = self.tab_ue4ss.item(r, 0), self.tab_ue4ss.item(r, 1)
            payload = chk.data(Qt.ItemDataRole.UserRole) if chk is not None else None
            if isinstance(payload, tuple) and len(payload) >= 2 and payload[0] == "managed":
                show = not needle or payload[1] in visible_ids
            else:
                show = not needle or (name is not None and needle in name.text().lower())
            self.tab_ue4ss.setRowHidden(r, not show)

        for r in range(self.tab_paks.rowCount()):
            chk, name = self.tab_paks.item(r, 0), self.tab_paks.item(r, 1)
            owner = chk.data(Qt.ItemDataRole.UserRole) if chk is not None else None
            if isinstance(owner, tuple) and len(owner) >= 2:
                show = not needle or owner[0] in visible_ids
            else:
                show = not needle or (name is not None and needle in name.text().lower())
            self.tab_paks.setRowHidden(r, not show)

        for r in range(self.tab_downloads.rowCount()):
            name = self.tab_downloads.item(r, 0)
            show = not needle or name is None or needle in name.text().lower()
            self.tab_downloads.setRowHidden(r, not show)

    def _managed_order_ids(self) -> list[str]:
        return [m.id for m in _ordered_mods(self.store.list_mods(), self.store.load_profile())]

    def _on_mod_row_moved(self, src: int, dst: int) -> None:
        ids = self._managed_order_ids()
        a, b = self.table.item(src, 0), self.table.item(dst, 0)
        src_id = str(a.data(Qt.ItemDataRole.UserRole)) if a is not None else ""
        dst_id = str(b.data(Qt.ItemDataRole.UserRole)) if b is not None else ""
        if src_id not in ids:
            return
        # Dropping on an unmanaged row sends the mod to the end of the managed order.
        self._move_mod_to(src_id, ids.index(dst_id) if dst_id in ids else len(ids) - 1)

    def _move_mod_to(self, mod_id: str, new_index: int) -> None:
        """Move a managed mod in the left-panel order, then rebuild pak + UE4SS order from it."""
        prof = self.store.load_profile()
        mods = self.store.list_mods()
        ids = [m.id for m in _ordered_mods(mods, prof)]
        if mod_id not in ids:
            return
        new_index = max(0, min(len(ids) - 1, new_index))
        old_index = ids.index(mod_id)
        if new_index == old_index:
            return
        ids.insert(new_index, ids.pop(old_index))
        prof.mod_order = ids
        by_id = {m.id: m for m in mods}
        prof.pak_order = [
            f"{mid}:{c.name}" for mid in ids if mid in by_id for c in by_id[mid].components if c.kind == "pak"
        ]
        prof.ue4ss_order = [
            c.name for mid in ids if mid in by_id for c in by_id[mid].components if c.kind == "ue4ss_mod"
        ]
        self.store.save_profile(prof)
        mgr = self._manager()
        if mgr is not None:
            try:
                mgr.apply_profile(prof)
            except DeployError as exc:
                QMessageBox.warning(self, self.t.tr("app.title"), str(exc))
        self.refresh_all()

    def _ctx_set_mod_order(self, mod_id: str) -> None:
        ids = self._managed_order_ids()
        if mod_id not in ids:
            return
        cur = ids.index(mod_id)
        val, ok = QInputDialog.getInt(
            self, self.t.tr("app.title"), self.t.tr("ctx.set_order_prompt"), cur, 0, len(ids) - 1
        )
        if ok:
            self._move_mod_to(mod_id, val)

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
        keybinds_tip = "Core UE4SS hotkeys (always on)"

        show_core = bool(self.store.load_settings().get("show_ue4ss_core", False))

        # Row tuple: (mode, checked, payload, label, state, text_color, state_color, bold_italic, tooltip, folder_path)
        # mode: "locked_core" (disabled hard-checked checkbox), "mod" (interactive checkbox)
        rows: list[tuple[str, bool, object, str, str, str, str, bool, str, Optional[Path]]] = []
        if self.game is not None:
            st = detect_mod_base(self.game)
            if show_core:
                if st.ue4ss.installed:
                    ver = st.ue4ss.version_line or "installed"
                    rows.append(("locked_core", True, None, f"UE4SS ({ver})", "Core", "#7A808C", "#7A808C", True, "Core UE4SS scripting engine.", self.game.ue4ss_dir))
                if st.enabler.present:
                    rows.append(("locked_core", True, None, f"Pak Enabler ({st.enabler.kind})", "Core", "#7A808C", "#7A808C", True, "Core unsigned pak loader.", self.game.binaries_dir))
                if st.bp_loader.kind != "none":
                    bp_lbl = "WMM Lua BP Loader" if st.bp_loader.kind == "wmm_lua" else "WFF_BPLoader"
                    bp_dir = (self.game.ue4ss_mods_dir / "WMM_BPLoader") if st.bp_loader.kind == "wmm_lua" else self.game.mods_pak_dir
                    rows.append(("locked_core", True, None, bp_lbl, "Core", "#7A808C", "#7A808C", True, "Core Blueprint mod loader.", bp_dir))

                # Keybinds: greyed-out hard-checked, bold italic, state 'Core'
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
                    if not show_core:
                        continue
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
            it_st.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
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
        if procs and not self._game_unlocked:
            QMessageBox.warning(self, self.t.tr("app.title"), self.t.tr("err.game_running", procs=", ".join(procs)))
            self.refresh_all()
            return
        want_on = item.checkState() == Qt.CheckState.Checked
        ok = False

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
                    ok = True
                    self._log(f"[UE4SS] {orig_name}: {'linked & enabled' if want_on else 'unlinked & disabled'}")
                except DeployError as exc:
                    QMessageBox.warning(self, self.t.tr("app.title"), str(exc))
        else:
            _, mod_name = payload
            if want_on and str(mod_name).lower() == "splitscreenmod":
                ans = QMessageBox.warning(
                    self,
                    self.t.tr("app.title"),
                    self.t.tr("warn.splitscreen_fps"),
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if ans != QMessageBox.StandardButton.Yes:
                    self.refresh_all()
                    return
            write_mods_txt(self.game.ue4ss_mods_txt, [(str(mod_name), want_on)], preserve_unmanaged=True)
            ok = True
            self._log(f"[UE4SS] {mod_name} : {1 if want_on else 0}")
        self.refresh_all()
        if ok and want_on:
            self._notify_restart_if_game_running()

    def _find_mod_config_file(self, folder: Path) -> Optional[Path]:
        if not folder.is_dir():
            return None
        for name in ("config.lua", "config.ini", "config.json", "settings.ini", "UE4SS-settings.ini"):
            cand = folder / name
            if cand.is_file():
                return cand
        return None

    def _find_managed_mod_config(self, mod: ModRecord) -> Optional[Path]:
        mdir = self.store.mod_dir(mod.id)
        for c in mod.components:
            if c.kind == "ue4ss_mod":
                cfg = self._find_mod_config_file(mdir / c.store_rel)
                if cfg is not None:
                    return cfg
        return None

    def _resolve_nexus_mod_id(self, mod: Optional[ModRecord]) -> Optional[int]:
        if mod is None:
            return None
        if mod.nexus_mod_id:
            return mod.nexus_mod_id
        if mod.source:
            return parse_nexus_mod_id_from_filename(Path(mod.source).name)
        return None

    def _visit_nexus_page(self, nexus_id: int) -> None:
        webbrowser.open(nexus_mod_url(nexus_id))

    def _endorse_nexus_mod(self, nexus_id: int, mod_name: str, mod_ver: str = "") -> None:
        settings = self.store.load_settings()
        raw_key = settings.get("nexus_api_key", "")
        api_key = self.plat.unprotect_secret(raw_key) if raw_key else ""
        if not api_key:
            QMessageBox.warning(self, self.t.tr("app.title"), self.t.tr("endorse.need_api_key"))
            return
        try:
            client = NexusClient(api_key)
            client.endorse_mod(nexus_id, version=mod_ver)
            self._log(f"Endorsed {mod_name} (Nexus #{nexus_id})")
            QMessageBox.information(
                self,
                self.t.tr("app.title"),
                self.t.tr("endorse.ok", name=mod_name, id=nexus_id),
            )
        except NexusError as exc:
            QMessageBox.warning(self, self.t.tr("app.title"), str(exc))

    def _append_nexus_menu_actions(
        self,
        menu: QMenu,
        nexus_id: Optional[int],
        mod_name: str,
        mod_ver: str = "",
    ) -> None:
        if nexus_id is None:
            return
        menu.addSeparator()
        act_visit = QAction(self.t.tr("ctx.visit_nexus"), self)
        act_visit.triggered.connect(lambda: self._visit_nexus_page(nexus_id))
        menu.addAction(act_visit)

        act_endorse = QAction(self.t.tr("ctx.endorse_nexus"), self)
        act_endorse.triggered.connect(lambda: self._endorse_nexus_mod(nexus_id, mod_name, mod_ver))
        menu.addAction(act_endorse)

    def _on_ue4ss_context_menu(self, pos) -> None:
        row = self.tab_ue4ss.rowAt(pos.y())
        sel_items = self._ue4ss_items(self._tab_rows(self.tab_ue4ss)) if row >= 0 else []
        if row < 0 or len(sel_items) > 1:
            menu = QMenu(self)
            if len(sel_items) > 1:
                self._add_toggle_actions(menu, sel_items, True, self._set_ue4ss_enabled)
            else:
                self.tab_ue4ss.clearSelection()
                self._add_toggle_actions(menu, self._ue4ss_items(), False, self._set_ue4ss_enabled)
            if menu.actions():
                menu.exec(self.tab_ue4ss.viewport().mapToGlobal(pos))
            return
        chk_item = self.tab_ue4ss.item(row, 0)
        item = self.tab_ue4ss.item(row, 1)
        if not item:
            return
        raw_path = str(item.data(Qt.ItemDataRole.UserRole) or "").strip()
        folder = Path(raw_path) if raw_path else None
        if folder is not None and (not folder.is_absolute() or not folder.exists()):
            folder = self.game.ue4ss_mods_dir if (self.game is not None and self.game.ue4ss_mods_dir.is_dir()) else None
        payload = chk_item.data(Qt.ItemDataRole.UserRole) if chk_item else None
        managed_mod_id = payload[1] if isinstance(payload, tuple) and len(payload) >= 2 and payload[0] == "managed" else None

        menu = QMenu(self)
        if managed_mod_id:
            act_det = QAction(self.t.tr("ctx.details"), self)
            act_det.triggered.connect(lambda: self._show_mod_details(managed_mod_id))
            menu.addAction(act_det)

        if folder is not None and folder.exists():
            act_open = QAction(self.t.tr("ctx.open_folder"), self)
            act_open.triggered.connect(lambda: self._open_path(folder))
            menu.addAction(act_open)

            cfg = self._find_mod_config_file(folder)
            if cfg is not None:
                act_cfg = QAction(self.t.tr("ctx.open_config"), self)
                act_cfg.triggered.connect(lambda: self._open_path(cfg))
                menu.addAction(act_cfg)

        if managed_mod_id:
            mod = self.store.load_mod(managed_mod_id)
            nid = self._resolve_nexus_mod_id(mod)
            if mod is not None and nid is not None:
                self._append_nexus_menu_actions(menu, nid, mod.name, mod.version or "")

        all_items = self._ue4ss_items()
        if all_items:
            menu.addSeparator()
            self._add_toggle_actions(menu, all_items, False, self._set_ue4ss_enabled)
        if menu.actions():
            menu.exec(self.tab_ue4ss.viewport().mapToGlobal(pos))

    def _on_paks_context_menu(self, pos) -> None:
        row = self.tab_paks.rowAt(pos.y())
        sel_items = self._pak_items(self._tab_rows(self.tab_paks)) if row >= 0 else []
        if row < 0 or len(sel_items) > 1:
            menu = QMenu(self)
            if len(sel_items) > 1:
                self._add_toggle_actions(menu, sel_items, True, self._set_paks_enabled)
            else:
                self.tab_paks.clearSelection()
                self._add_toggle_actions(menu, self._pak_items(), False, self._set_paks_enabled)
            if menu.actions():
                menu.exec(self.tab_paks.viewport().mapToGlobal(pos))
            return
        chk_item = self.tab_paks.item(row, 0)
        item = self.tab_paks.item(row, 1)
        if not item:
            return
        raw_path = item.data(Qt.ItemDataRole.UserRole)
        target = Path(raw_path) if raw_path else None
        folder = (target.parent if target.is_file() else target) if target is not None else None
        owner = chk_item.data(Qt.ItemDataRole.UserRole) if chk_item else None
        managed_mod_id = owner[0] if isinstance(owner, tuple) and len(owner) >= 2 else None

        menu = QMenu(self)
        if managed_mod_id:
            act_det = QAction(self.t.tr("ctx.details"), self)
            act_det.triggered.connect(lambda: self._show_mod_details(managed_mod_id))
            menu.addAction(act_det)

        if folder is not None and folder.exists():
            act_open = QAction(self.t.tr("ctx.open_folder"), self)
            act_open.triggered.connect(lambda: self._open_path(folder))
            menu.addAction(act_open)

        if managed_mod_id:
            mod = self.store.load_mod(managed_mod_id)
            nid = self._resolve_nexus_mod_id(mod)
            if mod is not None and nid is not None:
                self._append_nexus_menu_actions(menu, nid, mod.name, mod.version or "")

        if row in self._movable_pak_rows():
            menu.addSeparator()
            act_up = QAction(self.t.tr("ctx.move_up"), self)
            act_down = QAction(self.t.tr("ctx.move_down"), self)
            act_order = QAction(self.t.tr("ctx.set_order"), self)
            act_up.triggered.connect(lambda: self._on_pak_row_moved(row, row - 1))
            act_down.triggered.connect(lambda: self._on_pak_row_moved(row, row + 1))
            act_order.triggered.connect(lambda: self._ctx_set_pak_order(row))
            menu.addAction(act_up)
            menu.addAction(act_down)
            menu.addAction(act_order)

        all_items = self._pak_items()
        if all_items:
            menu.addSeparator()
            self._add_toggle_actions(menu, all_items, False, self._set_paks_enabled)
        if menu.actions():
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

        config_conflict_paks: set[str] = set()
        if self.conflict_report:
            for cpak, _cfgs in self.conflict_report.config_conflicts:
                config_conflict_paks.add(cpak.lower())
        mod_by_id = {m.id: m for m in mods}

        self._pak_rows = [(owner, folder_lbl, checked) for _p, folder_lbl, checked, owner, _u in entries]
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

            it_rank = QTableWidgetItem(str(r) if checked else "-")
            it_rank.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

            shown = owner[1] if owner is not None else pname  # managed paks show their original name
            display = shown if folder_lbl == "~mods" else f"[LogicMods] {shown}"
            it_name = QTableWidgetItem(display)
            if shown != pname:
                it_name.setToolTip(self.t.tr("pak.deployed_as", name=pname))
            if checked and self.game is not None:
                p_folder = self.game.logic_mods_dir if folder_lbl == "LogicMods" else self.game.mods_pak_dir
                it_name.setData(Qt.ItemDataRole.UserRole, str(p_folder / pname))
            elif owner is not None:
                it_name.setData(Qt.ItemDataRole.UserRole, str(self.store.mod_dir(owner[0])))

            has_cfg_conflict = pname.lower() in config_conflict_paks
            if not has_cfg_conflict and owner is not None:
                om = mod_by_id.get(owner[0])
                if om is not None:
                    has_cfg_conflict = any(
                        c.kind == "pak" and c.name == owner[1] and bool(c.config_overrides) for c in om.components
                    )

            if has_cfg_conflict and checked:
                st_txt = f"● {self.t.tr('status.conflict')}"
                color = "#E05252"
            elif is_unmanaged:
                st_txt = f"● {self.t.tr('status.unmanaged')}"
                color = "#7EA8D9"
            elif not checked:
                st_txt = "Disabled"
                color = "#7A808C"
            elif self.conflict_report and pname in self.conflict_report.per_pak and self.conflict_report.per_pak[pname]["assets_lost"] > 0:
                st_txt = f"● {self.t.tr('status.overridden')}"
                color = "#E5A93C"
            else:
                st_txt = f"● {self.t.tr('status.ok')}"
                color = "#4CAF7D"

            it_st = QTableWidgetItem(st_txt)
            it_st.setForeground(QColor(color))
            it_st.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            if is_unmanaged:
                it_name.setFont(bi_font)
                it_st.setFont(bi_font)
                for it in (chk, it_rank, it_name, it_st):
                    it.setToolTip(unmanaged_tip)

            self.tab_paks.setItem(r, 0, chk)
            self.tab_paks.setItem(r, 1, it_name)
            self.tab_paks.setItem(r, 2, it_rank)
            self.tab_paks.setItem(r, 3, it_st)

    def _movable_pak_rows(self) -> list[int]:
        """Displayed rows of managed, enabled ~mods paks (the ones whose order WMM controls)."""
        return [
            i for i, (owner, folder, checked) in enumerate(self._pak_rows)
            if owner is not None and checked and folder == "~mods"
        ]

    def _ctx_set_pak_order(self, row: int) -> None:
        movable = self._movable_pak_rows()
        if row not in movable:
            return
        val, ok = QInputDialog.getInt(
            self, self.t.tr("app.title"), self.t.tr("ctx.set_order_prompt"), row, movable[0], movable[-1]
        )
        if ok:
            self._on_pak_row_moved(row, val)

    def _on_pak_row_moved(self, src: int, dst: int) -> None:
        """Reorder paks only (pak_order); the left-panel mod order is left untouched."""
        movable = self._movable_pak_rows()
        if src not in movable:
            return
        dst = max(movable[0], min(movable[-1], dst))
        if dst == src:
            return
        keys = [f"{self._pak_rows[i][0][0]}:{self._pak_rows[i][0][1]}" for i in movable]
        key = keys.pop(movable.index(src))
        if dst > src:
            pos = sum(1 for m in movable if m <= dst) - 1
        else:
            pos = sum(1 for m in movable if m < dst)
        keys.insert(pos, key)
        mgr = self._manager()
        if mgr is None:
            return
        prof = self.store.load_profile()
        prof.pak_order = keys + [k for k in prof.pak_order if k not in set(keys)]
        self.store.save_profile(prof)
        try:
            mgr.apply_profile(prof)
            self._log(f"[Pak] order: {key} -> {pos}")
        except DeployError as exc:
            QMessageBox.warning(self, self.t.tr("app.title"), str(exc))
        self.refresh_all()

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
        ok = False
        try:
            mgr.apply_profile()
            ok = True
            self._log(f"[Pak] {orig_name}: {'enabled' if want_on else 'disabled'}")
        except DeployError as exc:
            QMessageBox.warning(self, self.t.tr("app.title"), str(exc))
        self.refresh_all()
        if ok and want_on:
            self._notify_restart_if_game_running()

    def _refresh_downloads_tab(self, mods: list[ModRecord]) -> None:
        settings = self.store.load_settings()
        mods_dir_raw = settings.get("mods_archive_dir", "")
        dl_dir = Path(mods_dir_raw) if mods_dir_raw else (self.package_root / "downloads")

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
            it_state.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
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
        menu = QMenu(self)
        row = self.tab_downloads.rowAt(pos.y())
        sel_paths = self._download_paths(self._tab_rows(self.tab_downloads)) if row >= 0 else []
        if len(sel_paths) > 1:
            act = QAction(self.t.tr("ctx.delete_selected_downloads"), self)
            act.triggered.connect(lambda: self._delete_downloads(sel_paths))
            menu.addAction(act)
        elif sel_paths:
            p = sel_paths[0]
            act_inst = QAction(self.t.tr("ctx.install_download"), self)
            act_open = QAction(self.t.tr("ctx.open_folder"), self)
            act_del = QAction(self.t.tr("ctx.delete_download"), self)
            act_inst.triggered.connect(lambda: self._install_path(p))
            act_open.triggered.connect(lambda: self._open_path(p.parent))
            act_del.triggered.connect(lambda: self._delete_download(p))
            menu.addAction(act_inst)
            menu.addAction(act_open)
            dl_nid = parse_nexus_mod_id_from_filename(p.name)
            if dl_nid is not None:
                self._append_nexus_menu_actions(menu, dl_nid, p.stem)
            menu.addSeparator()
            menu.addAction(act_del)
        all_paths = self._download_paths()
        if all_paths:
            if menu.actions():
                menu.addSeparator()
            act_all = QAction(self.t.tr("ctx.delete_all_downloads"), self)
            act_all.triggered.connect(lambda: self._delete_downloads(all_paths))
            menu.addAction(act_all)
        if menu.actions():
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

    def _selected_managed_ids(self) -> list[str]:
        ids: list[str] = []
        for idx in self.table.selectionModel().selectedRows():
            item = self.table.item(idx.row(), 0)
            mid = item.data(Qt.ItemDataRole.UserRole) if item else None
            if mid and mid not in self.unmanaged_mods and mid not in ids:
                ids.append(mid)
        return ids

    def _disable_mods(self, ids: list[str]) -> None:
        self._set_mods_enabled(ids, False)

    def _enable_mods(self, ids: list[str]) -> None:
        self._set_mods_enabled(ids, True)

    def _set_mods_enabled(self, ids: list[str], on: bool) -> None:
        """Enable or disable several mods with a single redeploy."""
        mgr = self._manager()
        if mgr is None or not ids:
            return
        prof = self.store.load_profile()
        if on:
            prof.enabled_mods = list(prof.enabled_mods) + [m for m in ids if m not in prof.enabled_mods]
        else:
            drop = set(ids)
            prof.enabled_mods = [m for m in prof.enabled_mods if m not in drop]
        self.store.save_profile(prof)
        try:
            mgr.apply_profile(prof)
            self._log(f"{'Enabled' if on else 'Disabled'} {len(ids)} mod(s)")
        except DeployError as exc:
            QMessageBox.warning(self, self.t.tr("app.title"), str(exc))
        self.refresh_all()
        if on:
            self._notify_restart_if_game_running()

    def _add_bulk_actions(self, menu: QMenu, scope_ids: list[str], selected: bool) -> None:
        """Enable/Disable (all|selected) entries for the given mod ids, only where they would change something."""
        enabled = set(self.store.load_profile().enabled_mods)
        off = [m for m in scope_ids if m not in enabled]
        on = [m for m in scope_ids if m in enabled]
        key = "selected" if selected else "all"
        if off:
            a = QAction(self.t.tr(f"ctx.enable_{key}"), self)
            a.triggered.connect(lambda: self._enable_mods(off))
            menu.addAction(a)
        if on:
            a = QAction(self.t.tr(f"ctx.disable_{key}"), self)
            a.triggered.connect(lambda: self._disable_mods(on))
            menu.addAction(a)

    def _on_mod_row_activated(self, row: int, col: int) -> None:
        """Double-click a mod row (not the checkbox) to open its details."""
        if col == 0:
            return
        item = self.table.item(row, 0)
        mid = item.data(Qt.ItemDataRole.UserRole) if item else None
        if mid:
            self._show_mod_details(str(mid))

    def _on_mods_space(self) -> None:
        ids = self._selected_managed_ids()
        if not ids:
            return
        enabled = set(self.store.load_profile().enabled_mods)
        self._set_mods_enabled(ids, ids[0] not in enabled)

    # --- right panel: selection helpers, bulk toggles ---

    def _tab_rows(self, tbl: QTableWidget) -> list[int]:
        rows = sorted({i.row() for i in tbl.selectionModel().selectedRows() if not tbl.isRowHidden(i.row())})
        if not rows and tbl.currentRow() >= 0:
            rows = [tbl.currentRow()]
        return rows

    def _ue4ss_items(self, rows: Optional[list[int]] = None) -> list[tuple[tuple, bool]]:
        """(payload, checked) for toggleable UE4SS rows; rows=None means every row."""
        out: list[tuple[tuple, bool]] = []
        for r in rows if rows is not None else range(self.tab_ue4ss.rowCount()):
            chk = self.tab_ue4ss.item(r, 0)
            pl = chk.data(Qt.ItemDataRole.UserRole) if chk is not None else None
            if isinstance(pl, tuple) and pl and pl[0] in ("managed", "unmanaged") and (chk.flags() & Qt.ItemFlag.ItemIsEnabled):
                out.append((pl, chk.checkState() == Qt.CheckState.Checked))
        return out

    def _pak_items(self, rows: Optional[list[int]] = None) -> list[tuple[tuple, bool]]:
        """((mod_id, original pak name), checked) for managed paks; rows=None means every row."""
        out: list[tuple[tuple, bool]] = []
        for r in rows if rows is not None else range(self.tab_paks.rowCount()):
            chk = self.tab_paks.item(r, 0)
            owner = chk.data(Qt.ItemDataRole.UserRole) if chk is not None else None
            if isinstance(owner, tuple) and len(owner) >= 2 and (chk.flags() & Qt.ItemFlag.ItemIsEnabled):
                out.append((owner, chk.checkState() == Qt.CheckState.Checked))
        return out

    def _add_toggle_actions(self, menu: QMenu, items: list[tuple[object, bool]], selected: bool, apply_fn) -> None:
        keys_on = [k for k, c in items if c]
        keys_off = [k for k, c in items if not c]
        key = "selected" if selected else "all"
        if keys_off:
            a = QAction(self.t.tr(f"ctx.enable_{key}"), self)
            a.triggered.connect(lambda: apply_fn(keys_off, True))
            menu.addAction(a)
        if keys_on:
            a = QAction(self.t.tr(f"ctx.disable_{key}"), self)
            a.triggered.connect(lambda: apply_fn(keys_on, False))
            menu.addAction(a)

    def _set_paks_enabled(self, owners: list[tuple], on: bool) -> None:
        mgr = self._manager()
        if mgr is None or not owners:
            return
        by_mod: dict[str, set[str]] = {}
        for owner in owners:
            by_mod.setdefault(owner[0], set()).add(owner[1])
        for mid, names in by_mod.items():
            mod = self.store.load_mod(mid)
            if mod is None:
                continue
            for c in mod.components:
                if c.kind == "pak" and c.name in names:
                    c.selected = on
            self.store.save_mod(mod)
        try:
            mgr.apply_profile()
            self._log(f"[Pak] {'enabled' if on else 'disabled'} {len(owners)} pak(s)")
        except DeployError as exc:
            QMessageBox.warning(self, self.t.tr("app.title"), str(exc))
        self.refresh_all()
        if on:
            self._notify_restart_if_game_running()

    def _set_ue4ss_enabled(self, payloads: list[tuple], on: bool) -> None:
        if self.game is None or not payloads:
            return
        procs = running_game_processes(self.plat)
        if procs and not self._game_unlocked:
            QMessageBox.warning(self, self.t.tr("app.title"), self.t.tr("err.game_running", procs=", ".join(procs)))
            return
        managed: dict[str, set[str]] = {}
        loose: list[str] = []
        for pl in payloads:
            if pl[0] == "managed":
                managed.setdefault(pl[1], set()).add(pl[2])
            else:
                loose.append(str(pl[1]))
        if on and any(n.lower() == "splitscreenmod" for n in loose):
            ans = QMessageBox.warning(
                self,
                self.t.tr("app.title"),
                self.t.tr("warn.splitscreen_fps"),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if ans != QMessageBox.StandardButton.Yes:
                loose = [n for n in loose if n.lower() != "splitscreenmod"]
        mgr = self._manager()
        try:
            if managed and mgr is not None:
                for mid, names in managed.items():
                    mod = self.store.load_mod(mid)
                    if mod is None:
                        continue
                    for c in mod.components:
                        if c.kind == "ue4ss_mod" and c.name in names:
                            c.selected = on
                    self.store.save_mod(mod)
                mgr.apply_profile()
            if loose:
                write_mods_txt(self.game.ue4ss_mods_txt, [(n, on) for n in loose], preserve_unmanaged=True)
            self._log(f"[UE4SS] {'enabled' if on else 'disabled'} {len(managed) + len(loose)} entr(ies)")
        except DeployError as exc:
            QMessageBox.warning(self, self.t.tr("app.title"), str(exc))
        self.refresh_all()
        if on:
            self._notify_restart_if_game_running()

    def _on_ue4ss_space(self) -> None:
        items = self._ue4ss_items(self._tab_rows(self.tab_ue4ss))
        if items:
            self._set_ue4ss_enabled([k for k, _ in items], not items[0][1])

    def _on_paks_space(self) -> None:
        items = self._pak_items(self._tab_rows(self.tab_paks))
        if items:
            self._set_paks_enabled([k for k, _ in items], not items[0][1])

    def _download_paths(self, rows: Optional[list[int]] = None) -> list[Path]:
        out: list[Path] = []
        for r in rows if rows is not None else range(self.tab_downloads.rowCount()):
            it = self.tab_downloads.item(r, 0)
            raw = it.data(Qt.ItemDataRole.UserRole) if it is not None else None
            if raw:
                out.append(Path(raw))
        return out

    def _delete_downloads(self, paths: list[Path]) -> None:
        paths = [p for p in paths if p.is_file()]
        if not paths:
            return
        ans = QMessageBox.question(
            self, self.t.tr("app.title"), self.t.tr("ctx.confirm_delete_downloads", count=len(paths))
        )
        if ans != QMessageBox.StandardButton.Yes:
            return
        for p in paths:
            try:
                p.unlink(missing_ok=True)
                self._log(f"Deleted download: {p.name}")
            except OSError as exc:
                QMessageBox.warning(self, self.t.tr("app.title"), str(exc))
                break
        self.refresh_all()

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
            QMessageBox.information(self, u.name, "\n".join(lines))
            return
        mod = self.store.load_mod(mod_id)
        if mod is None:
            return
        full_manifest = self.store.load_manifest()
        manifest = [e for e in full_manifest if e.mod_id == mod_id]
        names = {m.id: m.name for m in self.store.list_mods()}
        pak_owner = {e.deployed_name: names.get(e.mod_id, e.mod_id) for e in full_manifest if e.kind.startswith("pak_")}
        ModDetailsDialog(
            mod,
            manifest,
            self.conflict_report,
            self.t,
            store_dir=self.store.mod_dir(mod_id),
            pak_owner=pak_owner,
            on_visit_nexus=self._visit_nexus_page,
            on_endorse_nexus=self._endorse_nexus_mod,
            on_rename=lambda new: self._rename_mod(mod_id, new),
            parent=self,
        ).exec()

    def _rename_mod(self, mod_id: str, new_name: str) -> None:
        """Change the display name only; the store folder id, files and order stay as they are."""
        mod = self.store.load_mod(mod_id)
        if mod is None or not new_name.strip():
            return
        old = mod.name
        mod.name = new_name.strip()
        self.store.save_mod(mod)
        self._log(f"Renamed {old} -> {mod.name}")
        self.refresh_all()

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if self._suppress_table_signals or item.column() != 0:
            return
        mod_id = item.data(Qt.ItemDataRole.UserRole)
        want_on = item.checkState() == Qt.CheckState.Checked
        mgr = self._manager()
        if mgr is None:
            return
        ok = False
        try:
            mgr.set_enabled(mod_id, want_on)
            ok = True
            self._log(f"{'Enabled' if want_on else 'Disabled'} {mod_id}")
        except DeployError as exc:
            QMessageBox.warning(self, self.t.tr("app.title"), str(exc))
        self.refresh_all()
        if ok and want_on:
            self._notify_restart_if_game_running()

    def _prompt_update_target(
        self,
        archive: Path,
        candidates: list[ModRecord],
        nexus_id: Optional[int],
        preferred_id: Optional[str] = None,
    ) -> tuple[str, Optional[str], bool]:
        """Ask which installed mod this archive overrides, or whether to install it as a new mod.

        One dropdown: every installed mod with the same Nexus id / a similar name (closest file name first,
        or `preferred_id` first), then "Install as a new mod". Returns (action, mod_id, backup_configs) with
        action 'update', 'separate' or 'cancel'.
        """
        if preferred_id is not None:
            candidates = sorted(candidates, key=lambda m: m.id != preferred_id)
        dlg = QDialog(self)
        dlg.setWindowTitle(self.t.tr("update.title"))
        dlg.setMinimumWidth(520)
        lay = QVBoxLayout(dlg)
        lay.setSpacing(10)

        lbl = QLabel(
            self.t.tr("update.pick_body", file=archive.name, id=str(nexus_id) if nexus_id is not None else "-")
        )
        lbl.setWordWrap(True)
        lay.addWidget(lbl)

        combo = QComboBox()
        for m in candidates:
            combo.addItem(
                self.t.tr("update.opt_override", name=m.name, version=m.version or "-"), m.id
            )
        combo.addItem(self.t.tr("update.opt_new"), None)
        lay.addWidget(combo)

        chk_bak = QCheckBox(self.t.tr("update.backup_configs"))
        chk_bak.setChecked(True)
        lay.addWidget(chk_bak)
        combo.currentIndexChanged.connect(lambda _i: chk_bak.setEnabled(combo.currentData() is not None))

        footer = QHBoxLayout()
        footer.addStretch(1)
        btn_cancel = QPushButton(self.t.tr("btn.cancel"))
        btn_cancel.setFixedWidth(84)
        btn_cancel.clicked.connect(dlg.reject)
        footer.addWidget(btn_cancel)
        btn_ok = QPushButton(self.t.tr("btn.ok"))
        btn_ok.setFixedWidth(84)
        btn_ok.setProperty("accent", True)
        btn_ok.setDefault(True)
        btn_ok.clicked.connect(dlg.accept)
        footer.addWidget(btn_ok)
        lay.addLayout(footer)

        if dlg.exec() != QDialog.DialogCode.Accepted:
            return "cancel", None, False
        target = combo.currentData()
        if target is None:
            return "separate", None, False
        return "update", str(target), chk_bak.isChecked()

    def _install_path(
        self,
        path: Path,
        name: Optional[str] = None,
        version: str = "",
        nexus_mod_id: Optional[int] = None,
        skip_prompt: bool = False,
    ) -> None:
        mgr = self._manager()
        if mgr is None:
            QMessageBox.warning(self, self.t.tr("app.title"), self.t.tr("cli.game_not_found"))
            return

        resolved_nid = nexus_mod_id if nexus_mod_id is not None else parse_nexus_mod_id_from_filename(path.name)
        parsed_nx = parse_nexus_archive_name(path.name)
        if parsed_nx is not None and not version:
            version = parsed_nx[2]
        candidates = [] if skip_prompt else rank_update_targets(
            path.name, self.store.list_mods(), self._resolve_nexus_mod_id, resolved_nid
        )
        if candidates:
            action, target_id, backup_cfgs = self._prompt_update_target(path, candidates, resolved_nid)
            if action == "cancel":
                return
            if action == "update" and target_id:
                try:
                    rec, summaries = update_mod_preserving_configs(
                        mgr,
                        target_id,
                        path,
                        new_version=version,
                        backup_configs=backup_cfgs,
                        nexus_mod_id=resolved_nid,
                    )
                    self._log(
                        f"Updated {rec.name} from {path.name} (version {rec.version or '-'}, preserved "
                        f"{len(summaries)} config.lua{', backed up configs' if backup_cfgs else ''})"
                    )
                except Exception as exc:  # noqa: BLE001
                    self._log(f"Update of {target_id} from {path.name} failed: {exc}", "ERROR")
                    QMessageBox.warning(self, self.t.tr("app.title"), str(exc))
                self.refresh_all()
                return

        self._log(f"Installing {path}")
        try:
            rec = mgr.add_mod(path, name=name, version=version, nexus_mod_id=resolved_nid)
            self._log(f"Added {rec.name} ({rec.id}) [{rec.badge}]")
        except DeployError as exc:
            self._log(f"Install of {path.name} failed: {exc}", "ERROR")
            if "loader DLL" in str(exc):
                ans = QMessageBox.question(self, self.t.tr("app.title"), str(exc))
                if ans == QMessageBox.StandardButton.Yes:
                    rec = mgr.add_mod(
                        path, name=name, version=version, allow_loaders=True, nexus_mod_id=resolved_nid
                    )
                    self._log(f"Added {rec.name} ({rec.id}) [loader confirmed]")
            else:
                QMessageBox.warning(self, self.t.tr("app.title"), str(exc))
        except ExtractError as exc:
            self._log(f"Extraction of {path.name} failed: {exc}", "ERROR")
            QMessageBox.warning(self, self.t.tr("app.title"), str(exc))
        except Exception as exc:  # noqa: BLE001 - any other failure must reach the user and the log
            import traceback

            self._log(f"Install of {path.name} crashed: {traceback.format_exc().strip()}", "ERROR")
            QMessageBox.warning(self, self.t.tr("app.title"), f"{path.name}: {type(exc).__name__}: {exc}")
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
        dl_dir = Path(mods_dir_raw) if mods_dir_raw else (self.package_root / "downloads")
        auto_extract = bool(settings.get("auto_extract", False))

        self._dl_seq += 1
        dl_id = self._dl_seq
        self._dl_progress[dl_id] = {"name": uri.split("?")[0], "pct": -1, "speed": ""}
        self.right_tabs.setCurrentWidget(self.tab_downloads)
        self._refresh_downloads_tab(self.store.list_mods())
        self._apply_filter()
        self.statusBar().showMessage(self.t.tr("download.in_progress", name=uri.split("?")[0]))
        self._log(f"Downloading: {uri.split('?')[0]}")

        worker = _DownloadWorker(dl_id, uri, api_key, dl_dir, parent=self)
        self._active_downloads.append(worker)

        def _on_prog(did: int, fname: str, pct: int, spd: str) -> None:
            if did in self._dl_progress:
                self._dl_progress[did] = {"name": fname, "pct": pct, "speed": spd}
                self._refresh_downloads_tab(self.store.list_mods())
                self._apply_filter()
                self.statusBar().showMessage(f"Downloading {fname} ({pct}% — {spd})" if pct >= 0 else f"Downloading {fname} ({spd})")

        def _on_ok(did: int, dl_path_str: str, mod_name, mod_ver: str, nx_mod_id) -> None:
            self._dl_progress.pop(did, None)
            if worker in self._active_downloads:
                self._active_downloads.remove(worker)
            dl_path = Path(dl_path_str)
            self._log(f"Downloaded: {dl_path.name}")
            # Show the finished file (and drop the 100% bar) before any modal install prompt blocks the event loop.
            self.right_tabs.setCurrentWidget(self.tab_downloads)
            self.refresh_all()
            if auto_extract:
                QTimer.singleShot(
                    0, lambda: self._install_path(dl_path, name=mod_name, version=mod_ver, nexus_mod_id=nx_mod_id)
                )

        def _on_fail(did: int, err_msg: str) -> None:
            self._dl_progress.pop(did, None)
            if worker in self._active_downloads:
                self._active_downloads.remove(worker)
            self._log(f"Download failed: {err_msg}", "ERROR")
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

        # Warn if enabled mods need UE4SS or Pak Enabler that isn't installed in Win64/
        prof = self.store.load_profile()
        if prof.enabled_mods:
            st = detect_mod_base(self.game)
            enabled_recs = [m for m in (self.store.load_mod(mid) for mid in prof.enabled_mods) if m is not None]
            needs_ue4ss = any(
                c.kind in ("ue4ss_mod", "ue4ss_shared") and c.selected for m in enabled_recs for c in m.components
            )
            needs_paks = any(c.kind == "pak" and c.selected for m in enabled_recs for c in m.components)
            missing: list[str] = []
            if needs_ue4ss and not st.ue4ss.installed:
                missing.append("UE4SS")
            if needs_paks and not st.enabler.present:
                missing.append("Pak Enabler")
            if missing:
                ans = QMessageBox.warning(
                    self,
                    self.t.tr("app.title"),
                    self.t.tr("err.missing_mod_kit", missing=", ".join(missing)),
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.Yes,
                )
                if ans == QMessageBox.StandardButton.Yes:
                    self._open_onboarding()
                    self.refresh_all()
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
        dlg = LogDialog(self.applog.entries, self.t.tr("btn.log"), parent=self, translator=self.t)
        dlg.exec()

    # --- Context menu (Open folder, Move up/down, Update, Uninstall) ---

    def _on_table_context_menu(self, pos) -> None:
        sel_ids = self._selected_managed_ids()
        menu = QMenu(self)
        if len(sel_ids) > 1:
            self._add_bulk_actions(menu, sel_ids, selected=True)
            menu.exec(self.table.viewport().mapToGlobal(pos))
            return
        mod_id = self._selected_mod_id()
        if not mod_id:
            self._add_bulk_actions(menu, self._managed_order_ids(), selected=False)
            if menu.actions():
                menu.exec(self.table.viewport().mapToGlobal(pos))
            return
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
        act_up.triggered.connect(lambda: self._ctx_move_mod(mod_id, -1))
        act_down.triggered.connect(lambda: self._ctx_move_mod(mod_id, 1))
        act_order = QAction(self.t.tr("ctx.set_order"), self)
        act_order.triggered.connect(lambda: self._ctx_set_mod_order(mod_id))
        act_update.triggered.connect(lambda: self._ctx_update(mod_id))
        act_uninst.triggered.connect(lambda: self._ctx_uninstall(mod_id))

        menu.addAction(act_details)
        menu.addAction(act_open)
        mod = self.store.load_mod(mod_id)
        if mod is not None:
            cfg = self._find_managed_mod_config(mod)
            if cfg is not None:
                act_cfg = QAction(self.t.tr("ctx.open_config"), self)
                act_cfg.triggered.connect(lambda: self._open_path(cfg))
                menu.addAction(act_cfg)
        nid = self._resolve_nexus_mod_id(mod)
        if mod is not None and nid is not None:
            self._append_nexus_menu_actions(menu, nid, mod.name, mod.version or "")
        menu.addSeparator()
        menu.addAction(act_up)
        menu.addAction(act_down)
        menu.addAction(act_order)
        menu.addSeparator()
        menu.addAction(act_update)
        menu.addAction(act_uninst)
        menu.addSeparator()
        self._add_bulk_actions(menu, self._managed_order_ids(), selected=False)
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

    def _ctx_move_mod(self, mod_id: str, delta: int) -> None:
        ids = self._managed_order_ids()
        if mod_id in ids:
            self._move_mod_to(mod_id, ids.index(mod_id) + delta)

    def _ctx_update(self, mod_id: str) -> None:
        mgr = self._manager()
        if mgr is None:
            return
        existing = self.store.load_mod(mod_id)
        if existing is None:
            return
        chosen, _ = QFileDialog.getOpenFileName(
            self, self.t.tr("btn.update"), "", "Archives (*.zip *.7z *.rar *.pak);;All files (*.*)"
        )
        if not chosen:
            return
        arc = Path(chosen)
        nid = self._resolve_nexus_mod_id(existing)
        cands = rank_update_targets(arc.name, self.store.list_mods(), self._resolve_nexus_mod_id, nid)
        if all(c.id != existing.id for c in cands):
            cands.append(existing)
        action, target_id, backup_cfgs = self._prompt_update_target(arc, cands, nid, preferred_id=existing.id)
        if action == "separate":
            self._install_path(arc, skip_prompt=True)
            return
        if action != "update" or not target_id:
            return
        try:
            rec, summaries = update_mod_preserving_configs(
                mgr, target_id, arc, backup_configs=backup_cfgs
            )
            self._log(
                f"Updated {rec.name} from {arc.name} (version {rec.version or '-'}, preserved "
                f"{len(summaries)} config.lua{', backed up configs' if backup_cfgs else ''})"
            )
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
