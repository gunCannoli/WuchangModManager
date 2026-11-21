"""Engine.ini safe reader, backup, in-place section updater, and WFF_BPLoader migration (§4.4)."""

from __future__ import annotations

import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional

WMM_BP_SECTION = "/Game/Mods/WMM_BPLoader/WBP_WMM_BPLoader.WBP_WMM_BPLoader_C"
WFF_BP_SECTION = "/Game/Mods/WFF_BPLoader/WBP_WFF_BPLoader.WBP_WFF_BPLoader_C"
PERF_BLOCK_ID = "perf_preset"

# Clean, UE 5.1-safe performance preset (PSO/shader hitch reduction, async texture streaming,
# D3D12 stability). Applied inside reversible WMM-BEGIN/WMM-END markers.
_PERF_COMMON = """[SystemSettings]
r.CreateShadersOnLoad=1
r.ShaderPipelineCache.Enabled=1
r.ShaderPipelineCache.StartupMode=3
r.ShaderPipelineCache.BatchTime=4.0
r.Streaming.Boost=1
r.Streaming.LimitPoolSizeToVRAM=1
r.Streaming.UseAsyncRequestsForDDC=1
r.GTSyncType=1
r.OneFrameThreadLag=1
r.D3D12.GPUTimeout=0
gc.TimeBetweenPurgingPendingKillObjects=60
"""

PERF_PRESET_NO_VRR = (
    "; WMM UE 5.1 Performance Preset (No VRR)\n"
    + _PERF_COMMON
)

PERF_PRESET_VRR = (
    "; WMM UE 5.1 Performance Preset (VRR / G-Sync / FreeSync)\n"
    + _PERF_COMMON
    + "r.VSync=0\n"
    + "r.D3D12.UseAllowTearing=1\n"
)

_SECTION_HDR_RE = re.compile(r"^\s*\[([^\]]+)\]\s*$")


def _detect_encoding_and_eol(raw: bytes) -> tuple[str, str]:
    if raw.startswith(b"\xff\xfe"):
        enc = "utf-16"
        text = raw.decode(enc)
    elif raw.startswith(b"\xef\xbb\xbf"):
        enc = "utf-8-sig"
        text = raw.decode(enc)
    else:
        enc = "utf-8"
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            enc = "latin-1"
            text = raw.decode(enc)
    eol = "\r\n" if "\r\n" in text else "\n"
    return enc, eol


def read_section_entries(ini_path: Path, section_name: str, key: str = "Mods") -> list[str]:
    """Read all values of `key=...` inside `[section_name]` (ignoring `;` comments)."""
    if not ini_path.is_file():
        return []
    raw = ini_path.read_bytes()
    enc, _ = _detect_encoding_and_eol(raw)
    text = raw.decode(enc, errors="replace")

    in_sec = False
    target = section_name.strip().lower()
    key_low = key.strip().lower()
    values: list[str] = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith(";"):
            continue
        m = _SECTION_HDR_RE.match(s)
        if m:
            in_sec = m.group(1).strip().lower() == target
            continue
        if in_sec and "=" in s:
            k, v = s.split("=", 1)
            if k.strip().lower() == key_low:
                val = v.strip()
                if val and val not in values:
                    values.append(val)
    return values


def read_wff_bploader_mods(ini_path: Path) -> list[str]:
    """Read existing Mods= entries from WFF_BPLoader's Engine.ini section."""
    return read_section_entries(ini_path, WFF_BP_SECTION, key="Mods")


def migrate_wff_bploader_entries(
    ini_path: Path,
    known_bp_mods: set[str],
) -> list[str]:
    """Return only those WFF_BPLoader entries that correspond to real BP mods (with ModActor)."""
    known_low = {m.lower() for m in known_bp_mods}
    raw_entries = read_wff_bploader_mods(ini_path)
    return [e for e in raw_entries if e.lower() in known_low]


def backup_engine_ini(ini_path: Path, backup_dir: Path) -> Optional[Path]:
    """Copy Engine.ini to backup_dir/<timestamp>/Engine.ini before modifying."""
    if not ini_path.is_file():
        return None
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest_dir = backup_dir / stamp
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / ini_path.name
    shutil.copy2(ini_path, dest)
    return dest


