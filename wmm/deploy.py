"""One-copy-per-file mod installation, deployment, disable, adopt, and remove-all (§4)."""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from .detect import DetectedComponent, DetectionResult, detect_mod
from .engineini import apply_ini_tweak_block, update_wmm_bp_section
from .extract import extract_archive, sniff_archive_format
from .game import GameInstall, running_game_processes
from .paks import assign_custom_order_names
from .platform import Platform, get_platform
from .store import ManifestEntry, ModComponentRecord, ModRecord, Profile, Store
from .ue4ss import BASE_UE4SS_MODS, LAST_UE4SS_MOD, write_mods_txt

_IGNORED_UE4SS_DIRS = {n.lower() for n in BASE_UE4SS_MODS} | {
    LAST_UE4SS_MOD.lower(),
    "shared",
    "wmm_bploader",
    "actordumpermod",
    "cheatmanagerenablermod",
    "jsbluaprofilermod",
}


@dataclass
class UnmanagedMod:
    kind: str  # "pak", "ue4ss_mod"
    name: str
    path: Path
    badge: str  # "PAK", "BP", "UE4SS"
    folder: str  # "~mods", "LogicMods", "ue4ss"


def scan_unmanaged_mods(game: GameInstall, manifest: list[ManifestEntry]) -> list[UnmanagedMod]:
    """Find paks and UE4SS mods in the game folder that are not tracked in WMM's manifest."""
    managed_paths: set[str] = set()
    for e in manifest:
        try:
            managed_paths.add(str(Path(e.path).resolve()).lower())
        except OSError:
            managed_paths.add(e.path.lower())

    found: list[UnmanagedMod] = []

    # 1. Scan ~mods and LogicMods for unmanaged *.pak
    for folder_label, pak_dir in (("~mods", game.mods_pak_dir), ("LogicMods", game.logic_mods_dir)):
        if not pak_dir.is_dir():
            continue
        for p in sorted(pak_dir.glob("*.pak")):
            if not p.is_file():
                continue
            key = str(p.resolve()).lower()
            if key in managed_paths:
                continue
            badge = "PAK"
            try:
                from .pakindex import read_pak_index
                idx = read_pak_index(p)
                if idx.bp_mod_names():
                    badge = "BP"
            except Exception:
                pass
            found.append(UnmanagedMod(kind="pak", name=p.name, path=p, badge=badge, folder=folder_label))

    # 2. Scan ue4ss/Mods/* for unmanaged UE4SS mods
    if game.ue4ss_mods_dir.is_dir():
        for d in sorted(game.ue4ss_mods_dir.iterdir()):
            if not d.is_dir():
                continue
            if d.name.lower() in _IGNORED_UE4SS_DIRS:
                continue
            key = str(d.resolve()).lower()
            if key in managed_paths:
                continue
            has_entry = (d / "Scripts" / "main.lua").is_file() or (d / "dlls" / "main.dll").is_file()
            if has_entry:
                found.append(UnmanagedMod(kind="ue4ss_mod", name=d.name, path=d, badge="UE4SS", folder="ue4ss"))

    return found


class DeployError(Exception):
    """Raised when deployment is blocked (e.g. game running, loader unconfirmed, collision)."""


