"""Mod base detection and setup: UE4SS, community enabler (bitfix), foreign proxies, Lua BP loader (§5)."""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from .engineini import read_wff_bploader_mods
from .game import GameInstall, running_game_processes
from .platform import Platform, get_platform
from .store import ManifestEntry, Store
from .ue4ss import UE4SSStatus, detect_ue4ss, write_mods_txt

# Lua-first WMM BP Loader (§5.3): spawns /Game/Mods/<Name>/ModActor.ModActor_C
# after entry map load without claiming GameDefaultMap.
WMM_LUA_BPLOADER_SCRIPT = """-- WMM_BPLoader (Lua mode): spawns Blueprint ModActors listed in wmm_bp_mods.txt
-- Runs via UE4SS without claiming GameDefaultMap.
local UEHelpers = require("UEHelpers")

local MOD_LIST_FILE = "Mods/WMM_BPLoader/wmm_bp_mods.txt"
local spawned = {}

local function log(msg)
    print("[WMM_BPLoader] " .. tostring(msg) .. "\\n")
end

local function read_mod_list()
    local out = {}
    local f = io.open(MOD_LIST_FILE, "r")
    if not f then return out end
    for line in f:lines() do
        local name = line:match("^%s*([^;#%s][^\\r\\n]-)%s*$")
        if name and name ~= "" then
            out[#out + 1] = name
        end
    end
    f:close()
    return out
end

local function spawn_mod_actor(world, mod_name)
    if not world or not world:IsValid() then return end
    local ar_helpers = StaticFindObject("/Script/AssetRegistry.Default__AssetRegistryHelpers")
    if not ar_helpers or not ar_helpers:IsValid() then
        log("AssetRegistryHelpers unavailable; skipping " .. mod_name)
        return
    end
    local pkg = "/Game/Mods/" .. mod_name .. "/ModActor"
    local asset_data = {
        PackageName = UEHelpers.FindOrAddFName(pkg),
        AssetName = UEHelpers.FindOrAddFName("ModActor_C"),
    }
    local cls = ar_helpers:GetAsset(asset_data)
    if not cls or not cls:IsValid() then
        log("missing: " .. pkg .. ".ModActor_C")
        return
    end
    local actor = world:SpawnActor(cls, {}, {})
    if actor and actor:IsValid() then
        log("spawned: " .. mod_name .. " -> " .. actor:GetFullName())
        if actor.PreBeginPlay and actor.PreBeginPlay:IsValid() then
            pcall(function() actor:PreBeginPlay() end)
        end
    else
        log("spawn failed: " .. mod_name)
    end
end

local function spawn_all_for_world(world)
    if not world or not world:IsValid() then return end
    local wname = ""
    pcall(function() wname = world:GetFullName() end)
    if wname == "" or wname:find("Entry", 1, true) or wname:find("GameInitMap", 1, true) then
        return
    end
    if spawned[wname] then return end
    spawned[wname] = true
    local list = read_mod_list()
    if #list == 0 then return end
    log("world ready (" .. wname .. "), spawning " .. tostring(#list) .. " BP mod(s)")
    for _, mod_name in ipairs(list) do
        pcall(spawn_mod_actor, world, mod_name)
    end
end

RegisterHook("/Script/Engine.PlayerController:ClientRestart", function(ctx)
    local pc = ctx:get()
    if pc and pc:IsValid() then
        local ok, world = pcall(function() return pc:GetWorld() end)
        if ok and world and world:IsValid() then
            spawn_all_for_world(world)
        end
    end
end)

log("loaded (Lua BP loader mode)")
"""


@dataclass
class EnablerStatus:
    present: bool
    kind: str = "none"  # "wmm", "bitfix", "none"
    files: list[str] = field(default_factory=list)
    log_status: str = ""


@dataclass
class BPLoaderStatus:
    kind: str  # "wmm_lua", "wff_pak", "none"
    files: list[str] = field(default_factory=list)
    wff_ini_entries: list[str] = field(default_factory=list)


