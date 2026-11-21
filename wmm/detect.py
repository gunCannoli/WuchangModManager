"""Mod type detection and classification (§3).

Classifies an extracted archive or directory into typed components:
- ue4ss_mod       : folder containing Scripts/main.lua or dlls/main.dll
- ue4ss_shared    : Mods/shared/<Lib>/ or shared/<Lib>/
- pak             : *.pak (+ .ucas/.utoc/.sig companions) -> ~mods or LogicMods
- bp_mod          : pak whose index has /Game/Mods/<Name>/ModActor
- config_pak      : pak whose index has Config/UserEngine.ini or DefaultEngine.ini
- ini_tweak       : .ini fragment with [Section] headers
- loader          : proxy DLL (dwmapi, dxgi, d3d9, dsound, ...), UE4SS.dll, bitfix
- unknown         : .asi, .exe, scripts, etc. (listed, not installed)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .pakindex import PakReadError, read_pak_index

PROXY_DLL_STEMS = {
    "dwmapi",
    "dxgi",
    "d3d9",
    "d3d11",
    "d3d12",
    "dsound",
    "version",
    "winmm",
}

LOADER_DLL_NAMES = {"ue4ss.dll"}
COMPANION_EXTS = (".ucas", ".utoc", ".sig")
UNKNOWN_EXEC_EXTS = {".asi", ".exe", ".bat", ".cmd", ".ps1", ".sh", ".vbs", ".msi"}
INI_SECTION_RE = re.compile(r"^\s*\[[^\]]+\]\s*$", re.MULTILINE)


@dataclass
class DetectedComponent:
    kind: str  # ue4ss_mod, ue4ss_shared, pak, ini_tweak, loader, unknown
    name: str
    source_path: Path  # file or directory inside the extracted root
    target_kind: str = ""  # "~mods", "LogicMods", "ue4ss_mod", "ue4ss_shared", "engine_ini", "binaries", "none"
    companions: list[Path] = field(default_factory=list)
    bp_mod_names: list[str] = field(default_factory=list)
    config_overrides: list[str] = field(default_factory=list)
    pak_files: list[str] = field(default_factory=list)
    requires_confirmation: bool = False
    variant_group: Optional[str] = None
    notes: list[str] = field(default_factory=list)


@dataclass
class DetectionResult:
    source: Path
    components: list[DetectedComponent] = field(default_factory=list)
    variants: dict[str, list[DetectedComponent]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def badge(self) -> str:
        kinds = {c.kind for c in self.components}
        has_bp = any(c.bp_mod_names for c in self.components)
        has_pak = "pak" in kinds
        has_ue4ss = "ue4ss_mod" in kinds or "ue4ss_shared" in kinds
        if has_bp and has_ue4ss:
            return "BP+UE4SS"
        if has_bp:
            return "BP"
        if has_pak and has_ue4ss:
            return "PAK+UE4SS"
        if has_pak:
            return "PAK"
        if has_ue4ss:
            return "UE4SS"
        if "ini_tweak" in kinds:
            return "INI"
        if "loader" in kinds:
            return "LOADER"
        return "UNKNOWN"

    @property
    def has_loader(self) -> bool:
        return any(c.kind == "loader" for c in self.components)

    @property
    def has_config_override(self) -> bool:
        return any(bool(c.config_overrides) for c in self.components)


def _is_proxy_dll(path: Path) -> bool:
    if path.suffix.lower() != ".dll":
        return False
    low_name = path.name.lower()
    if low_name in LOADER_DLL_NAMES:
        return True
    stem = path.stem.lower()
    if stem in PROXY_DLL_STEMS or stem.startswith("xinput"):
        return True
    return False


def _is_under_folder(path: Path, root: Path, folder_name: str) -> bool:
    rel = path.relative_to(root)
    return any(p.lower() == folder_name.lower() for p in rel.parts[:-1])


def _is_under_any(path: Path, claimed_dirs: list[Path]) -> bool:
    for d in claimed_dirs:
        if path == d or d in path.parents:
            return True
    return False


def _detect_variants(paks: list[DetectedComponent], root: Path) -> dict[str, list[DetectedComponent]]:
    """Group competing pak variants (e.g. multiple subfolders each with a pak, or numbered variants)."""
    if len(paks) < 2:
        return {}
    # Heuristic 1: paks sitting in different immediate subdirectories under the same parent
    # (and not under standard game paths like ~mods or LogicMods)
    parent_map: dict[Path, list[DetectedComponent]] = {}
    for c in paks:
        parent_map.setdefault(c.source_path.parent, []).append(c)

    # If there are multiple sibling directories under the same grandparent and none are standard UE dirs
    standard = {"~mods", "logicmods", "paks", "content", "project_plague"}
    non_std_parents = [p for p in parent_map if p != root and p.name.lower() not in standard]
    if len(non_std_parents) >= 2:
        grandparents: dict[Path, list[DetectedComponent]] = {}
        for p in non_std_parents:
            grandparents.setdefault(p.parent, []).extend(parent_map[p])
        variants: dict[str, list[DetectedComponent]] = {}
        for gp, comps in grandparents.items():
            if len(comps) >= 2:
                group_name = gp.name if gp != root else "variants"
                for c in comps:
                    c.variant_group = group_name
                variants[group_name] = comps
        if variants:
            return variants

    # Heuristic 2: multiple paks in the same folder that share a common prefix with variant suffixes
    # (e.g. Foo_1_P.pak, Foo_2_P.pak or Foo_5s_P.pak, Foo_10s_P.pak)
    for parent, comps in parent_map.items():
        if len(comps) < 2:
            continue
        by_base: dict[str, list[DetectedComponent]] = {}
        for c in comps:
            # Strip _P, digits, 5s/10s, Merged/Full
            stem = Path(c.name).stem
            norm = re.sub(r"(_P|_\d+|_\d+s|_[0-9]+p)$", "", stem, flags=re.IGNORECASE)
            norm = re.sub(r"[\s_\-]+(\d+|v\d+[\.\d]*|merged|full|lite|\d+s)$", "", norm, flags=re.IGNORECASE)
            if norm and norm.lower() != stem.lower():
                by_base.setdefault(norm.lower(), []).append(c)
        variants = {}
        for base, grp in by_base.items():
            if len(grp) >= 2:
                for c in grp:
                    c.variant_group = base
                variants[base] = grp
        if variants:
            return variants

    return {}


def detect_mod(root: Path, aes_key_hex: Optional[str] = None) -> DetectionResult:
    """Inspect an extracted archive directory (or single file) and classify all components."""
    root = Path(root)
    result = DetectionResult(source=root)

    if root.is_file():
        if root.suffix.lower() == ".pak":
            comp = _classify_pak(root, root.parent, aes_key_hex)
            result.components.append(comp)
        elif root.suffix.lower() == ".ini":
            comp = _classify_ini(root)
            if comp:
                result.components.append(comp)
        elif _is_proxy_dll(root):
            result.components.append(
                DetectedComponent(
                    kind="loader",
                    name=root.name,
                    source_path=root,
                    target_kind="binaries",
                    requires_confirmation=True,
                )
            )
        else:
            result.components.append(
                DetectedComponent(kind="unknown", name=root.name, source_path=root, target_kind="none")
            )
        return result

    if not root.is_dir():
        result.warnings.append(f"Path does not exist: {root}")
        return result

    claimed_dirs: list[Path] = []

    # 1. Detect loader DLLs & bitfix folders first (safety rule §1.10 / §3)
    for p in sorted(root.rglob("*")):
        if p.is_dir() and p.name.lower() == "bitfix":
            result.components.append(
                DetectedComponent(
                    kind="loader",
                    name="bitfix",
                    source_path=p,
                    target_kind="binaries",
                    requires_confirmation=True,
                    notes=["Wuchang Mod Enabler (bitfix)"],
                )
            )
            claimed_dirs.append(p)
        elif p.is_file() and _is_proxy_dll(p):
            result.components.append(
                DetectedComponent(
                    kind="loader",
                    name=p.name,
                    source_path=p,
                    target_kind="binaries",
                    requires_confirmation=True,
                )
            )

    # 2. Detect UE4SS shared libraries: .../Mods/shared/<Lib>/ or .../shared/<Lib>/
    for p in sorted(root.rglob("*")):
        if not p.is_dir():
            continue
        if p.name.lower() == "shared" and (
            p.parent.name.lower() == "mods" or any(c.is_dir() for c in p.iterdir())
        ):
            # Only treat as UE4SS shared if children contain .lua files
            for lib_dir in sorted(p.iterdir()):
                if lib_dir.is_dir() and any(lib_dir.rglob("*.lua")):
                    if not _is_under_any(lib_dir, claimed_dirs):
                        result.components.append(
                            DetectedComponent(
                                kind="ue4ss_shared",
                                name=lib_dir.name,
                                source_path=lib_dir,
                                target_kind="ue4ss_shared",
                            )
                        )
                        claimed_dirs.append(lib_dir)

    # 3. Detect UE4SS mods: <ModFolder>/Scripts/main.lua or <ModFolder>/dlls/main.dll
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        if _is_under_any(p, claimed_dirs):
            continue
        low = p.name.lower()
        parent_low = p.parent.name.lower()
        if (low == "main.lua" and parent_low == "scripts") or (low == "main.dll" and parent_low == "dlls"):
            mod_dir = p.parent.parent
            if mod_dir == root:
                # Archive had Scripts/main.lua directly at root -> use archive folder name
                mod_name = root.name
            else:
                mod_name = mod_dir.name
            if not _is_under_any(mod_dir, claimed_dirs):
                result.components.append(
                    DetectedComponent(
                        kind="ue4ss_mod",
                        name=mod_name,
                        source_path=mod_dir,
                        target_kind="ue4ss_mod",
                    )
                )
                claimed_dirs.append(mod_dir)

    # 4. Detect .pak files (+ companions .ucas/.utoc/.sig)
    pak_components: list[DetectedComponent] = []
    for p in sorted(root.rglob("*.pak")):
        if not p.is_file() or _is_under_any(p, claimed_dirs):
            continue
        comp = _classify_pak(p, root, aes_key_hex)
        result.components.append(comp)
        pak_components.append(comp)

    # Detect pak variants and duplicate-named UE4SS mod variants (e.g. Sheathe Only vs Sheathe+Position)
    result.variants = _detect_variants(pak_components, root)
    ue4ss_by_name: dict[str, list[DetectedComponent]] = {}
    for c in result.components:
        if c.kind == "ue4ss_mod":
            ue4ss_by_name.setdefault(c.name.lower(), []).append(c)
    for key, comps in ue4ss_by_name.items():
        if len(comps) >= 2:
            grp = f"ue4ss:{comps[0].name}"
            for c in comps:
                c.variant_group = grp
            result.variants[grp] = comps

    # 5. Detect .ini tweak fragments (not inside claimed UE4SS/loader dirs, not UE4SS-settings.ini)
    skip_ini_names = {"ue4ss-settings.ini", "optiscaler.ini"}
    for p in sorted(root.rglob("*.ini")):
        if not p.is_file() or _is_under_any(p, claimed_dirs):
            continue
        if p.name.lower() in skip_ini_names:
            continue
        comp = _classify_ini(p)
        if comp:
            result.components.append(comp)

    # 6. Flag unknown executables / scripts / .asi outside claimed dirs
    for p in sorted(root.rglob("*")):
        if not p.is_file() or _is_under_any(p, claimed_dirs):
            continue
        if p.suffix.lower() in UNKNOWN_EXEC_EXTS:
            result.components.append(
                DetectedComponent(
                    kind="unknown",
                    name=p.name,
                    source_path=p,
                    target_kind="none",
                )
            )

    return result


def _classify_pak(pak_path: Path, root: Path, aes_key_hex: Optional[str]) -> DetectedComponent:
    companions: list[Path] = []
    for ext in COMPANION_EXTS:
        cand = pak_path.with_suffix(ext)
        if cand.is_file():
            companions.append(cand)

    target_kind = "LogicMods" if _is_under_folder(pak_path, root, "LogicMods") else "~mods"
    bp_names: list[str] = []
    cfg_overrides: list[str] = []
    pak_files: list[str] = []
    notes: list[str] = []

    try:
        idx = read_pak_index(pak_path, aes_key_hex=aes_key_hex)
        pak_files = idx.files
        bp_names = idx.bp_mod_names()
        cfg_overrides = idx.config_override_files()
        if cfg_overrides:
            notes.append(f"Config override: {', '.join(cfg_overrides)}")
        if bp_names:
            notes.append(f"BP ModActor: {', '.join(bp_names)}")
    except PakReadError as exc:
        notes.append(f"Unreadable pak index: {exc}")

    return DetectedComponent(
        kind="pak",
        name=pak_path.name,
        source_path=pak_path,
        target_kind=target_kind,
        companions=companions,
        bp_mod_names=bp_names,
        config_overrides=cfg_overrides,
        pak_files=pak_files,
        notes=notes,
    )


def _classify_ini(ini_path: Path) -> Optional[DetectedComponent]:
    try:
        text = ini_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    if INI_SECTION_RE.search(text):
        return DetectedComponent(
            kind="ini_tweak",
            name=ini_path.name,
            source_path=ini_path,
            target_kind="engine_ini",
        )
    return None
