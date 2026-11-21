"""Pak priority model, override/conflict graph, and custom order renaming (§4.2)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .pakindex import PakIndex, PakReadError, read_pak_index

# Matches trailing _<number>_P.pak or _P.pak (case-insensitive)
_P_NUM_RE = re.compile(r"^(?P<base>.+?)_(?P<num>\d+)_P$", re.IGNORECASE)
_P_PLAIN_RE = re.compile(r"^(?P<base>.+?)_P$", re.IGNORECASE)

FOLDER_RANKS = {
    "Paks": 0,
    "~mods": 1,
    "LogicMods": 2,
}


@dataclass
class PakOrderRule:
    """Per-build pak conflict resolution rule (verified via Phase 0b/0d probe)."""

    verified: bool = False
    # True = higher _P chunk number wins (confirmed by KB §1: _0_P / _1_P outrank unnumbered)
    higher_chunk_wins: bool = True
    # True = lexicographically later name wins (e.g. ZZZ > AAA); False = earlier name wins
    name_ascending_later_wins: bool = True
    build_id: str = ""

    def to_dict(self) -> dict:
        return {
            "verified": self.verified,
            "higher_chunk_wins": self.higher_chunk_wins,
            "name_ascending_later_wins": self.name_ascending_later_wins,
            "build_id": self.build_id,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PakOrderRule":
        return cls(
            verified=bool(d.get("verified", False)),
            higher_chunk_wins=bool(d.get("higher_chunk_wins", True)),
            name_ascending_later_wins=bool(d.get("name_ascending_later_wins", True)),
            build_id=str(d.get("build_id", "")),
        )


@dataclass
class PakPriority:
    filename: str
    folder: str  # "Paks", "~mods", "LogicMods"
    has_p: bool
    chunk_num: int  # -1 if no _P, 0 if plain _P without number, else int(num)
    sort_key: tuple


@dataclass
class PakConflictReport:
    verified: bool
    # pak_name -> {"wins": [other_paks], "loses": [other_paks], "assets_won": int, "assets_lost": int}
    per_pak: dict[str, dict] = field(default_factory=dict)
    # normalized_asset_path -> ordered list of pak names (loser -> ... -> winner)
    asset_owners: dict[str, list[str]] = field(default_factory=dict)
    # config override conflicts (paks shipping UserEngine.ini / DefaultEngine.ini)
    config_conflicts: list[tuple[str, list[str]]] = field(default_factory=list)


def parse_pak_priority(filename: str, folder: str = "~mods", rule: Optional[PakOrderRule] = None) -> PakPriority:
    """Compute a pak's effective sort key under the given PakOrderRule (higher key = higher priority)."""
    r = rule or PakOrderRule()
    stem = Path(filename).stem
    m = _P_NUM_RE.match(stem)
    if m:
        has_p = True
        chunk_num = int(m.group("num"))
    elif _P_PLAIN_RE.match(stem):
        has_p = True
        chunk_num = 0
    else:
        has_p = False
        chunk_num = -1

    folder_rank = FOLDER_RANKS.get(folder, 1)
    chunk_weight = chunk_num if r.higher_chunk_wins else -chunk_num
    # For name comparison, invert bytes if earlier-name wins
    name_low = filename.lower()
    if r.name_ascending_later_wins:
        name_key = tuple(ord(c) for c in name_low)
    else:
        name_key = tuple(-ord(c) for c in name_low)

    sort_key = (folder_rank, 1 if has_p else 0, chunk_weight, name_key)
    return PakPriority(
        filename=filename,
        folder=folder,
        has_p=has_p,
        chunk_num=chunk_num,
        sort_key=sort_key,
    )


def strip_wmm_order_number(filename: str) -> str:
    """Strip a WMM-assigned _<num>_P suffix to recover the base stem + _P.pak."""
    p = Path(filename)
    m = _P_NUM_RE.match(p.stem)
    if m:
        return f"{m.group('base')}_P{p.suffix}"
    return filename


