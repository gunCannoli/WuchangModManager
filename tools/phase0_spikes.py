"""Phase 0 live spike harness for Wuchang Mod Manager (plan rev 3 §7).

Safely sets up, checks, and restores reversible live tests for:
  - 0a: Unsigned ~mods paks without bitfix (temporarily moves d3d9.dll & dsound.dll)
  - 0b: Real pak conflict-winner rule on this build (AAAA_wmm_probe_P vs ZZZZ_wmm_probe_P)
  - 0c: UE4SS mod via Windows directory junction (script paths + writes)
  - 0d: _P-number vs name weight (no-_P vs plain _P vs _1_P vs _999_P)

Usage:
  python tools/phase0_spikes.py setup-bc      # Stage 1: tests 0b (name order) + 0c (junction)
  python tools/phase0_spikes.py check         # Read UE4SS.log + junction write result
  python tools/phase0_spikes.py setup-d       # Stage 2: tests 0d (_P-number vs name weight)
  python tools/phase0_spikes.py setup-a       # Stage 3: tests 0a (unsigned ~mods without bitfix)
  python tools/phase0_spikes.py restore       # Cleanly undo all Phase 0 changes & restore backups
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = ROOT.parent
sys.path.insert(0, str(ROOT))

from wmm.game import detect_all_installs, running_game_processes
from wmm.platform import get_platform
from wmm.ue4ss import write_mods_txt

WORK_DIR = ROOT / "tools" / "phase0_work"
STATE_FILE = WORK_DIR / "state.json"
REPAK_EXE = REPO_ROOT / "WuchangBodyMorph" / "tools" / "dl" / "repak.exe"

PROBE_MOD_NAME = "WMM_Phase0Probe"
PROBE_LUA = """-- WMM Phase 0 live spike probe (0a, 0b, 0c, 0d)
local function log(msg)
    print("[WMM_Phase0] " .. tostring(msg) .. "\\n")
end

local src = debug.getinfo(1, "S").source or ""
log("0c_script_source=" .. src)

-- Derive mod folder from script path and test writing through the junction
local mod_dir = src:gsub("^@", ""):gsub("[/\\\\]Scripts[/\\\\][^/\\\\]+$", "")
local write_path = mod_dir .. "/junction_write_ok.txt"
local f, err = io.open(write_path, "wb")
if f then
    f:write("ok\\nscript=" .. src .. "\\n")
    f:close()
    log("0c_write_ok=true path=" .. write_path)
else
    log("0c_write_ok=false err=" .. tostring(err))
end

local function read_probe_ini()
    local ok, gps = pcall(StaticFindObject, "/Script/EngineSettings.Default__GeneralProjectSettings")
    if not ok or not gps or not gps:IsValid() then
        log("probe_gps=invalid")
        return
    end
    local ok2, val = pcall(function() return gps.SupportContact:ToString() end)
    log("probe_support_contact=" .. (ok2 and tostring(val) or ("ERR:" .. tostring(val))))
end