@dataclass
class ModBaseStatus:
    ue4ss: UE4SSStatus
    enabler: EnablerStatus
    bp_loader: BPLoaderStatus
    engine_ini_exists: bool
    foreign_proxies: list[str] = field(default_factory=list)


def _ue4ss_ini_has_unsigned_paks(ue4ss_ini: Path) -> bool:
    if not ue4ss_ini.is_file():
        return False
    try:
        for line in ue4ss_ini.read_text(encoding="utf-8", errors="ignore").splitlines():
            s = line.strip()
            if s.lower().startswith("enableunsignedpaks") and "=" in s:
                val = s.split("=", 1)[1].strip().lower()
                return val in ("1", "true", "yes")
    except OSError:
        pass
    return False


def detect_enabler(binaries_dir: Path) -> EnablerStatus:
    """Detect WMM Pak Enabler (dwmapi built-in, standalone dsound.dll) or community bitfix (§5.2)."""
    binaries_dir = Path(binaries_dir)

    # 1. Check WMM Pak Enabler live log (confirmed active on last launch)
    wmm_log = binaries_dir / "wmm_pak_enabler.log"
    if wmm_log.is_file():
        files = ["wmm_pak_enabler.log"]
        if (binaries_dir / "dwmapi.dll").is_file():
            files.insert(0, "dwmapi.dll")
        elif (binaries_dir / "dsound.dll").is_file():
            files.insert(0, "dsound.dll")
        try:
            log_line = wmm_log.read_text(encoding="utf-8", errors="ignore").strip().splitlines()[0]
        except (OSError, IndexError):
            log_line = ""
        return EnablerStatus(present=True, kind="wmm", files=files, log_status=log_line)

    # 2. Check built-in WMM Pak Enabler configured in UE4SS-settings.ini (before first launch)
    ue4ss_ini = binaries_dir / "ue4ss" / "UE4SS-settings.ini"
    if (binaries_dir / "dwmapi.dll").is_file() and _ue4ss_ini_has_unsigned_paks(ue4ss_ini):
        return EnablerStatus(present=True, kind="wmm", files=["dwmapi.dll", "UE4SS-settings.ini"])

    # 3. Check community bitfix
    found: list[str] = []
    if (binaries_dir / "bitfix").is_dir():
        found.append("bitfix/")
    if (binaries_dir / "bitfix.txt").is_file():
        found.append("bitfix.txt")
    for dll in ("d3d9.dll", "dsound.dll"):
        if (binaries_dir / dll).is_file():
            found.append(dll)
    has_bitfix_marker = any(x.startswith("bitfix") for x in found)
    if has_bitfix_marker:
        return EnablerStatus(present=True, kind="bitfix", files=found)

    # 4. Standalone WMM dsound.dll placed (before first launch, no bitfix/)
    if (binaries_dir / "dsound.dll").is_file():
        return EnablerStatus(present=True, kind="wmm", files=["dsound.dll"])

    return EnablerStatus(present=False, kind="none", files=found)


def install_wmm_pak_enabler(
    game: GameInstall,
    store: Store,
    dll_source: Path,
    platform: Optional[Platform] = None,
) -> Path:
    """Install WMM's own standalone dsound.dll Pak Enabler into game.binaries_dir (backs up existing)."""
    plat = platform or get_platform()
    running = running_game_processes(plat)
    if running:
        raise RuntimeError(f"Game is running ({', '.join(running)})")
    dll_source = Path(dll_source)
    if not dll_source.is_file():
        raise FileNotFoundError(f"WMM Pak Enabler DLL not found: {dll_source}")

    dst = game.binaries_dir / "dsound.dll"
    if dst.exists():
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        bdir = store.backup_dir / f"enabler_{stamp}"
        bdir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(dst, bdir / dst.name)
    shutil.copy2(dll_source, dst)
    return dst


