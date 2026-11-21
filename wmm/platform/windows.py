"""Windows platform implementation for WMM."""

from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes
import locale
import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from . import Platform

try:
    import _winapi
except ImportError:  # pragma: no cover - non-Windows
    _winapi = None

try:
    import winreg
except ImportError:  # pragma: no cover - non-Windows
    winreg = None


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", ctypes.wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


class WindowsPlatform(Platform):
    @property
    def name(self) -> str:
        return "windows"

    def create_dir_link(self, link_path: Path, target_path: Path) -> None:
        link_path = Path(link_path)
        target_path = Path(target_path).resolve()
        link_path.parent.mkdir(parents=True, exist_ok=True)
        if _winapi is None:
            raise RuntimeError("_winapi not available on this platform")
        _winapi.CreateJunction(str(target_path), str(link_path))

    def is_dir_link(self, path: Path) -> bool:
        path = Path(path)
        is_junc = getattr(os.path, "isjunction", None)
        if is_junc is not None and is_junc(path):
            return True
        return path.is_symlink()

    def remove_dir_link(self, link_path: Path) -> None:
        link_path = Path(link_path)
        if not self.is_dir_link(link_path):
            raise ValueError(f"Refusing to remove non-link directory: {link_path}")
        os.rmdir(link_path)

    def same_volume(self, a: Path, b: Path) -> bool:
        pa = Path(a)
        pb = Path(b)
        # Resolve existing parents so non-existent leaf paths still work
        while not pa.exists() and pa.parent != pa:
            pa = pa.parent
        while not pb.exists() and pb.parent != pb:
            pb = pb.parent
        if pa.drive and pb.drive:
            return pa.drive.upper() == pb.drive.upper()
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
        val = os.environ.get("LOCALAPPDATA")
        return Path(val) if val else None

    def is_process_running(self, exe_names: list[str]) -> list[str]:
        if not exe_names:
            return []
        targets = {e.lower() for e in exe_names}
        try:
            res = subprocess.run(
                ["tasklist", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                timeout=5,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if res.returncode != 0:
                return []
            found: list[str] = []
            seen: set[str] = set()
            for line in res.stdout.splitlines():
                line = line.strip()
                if not line.startswith('"'):
                    continue
                img = line.split('"', 2)[1]
                low = img.lower()
                if low in targets and low not in seen:
                    seen.add(low)
                    found.append(img)
            return found
        except Exception:
            return []

    def launch_uri(self, uri: str) -> None:
        os.startfile(uri)  # type: ignore[attr-defined]

    def launch_exe(self, exe_path: Path, args: Optional[list[str]] = None) -> None:
        cmd = [str(exe_path)] + (args or [])
        subprocess.Popen(cmd, cwd=str(exe_path.parent))

    def protect_secret(self, plaintext: str) -> str:
        if not plaintext:
            return ""
        raw = plaintext.encode("utf-8")
        buf = ctypes.create_string_buffer(raw, len(raw))
        in_blob = _DATA_BLOB(len(raw), buf)
        out_blob = _DATA_BLOB()
        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        ok = crypt32.CryptProtectData(
            ctypes.byref(in_blob),
            "WMM",
            None,
            None,
            None,
            0,
            ctypes.byref(out_blob),
        )
        if not ok:
            raise OSError("CryptProtectData failed")
        try:
            encrypted = ctypes.string_at(out_blob.pbData, out_blob.cbData)
            return "dpapi:" + base64.b64encode(encrypted).decode("ascii")
        finally:
            kernel32.LocalFree(out_blob.pbData)

    def unprotect_secret(self, token: str) -> str:
        if not token:
            return ""
        if not token.startswith("dpapi:"):
            return token
        raw = base64.b64decode(token[6:])
        buf = ctypes.create_string_buffer(raw, len(raw))
        in_blob = _DATA_BLOB(len(raw), buf)
        out_blob = _DATA_BLOB()
        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        ok = crypt32.CryptUnprotectData(
            ctypes.byref(in_blob),
            None,
            None,
            None,
            None,
            0,
            ctypes.byref(out_blob),
        )
        if not ok:
            raise OSError("CryptUnprotectData failed")
        try:
            decrypted = ctypes.string_at(out_blob.pbData, out_blob.cbData)
            return decrypted.decode("utf-8")
        finally:
            kernel32.LocalFree(out_blob.pbData)

    def register_nxm(self, wmm_exe: Path) -> Optional[str]:
        if winreg is None:
            return None
        cmd_key = r"Software\Classes\nxm\shell\open\command"
        prev: Optional[str] = None
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, cmd_key, 0, winreg.KEY_READ) as k:
                prev, _ = winreg.QueryValueEx(k, "")
        except FileNotFoundError:
            prev = None
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Classes\nxm") as k:
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ, "URL:Nexus Mod Manager Protocol")
            winreg.SetValueEx(k, "URL Protocol", 0, winreg.REG_SZ, "")
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, cmd_key) as k:
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ, f'"{wmm_exe}" cli nxm "%1"')
        return prev

    def unregister_nxm(self, previous_handler: Optional[str] = None) -> None:
        if winreg is None:
            return
        cmd_key = r"Software\Classes\nxm\shell\open\command"
        if previous_handler:
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, cmd_key) as k:
                winreg.SetValueEx(k, "", 0, winreg.REG_SZ, previous_handler)
        else:
            for sub in (
                r"Software\Classes\nxm\shell\open\command",
                r"Software\Classes\nxm\shell\open",
                r"Software\Classes\nxm\shell",
                r"Software\Classes\nxm",
            ):
                try:
                    winreg.DeleteKey(winreg.HKEY_CURRENT_USER, sub)
                except FileNotFoundError:
                    pass

    def set_dll_override(self, steam_app_id: str, dll_name: str, steam_root: Optional[Path] = None) -> bool:
        return True  # Not needed on native Windows

    def ui_language(self) -> str:
        try:
            loc = locale.getlocale()[0]
            if loc:
                return loc.replace("_", "-")
        except Exception:
            pass
        return "en"
