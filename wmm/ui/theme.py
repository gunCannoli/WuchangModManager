"""Dark Fusion palette + minimal QSS, one accent colour, no animations (§2.1)."""

from __future__ import annotations

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

ACCENT_HEX = "#C85A32"  # Warm vermilion accent

DARK_QSS = f"""
QMainWindow, QDialog {{
    background-color: #1A1B1E;
    color: #E2E4E9;
}}
QToolBar {{
    background-color: #202226;
    border-bottom: 1px solid #2E3138;
    spacing: 6px;
    padding: 4px 8px;
}}
QPushButton, QToolButton {{
    background-color: #2A2D34;
    color: #E2E4E9;
    border: 1px solid #3A3E47;
    border-radius: 3px;
    padding: 5px 10px;
}}
QPushButton:hover, QToolButton:hover {{
    background-color: #343842;
    border-color: {ACCENT_HEX};
}}
QPushButton:pressed, QToolButton:pressed {{
    background-color: #23262C;
}}
QPushButton:disabled {{
    color: #6B707B;
    background-color: #222429;
}}
QPushButton[accent="true"] {{
    background-color: {ACCENT_HEX};
    border-color: {ACCENT_HEX};
    color: #FFFFFF;
    font-weight: 600;
}}
QPushButton[accent="true"]:hover {{
    background-color: #D9683F;
}}
QTableWidget, QTreeWidget, QListWidget, QTextEdit, QPlainTextEdit {{
    background-color: #16171A;
    alternate-background-color: #1C1E22;
    color: #E2E4E9;
    border: 1px solid #2E3138;
    gridline-color: #262930;
    selection-background-color: {ACCENT_HEX};
    selection-color: #FFFFFF;
}}
QHeaderView::section {{
    background-color: #202226;
    color: #AEB3BE;
    border: none;
    border-right: 1px solid #2E3138;
    border-bottom: 1px solid #2E3138;
    padding: 5px 8px;
    font-weight: 600;
}}
QLineEdit, QComboBox {{
    background-color: #16171A;
    color: #E2E4E9;
    border: 1px solid #3A3E47;
    border-radius: 3px;
    padding: 4px 8px;
}}
QLineEdit:focus, QComboBox:focus {{
    border-color: {ACCENT_HEX};
}}
QStatusBar {{
    background-color: #202226;
    color: #AEB3BE;
    border-top: 1px solid #2E3138;
}}
QGroupBox {{
    border: 1px solid #2E3138;
    border-radius: 3px;
    margin-top: 8px;
    padding-top: 10px;
    color: #AEB3BE;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 8px;
    padding: 0 4px;
}}
QSplitter::handle {{
    background-color: #2E3138;
}}
"""


def apply_dark_theme(app: QApplication) -> None:
    app.setStyle("Fusion")
    pal = QPalette()
    pal.setColor(QPalette.ColorRole.Window, QColor("#1A1B1E"))
    pal.setColor(QPalette.ColorRole.WindowText, QColor("#E2E4E9"))
    pal.setColor(QPalette.ColorRole.Base, QColor("#16171A"))
    pal.setColor(QPalette.ColorRole.AlternateBase, QColor("#1C1E22"))
    pal.setColor(QPalette.ColorRole.Text, QColor("#E2E4E9"))
    pal.setColor(QPalette.ColorRole.Button, QColor("#2A2D34"))
    pal.setColor(QPalette.ColorRole.ButtonText, QColor("#E2E4E9"))
    pal.setColor(QPalette.ColorRole.Highlight, QColor(ACCENT_HEX))
    pal.setColor(QPalette.ColorRole.HighlightedText, QColor("#FFFFFF"))
    pal.setColor(QPalette.ColorRole.PlaceholderText, QColor("#7A808C"))
    app.setPalette(pal)
    app.setStyleSheet(DARK_QSS)