def detect_foreign_proxies(binaries_dir: Path) -> list[str]:
    """Detect foreign proxies (OptiScaler, ReShade, fakenvapi) that WMM must never touch (§1.10)."""
    binaries_dir = Path(binaries_dir)
    foreign: list[str] = []
    if (binaries_dir / "OptiScaler.ini").is_file() or (binaries_dir / "dxgi.dll").is_file():
        if (binaries_dir / "OptiScaler.ini").is_file():
            foreign.append("OptiScaler (dxgi.dll)")
        elif (binaries_dir / "dxgi.dll").is_file():
            foreign.append("dxgi.dll")
    if (binaries_dir / "ReShade.ini").is_file():
        foreign.append("ReShade")
    if (binaries_dir / "nvapi64.dll").is_file() or (binaries_dir / "fakenvapi.ini").is_file():
        foreign.append("fakenvapi")
    return foreign


def detect_bp_loader(game: GameInstall) -> BPLoaderStatus:
    """Detect whether WMM Lua BP loader or legacy WFF_BPLoader pak is installed."""
    wmm_lua = game.ue4ss_mods_dir / "WMM_BPLoader" / "Scripts" / "main.lua"
    wff_entries = read_wff_bploader_mods(game.engine_ini_path) if game.engine_ini_path else []
    if wmm_lua.is_file():
        return BPLoaderStatus(kind="wmm_lua", files=[str(wmm_lua)], wff_ini_entries=wff_entries)

    wff_files: list[str] = []
    if game.mods_pak_dir.is_dir():
        for name in ("WFF_BPLoader.pak", "WFF_BPLoader_P.pak"):
            p = game.mods_pak_dir / name
            if p.is_file():
                wff_files.append(str(p))
    if wff_files:
        return BPLoaderStatus(kind="wff_pak", files=wff_files, wff_ini_entries=wff_entries)
    return BPLoaderStatus(kind="none", files=[], wff_ini_entries=wff_entries)


@dataclass
class OptionalPackages:
    ue4ss_archive: Optional[Path] = None
    pak_enabler_dll: Optional[Path] = None
    bploader_archive: Optional[Path] = None


def find_optional_packages(package_root: Path, mods_archive_dir: Optional[Path] = None) -> OptionalPackages:
    """Scan WMM root, base/, and the user's mods folder for optional Nexus downloads."""
    pkg = OptionalPackages()
    # 1. Bundled or built standalone Pak Enabler DLL
    for cand in (
        package_root / "base" / "sigfix" / "dsound.dll",
        package_root / "native" / "pak_enabler" / "build" / "dsound.dll",
    ):
        if cand.is_file():
            pkg.pak_enabler_dll = cand
            break

    # 2. Scan WMM root, base/, and user's mods archive folder for optional UE4SS / BP Loader zips
    search_dirs = [package_root, package_root / "base"]
    if mods_archive_dir is not None and Path(mods_archive_dir).is_dir():
        search_dirs.append(Path(mods_archive_dir))

    for d in search_dirs:
        if not d.is_dir():
            continue
        for p in sorted(d.iterdir()):
            if not p.is_file() or p.suffix.lower() not in (".zip", ".7z", ".rar"):
                continue
            low = p.name.lower()
            if pkg.ue4ss_archive is None and "ue4ss" in low:
                pkg.ue4ss_archive = p
            elif pkg.bploader_archive is None and ("bploader" in low or "blueprint" in low):
                pkg.bploader_archive = p
    return pkg


