"""OS abstraction layer for WMM (Windows and Linux/Proton).

Everything above this package is platform-free. Unit tests can inject a mock
Platform implementation so detection, deployment, and config logic run on any OS.
"""

from __future__ import annotations

import os
import sys
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional


class Platform(ABC):
    """Abstract OS operations required by WMM."""

    @property
    @abstractmethod
    def name(self) -> str:
        """'windows' or 'linux'."""

    @abstractmethod
    def create_dir_link(self, link_path: Path, target_path: Path) -> None:
        """Create a directory junction (Windows) or symlink (Linux)."""

    @abstractmethod
    def is_dir_link(self, path: Path) -> bool:
        """Return True if path is a directory junction or symlink."""

    @abstractmethod
    def remove_dir_link(self, link_path: Path) -> None:
        """Remove a directory junction or symlink without touching its target."""

    @abstractmethod
    def same_volume(self, a: Path, b: Path) -> bool:
        """Return True if two paths reside on the same filesystem/volume."""

    @abstractmethod
    def hardlink_or_move(self, src: Path, dst: Path) -> str:
        """Deploy a file with zero duplicate bytes: hardlink if same volume, else move.

        Returns 'hardlink' or 'move'.
        """

    @abstractmethod
    def default_local_appdata(self) -> Optional[Path]:
        """Return the OS user local config base (%LOCALAPPDATA% on Windows)."""

    @abstractmethod
    def is_process_running(self, exe_names: list[str]) -> list[str]:
        """Return subset of exe_names currently running."""

    @abstractmethod
    def launch_uri(self, uri: str) -> None:
        """Open a launcher URI (steam://, com.epicgames.launcher://, etc.)."""

    @abstractmethod
    def launch_exe(self, exe_path: Path, args: Optional[list[str]] = None) -> None:
        """Launch an executable directly."""

    @abstractmethod
    def protect_secret(self, plaintext: str) -> str:
        """Encrypt/obfuscate a secret (e.g. Nexus API key) for local storage."""

    @abstractmethod
    def unprotect_secret(self, token: str) -> str:
        """Decrypt a secret previously protected by protect_secret."""

    @abstractmethod
    def register_nxm(self, wmm_exe: Path) -> Optional[str]:
        """Register nxm:// handler. Returns previous handler command if any."""

    @abstractmethod
    def unregister_nxm(self, previous_handler: Optional[str] = None) -> None:
        """Unregister nxm:// handler, optionally restoring previous_handler."""

    @abstractmethod
    def set_dll_override(self, steam_app_id: str, dll_name: str, steam_root: Optional[Path] = None) -> bool:
        """Configure DLL override (no-op on Windows; Proton WINEDLLOVERRIDES on Linux)."""

    @abstractmethod
    def ui_language(self) -> str:
        """Return OS UI locale code (e.g. 'en', 'pt-BR')."""


_cached_platform: Optional[Platform] = None


def get_platform() -> Platform:
    """Return the Platform implementation for the current OS."""
    global _cached_platform
    if _cached_platform is None:
        if sys.platform == "win32":
            from .windows import WindowsPlatform
            _cached_platform = WindowsPlatform()
        else:
            from .linux import LinuxPlatform
            _cached_platform = LinuxPlatform()
    return _cached_platform
