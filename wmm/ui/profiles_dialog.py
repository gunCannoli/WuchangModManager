"""Profiles manager dialog: list, inline create, clone, and delete profiles."""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..i18n import Translator
from ..store import Profile, Store, slugify_id


class ProfilesDialog(QDialog):
    def __init__(
        self,
        store: Store,
        translator: Translator,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self.store = store
        self.t = translator
        self.active_profile_changed = False
        self._pending_item: Optional[QListWidgetItem] = None
        self._pending_clone_src: Optional[str] = None
        self._committing = False

        self.setWindowTitle(self.t.tr("profiles.title"))
        self.setMinimumSize(380, 280)
        self._build_ui()
        self._reload_list()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setSpacing(10)
        btn_width = 84

        body = QHBoxLayout()
        body.setSpacing(10)

        self.list_profiles = QListWidget()
        self.list_profiles.currentItemChanged.connect(self._update_buttons)
        self.list_profiles.itemChanged.connect(self._on_item_changed)
        self.list_profiles.itemDelegate().closeEditor.connect(self._on_editor_closed)
        body.addWidget(self.list_profiles, 1)

        actions = QVBoxLayout()
        actions.setSpacing(6)

        self.btn_create = QPushButton(self.t.tr("btn.create"))
        self.btn_create.setFixedWidth(btn_width)
        self.btn_create.clicked.connect(self._on_create)

        self.btn_clone = QPushButton(self.t.tr("btn.clone"))
        self.btn_clone.setFixedWidth(btn_width)
        self.btn_clone.clicked.connect(self._on_clone)

        self.btn_delete = QPushButton(self.t.tr("btn.delete"))
        self.btn_delete.setFixedWidth(btn_width)
        self.btn_delete.clicked.connect(self._on_delete)

        actions.addWidget(self.btn_create)
        actions.addWidget(self.btn_clone)
        actions.addWidget(self.btn_delete)
        actions.addStretch(1)
        body.addLayout(actions)

        root.addLayout(body, 1)

        footer = QHBoxLayout()
        footer.addStretch(1)
        self.btn_close = QPushButton(self.t.tr("btn.close"))
        self.btn_close.setFixedWidth(btn_width)
        self.btn_close.clicked.connect(self.accept)
        footer.addWidget(self.btn_close)
        root.addLayout(footer)

    def _reload_list(self, select_name: Optional[str] = None) -> None:
        self._pending_item = None
        self._pending_clone_src = None
        self.list_profiles.blockSignals(True)
        self.list_profiles.clear()
        profiles = self.store.list_profiles()
        active = self.store.load_settings().get("active_profile", "default")
        target = select_name or active
        selected_row = 0
        for idx, pname in enumerate(profiles):
            item = QListWidgetItem(pname)
            item.setData(Qt.ItemDataRole.UserRole, pname)
            self.list_profiles.addItem(item)
            if pname == target:
                selected_row = idx
        self.list_profiles.blockSignals(False)
        if self.list_profiles.count() > 0:
            self.list_profiles.setCurrentRow(selected_row)
        self._update_buttons()

    def _selected_profile_name(self) -> Optional[str]:
        item = self.list_profiles.currentItem()
        if item is None or item is self._pending_item:
            return None
        val = item.data(Qt.ItemDataRole.UserRole)
        return str(val) if val else None

    def _update_buttons(self) -> None:
        sel = self._selected_profile_name()
        has_sel = sel is not None
        can_delete = has_sel and len(self.store.list_profiles()) > 1
        self.btn_clone.setEnabled(has_sel)
        self.btn_delete.setEnabled(can_delete)

    def _start_inline_row(self, initial_text: str = "", clone_src: Optional[str] = None) -> None:
        if self._pending_item is not None:
            self._remove_pending_row()
        self._pending_clone_src = clone_src
        item = QListWidgetItem(initial_text)
        item.setFlags(
            Qt.ItemFlag.ItemIsSelectable
            | Qt.ItemFlag.ItemIsEnabled
            | Qt.ItemFlag.ItemIsEditable
        )
        self.list_profiles.blockSignals(True)
        self.list_profiles.addItem(item)
        self._pending_item = item
        self.list_profiles.setCurrentItem(item)
        self.list_profiles.blockSignals(False)
        self._update_buttons()
        self.list_profiles.editItem(item)

    def _remove_pending_row(self) -> None:
        if self._pending_item is not None:
            row = self.list_profiles.row(self._pending_item)
            self._pending_item = None
            self._pending_clone_src = None
            if row >= 0:
                self.list_profiles.blockSignals(True)
                self.list_profiles.takeItem(row)
                self.list_profiles.blockSignals(False)
            self._update_buttons()

    def _on_create(self) -> None:
        self._start_inline_row(initial_text="", clone_src=None)

    def _on_clone(self) -> None:
        src = self._selected_profile_name()
        if not src:
            return
        base = f"{src}_copy"
        existing = set(self.store.list_profiles())
        cand = base
        i = 2
        while slugify_id(cand) in existing:
            cand = f"{base}_{i}"
            i += 1
        self._start_inline_row(initial_text=cand, clone_src=src)

    def _commit_pending(self, raw_name: str) -> None:
        if self._committing or self._pending_item is None:
            return
        self._committing = True
        try:
            clone_src = self._pending_clone_src
            name = raw_name.strip()
            if not name:
                self._remove_pending_row()
                return
            slug = slugify_id(name)
            existing = set(self.store.list_profiles())
            if slug in existing:
                self._remove_pending_row()
                QMessageBox.warning(
                    self,
                    self.t.tr("profiles.title"),
                    self.t.tr("profiles.exists", name=slug),
                )
                return
            if clone_src:
                self.store.clone_profile(clone_src, slug)
            else:
                self.store.save_profile(Profile(name=slug))
            self._reload_list(select_name=slug)
        finally:
            self._committing = False

    def _on_item_changed(self, item: QListWidgetItem) -> None:
        if item is self._pending_item:
            self._commit_pending(item.text())

    def _on_editor_closed(self, _editor, _hint) -> None:
        if self._pending_item is not None and not self._committing:
            # User cancelled (Esc) or left text unchanged/empty
            txt = self._pending_item.text().strip()
            if not txt:
                self._remove_pending_row()
            else:
                self._commit_pending(txt)

    def _on_delete(self) -> None:
        sel = self._selected_profile_name()
        if not sel:
            return
        if len(self.store.list_profiles()) <= 1:
            return
        ans = QMessageBox.question(
            self,
            self.t.tr("profiles.title"),
            self.t.tr("profiles.confirm_delete", name=sel),
        )
        if ans != QMessageBox.StandardButton.Yes:
            return
        settings = self.store.load_settings()
        was_active = settings.get("active_profile", "default") == sel
        if self.store.delete_profile(sel):
            if was_active:
                remaining = self.store.list_profiles()
                settings["active_profile"] = remaining[0]
                self.store.save_settings(settings)
                self.active_profile_changed = True
            self._reload_list()