def assign_custom_order_names(
    ordered_paks: list[dict],
    rule: Optional[PakOrderRule] = None,
    start_num: int = 9000,
    step: int = 10,
) -> dict[str, str]:
    """Compute deployed filenames for a desired pak order (lowest priority -> highest priority).

    Each item in ordered_paks is a dict with:
      - 'id': unique key (e.g. mod_id + ':' + original_name)
      - 'original_name': str (e.g. 'Foo_P.pak')
      - 'folder': '~mods' or 'LogicMods'
      - 'locked': bool (if True, never rename)

    Returns {id: deployed_name}. Paks keep their original name whenever their natural
    order already matches the desired order, or if they are locked / in LogicMods.
    """
    r = rule or PakOrderRule()
    result: dict[str, str] = {}

    # Check if the current original names already sort strictly in the desired order
    movable = [p for p in ordered_paks if p.get("folder", "~mods") == "~mods" and not p.get("locked", False)]
    for p in ordered_paks:
        result[p["id"]] = p["original_name"]

    if len(movable) <= 1:
        return result

    keys = [parse_pak_priority(p["original_name"], "~mods", r).sort_key for p in movable]
    if all(keys[i] < keys[i + 1] for i in range(len(keys) - 1)):
        return result

    # Assign monotonic chunk numbers realising the desired order
    total = len(movable)
    for idx, p in enumerate(movable):
        orig = p["original_name"]
        stem = Path(orig).stem
        suffix = Path(orig).suffix or ".pak"
        m_num = _P_NUM_RE.match(stem)
        m_p = _P_PLAIN_RE.match(stem)
        if m_num:
            base = m_num.group("base")
        elif m_p:
            base = m_p.group("base")
        else:
            base = stem

        if r.higher_chunk_wins:
            num = start_num + idx * step
        else:
            num = start_num + (total - 1 - idx) * step
        result[p["id"]] = f"{base}_{num}_P{suffix}"

    return result


def analyze_pak_conflicts(
    pak_entries: list[tuple[str, str, PakIndex]],  # (display_name, folder, PakIndex)
    rule: Optional[PakOrderRule] = None,
) -> PakConflictReport:
    """Compute asset-level overrides and config conflicts across a set of paks."""
    r = rule or PakOrderRule()
    report = PakConflictReport(verified=r.verified)

    # Sort paks from lowest priority to highest priority
    ranked = sorted(
        pak_entries,
        key=lambda item: parse_pak_priority(item[0], item[1], r).sort_key,
    )

    for name, _folder, idx in ranked:
        report.per_pak[name] = {
            "wins": [],
            "loses": [],
            "assets_won": 0,
            "assets_lost": 0,
            "file_count": len(idx.files),
        }
        cfgs = idx.config_override_files()
        if cfgs:
            report.config_conflicts.append((name, cfgs))

    asset_map: dict[str, list[str]] = {}
    for name, _folder, idx in ranked:
        for f in idx.files:
            key = f.lower()
            asset_map.setdefault(key, []).append(name)

    win_sets: dict[str, set[str]] = {name: set() for name, _, _ in ranked}
    lose_sets: dict[str, set[str]] = {name: set() for name, _, _ in ranked}

    for asset, owners in asset_map.items():
        if len(owners) < 2:
            continue
        report.asset_owners[asset] = owners
        winner = owners[-1]
        losers = owners[:-1]
        report.per_pak[winner]["assets_won"] += 1
        for loser in losers:
            report.per_pak[loser]["assets_lost"] += 1
            win_sets[winner].add(loser)
            lose_sets[loser].add(winner)

    for name in report.per_pak:
        report.per_pak[name]["wins"] = sorted(win_sets[name])
        report.per_pak[name]["loses"] = sorted(lose_sets[name])

    return report


def scan_folder_conflicts(
    mods_pak_dir: Path,
    rule: Optional[PakOrderRule] = None,
    aes_key_hex: Optional[str] = None,
) -> PakConflictReport:
    """Convenience helper: read all *.pak in mods_pak_dir and analyze conflicts."""
    entries: list[tuple[str, str, PakIndex]] = []
    if mods_pak_dir.is_dir():
        for p in sorted(mods_pak_dir.glob("*.pak")):
            try:
                idx = read_pak_index(p, aes_key_hex=aes_key_hex)
                entries.append((p.name, "~mods", idx))
            except PakReadError:
                continue
    return analyze_pak_conflicts(entries, rule=rule)
