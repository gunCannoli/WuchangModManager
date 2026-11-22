"""UE4SS mods.txt (LF-only) writer, mods.json sync, and install/version detection (§4.3, §5.1)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

BASE_UE4SS_MODS = [
    "ConsoleCommandsMod",
    "ConsoleEnablerMod",
    "SplitScreenMod",
    "LineTraceMod",
    "BPModLoaderMod",
    "BPML_GenericFunctions",
    "jsbLuaProfilerMod",
]

# Keybinds must always be last in mods.txt
LAST_UE4SS_MOD = "Keybinds"

MODS_TXT_LINE_RE = re.compile(r"^\s*([^;:#\s][^:=]*?)\s*:\s*([01])\s*$")
UE4SS_LOG_VER_RE = re.compile(r"UE4SS(?:\s*-\s*|\s+)(v[^\r\n]+)", re.IGNORECASE)


@dataclass
class UE4SSStatus:
    installed: bool
    proxy_dll: Optional[Path] = None
    ue4ss_dll: Optional[Path] = None
    version_line: str = ""
    is_fork: bool = False
    mods_txt_exists: bool = False
    mods_txt_has_crlf: bool = False
    settings_ini: Optional[Path] = None


def detect_ue4ss(binaries_dir: Path) -> UE4SSStatus:
    """Inspect binaries_dir (Win64 / WinGDK) for an existing UE4SS installation."""
    binaries_dir = Path(binaries_dir)
    ue4ss_dir = binaries_dir / "ue4ss"
    ue4ss_dll = ue4ss_dir / "UE4SS.dll"

    proxy_dll: Optional[Path] = None
    for cand_name in ("dwmapi.dll", "version.dll", "xinput1_3.dll", "xinput1_4.dll"):
        cand = binaries_dir / cand_name
        if cand.is_file():
            proxy_dll = cand
            break

    installed = ue4ss_dll.is_file() and proxy_dll is not None
    version_line = ""
    is_fork = False

    log_path = ue4ss_dir / "UE4SS.log"
    if log_path.is_file():
        try:
            head = log_path.read_text(encoding="utf-8", errors="ignore")[:2048]
            m = UE4SS_LOG_VER_RE.search(head)
            if m:
                version_line = m.group(1).strip()
            if "fork" in version_line.lower() or "a076aea6" in version_line.lower() or "guncannoli" in version_line.lower():
                is_fork = True
        except OSError:
            pass

    mods_txt = ue4ss_dir / "Mods" / "mods.txt"
    mods_txt_exists = mods_txt.is_file()
    mods_txt_has_crlf = False
    if mods_txt_exists:
        try:
            mods_txt_has_crlf = b"\r\n" in mods_txt.read_bytes()
        except OSError:
            pass

    settings_ini = ue4ss_dir / "UE4SS-settings.ini"
    return UE4SSStatus(
        installed=installed,
        proxy_dll=proxy_dll,
        ue4ss_dll=ue4ss_dll if ue4ss_dll.is_file() else None,
        version_line=version_line,
        is_fork=is_fork,
        mods_txt_exists=mods_txt_exists,
        mods_txt_has_crlf=mods_txt_has_crlf,
        settings_ini=settings_ini if settings_ini.is_file() else None,
    )


def parse_mods_txt(path: Path) -> list[tuple[str, bool]]:
    """Parse UE4SS mods.txt into an ordered list of (mod_name, enabled)."""
    if not path.is_file():
        return []
    entries: list[tuple[str, bool]] = []
    seen: set[str] = set()
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        s = line.strip()
        if not s or s.startswith(";"):
            continue
        m = MODS_TXT_LINE_RE.match(s)
        if not m:
            continue
        name = m.group(1).strip()
        enabled = m.group(2) == "1"
        low = name.lower()
        if low not in seen:
            seen.add(low)
            entries.append((name, enabled))
    return entries


def write_mods_txt(
    mods_txt_path: Path,
    managed_mods: list[tuple[str, bool]],
    preserve_unmanaged: bool = True,
    sync_mods_json: bool = True,
    remove_names: Optional[set[str]] = None,
) -> list[tuple[str, bool]]:
    """Write UE4SS mods.txt strictly with LF line endings (§4.3).

    Order:
    1. Base UE4SS mods (preserving existing enabled state if present)
    2. Managed mods (in the order provided)
    3. Unmanaged mods already in mods.txt (if preserve_unmanaged=True, excluding remove_names)
    4. Keybinds : 1 (always last)
    """
    mods_txt_path = Path(mods_txt_path)
    existing = parse_mods_txt(mods_txt_path)
    existing_map = {n.lower(): (n, en) for n, en in existing}
    managed_map = {n.lower(): (n, en) for n, en in managed_mods}
    remove_lower = {n.lower() for n in (remove_names or set())}

    ordered: list[tuple[str, bool]] = []
    emitted: set[str] = set()

    def add(name: str, enabled: bool) -> None:
        low = name.lower()
        if low not in emitted:
            emitted.add(low)
            ordered.append((name, enabled))

    # 1. Base UE4SS mods first
    for base_name in BASE_UE4SS_MODS:
        low = base_name.lower()
        if low in managed_map:
            add(*managed_map[low])
        elif low in existing_map:
            add(*existing_map[low])

    # 2. Managed mods in requested order (excluding Keybinds, which goes last)
    for name, enabled in managed_mods:
        if name.lower() == LAST_UE4SS_MOD.lower():
            continue
        add(name, enabled)

    # 3. Unmanaged mods from existing mods.txt
    if preserve_unmanaged:
        for name, enabled in existing:
            low = name.lower()
            if low == LAST_UE4SS_MOD.lower() or low in remove_lower:
                continue
            if low not in emitted:
                add(name, enabled)

    # 4. Keybinds always last
    kb_low = LAST_UE4SS_MOD.lower()
    if kb_low in managed_map:
        add(*managed_map[kb_low])
    elif kb_low in existing_map:
        add(*existing_map[kb_low])
    else:
        add(LAST_UE4SS_MOD, True)

    mods_txt_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"{name} : {1 if en else 0}" for name, en in ordered]
    content = "\n".join(lines) + "\n"
    # Strictly LF only — never CRLF!
    mods_txt_path.write_bytes(content.encode("utf-8"))

    if sync_mods_json:
        mods_json_path = mods_txt_path.parent / "mods.json"
        if mods_json_path.is_file():
            try:
                data = json.loads(mods_json_path.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    state_map = {n.lower(): en for n, en in ordered}
                    for item in data:
                        if isinstance(item, dict) and "mod_name" in item:
                            low = str(item["mod_name"]).lower()
                            if low in state_map:
                                item["mod_enabled"] = state_map[low]
                            elif low in remove_lower:
                                item["mod_enabled"] = False
                    mods_json_path.write_bytes(
                        (json.dumps(data, indent=4) + "\n").encode("utf-8")
                    )
            except (OSError, json.JSONDecodeError):
                pass

    return ordered
