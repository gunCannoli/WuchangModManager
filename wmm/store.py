"""Portable data store: settings.json, manifest.json, mods/<id>/wmm.json, profiles/<name>.json (§2)."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .paks import PakOrderRule


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def slugify_id(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", name.strip()).strip("_.-")
    return s or "mod"


@dataclass
class ModComponentRecord:
    kind: str  # pak, ue4ss_mod, ue4ss_shared, ini_tweak, loader
    name: str
    target_kind: str  # ~mods, LogicMods, ue4ss_mod, ue4ss_shared, engine_ini, binaries
    store_rel: str  # path relative to data/mods/<id>/
    companions: list[str] = field(default_factory=list)  # relative to data/mods/<id>/
    bp_mod_names: list[str] = field(default_factory=list)
    config_overrides: list[str] = field(default_factory=list)
    locked: bool = False
    selected: bool = True  # False for unselected variants

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ModComponentRecord":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class ModRecord:
    id: str
    name: str
    version: str = ""
    badge: str = "UNKNOWN"
    source: str = ""
    installed_at: str = field(default_factory=_utc_now)
    nexus_mod_id: Optional[int] = None
    nexus_file_id: Optional[int] = None
    adopted: bool = False
    components: list[ModComponentRecord] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["components"] = [c.to_dict() for c in self.components]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "ModRecord":
        comps = [ModComponentRecord.from_dict(c) for c in d.get("components", [])]
        data = {k: v for k, v in d.items() if k in cls.__dataclass_fields__ and k != "components"}
        return cls(**data, components=comps)


@dataclass
class Profile:
    name: str = "default"
    enabled_mods: list[str] = field(default_factory=list)
    pak_order: list[str] = field(default_factory=list)  # "mod_id:original_name" (low -> high)
    ue4ss_order: list[str] = field(default_factory=list)  # UE4SS mod folder names

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Profile":
        return cls(
            name=d.get("name", "default"),
            enabled_mods=list(d.get("enabled_mods", [])),
            pak_order=list(d.get("pak_order", [])),
            ue4ss_order=list(d.get("ue4ss_order", [])),
        )


@dataclass
class ManifestEntry:
    path: str  # deployed absolute path in game folder
    mod_id: str
    kind: str  # pak_hardlink, pak_move, ue4ss_junction, ue4ss_shared_junction, ini_block
    store_path: str  # path in data/mods/<id>/...
    original_name: str
    deployed_name: str
    backup_path: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ManifestEntry":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


class Store:
    """Manages WMM's portable data/ directory."""

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.mods_dir = self.data_dir / "mods"
        self.profiles_dir = self.data_dir / "profiles"
        self.downloads_dir = self.data_dir / "downloads"
        self.backup_dir = self.data_dir / "backup"
        self.logs_dir = self.data_dir / "logs"
        self.settings_path = self.data_dir / "settings.json"
        self.manifest_path = self.data_dir / "manifest.json"
        self.ensure_dirs()

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.mods_dir, self.profiles_dir, self.downloads_dir, self.backup_dir, self.logs_dir):
            d.mkdir(parents=True, exist_ok=True)

    # --- Settings ---

    def load_settings(self) -> dict[str, Any]:
        if not self.settings_path.is_file():
            return {
                "game_path": "",
                "mods_archive_dir": "",
                "language": "en",
                "nexus_api_key": "",
                "auto_extract": False,
                "keep_archives": True,
                "active_profile": "default",
                "aes_key": "",
                "pak_order_rules": {},
            }
        try:
            return json.loads(self.settings_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"active_profile": "default", "pak_order_rules": {}}

    def save_settings(self, settings: dict[str, Any]) -> None:
        self.settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8", newline="\n")

    def get_pak_order_rule(self, build_id: str) -> PakOrderRule:
        settings = self.load_settings()
        rules = settings.get("pak_order_rules", {})
        if build_id and build_id in rules:
            return PakOrderRule.from_dict(rules[build_id])
        return PakOrderRule(verified=False, build_id=build_id)

    def save_pak_order_rule(self, rule: PakOrderRule) -> None:
        settings = self.load_settings()
        rules = settings.setdefault("pak_order_rules", {})
        rules[rule.build_id or "default"] = rule.to_dict()
        self.save_settings(settings)

    # --- Mods ---

    def mod_dir(self, mod_id: str) -> Path:
        return self.mods_dir / mod_id

    def unique_mod_id(self, desired_name: str) -> str:
        base = slugify_id(desired_name)
        if not (self.mods_dir / base).exists():
            return base
        i = 2
        while (self.mods_dir / f"{base}_{i}").exists():
            i += 1
        return f"{base}_{i}"

    def save_mod(self, mod: ModRecord) -> None:
        mdir = self.mod_dir(mod.id)
        mdir.mkdir(parents=True, exist_ok=True)
        (mdir / "wmm.json").write_text(json.dumps(mod.to_dict(), indent=2) + "\n", encoding="utf-8", newline="\n")

    def load_mod(self, mod_id: str) -> Optional[ModRecord]:
        meta = self.mod_dir(mod_id) / "wmm.json"
        if not meta.is_file():
            return None
        try:
            return ModRecord.from_dict(json.loads(meta.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            return None

    def list_mods(self) -> list[ModRecord]:
        mods: list[ModRecord] = []
        if not self.mods_dir.is_dir():
            return mods
        for child in sorted(self.mods_dir.iterdir()):
            if child.is_dir() and (child / "wmm.json").is_file():
                rec = self.load_mod(child.name)
                if rec is not None:
                    mods.append(rec)
        return mods

    # --- Profiles ---

    def profile_path(self, name: str) -> Path:
        return self.profiles_dir / f"{slugify_id(name)}.json"

    def load_profile(self, name: Optional[str] = None) -> Profile:
        if name is None:
            name = self.load_settings().get("active_profile", "default")
        p = self.profile_path(name)
        if not p.is_file():
            prof = Profile(name=name)
            self.save_profile(prof)
            return prof
        return Profile.from_dict(json.loads(p.read_text(encoding="utf-8")))

    def save_profile(self, profile: Profile) -> None:
        self.profile_path(profile.name).write_text(
            json.dumps(profile.to_dict(), indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )

    def list_profiles(self) -> list[str]:
        if not self.profiles_dir.is_dir():
            return ["default"]
        names = [p.stem for p in sorted(self.profiles_dir.glob("*.json"))]
        return names or ["default"]

    def clone_profile(self, source_name: str, new_name: str) -> Profile:
        src = self.load_profile(source_name)
        cloned = Profile(
            name=new_name,
            enabled_mods=list(src.enabled_mods),
            pak_order=list(src.pak_order),
            ue4ss_order=list(src.ue4ss_order),
        )
        self.save_profile(cloned)
        return cloned

    def delete_profile(self, name: str) -> bool:
        profiles = self.list_profiles()
        if len(profiles) <= 1:
            return False
        p = self.profile_path(name)
        if not p.is_file():
            return False
        p.unlink()
        return True

    # --- Manifest ---

    def load_manifest(self) -> list[ManifestEntry]:
        if not self.manifest_path.is_file():
            return []
        try:
            raw = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            return [ManifestEntry.from_dict(e) for e in raw]
        except (OSError, json.JSONDecodeError):
            return []

    def save_manifest(self, entries: list[ManifestEntry]) -> None:
        self.manifest_path.write_text(
            json.dumps([e.to_dict() for e in entries], indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
