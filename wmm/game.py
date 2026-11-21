"""Game install detection across launchers and OSes (Steam, Epic, GOG, Game Pass, Proton).

Version-agnostic (§9): never hardcodes Win64, Windows config dir, build id, or engine version.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import PROJECT_NAME, STEAM_APP_ID
from .platform import Platform, get_platform

GAME_PROCESS_NAMES = [
    "Project_Plague-Win64-Shipping.exe",
    "Project_Plague-WinGDK-Shipping.exe",
    "Project_Plague.exe",
    "WUCHANG_EGS.exe",
    "gamelaunchhelper.exe",
]

SHIPPING_EXE_CANDIDATES = (
    ("Binaries/Win64/Project_Plague-Win64-Shipping.exe", "Win64", "Windows"),
    ("Binaries/WinGDK/Project_Plague-WinGDK-Shipping.exe", "WinGDK", "WinGDK"),
)


@dataclass
class GameInstall:
    root: Path
    launcher: str  # "steam", "epic", "gog", "gamepass", "manual"
    exe_path: Path
    binaries_dir: Path  # e.g. <root>/Project_Plague/Binaries/Win64 or WinGDK
    paks_dir: Path  # e.g. <root>/Project_Plague/Content/Paks
    mods_pak_dir: Path  # <paks_dir>/~mods
    logic_mods_dir: Path  # <paks_dir>/LogicMods
    ue4ss_dir: Path  # <binaries_dir>/ue4ss
    config_dir: Optional[Path] = None  # Saved/Config/Windows or WinGDK or Proton prefix
    build_id: str = ""
    engine_version: str = ""
    is_proton: bool = False
    epic_app_name: Optional[str] = None
    steam_root: Optional[Path] = None
    extra: dict = field(default_factory=dict)

    @property
    def engine_ini_path(self) -> Optional[Path]:
        if self.config_dir is None:
            return None
        return self.config_dir / "Engine.ini"

    @property
    def ue4ss_mods_dir(self) -> Path:
        return self.ue4ss_dir / "Mods"

    @property
    def ue4ss_mods_txt(self) -> Path:
        return self.ue4ss_mods_dir / "mods.txt"

    def launch_command(self) -> tuple[str, str]:
        """Return (kind, target) where kind is 'uri' or 'exe'."""
        if self.launcher == "steam":
            return ("uri", f"steam://rungameid/{STEAM_APP_ID}")
        if self.launcher == "epic" and self.epic_app_name:
            return ("uri", f"com.epicgames.launcher://apps/{self.epic_app_name}?action=launch")
        if self.launcher == "gamepass":
            helper = self.root / "gamelaunchhelper.exe"
            if helper.exists():
                return ("exe", str(helper))
        return ("exe", str(self.exe_path))


def find_ci_path(base: Path, rel_parts: tuple[str, ...] | list[str]) -> Optional[Path]:
    """Resolve a relative path case-insensitively (needed for Linux ext4 + Proton)."""
    cur = base
    for part in rel_parts:
        direct = cur / part
        if direct.exists():
            cur = direct
            continue
        if not cur.is_dir():
            return None
        low = part.lower()
        match = None
        try:
            for child in cur.iterdir():
                if child.name.lower() == low:
                    match = child
                    break
        except OSError:
            return None
        if match is None:
            return None
        cur = match
    return cur


def _read_pe_build_signature(exe_path: Path) -> str:
    """Derive a stable build id from PE TimeDateStamp + file size when no manifest exists."""
    try:
        st = exe_path.stat()
        with exe_path.open("rb") as f:
            dos = f.read(64)
            if len(dos) >= 64 and dos[:2] == b"MZ":
                pe_off = struct.unpack_from("<I", dos, 0x3C)[0]
                f.seek(pe_off)
                pe = f.read(24)
                if len(pe) >= 24 and pe[:4] == b"PE\x00\x00":
                    ts = struct.unpack_from("<I", pe, 8)[0]
                    return f"pe-{ts:08x}-{st.st_size}"
        h = hashlib.sha256(f"{st.st_size}:{int(st.st_mtime)}".encode()).hexdigest()[:12]
        return f"exe-{h}"
    except OSError:
        return "unknown"


def read_engine_version(exe_path: Path) -> str:
    """Read UE ProductVersion string (e.g. '++UE5+Release-5.1-CL-0') from the exe without OS-specific APIs."""
    pattern = re.compile(rb"(?:\+\x00\+\x00U\x00E\x00[45]\x00\+\x00(?:[\x20-\x7e]\x00){4,60})")
    try:
        size = exe_path.stat().st_size
        # Version resource is almost always near the end of the PE image (.rsrc section)
        # Scan last 4 MB first, then first 4 MB as fallback.
        with exe_path.open("rb") as f:
            tail_start = max(0, size - 4 * 1024 * 1024)
            f.seek(tail_start)
            chunk = f.read(4 * 1024 * 1024)
            m = pattern.search(chunk)
            if m:
                return m.group(0).decode("utf-16-le", errors="ignore").strip("\x00")
            if tail_start > 0:
                f.seek(0)
                chunk = f.read(min(size, 2 * 1024 * 1024))
                m = pattern.search(chunk)
                if m:
                    return m.group(0).decode("utf-16-le", errors="ignore").strip("\x00")
    except OSError:
        pass
    return ""


def parse_vdf_paths(vdf_text: str) -> list[str]:
    """Extract all 'path' values from Steam libraryfolders.vdf (both old and new formats)."""
    paths: list[str] = []
    for m in re.finditer(r'"path"\s+"([^"]+)"', vdf_text, re.IGNORECASE):
        raw = m.group(1).replace("\\\\", "\\")
        paths.append(raw)
    return paths


def parse_acf_buildid(acf_text: str) -> str:
    """Extract 'buildid' from Steam appmanifest_<appid>.acf."""
    m = re.search(r'"buildid"\s+"(\d+)"', acf_text, re.IGNORECASE)
    return m.group(1) if m else ""


def _resolve_config_dir(
    root: Path,
    config_subdir: str,
    platform: Platform,
    steam_library: Optional[Path] = None,
    is_proton: bool = False,
) -> Optional[Path]:
    if is_proton and steam_library is not None:
        compat = (
            steam_library
            / "steamapps"
            / "compatdata"
            / STEAM_APP_ID
            / "pfx"
            / "drive_c"
            / "users"
            / "steamuser"
            / "AppData"
            / "Local"
            / PROJECT_NAME
            / "Saved"
            / "Config"
            / config_subdir
        )
        return compat

    local_appdata = platform.default_local_appdata()
    if local_appdata is None:
        return None
    base = local_appdata / PROJECT_NAME / "Saved" / "Config"
    preferred = base / config_subdir
    if preferred.exists():
        return preferred
    # Auto-detect Windows vs WinGDK if preferred does not exist yet
    for alt in ("Windows", "WinGDK"):
        cand = base / alt
        if cand.exists():
            return cand
    return preferred


def validate_game_root(
    root: Path,
    launcher: str = "manual",
    platform: Optional[Platform] = None,
    steam_library: Optional[Path] = None,
    epic_app_name: Optional[str] = None,
    is_proton: bool = False,
) -> Optional[GameInstall]:
    """Validate a candidate game root directory and return a GameInstall, or None if invalid."""
    plat = platform or get_platform()
    root = Path(root)
    if not root.is_dir():
        return None

    proj_dir = find_ci_path(root, [PROJECT_NAME])
    if proj_dir is None:
        return None

    exe_path: Optional[Path] = None
    binaries_subdir = "Win64"
    config_subdir = "Windows"

    for rel, bsub, csub in SHIPPING_EXE_CANDIDATES:
        found = find_ci_path(proj_dir, rel.split("/"))
        if found is not None and found.is_file():
            exe_path = found
            binaries_subdir = bsub
            config_subdir = csub
            break

    if exe_path is None:
        return None

    binaries_dir = exe_path.parent
    paks_dir = find_ci_path(proj_dir, ["Content", "Paks"]) or (proj_dir / "Content" / "Paks")
    mods_pak_dir = find_ci_path(paks_dir, ["~mods"]) or (paks_dir / "~mods")
    logic_mods_dir = find_ci_path(paks_dir, ["LogicMods"]) or (paks_dir / "LogicMods")
    ue4ss_dir = find_ci_path(binaries_dir, ["ue4ss"]) or (binaries_dir / "ue4ss")

    # Infer Game Pass if WinGDK or gamelaunchhelper.exe is present
    if launcher == "manual":
        if binaries_subdir == "WinGDK" or (root / "gamelaunchhelper.exe").exists():
            launcher = "gamepass"
        elif (root / "WUCHANG_EGS.exe").exists():
            launcher = "epic"

    # Build ID: Steam appmanifest first, else PE signature
    build_id = ""
    if steam_library is not None:
        acf = steam_library / "steamapps" / f"appmanifest_{STEAM_APP_ID}.acf"
        if acf.is_file():
            try:
                build_id = parse_acf_buildid(acf.read_text(encoding="utf-8", errors="ignore"))
            except OSError:
                pass
    if not build_id:
        build_id = _read_pe_build_signature(exe_path)

    engine_version = read_engine_version(exe_path)
    config_dir = _resolve_config_dir(
        root=root,
        config_subdir=config_subdir,
        platform=plat,
        steam_library=steam_library,
        is_proton=is_proton,
    )

    return GameInstall(
        root=root,
        launcher=launcher,
        exe_path=exe_path,
        binaries_dir=binaries_dir,
        paks_dir=paks_dir,
        mods_pak_dir=mods_pak_dir,
        logic_mods_dir=logic_mods_dir,
        ue4ss_dir=ue4ss_dir,
        config_dir=config_dir,
        build_id=build_id,
        engine_version=engine_version,
        is_proton=is_proton,
        epic_app_name=epic_app_name,
        steam_root=steam_library,
    )


def detect_steam_installs(
    steam_roots: Optional[list[Path]] = None,
    platform: Optional[Platform] = None,
) -> list[GameInstall]:
    """Detect Steam installs across all libraryfolders.vdf entries."""
    plat = platform or get_platform()
    if steam_roots is None:
        steam_roots = []
        if plat.name == "windows":
            # Check registry + common paths
            try:
                import winreg
                for key_path in (r"SOFTWARE\WOW6432Node\Valve\Steam", r"SOFTWARE\Valve\Steam"):
                    try:
                        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path) as k:
                            val, _ = winreg.QueryValueEx(k, "InstallPath")
                            steam_roots.append(Path(val))
                    except OSError:
                        pass
                try:
                    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
                        val, _ = winreg.QueryValueEx(k, "SteamPath")
                        steam_roots.append(Path(val))
                except OSError:
                    pass
            except ImportError:
                pass
            for cand in (Path(r"C:\Program Files (x86)\Steam"), Path(r"C:\Steam"), Path(r"C:\game\steam")):
                if cand.is_dir():
                    steam_roots.append(cand)
        else:
            home = Path.home()
            for cand in (
                home / ".local/share/Steam",
                home / ".steam/steam",
                home / ".var/app/com.valvesoftware.Steam/.local/share/Steam",
            ):
                if cand.is_dir():
                    steam_roots.append(cand)

    libraries: list[Path] = []
    seen_libs: set[str] = set()
    for sr in steam_roots:
        if not sr.is_dir():
            continue
        key = str(sr.resolve()).lower()
        if key not in seen_libs:
            seen_libs.add(key)
            libraries.append(sr)
        vdf = sr / "steamapps" / "libraryfolders.vdf"
        if vdf.is_file():
            try:
                for p in parse_vdf_paths(vdf.read_text(encoding="utf-8", errors="ignore")):
                    lp = Path(p)
                    lk = str(lp).lower()
                    if lk not in seen_libs:
                        seen_libs.add(lk)
                        libraries.append(lp)
            except OSError:
                pass

    results: list[GameInstall] = []
    seen_roots: set[str] = set()
    for lib in libraries:
        # Prefer reading installdir from appmanifest_2277560.acf
        install_dirs = ["Wuchang Fallen Feathers"]
        acf = lib / "steamapps" / f"appmanifest_{STEAM_APP_ID}.acf"
        if acf.is_file():
            try:
                txt = acf.read_text(encoding="utf-8", errors="ignore")
                m = re.search(r'"installdir"\s+"([^"]+)"', txt, re.IGNORECASE)
                if m and m.group(1) not in install_dirs:
                    install_dirs.insert(0, m.group(1))
            except OSError:
                pass

        for idir in install_dirs:
            cand = lib / "steamapps" / "common" / idir
            if not cand.is_dir():
                continue
            rk = str(cand.resolve()).lower()
            if rk in seen_roots:
                continue
            is_proton = plat.name == "linux"
            inst = validate_game_root(
                cand,
                launcher="steam",
                platform=plat,
                steam_library=lib,
                is_proton=is_proton,
            )
            if inst is not None:
                seen_roots.add(rk)
                results.append(inst)
    return results


def detect_epic_installs(
    manifests_dir: Optional[Path] = None,
    platform: Optional[Platform] = None,
) -> list[GameInstall]:
    """Detect Epic Games Store installs from Manifests/*.item JSON files."""
    plat = platform or get_platform()
    if manifests_dir is None:
        progdata = os.environ.get("PROGRAMDATA", r"C:\ProgramData")
        manifests_dir = Path(progdata) / "Epic" / "EpicGamesLauncher" / "Data" / "Manifests"
    if not manifests_dir.is_dir():
        return []

    results: list[GameInstall] = []
    for item in manifests_dir.glob("*.item"):
        try:
            data = json.loads(item.read_text(encoding="utf-8", errors="ignore"))
        except (OSError, json.JSONDecodeError):
            continue
        loc = data.get("InstallLocation")
        if not loc:
            continue
        inst = validate_game_root(
            Path(loc),
            launcher="epic",
            platform=plat,
            epic_app_name=data.get("AppName"),
        )
        if inst is not None:
            results.append(inst)
    return results


def detect_gamepass_installs(
    xbox_roots: Optional[list[Path]] = None,
    platform: Optional[Platform] = None,
) -> list[GameInstall]:
    """Detect Xbox Game Pass / GDK installs under <Drive>:\\XboxGames\\*\\Content."""
    plat = platform or get_platform()
    if xbox_roots is None:
        xbox_roots = []
        if plat.name == "windows":
            for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
                cand = Path(f"{letter}:\\XboxGames")
                if cand.is_dir():
                    xbox_roots.append(cand)
    results: list[GameInstall] = []
    for xr in xbox_roots:
        if not xr.is_dir():
            continue
        try:
            children = list(xr.iterdir())
        except OSError:
            continue
        for child in children:
            for cand in (child / "Content", child):
                if cand.is_dir():
                    inst = validate_game_root(cand, launcher="gamepass", platform=plat)
                    if inst is not None:
                        results.append(inst)
                        break
    return results


def detect_all_installs(platform: Optional[Platform] = None) -> list[GameInstall]:
    """Run all launcher detectors in order and deduplicate by resolved root."""
    plat = platform or get_platform()
    found: list[GameInstall] = []
    seen: set[str] = set()
    for inst in (
        detect_steam_installs(platform=plat)
        + detect_epic_installs(platform=plat)
        + detect_gamepass_installs(platform=plat)
    ):
        key = str(inst.root.resolve()).lower()
        if key not in seen:
            seen.add(key)
            found.append(inst)
    return found


def running_game_processes(platform: Optional[Platform] = None) -> list[str]:
    """Return list of running game executable names, empty if game is closed."""
    plat = platform or get_platform()
    return plat.is_process_running(GAME_PROCESS_NAMES)