ExecuteWithDelay(1500, function()
    ExecuteInGameThread(read_probe_ini)
end)
"""

PROBE_PAKS = [
    "AAAA_wmm_probe_P.pak",
    "ZZZZ_wmm_probe_P.pak",
    "ZZZZ_wmm_probe.pak",
    "AAAA_wmm_probe_1_P.pak",
    "BBBB_wmm_probe_999_P.pak",
]


def _get_game():
    installs = detect_all_installs()
    if not installs:
        raise SystemExit("Game install not found.")
    return installs[0]


def _assert_closed():
    procs = running_game_processes()
    if procs:
        raise SystemExit(f"Game is running ({', '.join(procs)}). Close it first.")


def _load_state() -> dict:
    if STATE_FILE.is_file():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"mods_txt_backed_up": False, "bitfix_moved": []}


def _save_state(st: dict) -> None:
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(st, indent=2) + "\n", encoding="utf-8")


def _build_probe_pak(out_pak: Path, tag: str) -> None:
    if not REPAK_EXE.is_file():
        raise SystemExit(f"repak.exe not found at {REPAK_EXE}")
    stage = WORK_DIR / f"stage_{tag}"
    if stage.exists():
        shutil.rmtree(stage)
    ini = stage / "Project_Plague" / "Config" / "UserGame.ini"
    ini.parent.mkdir(parents=True, exist_ok=True)
    ini.write_text(
        f"[/Script/EngineSettings.GeneralProjectSettings]\nSupportContact=WMM_PROBE_{tag}\n",
        encoding="utf-8",
        newline="\n",
    )
    out_pak.parent.mkdir(parents=True, exist_ok=True)
    res = subprocess.run(
        [str(REPAK_EXE), "pack", "--version", "V11", str(stage), str(out_pak)],
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        raise SystemExit(f"repak failed: {res.stderr or res.stdout}")


def _clean_probe_paks(mods_pak_dir: Path) -> None:
    for name in PROBE_PAKS:
        p = mods_pak_dir / name
        if p.exists():
            p.unlink()


def _install_junction_probe(game, state: dict) -> None:
    plat = get_platform()
    store_mod = WORK_DIR / "junction_store" / PROBE_MOD_NAME
    (store_mod / "Scripts").mkdir(parents=True, exist_ok=True)
    (store_mod / "Scripts" / "main.lua").write_bytes(PROBE_LUA.encode("utf-8"))
    (store_mod / "enabled.txt").write_bytes(b"")
    marker = store_mod / "junction_write_ok.txt"
    if marker.exists():
        marker.unlink()

    link_path = game.ue4ss_mods_dir / PROBE_MOD_NAME
    if plat.is_dir_link(link_path):
        plat.remove_dir_link(link_path)
    elif link_path.exists():
        shutil.rmtree(link_path)
    plat.create_dir_link(link_path, store_mod)

    # Back up mods.txt once
    if not state.get("mods_txt_backed_up") and game.ue4ss_mods_txt.is_file():
        backup = WORK_DIR / "mods.txt.bak"
        shutil.copy2(game.ue4ss_mods_txt, backup)
        state["mods_txt_backed_up"] = True
    write_mods_txt(game.ue4ss_mods_txt, [(PROBE_MOD_NAME, True)], preserve_unmanaged=True)


def _restore_bitfix(game, state: dict) -> None:
    moved = state.get("bitfix_moved", [])
    for fname in moved:
        src = WORK_DIR / "bitfix_bak" / fname
        dst = game.binaries_dir / fname
        if src.is_file() and not dst.exists():
            shutil.move(str(src), str(dst))
    state["bitfix_moved"] = []


def cmd_setup_bc() -> None:
    _assert_closed()
    game = _get_game()
    state = _load_state()
    _restore_bitfix(game, state)
    _clean_probe_paks(game.mods_pak_dir)
    _install_junction_probe(game, state)

    _build_probe_pak(game.mods_pak_dir / "AAAA_wmm_probe_P.pak", "AAAA_P")
    _build_probe_pak(game.mods_pak_dir / "ZZZZ_wmm_probe_P.pak", "ZZZZ_P")
    state["stage"] = "bc"
    _save_state(state)
    print("Stage 1 (0b + 0c) ready:")
    print(f"  Junction: {game.ue4ss_mods_dir / PROBE_MOD_NAME} -> {WORK_DIR / 'junction_store' / PROBE_MOD_NAME}")
    print("  Paks:     AAAA_wmm_probe_P.pak vs ZZZZ_wmm_probe_P.pak")
    print("Launch the game to the main menu, close it, then run: python tools/phase0_spikes.py check")


def cmd_setup_d() -> None:
    _assert_closed()
    game = _get_game()
    state = _load_state()
    _restore_bitfix(game, state)
    _clean_probe_paks(game.mods_pak_dir)
    _install_junction_probe(game, state)

    _build_probe_pak(game.mods_pak_dir / "ZZZZ_wmm_probe.pak", "ZZZZ_NO_P")
    _build_probe_pak(game.mods_pak_dir / "ZZZZ_wmm_probe_P.pak", "ZZZZ_PLAIN_P")
    _build_probe_pak(game.mods_pak_dir / "AAAA_wmm_probe_1_P.pak", "AAAA_1_P")
    _build_probe_pak(game.mods_pak_dir / "BBBB_wmm_probe_999_P.pak", "BBBB_999_P")
    state["stage"] = "d"
    _save_state(state)
    print("Stage 2 (0d: _P-number vs plain _P vs no-_P) ready:")
    print("  Paks: ZZZZ_wmm_probe.pak, ZZZZ_wmm_probe_P.pak, AAAA_wmm_probe_1_P.pak, BBBB_wmm_probe_999_P.pak")
    print("Launch the game to the main menu, close it, then run: python tools/phase0_spikes.py check")


def cmd_setup_a() -> None:
    _assert_closed()
    game = _get_game()
    state = _load_state()
    _clean_probe_paks(game.mods_pak_dir)
    _install_junction_probe(game, state)
    _build_probe_pak(game.mods_pak_dir / "AAAA_wmm_probe_P.pak", "NO_BITFIX_OK")

    bak_dir = WORK_DIR / "bitfix_bak"
    bak_dir.mkdir(parents=True, exist_ok=True)
    moved = list(state.get("bitfix_moved", []))
    for dll in ("d3d9.dll", "dsound.dll"):
        src = game.binaries_dir / dll
        if src.is_file():
            shutil.move(str(src), str(bak_dir / dll))
            if dll not in moved:
                moved.append(dll)
    state["bitfix_moved"] = moved
    state["stage"] = "a"
    _save_state(state)
    print("Stage 3 (0a: unsigned ~mods without bitfix) ready:")
    print(f"  Temporarily moved bitfix DLLs: {moved}")
    print("  Pak: AAAA_wmm_probe_P.pak (expected tag: WMM_PROBE_NO_BITFIX_OK)")
    print("Launch the game to the main menu, close it, then run: python tools/phase0_spikes.py check")


def cmd_setup_wmm_enabler() -> None:
    _assert_closed()
    game = _get_game()
    state = _load_state()
    _clean_probe_paks(game.mods_pak_dir)
    _install_junction_probe(game, state)
    _build_probe_pak(game.mods_pak_dir / "AAAA_wmm_probe_P.pak", "WMM_ENABLER_OK")

    bak_dir = WORK_DIR / "bitfix_bak"
    bak_dir.mkdir(parents=True, exist_ok=True)
    moved = list(state.get("bitfix_moved", []))
    for dll in ("d3d9.dll", "dsound.dll"):
        src = game.binaries_dir / dll
        if src.is_file() and dll not in moved:
            shutil.move(str(src), str(bak_dir / dll))
            moved.append(dll)
    state["bitfix_moved"] = moved

    wmm_dll = ROOT / "native" / "pak_enabler" / "build" / "dsound.dll"
    if not wmm_dll.is_file():
        raise SystemExit(f"WMM dsound.dll not built yet at {wmm_dll}")
    shutil.copy2(wmm_dll, game.binaries_dir / "dsound.dll")
    state["wmm_enabler_placed"] = True
    state["stage"] = "wmm-enabler"
    _save_state(state)
    print("WMM Pak Enabler live test staged:")
    print(f"  bitfix backed up: {moved}")
    print(f"  Placed WMM dsound.dll ({wmm_dll.stat().st_size} bytes)")
    print("  Pak: AAAA_wmm_probe_P.pak (expected tag: WMM_PROBE_WMM_ENABLER_OK)")
    print("Launch the game to the main menu, close it, then run: python tools/phase0_spikes.py check")


def cmd_check() -> None:
    game = _get_game()
    state = _load_state()
    print(f"Checking Phase 0 results (stage={state.get('stage', '?')}):")

    enabler_log = game.binaries_dir / "wmm_pak_enabler.log"
    if enabler_log.is_file():
        print("  [wmm_pak_enabler.log]", enabler_log.read_text(encoding="utf-8", errors="ignore").strip())

    marker = WORK_DIR / "junction_store" / PROBE_MOD_NAME / "junction_write_ok.txt"
    if marker.is_file():
        print("  [0c junction write] OK ->", marker.read_text(encoding="utf-8").strip().replace("\n", " | "))
    else:
        print("  [0c junction write] NOT FOUND yet")

    log_path = game.ue4ss_dir / "UE4SS.log"
    if log_path.is_file():
        lines = [
            ln.strip()
            for ln in log_path.read_text(encoding="utf-8", errors="ignore").splitlines()
            if "[WMM_Phase0]" in ln
        ]
        if lines:
            print("  [UE4SS.log lines]:")
            for ln in lines:
                print("   ", ln)
        else:
            print("  [UE4SS.log] No [WMM_Phase0] lines found.")
    else:
        print("  [UE4SS.log] Missing.")


def cmd_restore() -> None:
    _assert_closed()
    game = _get_game()
    state = _load_state()
    plat = get_platform()

    _clean_probe_paks(game.mods_pak_dir)
    if state.get("wmm_enabler_placed"):
        wmm_dst = game.binaries_dir / "dsound.dll"
        if wmm_dst.is_file():
            wmm_dst.unlink()
        wmm_log = game.binaries_dir / "wmm_pak_enabler.log"
        if wmm_log.is_file():
            wmm_log.unlink()
        state["wmm_enabler_placed"] = False
    _restore_bitfix(game, state)

    link_path = game.ue4ss_mods_dir / PROBE_MOD_NAME
    if plat.is_dir_link(link_path):
        plat.remove_dir_link(link_path)

    backup = WORK_DIR / "mods.txt.bak"
    if state.get("mods_txt_backed_up") and backup.is_file():
        shutil.copy2(backup, game.ue4ss_mods_txt)
        state["mods_txt_backed_up"] = False

    state["stage"] = "restored"
    _save_state(state)
    print("Phase 0 restored cleanly: probe paks removed, junction removed, mods.txt & bitfix restored.")


def main() -> int:
    p = argparse.ArgumentParser(description="WMM Phase 0 live spike harness")
    p.add_argument("action", choices=["setup-bc", "setup-d", "setup-a", "setup-wmm-enabler", "check", "restore"])
    args = p.parse_args()
    {
        "setup-bc": cmd_setup_bc,
        "setup-d": cmd_setup_d,
        "setup-a": cmd_setup_a,
        "setup-wmm-enabler": cmd_setup_wmm_enabler,
        "check": cmd_check,
        "restore": cmd_restore,
    }[args.action]()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