def _replace_section_in_text(text: str, section_name: str, body_lines: list[str]) -> str:
    """Replace all occurrences of [section_name] with a single updated block (or remove if body empty)."""
    lines = text.splitlines()
    target = section_name.strip().lower()
    out: list[str] = []
    i = 0
    replaced = False

    while i < len(lines):
        m = _SECTION_HDR_RE.match(lines[i].strip())
        if m and m.group(1).strip().lower() == target:
            # Skip entire existing section body up to next [Section]
            i += 1
            while i < len(lines) and not _SECTION_HDR_RE.match(lines[i].strip()):
                i += 1
            # Emit replacement once at the first occurrence
            if not replaced and body_lines:
                out.append(f"[{section_name}]")
                out.extend(body_lines)
                out.append("")
                replaced = True
            continue
        out.append(lines[i])
        i += 1

    if not replaced and body_lines:
        if out and out[-1].strip() != "":
            out.append("")
        out.append(f"[{section_name}]")
        out.extend(body_lines)
        out.append("")

    # Collapse trailing blank lines to at most one newline
    while len(out) >= 2 and out[-1] == "" and out[-2] == "":
        out.pop()
    return "\n".join(out) + ("\n" if out else "")


def update_wmm_bp_section(
    ini_path: Path,
    bp_mod_names: list[str],
    backup_dir: Optional[Path] = None,
    section_name: str = WMM_BP_SECTION,
) -> Path:
    """Update WMM's BP loader section in Engine.ini in place, preserving all other lines and encoding."""
    ini_path = Path(ini_path)
    if ini_path.is_file():
        if backup_dir is not None:
            backup_engine_ini(ini_path, backup_dir)
        raw = ini_path.read_bytes()
        enc, eol = _detect_encoding_and_eol(raw)
        text = raw.decode(enc, errors="replace")
    else:
        ini_path.parent.mkdir(parents=True, exist_ok=True)
        enc, eol = "utf-8", "\r\n"
        text = ""

    body = [f"Mods={name}" for name in bp_mod_names]
    updated = _replace_section_in_text(text, section_name, body)
    if eol == "\r\n":
        updated = updated.replace("\r\n", "\n").replace("\n", "\r\n")
    ini_path.write_bytes(updated.encode(enc))
    return ini_path


def apply_ini_tweak_block(
    ini_path: Path,
    mod_id: str,
    tweak_text: Optional[str],
    backup_dir: Optional[Path] = None,
) -> Path:
    """Append, replace, or remove a marked WMM ini-tweak block in Engine.ini."""
    ini_path = Path(ini_path)
    start_marker = f"; --- WMM-BEGIN:{mod_id} ---"
    end_marker = f"; --- WMM-END:{mod_id} ---"

    if ini_path.is_file():
        if backup_dir is not None:
            backup_engine_ini(ini_path, backup_dir)
        raw = ini_path.read_bytes()
        enc, eol = _detect_encoding_and_eol(raw)
        text = raw.decode(enc, errors="replace")
    else:
        ini_path.parent.mkdir(parents=True, exist_ok=True)
        enc, eol = "utf-8", "\r\n"
        text = ""

    # Strip existing block for mod_id if present
    lines = text.splitlines()
    cleaned: list[str] = []
    skipping = False
    for line in lines:
        if line.strip() == start_marker:
            skipping = True
            continue
        if skipping:
            if line.strip() == end_marker:
                skipping = False
            continue
        cleaned.append(line)

    if tweak_text is not None and tweak_text.strip():
        if cleaned and cleaned[-1].strip() != "":
            cleaned.append("")
        cleaned.append(start_marker)
        cleaned.extend(tweak_text.strip().splitlines())
        cleaned.append(end_marker)
        cleaned.append("")

    updated = "\n".join(cleaned) + ("\n" if cleaned else "")
    if eol == "\r\n":
        updated = updated.replace("\r\n", "\n").replace("\n", "\r\n")
    ini_path.write_bytes(updated.encode(enc))
    return ini_path


def read_perf_preset_mode(ini_path: Path) -> str:
    """Return 'vrr', 'no_vrr', or 'none' based on existing WMM perf_preset block in Engine.ini."""
    if not ini_path.is_file():
        return "none"
    raw = ini_path.read_bytes()
    enc, _ = _detect_encoding_and_eol(raw)
    text = raw.decode(enc, errors="replace")
    start_marker = f"; --- WMM-BEGIN:{PERF_BLOCK_ID} ---"
    if start_marker not in text:
        return "none"
    if "r.D3D12.UseAllowTearing=1" in text:
        return "vrr"
    return "no_vrr"


def apply_perf_preset(ini_path: Path, mode: str, backup_dir: Optional[Path] = None) -> Path:
    """Apply ('vrr', 'no_vrr') or remove ('none') WMM's UE 5.1 performance preset in Engine.ini."""
    if mode == "vrr":
        block = PERF_PRESET_VRR
    elif mode == "no_vrr":
        block = PERF_PRESET_NO_VRR
    else:
        block = None
    return apply_ini_tweak_block(ini_path, PERF_BLOCK_ID, block, backup_dir=backup_dir)