def enable_unsigned_paks_in_ue4ss_ini(ue4ss_ini: Path) -> bool:
    """Ensure [General] EnableUnsignedPaks = 1 is set in UE4SS-settings.ini (preserves EOLs)."""
    if not ue4ss_ini.is_file():
        return False
    raw = ue4ss_ini.read_bytes()
    eol = "\r\n" if b"\r\n" in raw else "\n"
    text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines()

    in_gen = False
    replaced = False
    out: list[str] = []
    for line in lines:
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            if in_gen and not replaced:
                out.append("EnableUnsignedPaks = 1")
                replaced = True
            in_gen = s.lower() == "[general]"
            out.append(line)
            continue
        if in_gen and s.lower().startswith("enableunsignedpaks"):
            out.append("EnableUnsignedPaks = 1")
            replaced = True
            continue
        out.append(line)

    if in_gen and not replaced:
        out.append("EnableUnsignedPaks = 1")
        replaced = True
    elif not replaced:
        out.extend(["", "[General]", "EnableUnsignedPaks = 1"])

    ue4ss_ini.write_bytes((eol.join(out) + eol).encode("utf-8"))
    return True


def detect_mod_base(game: GameInstall) -> ModBaseStatus:
    """Inspect all onboarding / mod base components for a GameInstall."""
    return ModBaseStatus(
        ue4ss=detect_ue4ss(game.binaries_dir),
        enabler=detect_enabler(game.binaries_dir),
        bp_loader=detect_bp_loader(game),
        engine_ini_exists=bool(game.engine_ini_path and game.engine_ini_path.is_file()),
        foreign_proxies=detect_foreign_proxies(game.binaries_dir),
    )


def install_wmm_lua_bploader(
    game: GameInstall,
    store: Store,
    bp_mod_names: Optional[list[str]] = None,
    platform: Optional[Platform] = None,
) -> Path:
    """Install WMM's Lua BP loader into ue4ss/Mods/WMM_BPLoader/ and register it in mods.txt."""
    plat = platform or get_platform()
    running = running_game_processes(plat)
    if running:
        raise RuntimeError(f"Game is running ({', '.join(running)})")

    mod_dir = game.ue4ss_mods_dir / "WMM_BPLoader"
    scripts_dir = mod_dir / "Scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    main_lua = scripts_dir / "main.lua"
    main_lua.write_bytes(WMM_LUA_BPLOADER_SCRIPT.encode("utf-8"))
    (mod_dir / "enabled.txt").write_bytes(b"")

    sync_wmm_lua_bploader_list(game, bp_mod_names or [])
    write_mods_txt(game.ue4ss_mods_txt, [("WMM_BPLoader", True)], preserve_unmanaged=True)
    return mod_dir


def sync_wmm_lua_bploader_list(game: GameInstall, bp_mod_names: list[str]) -> Path:
    """Write Mods/WMM_BPLoader/wmm_bp_mods.txt (LF-only) with enabled BP mod names."""
    mod_dir = game.ue4ss_mods_dir / "WMM_BPLoader"
    mod_dir.mkdir(parents=True, exist_ok=True)
    list_path = mod_dir / "wmm_bp_mods.txt"
    content = "\n".join(bp_mod_names) + ("\n" if bp_mod_names else "")
    list_path.write_bytes(content.encode("utf-8"))
    return list_path


def install_ue4ss_base(
    game: GameInstall,
    store: Store,
    payload_dir: Path,
    platform: Optional[Platform] = None,
) -> list[Path]:
    """Install a UE4SS base payload (dwmapi.dll + ue4ss/) into game.binaries_dir, backing up replaced files."""
    plat = platform or get_platform()
    running = running_game_processes(plat)
    if running:
        raise RuntimeError(f"Game is running ({', '.join(running)})")

    payload_dir = Path(payload_dir)
    if not payload_dir.is_dir():
        raise FileNotFoundError(f"UE4SS payload directory not found: {payload_dir}")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    bdir = store.backup_dir / f"ue4ss_{stamp}"
    placed: list[Path] = []

    for src in sorted(payload_dir.rglob("*")):
        if not src.is_file():
            continue
        rel = src.relative_to(payload_dir)
        dst = game.binaries_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            bdest = bdir / rel
            bdest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(dst, bdest)
        shutil.copy2(src, dst)
        placed.append(dst)

    return placed
