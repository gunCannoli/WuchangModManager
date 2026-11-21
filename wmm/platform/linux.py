"""Linux / Proton platform implementation for WMM."""

from __future__ import annotations

import base64
import locale
import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from . import Platform


class LinuxPlatform(Platform):
    @property
    def name(self) -> str:
        return "linux"

    def create_dir_link(self, link_path: Path, target_path: Path) -> None:
        link_path = Path(link_path)
        target_path = Path(target_path).resolve()
        link_path.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(target_path, link_path, target_is_directory=True)

    def is_dir_link(self, path: Path) -> bool:
        return Path(path).is_symlink()

    def remove_dir_link(self, link_path: Path) -> None:
        link_path = Path(link_path)
        if not self.is_dir_link(link_path):
            raise ValueError(f"Refusing to remove non-symlink directory: {link_path}")
        link_path.unlink()

    def same_volume(self, a: Path, b: Path) -> bool:
        pa = Path(a)
        pb = Path(b)
        while not pa.exists() and pa.parent != pa:
            pa = pa.parent
        while not pb.exists() and pb.parent != pb:
            pb = pb.parent
        try:
            return os.stat(pa).st_dev == os.stat(pb).st_dev
        except OSError:
            return False

    def hardlink_or_move(self, src: Path, dst: Path) -> str:
        src = Path(src)
        dst = Path(dst)
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            dst.unlink()
        if self.same_volume(src, dst):
            try:
                os.link(src, dst)
                return "hardlink"
            except OSError:
                pass
        shutil.move(str(src), str(dst))
        return "move"

    def default_local_appdata(self) -> Optional[Path]:
        # On Linux + Proton, Engine.ini lives inside the Proton compatdata prefix,
        # which game.py resolves directly. Fallback to XDG_DATA_HOME.
        xdg = os.environ.get("XDG_DATA_HOME")
        if xdg:
            return Path(xdg)
        return Path.home() / ".local" / "share"

    def is_process_running(self, exe_names: list[str]) -> list[str]:
        if not exe_names:
            return []
        targets = {e.lower() for e in exe_names}
        found: list[str] = []
        seen: set[str] = set()
        proc = Path("/proc")
        if not proc.is_dir():
            return []
        for entry in proc.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                cmdline = (entry / "cmdline").read_bytes().replace(b"\x00", b" ").decode("utf-8", "ignore").lower()
            except OSError:
                continue
            for t in targets:
                if t in cmdline and t not in seen:
                    seen.add(t)
                    found.append(t)
        return found

    def launch_uri(self, uri: str) -> None:
        subprocess.Popen(["xdg-open", uri])

    def launch_exe(self, exe_path: Path, args: Optional[list[str]] = None) -> None:
        cmd = [str(exe_path)] + (args or [])
        subprocess.Popen(cmd, cwd=str(exe_path.parent))

    def protect_secret(self, plaintext: str) -> str:
        if not plaintext:
            return ""
        # Obfuscated local storage fallback (libsecret can be layered later)
        return "b64:" + base64.b64encode(plaintext.encode("utf-8")).decode("ascii")

    def unprotect_secret(self, token: str) -> str:
        if not token:
            return ""
        if token.startswith("b64:"):
            return base64.b64decode(token[4:]).decode("utf-8")
        return token

    def register_nxm(self, wmm_exe: Path) -> Optional[str]:
        apps = Path.home() / ".local" / "share" / "applications"
        apps.mkdir(parents=True, exist_ok=True)
        desktop = apps / "wmm-nxm.desktop"
        desktop.write_text(
            "[Desktop Entry]\n"
            "Type=Application\n"
            "Name=Wuchang Mod Manager\n"
            f'Exec="{wmm_exe}" cli nxm %u\n'
            "MimeType=x-scheme-handler/nxm;\n"
            "NoDisplay=true\n",
            encoding="utf-8",
            newline="\n",
        )
        try:
            subprocess.run(["xdg-mime", "default", "wmm-nxm.desktop", "x-scheme-handler/nxm"], check=False)
        except FileNotFoundError:
            pass
        return None

    def unregister_nxm(self, previous_handler: Optional[str] = None) -> None:
        desktop = Path.home() / ".local" / "share" / "applications" / "wmm-nxm.desktop"
        if desktop.exists():
            desktop.unlink()

    def set_dll_override(self, steam_app_id: str, dll_name: str, steam_root: Optional[Path] = None) -> bool:
        # Steam localconfig.vdf editing is intentionally conservative; return False
        # when not configured so onboarding can display the exact launch option string.
        return False

    def ui_language(self) -> str:
        try:
            loc = locale.getlocale()[0]
            if loc:
                return loc.replace("_", "-")
        except Exception:
            pass
        return "en"
