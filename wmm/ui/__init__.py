"""PySide6 dark Fusion UI for Wuchang Mod Manager (§2.1, §5)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional


def _app_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent.parent


def launch_gui(
    data_dir: Optional[Path] = None,
    game_override: Optional[Path] = None,
    initial_nxm_uri: Optional[str] = None,
) -> int:
    """Launch the PySide6 WMM main window (exits cleanly if initial onboarding is closed)."""
    from PySide6.QtCore import QTimer
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication, QDialog

    from ..game import detect_all_installs, validate_game_root
    from ..i18n import Translator
    from ..store import Store
    from .main_window import MainWindow
    from .onboarding import OnboardingDialog
    from .theme import apply_dark_theme

    app = QApplication.instance() or QApplication(sys.argv)
    apply_dark_theme(app)

    root = _app_root()
    for icon_cand in (root / "assets" / "wmm.ico", root / "assets" / "wmm.png"):
        if icon_cand.is_file():
            app.setWindowIcon(QIcon(str(icon_cand)))
            break

    store = Store(data_dir or (root / "data"))
    settings = store.load_settings()
    game = validate_game_root(game_override) if game_override else (
        validate_game_root(Path(settings["game_path"])) if settings.get("game_path") else None
    )
    if game is None:
        found = detect_all_installs()
        game = found[0] if found else None

    # First-run onboarding: closing or canceling exits the whole app and re-prompts next launch
    if game is None or not settings.get("onboarded", False):
        t = Translator(root / "lang", language=settings.get("language", "en"))
        dlg = OnboardingDialog(store, t, game=game, package_root=root)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return 0
        game = dlg.game

    win = MainWindow(
        data_dir=data_dir or (root / "data"),
        game_override=game.root if game else game_override,
        run_onboarding_if_needed=False,
    )
    win.show()
    if initial_nxm_uri:
        QTimer.singleShot(150, lambda: win.handle_nxm_uri(initial_nxm_uri))
    return app.exec()
