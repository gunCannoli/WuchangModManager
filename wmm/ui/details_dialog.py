"""Structured Mod Details modal with Overview, Components & Deployed, and Winning/Losing Overrides."""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..i18n import Translator
from ..nexus import parse_nexus_mod_id_from_filename
from ..paks import PakConflictReport
from ..store import ManifestEntry, ModRecord


class ModDetailsDialog(QDialog):
    def __init__(
        self,
        mod: ModRecord,
        manifest: list[ManifestEntry],
        conflict_report: Optional[PakConflictReport],
        translator: Translator,
        on_visit_nexus: Optional[Callable[[int], None]] = None,
        on_endorse_nexus: Optional[Callable[[int, str, str], None]] = None,
        on_rename: Optional[Callable[[str], None]] = None,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self.mod = mod
        self.manifest = manifest
        self.conflict_report = conflict_report
        self.t = translator
        self.on_visit_nexus = on_visit_nexus
        self.on_endorse_nexus = on_endorse_nexus
        self.on_rename = on_rename

        self.nexus_id: Optional[int] = mod.nexus_mod_id
        if self.nexus_id is None and mod.source:
            self.nexus_id = parse_nexus_mod_id_from_filename(Path(mod.source).name)

        self.setWindowTitle(f"{mod.name} [{mod.badge}]")
        self.setMinimumSize(600, 520)
        self._build_ui()

    def _make_label(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setFixedWidth(self._label_width)
        lbl.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        return lbl

    def _make_readonly_field(self, text: str) -> QLineEdit:
        ed = QLineEdit(text)
        ed.setReadOnly(True)
        ed.setAlignment(Qt.AlignmentFlag.AlignLeft)
        ed.setCursorPosition(0)
        return ed

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setSpacing(10)
        btn_width = 80

        fm = self.fontMetrics()
        label_texts = (
            self.t.tr("col.name"),
            "ID",
            self.t.tr("col.type"),
            self.t.tr("col.version"),
            self.t.tr("details.nexus_id"),
            "Source",
        )
        self._label_width = max(fm.horizontalAdvance(t) for t in label_texts) + 6

        # 1. Overview section
        ov_group = QGroupBox(self.t.tr("details.overview_group").replace("&", "&&"))
        ov_form = QFormLayout(ov_group)
        ov_form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        ov_form.setHorizontalSpacing(8)
        ov_form.setVerticalSpacing(6)

        name_row = QHBoxLayout()
        name_row.setSpacing(6)
        self.edit_name = QLineEdit(self.mod.name)
        self.edit_name.setCursorPosition(0)
        name_row.addWidget(self.edit_name, 1)
        self.btn_rename = QPushButton(self.t.tr("details.rename"))
        self.btn_rename.setEnabled(False)
        self.btn_rename.clicked.connect(self._apply_rename)
        self.edit_name.textChanged.connect(
            lambda txt: self.btn_rename.setEnabled(bool(txt.strip()) and txt.strip() != self.mod.name)
        )
        self.edit_name.returnPressed.connect(self._apply_rename)
        if self.on_rename is None:
            self.edit_name.setReadOnly(True)
            self.btn_rename.hide()
        name_row.addWidget(self.btn_rename)
        ov_form.addRow(self._make_label(self.t.tr("col.name")), name_row)
        ov_form.addRow(self._make_label("ID"), self._make_readonly_field(self.mod.id))

        meta_row = QHBoxLayout()
        meta_row.setSpacing(8)
        meta_row.addWidget(self._make_readonly_field(self.mod.badge), 1)
        lbl_ver = QLabel(self.t.tr("col.version"))
        meta_row.addWidget(lbl_ver)
        meta_row.addWidget(self._make_readonly_field(self.mod.version or "-"), 1)
        ov_form.addRow(self._make_label(self.t.tr("col.type")), meta_row)

        if self.nexus_id is not None:
            nx_row = QHBoxLayout()
            nx_row.setSpacing(6)
            nx_row.addWidget(self._make_readonly_field(str(self.nexus_id)), 1)
            if self.on_visit_nexus is not None:
                btn_visit = QPushButton(self.t.tr("ctx.visit_nexus"))
                btn_visit.clicked.connect(lambda: self.on_visit_nexus(self.nexus_id))  # type: ignore[arg-type]
                nx_row.addWidget(btn_visit)
            if self.on_endorse_nexus is not None:
                btn_end = QPushButton(self.t.tr("ctx.endorse_nexus"))
                btn_end.clicked.connect(
                    lambda: self.on_endorse_nexus(self.nexus_id, self.mod.name, self.mod.version or "")  # type: ignore[arg-type]
                )
                nx_row.addWidget(btn_end)
            ov_form.addRow(self._make_label(self.t.tr("details.nexus_id")), nx_row)

        ov_form.addRow(self._make_label("Source"), self._make_readonly_field(self.mod.source or "-"))
        root.addWidget(ov_group)

        # 2. Components & Deployed section
        comp_group = QGroupBox(self.t.tr("details.components_group").replace("&", "&&"))
        comp_lay = QVBoxLayout(comp_group)
        comp_view = QPlainTextEdit()
        comp_view.setReadOnly(True)
        comp_lines: list[str] = []
        for c in self.mod.components:
            sel = "" if c.selected else " (disabled)"
            comp_lines.append(f"[{c.kind}] {c.name} -> {c.target_kind}{sel}")
            if c.bp_mod_names:
                comp_lines.append(f"    BP ModActor: {', '.join(c.bp_mod_names)}")
            if c.config_overrides:
                comp_lines.append(f"    WARNING Config override: {', '.join(c.config_overrides)}")
        if self.manifest:
            comp_lines.append("")
            comp_lines.append("Deployed:")
            for e in self.manifest:
                comp_lines.append(f"  {e.deployed_name} ({e.kind})")
        comp_view.setPlainText("\n".join(comp_lines) or "-")
        comp_view.setMaximumHeight(130)
        comp_lay.addWidget(comp_view)
        root.addWidget(comp_group)

        # 3. Overrides & Conflicts section (Winning overrides, Losing overrides, Config conflicts)
        ov_conf_group = QGroupBox(self.t.tr("details.overrides_group").replace("&", "&&"))
        ov_conf_lay = QVBoxLayout(ov_conf_group)
        self.overrides_view = QPlainTextEdit()
        self.overrides_view.setReadOnly(True)
        self.overrides_view.setPlainText(self._build_overrides_text())
        ov_conf_lay.addWidget(self.overrides_view)
        root.addWidget(ov_conf_group, 1)

        # Footer
        footer = QHBoxLayout()
        footer.addStretch(1)
        btn_close = QPushButton(self.t.tr("btn.close"))
        btn_close.setFixedWidth(btn_width)
        btn_close.clicked.connect(self.accept)
        footer.addWidget(btn_close)
        root.addLayout(footer)

    def _apply_rename(self) -> None:
        new = self.edit_name.text().strip()
        if not new or new == self.mod.name or self.on_rename is None:
            return
        self.on_rename(new)
        self.mod.name = new
        self.setWindowTitle(f"{new} [{self.mod.badge}]")
        self.btn_rename.setEnabled(False)

    def _build_overrides_text(self) -> str:
        deployed_paks = {
            e.deployed_name
            for e in self.manifest
            if e.kind.startswith("pak_") and e.deployed_name.lower().endswith(".pak")
        }
        winning: list[str] = []
        losing: list[str] = []
        cfg_lines: list[str] = []

        for c in self.mod.components:
            if c.config_overrides:
                cfg_lines.append(f"  {c.name}: {', '.join(c.config_overrides)}")

        if self.conflict_report and deployed_paks:
            for asset, owners in sorted(self.conflict_report.asset_owners.items()):
                winner = owners[-1]
                losers = owners[:-1]
                if winner in deployed_paks:
                    other_losers = [o for o in losers if o not in deployed_paks]
                    if other_losers:
                        winning.append(f"  [{winner}] {asset}\n      overrides: {', '.join(other_losers)}")
                for lp in losers:
                    if lp in deployed_paks and winner not in deployed_paks:
                        losing.append(f"  [{lp}] {asset}\n      overridden by: {winner}")

        sections: list[str] = []
        if winning:
            sections.append(f"{self.t.tr('details.winning_overrides')} ({len(winning)}):")
            sections.extend(winning)
        if losing:
            if sections:
                sections.append("")
            sections.append(f"{self.t.tr('details.losing_overrides')} ({len(losing)}):")
            sections.extend(losing)
        if cfg_lines:
            if sections:
                sections.append("")
            sections.append(f"{self.t.tr('details.config_conflicts')} ({len(cfg_lines)}):")
            sections.extend(cfg_lines)

        if not sections:
            return self.t.tr("details.no_overrides")
        return "\n".join(sections)