class Manager:
    """High-level mod manager coordinating Store, GameInstall, Platform, and deployment."""

    def __init__(
        self,
        store: Store,
        game: GameInstall,
        platform: Optional[Platform] = None,
        tools_dir: Optional[Path] = None,
        allow_while_running: bool = False,
    ):
        self.store = store
        self.game = game
        self.platform = platform or get_platform()
        self.tools_dir = tools_dir
        self.allow_while_running = allow_while_running

    def _assert_game_closed(self) -> None:
        if self.allow_while_running:
            return
        running = running_game_processes(self.platform)
        if running:
            raise DeployError(f"Game is running ({', '.join(running)}); close it before changing files.")

    def _backup_existing(self, target: Path) -> Optional[str]:
        if not target.exists() and not self.platform.is_dir_link(target):
            return None
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        bdir = self.store.backup_dir / stamp
        bdir.mkdir(parents=True, exist_ok=True)
        dest = bdir / target.name
        shutil.move(str(target), str(dest))
        return str(dest)

    # --- Adding mods into the store ---

    def add_mod(
        self,
        source: Path,
        name: Optional[str] = None,
        version: str = "",
        allow_loaders: bool = False,
        selected_variants: Optional[set[str]] = None,
        keep_archive: Optional[bool] = None,
        nexus_mod_id: Optional[int] = None,
    ) -> ModRecord:
        """Extract/inspect source (archive or folder), store files once under data/mods/<id>/."""
        source = Path(source)
        settings = self.store.load_settings()
        aes_key = settings.get("aes_key") or None

        is_archive = source.is_file() and (
            source.suffix.lower() in (".zip", ".7z", ".rar")
            or sniff_archive_format(source) is not None
        )
        tmp_dir: Optional[Path] = None
        try:
            if is_archive:
                tmp_dir = Path(tempfile.mkdtemp(prefix="wmm_ext_", dir=str(self.store.downloads_dir)))
                extract_archive(source, tmp_dir, tools_dir=self.tools_dir)
                inspect_root = tmp_dir
            else:
                inspect_root = source

            det = detect_mod(inspect_root, aes_key_hex=aes_key)
            if det.has_loader and not allow_loaders:
                loaders = [c.name for c in det.components if c.kind == "loader"]
                raise DeployError(
                    f"Mod ships loader DLL(s) ({', '.join(loaders)}); pass allow_loaders=True to confirm."
                )

            installable = [c for c in det.components if c.kind in ("pak", "ue4ss_mod", "ue4ss_shared", "ini_tweak")]
            if allow_loaders:
                installable.extend(c for c in det.components if c.kind == "loader")
            if not installable:
                raise DeployError(f"No installable mod components found in {source.name}")

            mod_name = name or source.stem
            mod_id = self.store.unique_mod_id(mod_name)
            mod_store_dir = self.store.mod_dir(mod_id)
            mod_store_dir.mkdir(parents=True, exist_ok=True)

            # Decide which variants are selected (default: first variant in each group)
            default_selected_ids: set[int] = set()
            for _grp, comps in det.variants.items():
                if comps:
                    default_selected_ids.add(id(comps[0]))

            comp_records: list[ModComponentRecord] = []
            for comp in installable:
                is_selected = True
                if comp.variant_group is not None:
                    if selected_variants is not None:
                        is_selected = comp.name in selected_variants
                    else:
                        is_selected = id(comp) in default_selected_ids

                rec = self._ingest_component(comp, mod_store_dir, move_from_tmp=(tmp_dir is not None))
                rec.selected = is_selected
                comp_records.append(rec)

            resolved_nexus_id = nexus_mod_id
            if resolved_nexus_id is None:
                from .nexus import parse_nexus_mod_id_from_filename
                resolved_nexus_id = parse_nexus_mod_id_from_filename(source.name)

            record = ModRecord(
                id=mod_id,
                name=mod_name,
                version=version,
                badge=det.badge,
                source=str(source),
                nexus_mod_id=resolved_nexus_id,
                components=comp_records,
            )
            self.store.save_mod(record)

            # Delete archive after install if keep_archives is False and it lives in WMM's downloads/mods folder
            if keep_archive is None:
                keep_archive = bool(settings.get("keep_archives", True))
            if is_archive and not keep_archive:
                try:
                    src_res = source.resolve()
                    managed_dl_dirs = [self.store.downloads_dir.resolve()]
                    mad = settings.get("mods_archive_dir", "")
                    if mad:
                        managed_dl_dirs.append(Path(mad).resolve())
                    if any(d in src_res.parents for d in managed_dl_dirs):
                        source.unlink(missing_ok=True)
                except OSError:
                    pass

            return record
        finally:
            if tmp_dir is not None and tmp_dir.exists():
                shutil.rmtree(tmp_dir, ignore_errors=True)

    def _ingest_component(
        self,
        comp: DetectedComponent,
        mod_store_dir: Path,
        move_from_tmp: bool,
    ) -> ModComponentRecord:
        if comp.kind == "pak":
            sub = mod_store_dir / ("LogicMods" if comp.target_kind == "LogicMods" else "paks")
            sub.mkdir(parents=True, exist_ok=True)
            dst = sub / comp.source_path.name
            if move_from_tmp:
                shutil.move(str(comp.source_path), str(dst))
            else:
                shutil.copy2(comp.source_path, dst)
            comp_rels: list[str] = []
            for cp in comp.companions:
                cdst = sub / cp.name
                if move_from_tmp:
                    shutil.move(str(cp), str(cdst))
                else:
                    shutil.copy2(cp, cdst)
                comp_rels.append(str(cdst.relative_to(mod_store_dir)).replace("\\", "/"))
            return ModComponentRecord(
                kind="pak",
                name=comp.name,
                target_kind=comp.target_kind,
                store_rel=str(dst.relative_to(mod_store_dir)).replace("\\", "/"),
                companions=comp_rels,
                bp_mod_names=list(comp.bp_mod_names),
                config_overrides=list(comp.config_overrides),
                locked=(comp.target_kind == "LogicMods"),
            )

        if comp.kind in ("ue4ss_mod", "ue4ss_shared"):
            sub = mod_store_dir / ("ue4ss_shared" if comp.kind == "ue4ss_shared" else "ue4ss")
            sub.mkdir(parents=True, exist_ok=True)
            dst = sub / comp.name
            if dst.exists():
                shutil.rmtree(dst)
            if move_from_tmp:
                shutil.move(str(comp.source_path), str(dst))
            else:
                shutil.copytree(comp.source_path, dst)
            return ModComponentRecord(
                kind=comp.kind,
                name=comp.name,
                target_kind=comp.target_kind,
                store_rel=str(dst.relative_to(mod_store_dir)).replace("\\", "/"),
            )

        if comp.kind == "ini_tweak":
            sub = mod_store_dir / "ini"
            sub.mkdir(parents=True, exist_ok=True)
            dst = sub / comp.source_path.name
            if move_from_tmp:
                shutil.move(str(comp.source_path), str(dst))
            else:
                shutil.copy2(comp.source_path, dst)
            return ModComponentRecord(
                kind="ini_tweak",
                name=comp.name,
                target_kind="engine_ini",
                store_rel=str(dst.relative_to(mod_store_dir)).replace("\\", "/"),
            )

        # loader (only when explicitly confirmed)
        sub = mod_store_dir / "loader"
        sub.mkdir(parents=True, exist_ok=True)
        dst = sub / comp.source_path.name
        if comp.source_path.is_dir():
            if move_from_tmp:
                shutil.move(str(comp.source_path), str(dst))
            else:
                shutil.copytree(comp.source_path, dst)
        else:
            if move_from_tmp:
                shutil.move(str(comp.source_path), str(dst))
            else:
                shutil.copy2(comp.source_path, dst)
        return ModComponentRecord(
            kind="loader",
            name=comp.name,
            target_kind="binaries",
            store_rel=str(dst.relative_to(mod_store_dir)).replace("\\", "/"),
        )

    # --- Adopt unmanaged files already in the game folder (zero copy) ---

    def adopt_pak(self, pak_path: Path, name: Optional[str] = None) -> ModRecord:
        """Adopt an unmanaged pak already in ~mods/ or LogicMods/ into WMM with zero duplicate copy."""
        self._assert_game_closed()
        pak_path = Path(pak_path)
        if not pak_path.is_file():
            raise DeployError(f"Pak not found: {pak_path}")

        det = detect_mod(pak_path)
        comp = det.components[0]
        is_logic = pak_path.parent.name.lower() == "logicmods"
        target_kind = "LogicMods" if is_logic else "~mods"

        mod_name = name or pak_path.stem
        mod_id = self.store.unique_mod_id(mod_name)
        mod_store_dir = self.store.mod_dir(mod_id)
        sub = mod_store_dir / ("LogicMods" if is_logic else "paks")
        sub.mkdir(parents=True, exist_ok=True)
        store_pak = sub / pak_path.name

        # Move to store then immediately re-deploy via hardlink/move so bytes exist once
        shutil.move(str(pak_path), str(store_pak))
        comp_rels: list[str] = []
        for cp in comp.companions:
            if cp.is_file():
                cdst = sub / cp.name
                shutil.move(str(cp), str(cdst))
                comp_rels.append(str(cdst.relative_to(mod_store_dir)).replace("\\", "/"))

        rec = ModRecord(
            id=mod_id,
            name=mod_name,
            badge=det.badge,
            source=str(pak_path),
            adopted=True,
            components=[
                ModComponentRecord(
                    kind="pak",
                    name=pak_path.name,
                    target_kind=target_kind,
                    store_rel=str(store_pak.relative_to(mod_store_dir)).replace("\\", "/"),
                    companions=comp_rels,
                    bp_mod_names=list(comp.bp_mod_names),
                    config_overrides=list(comp.config_overrides),
                    locked=is_logic,
                )
            ],
        )
        self.store.save_mod(rec)
        prof = self.store.load_profile()
        if mod_id not in prof.enabled_mods:
            prof.enabled_mods.append(mod_id)
            self.store.save_profile(prof)
        self.apply_profile(prof)
        return rec

    def adopt_ue4ss_mod(self, mod_folder: Path, name: Optional[str] = None) -> ModRecord:
        """Adopt an unmanaged UE4SS mod folder into WMM: move to store + junction back (zero copy)."""
        self._assert_game_closed()
        mod_folder = Path(mod_folder)
        if not mod_folder.is_dir() or self.platform.is_dir_link(mod_folder):
            raise DeployError(f"Not an unmanaged UE4SS mod directory: {mod_folder}")

        mod_name = name or mod_folder.name
        mod_id = self.store.unique_mod_id(mod_name)
        mod_store_dir = self.store.mod_dir(mod_id)
        sub = mod_store_dir / "ue4ss"
        sub.mkdir(parents=True, exist_ok=True)
        dst = sub / mod_folder.name
        shutil.move(str(mod_folder), str(dst))

        rec = ModRecord(
            id=mod_id,
            name=mod_name,
            badge="UE4SS",
            source=str(mod_folder),
            adopted=True,
            components=[
                ModComponentRecord(
                    kind="ue4ss_mod",
                    name=mod_folder.name,
                    target_kind="ue4ss_mod",
                    store_rel=str(dst.relative_to(mod_store_dir)).replace("\\", "/"),
                )
            ],
        )
        self.store.save_mod(rec)
        prof = self.store.load_profile()
        if mod_id not in prof.enabled_mods:
            prof.enabled_mods.append(mod_id)
            self.store.save_profile(prof)
        self.apply_profile(prof)
        return rec

    # --- Enable / Disable / Apply Profile / Remove All ---

    def set_enabled(self, mod_id: str, enabled: bool, profile_name: Optional[str] = None) -> Profile:
        prof = self.store.load_profile(profile_name)
        if self.store.load_mod(mod_id) is None:
            raise DeployError(f"Unknown mod id: {mod_id}")
        if enabled:
            if mod_id not in prof.enabled_mods:
                prof.enabled_mods.append(mod_id)
        else:
            prof.enabled_mods = [m for m in prof.enabled_mods if m != mod_id]
        self.store.save_profile(prof)
        self.apply_profile(prof)
        return prof

    def _undeploy_manifest(self, manifest: list[ManifestEntry], keep_paths: Optional[set[str]] = None) -> None:
        """Cleanly undo every manifest entry without touching unmanaged files."""
        keep_lower = {p.lower() for p in (keep_paths or set())}
        for entry in reversed(manifest):
            target = Path(entry.path)
            store_path = Path(entry.store_path)
            if str(target).lower() in keep_lower and target.exists():
                continue

            try:
                if entry.kind == "pak_hardlink":
                    if target.exists():
                        target.unlink()
                elif entry.kind == "pak_move":
                    if target.exists():
                        store_path.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(target), str(store_path))
                elif entry.kind in ("ue4ss_junction", "ue4ss_shared_junction"):
                    if self.platform.is_dir_link(target):
                        self.platform.remove_dir_link(target)
                elif entry.kind == "ini_block":
                    if self.game.engine_ini_path is not None and self.game.engine_ini_path.is_file():
                        apply_ini_tweak_block(self.game.engine_ini_path, entry.mod_id, None)
            except PermissionError as exc:
                raise DeployError(
                    f"'{target.name}' is currently locked by the running game. Close the game to remove or modify it."
                ) from exc

            if entry.backup_path:
                bp = Path(entry.backup_path)
                if bp.exists() and not target.exists():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(bp), str(target))

    def remove_all(self) -> int:
        """Undo every manifest entry, restore backups, and clear WMM's Engine.ini section."""
        self._assert_game_closed()
        manifest = self.store.load_manifest()
        count = len(manifest)
        self._undeploy_manifest(manifest)
        self.store.save_manifest([])

        if self.game.ue4ss_mods_txt.is_file():
            all_managed_names = {
                comp.name
                for mod in self.store.list_mods()
                for comp in mod.components
                if comp.kind == "ue4ss_mod"
            }
            if all_managed_names:
                write_mods_txt(
                    self.game.ue4ss_mods_txt,
                    [],
                    preserve_unmanaged=True,
                    remove_names=all_managed_names,
                )

        if (self.game.ue4ss_mods_dir / "WMM_BPLoader").is_dir():
            from .base import sync_wmm_lua_bploader_list
            sync_wmm_lua_bploader_list(self.game, [])

        if self.game.engine_ini_path is not None and self.game.engine_ini_path.is_file():
            update_wmm_bp_section(self.game.engine_ini_path, [], backup_dir=self.store.backup_dir)
        return count

    def apply_profile(self, profile: Optional[Profile] = None) -> list[ManifestEntry]:
        """Reconcile the game folder with the given profile (one copy per file)."""
        self._assert_game_closed()
        prof = profile or self.store.load_profile()

        # 1. Gather enabled mods & compute desired pak names first so locked hardlinks
        #    that stay unchanged can be kept in place when allow_while_running=True.
        old_manifest = self.store.load_manifest()
        enabled_mods: list[ModRecord] = []
        for mid in prof.enabled_mods:
            rec = self.store.load_mod(mid)
            if rec is not None:
                enabled_mods.append(rec)

        pak_items: list[tuple[ModRecord, ModComponentRecord]] = []
        for mod in enabled_mods:
            for comp in mod.components:
                if comp.kind == "pak" and comp.selected:
                    pak_items.append((mod, comp))

        order_index = {key: i for i, key in enumerate(prof.pak_order)}
        pak_items.sort(key=lambda pair: order_index.get(f"{pair[0].id}:{pair[1].name}", 10**9))

        rule = self.store.get_pak_order_rule(self.game.build_id)
        order_spec = [
            {
                "id": f"{mod.id}:{comp.name}",
                "original_name": comp.name,
                "folder": comp.target_kind,
                "locked": comp.locked,
            }
            for mod, comp in pak_items
        ]
        deployed_names = assign_custom_order_names(order_spec, rule=rule)

        keep_paths: set[str] = set()
        if self.allow_while_running:
            desired_map = {}
            for mod, comp in pak_items:
                tdir = self.game.logic_mods_dir if comp.target_kind == "LogicMods" else self.game.mods_pak_dir
                dep_name = deployed_names[f"{mod.id}:{comp.name}"]
                desired_map[str(tdir / dep_name).lower()] = str(self.store.mod_dir(mod.id) / comp.store_rel).lower()
            for e in old_manifest:
                if e.kind == "pak_hardlink" and e.path.lower() in desired_map:
                    if desired_map[e.path.lower()] == e.store_path.lower():
                        keep_paths.add(e.path)

        # Undo current WMM manifest (skipping unchanged locked hardlinks if game is running)
        self._undeploy_manifest(old_manifest, keep_paths=keep_paths)
        new_manifest: list[ManifestEntry] = []

        # 4. Deploy paks (hardlink on same volume, move on cross-volume)
        bp_mod_names: list[str] = []
        for mod, comp in pak_items:
            mod_dir = self.store.mod_dir(mod.id)
            src_pak = mod_dir / comp.store_rel
            if not src_pak.is_file():
                continue
            target_dir = self.game.logic_mods_dir if comp.target_kind == "LogicMods" else self.game.mods_pak_dir
            target_dir.mkdir(parents=True, exist_ok=True)

            dep_name = deployed_names[f"{mod.id}:{comp.name}"]
            dst_pak = target_dir / dep_name
            if dst_pak.exists() and str(dst_pak) in keep_paths:
                backup = None
                mode = "hardlink"
            else:
                backup = self._backup_existing(dst_pak)
                mode = self.platform.hardlink_or_move(src_pak, dst_pak)
            new_manifest.append(
                ManifestEntry(
                    path=str(dst_pak),
                    mod_id=mod.id,
                    kind=f"pak_{mode}",
                    store_path=str(src_pak),
                    original_name=comp.name,
                    deployed_name=dep_name,
                    backup_path=backup,
                )
            )
            # Companions (.ucas, .utoc, .sig) follow the deployed stem
            dep_stem = Path(dep_name).stem
            for crel in comp.companions:
                csrc = mod_dir / crel
                if not csrc.is_file():
                    continue
                cdst = target_dir / f"{dep_stem}{csrc.suffix}"
                cbackup = self._backup_existing(cdst)
                cmode = self.platform.hardlink_or_move(csrc, cdst)
                new_manifest.append(
                    ManifestEntry(
                        path=str(cdst),
                        mod_id=mod.id,
                        kind=f"pak_{cmode}",
                        store_path=str(csrc),
                        original_name=csrc.name,
                        deployed_name=cdst.name,
                        backup_path=cbackup,
                    )
                )

            if comp.target_kind == "~mods":
                for bp in comp.bp_mod_names:
                    if bp not in bp_mod_names:
                        bp_mod_names.append(bp)

        # 5. Deploy UE4SS mods & shared libs via directory junctions/symlinks
        ue4ss_entries: list[tuple[str, bool]] = []
        ue4ss_order_idx = {n.lower(): i for i, n in enumerate(prof.ue4ss_order)}
        ue4ss_mod_comps: list[tuple[ModRecord, ModComponentRecord]] = []

        for mod in enabled_mods:
            mod_dir = self.store.mod_dir(mod.id)
            for comp in mod.components:
                if not comp.selected:
                    continue
                if comp.kind == "ue4ss_shared":
                    src_dir = mod_dir / comp.store_rel
                    dst_link = self.game.ue4ss_mods_dir / "shared" / comp.name
                    backup = self._backup_existing(dst_link)
                    self.platform.create_dir_link(dst_link, src_dir)
                    new_manifest.append(
                        ManifestEntry(
                            path=str(dst_link),
                            mod_id=mod.id,
                            kind="ue4ss_shared_junction",
                            store_path=str(src_dir),
                            original_name=comp.name,
                            deployed_name=comp.name,
                            backup_path=backup,
                        )
                    )
                elif comp.kind == "ue4ss_mod":
                    ue4ss_mod_comps.append((mod, comp))

        ue4ss_mod_comps.sort(key=lambda pair: ue4ss_order_idx.get(pair[1].name.lower(), 10**9))
        for mod, comp in ue4ss_mod_comps:
            mod_dir = self.store.mod_dir(mod.id)
            src_dir = mod_dir / comp.store_rel
            dst_link = self.game.ue4ss_mods_dir / comp.name
            backup = self._backup_existing(dst_link)
            self.platform.create_dir_link(dst_link, src_dir)
            ue4ss_entries.append((comp.name, True))
            new_manifest.append(
                ManifestEntry(
                    path=str(dst_link),
                    mod_id=mod.id,
                    kind="ue4ss_junction",
                    store_path=str(src_dir),
                    original_name=comp.name,
                    deployed_name=comp.name,
                    backup_path=backup,
                )
            )

        # Update mods.txt (LF-only) if any UE4SS mods exist or mods.txt already exists
        if ue4ss_entries or self.game.ue4ss_mods_txt.is_file():
            # Prune managed UE4SS mods that are not active in this profile so switching
            # profiles cleanly removes them from mods.txt instead of leaving stale entries.
            active_lower = {n.lower() for n, _ in ue4ss_entries}
            inactive_managed: set[str] = set()
            for mod in self.store.list_mods():
                for comp in mod.components:
                    if comp.kind == "ue4ss_mod" and comp.name.lower() not in active_lower:
                        inactive_managed.add(comp.name)
            write_mods_txt(
                self.game.ue4ss_mods_txt,
                ue4ss_entries,
                preserve_unmanaged=True,
                remove_names=inactive_managed,
            )

        # 6. Sync WMM Lua BP loader list (if installed) & Engine.ini for BP mods & ini tweaks
        if (self.game.ue4ss_mods_dir / "WMM_BPLoader").is_dir():
            from .base import sync_wmm_lua_bploader_list
            sync_wmm_lua_bploader_list(self.game, bp_mod_names)

        if self.game.engine_ini_path is not None and (bp_mod_names or self.game.engine_ini_path.is_file()):
            if bp_mod_names or any(e.kind == "ini_block" for e in old_manifest):
                update_wmm_bp_section(
                    self.game.engine_ini_path,
                    bp_mod_names,
                    backup_dir=self.store.backup_dir,
                )
            for mod in enabled_mods:
                mod_dir = self.store.mod_dir(mod.id)
                for comp in mod.components:
                    if comp.kind == "ini_tweak" and comp.selected:
                        src_ini = mod_dir / comp.store_rel
                        if src_ini.is_file():
                            apply_ini_tweak_block(
                                self.game.engine_ini_path,
                                mod.id,
                                src_ini.read_text(encoding="utf-8", errors="replace"),
                                backup_dir=self.store.backup_dir,
                            )
                            new_manifest.append(
                                ManifestEntry(
                                    path=str(self.game.engine_ini_path),
                                    mod_id=mod.id,
                                    kind="ini_block",
                                    store_path=str(src_ini),
                                    original_name=comp.name,
                                    deployed_name=comp.name,
                                )
                            )

        self.store.save_manifest(new_manifest)
        return new_manifest
