"""Command-line interface for Wuchang Mod Manager (§2)."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

from . import __version__
from .base import detect_mod_base
from .deploy import DeployError, Manager
from .detect import detect_mod
from .extract import ExtractError, extract_archive
from .game import GameInstall, detect_all_installs, validate_game_root
from .i18n import Translator, update_en_template
from .paks import scan_folder_conflicts
from .store import Profile, Store
from .ue4ss import detect_ue4ss


def _package_root() -> Path:
    """Return portable WMM root folder (next to WMM.exe when frozen, else repo package root)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _resolve_game(store: Store, game_arg: Optional[str]) -> Optional[GameInstall]:
    if game_arg:
        return validate_game_root(Path(game_arg))
    saved = store.load_settings().get("game_path")
    if saved:
        inst = validate_game_root(Path(saved))
        if inst is not None:
            return inst
    found = detect_all_installs()
    return found[0] if found else None


def cmd_detect_game(args: argparse.Namespace, store: Store, t: Translator) -> int:
    installs = [validate_game_root(Path(args.game))] if args.game else detect_all_installs()
    installs = [i for i in installs if i is not None]
    if not installs:
        print(t.tr("cli.game_not_found"))
        return 1
    for inst in installs:
        print(t.tr("cli.game_found", root=inst.root, launcher=inst.launcher, build_id=inst.build_id))
        print(f"  exe:        {inst.exe_path}")
        print(f"  engine:     {inst.engine_version or '(unknown)'}")
        print(f"  binaries:   {inst.binaries_dir}")
        print(f"  ~mods:      {inst.mods_pak_dir}")
        print(f"  Engine.ini: {inst.engine_ini_path} ({'found' if inst.engine_ini_path and inst.engine_ini_path.is_file() else 'missing'})")
        ue = detect_ue4ss(inst.binaries_dir)
        ue_str = f"installed ({ue.version_line or 'version unknown'})" if ue.installed else "not installed"
        if ue.mods_txt_has_crlf:
            ue_str += " [WARNING: mods.txt has CRLF!]"
        print(f"  UE4SS:      {ue_str}")
    return 0


def cmd_inspect(args: argparse.Namespace, store: Store, t: Translator) -> int:
    src = Path(args.path)
    tools_dir = _package_root() / "tools"
    aes_key = store.load_settings().get("aes_key") or None
    tmp_dir: Optional[Path] = None
    try:
        if src.is_file() and src.suffix.lower() in (".zip", ".7z", ".rar"):
            tmp_dir = Path(tempfile.mkdtemp(prefix="wmm_insp_"))
            extract_archive(src, tmp_dir, tools_dir=tools_dir)
            det = detect_mod(tmp_dir, aes_key_hex=aes_key)
        else:
            det = detect_mod(src, aes_key_hex=aes_key)
    except ExtractError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    finally:
        if tmp_dir and tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)

    if args.json:
        out = {
            "source": str(src),
            "badge": det.badge,
            "has_loader": det.has_loader,
            "has_config_override": det.has_config_override,
            "components": [
                {
                    "kind": c.kind,
                    "name": c.name,
                    "target": c.target_kind,
                    "bp_mods": c.bp_mod_names,
                    "config_overrides": c.config_overrides,
                    "variant_group": c.variant_group,
                    "requires_confirmation": c.requires_confirmation,
                    "file_count": len(c.pak_files),
                    "notes": c.notes,
                }
                for c in det.components
            ],
        }
        print(json.dumps(out, indent=2))
        return 0

    print(f"Source: {src.name}  [{det.badge}]")
    if det.has_loader:
        print("  WARNING: contains loader DLL / bitfix (requires explicit confirmation)")
    if det.has_config_override:
        print("  WARNING: ships Config/UserEngine.ini or DefaultEngine.ini (conflicts with BP loader)")
    for c in det.components:
        extra = []
        if c.bp_mod_names:
            extra.append(f"BP={','.join(c.bp_mod_names)}")
        if c.config_overrides:
            extra.append(f"CFG={','.join(c.config_overrides)}")
        if c.variant_group:
            extra.append(f"variant={c.variant_group}")
        if c.pak_files:
            extra.append(f"{len(c.pak_files)} files")
        suffix = f" ({'; '.join(extra)})" if extra else ""
        print(f"  - [{c.kind}] {c.name} -> {c.target_kind}{suffix}")
    return 0


def cmd_add(args: argparse.Namespace, store: Store, t: Translator) -> int:
    game = _resolve_game(store, args.game)
    if game is None:
        print(t.tr("cli.game_not_found"), file=sys.stderr)
        return 1
    mgr = Manager(store, game, tools_dir=_package_root() / "tools")
    try:
        rec = mgr.add_mod(
            Path(args.path),
            name=args.name,
            version=args.version or "",
            allow_loaders=args.allow_loaders,
        )
        print(t.tr("cli.mod_added", name=rec.name, id=rec.id, badge=rec.badge))
        if args.enable:
            mgr.set_enabled(rec.id, True)
            print(t.tr("cli.mod_enabled", id=rec.id))
        return 0
    except (DeployError, ExtractError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


def cmd_toggle(args: argparse.Namespace, store: Store, t: Translator, enable: bool) -> int:
    game = _resolve_game(store, args.game)
    if game is None:
        print(t.tr("cli.game_not_found"), file=sys.stderr)
        return 1
    mgr = Manager(store, game, tools_dir=_package_root() / "tools")
    try:
        mgr.set_enabled(args.mod_id, enable)
        key = "cli.mod_enabled" if enable else "cli.mod_disabled"
        print(t.tr(key, id=args.mod_id))
        return 0
    except DeployError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


def cmd_list(args: argparse.Namespace, store: Store, t: Translator) -> int:
    prof = store.load_profile()
    enabled = set(prof.enabled_mods)
    mods = store.list_mods()
    if not mods:
        print("No mods installed.")
        return 0
    for m in mods:
        mark = "[x]" if m.id in enabled else "[ ]"
        ver = f" v{m.version}" if m.version else ""
        print(f"{mark} {m.id:30s} [{m.badge:9s}] {m.name}{ver}")
    return 0


def cmd_conflicts(args: argparse.Namespace, store: Store, t: Translator) -> int:
    game = _resolve_game(store, args.game)
    if game is None:
        print(t.tr("cli.game_not_found"), file=sys.stderr)
        return 1
    rule = store.get_pak_order_rule(game.build_id)
    aes_key = store.load_settings().get("aes_key") or None
    rep = scan_folder_conflicts(game.mods_pak_dir, rule=rule, aes_key_hex=aes_key)
    tag = "verified" if rep.verified else "unverified"
    print(f"Pak folder: {game.mods_pak_dir} ({len(rep.per_pak)} paks, order rule: {tag})")
    if rep.config_conflicts:
        print("Config override paks:")
        for pname, cfgs in rep.config_conflicts:
            print(f"  ! {pname}: {', '.join(cfgs)}")
    if not rep.asset_owners:
        print(t.tr("cli.conflicts_none"))
        return 0
    print(f"Overlapping assets: {len(rep.asset_owners)}")
    for pname, info in rep.per_pak.items():
        if info["assets_won"] or info["assets_lost"]:
            print(
                f"  {pname:40s} wins {info['assets_won']:3d} / loses {info['assets_lost']:3d}"
                + (f"  (overridden by: {', '.join(info['loses'])})" if info["loses"] else "")
            )
    return 0


def cmd_reorder(args: argparse.Namespace, store: Store, t: Translator) -> int:
    game = _resolve_game(store, args.game)
    if game is None:
        print(t.tr("cli.game_not_found"), file=sys.stderr)
        return 1
    prof = store.load_profile()
    prof.pak_order = list(args.pak_keys)
    store.save_profile(prof)
    mgr = Manager(store, game, tools_dir=_package_root() / "tools")
    try:
        mgr.apply_profile(prof)
        print(f"Applied custom pak order ({len(prof.pak_order)} entries).")
        return 0
    except DeployError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


def cmd_profile(args: argparse.Namespace, store: Store, t: Translator) -> int:
    if args.action == "list":
        active = store.load_settings().get("active_profile", "default")
        for p in store.list_profiles():
            mark = "*" if p == active else " "
            print(f"{mark} {p}")
        return 0
    if args.action == "create":
        prof = Profile(name=args.name)
        store.save_profile(prof)
        print(f"Created profile: {prof.name}")
        return 0
    if args.action == "use":
        game = _resolve_game(store, args.game)
        if game is None:
            print(t.tr("cli.game_not_found"), file=sys.stderr)
            return 1
        settings = store.load_settings()
        settings["active_profile"] = args.name
        store.save_settings(settings)
        prof = store.load_profile(args.name)
        mgr = Manager(store, game, tools_dir=_package_root() / "tools")
        mgr.apply_profile(prof)
        print(f"Switched to profile: {prof.name}")
        return 0
    return 1


def cmd_adopt(args: argparse.Namespace, store: Store, t: Translator) -> int:
    game = _resolve_game(store, args.game)
    if game is None:
        print(t.tr("cli.game_not_found"), file=sys.stderr)
        return 1
    target = Path(args.path)
    mgr = Manager(store, game, tools_dir=_package_root() / "tools")
    try:
        if target.is_file() and target.suffix.lower() == ".pak":
            rec = mgr.adopt_pak(target, name=args.name)
        elif target.is_dir():
            rec = mgr.adopt_ue4ss_mod(target, name=args.name)
        else:
            print(f"Error: expected a .pak file or UE4SS mod folder: {target}", file=sys.stderr)
            return 2
        print(t.tr("cli.adopted", name=rec.name, id=rec.id))
        return 0
    except DeployError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


def cmd_remove_all(args: argparse.Namespace, store: Store, t: Translator) -> int:
    game = _resolve_game(store, args.game)
    if game is None:
        print(t.tr("cli.game_not_found"), file=sys.stderr)
        return 1
    mgr = Manager(store, game, tools_dir=_package_root() / "tools")
    try:
        count = mgr.remove_all()
        print(t.tr("cli.removed_all", count=count))
        return 0
    except DeployError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


def cmd_base_status(args: argparse.Namespace, store: Store, t: Translator) -> int:
    game = _resolve_game(store, args.game)
    if game is None:
        print(t.tr("cli.game_not_found"), file=sys.stderr)
        return 1
    st = detect_mod_base(game)
    print(f"Game:            {game.root} ({game.launcher}, build {game.build_id})")
    print(f"Engine.ini:      {'found' if st.engine_ini_exists else 'missing'}")
    print(f"UE4SS:           {'installed' if st.ue4ss.installed else 'missing'}" + (f" ({st.ue4ss.version_line})" if st.ue4ss.version_line else ""))
    print(f"Pak enabler:     {st.enabler.kind}" + (f" ({', '.join(st.enabler.files)})" if st.enabler.files else ""))
    print(f"BP loader:       {st.bp_loader.kind}" + (f" (WFF Engine.ini entries: {len(st.bp_loader.wff_ini_entries)})" if st.bp_loader.wff_ini_entries else ""))
    if st.foreign_proxies:
        print(f"Foreign proxies: {', '.join(st.foreign_proxies)} (untouched)")
    return 0


def steam_launch_option_string() -> str:
    """Return the exact string to paste into Steam -> Properties -> Launch Options."""
    exe = Path(sys.executable)
    if getattr(sys, "frozen", False):
        return f'"{exe}" cli launch -- %command%'
    return f'"{exe}" -m wmm cli launch -- %command%'


def create_desktop_shortcut(game: Optional[GameInstall] = None) -> Path:
    """Create a Desktop shortcut that applies WMM's active profile and launches the game directly."""
    desktop = Path.home() / "Desktop"
    desktop.mkdir(parents=True, exist_ok=True)
    pkg_root = _package_root()

    if sys.platform == "win32":
        lnk_path = desktop / "Wuchang (WMM).lnk"
        if getattr(sys, "frozen", False):
            target = str(Path(sys.executable))
            args = "cli play"
        else:
            py = Path(sys.executable)
            pyw = py.with_name("pythonw.exe")
            target = str(pyw if pyw.is_file() else py)
            args = "-m wmm cli play"
        icon = str(game.exe_path) if (game is not None and game.exe_path.is_file()) else target
        ps_script = (
            "$ws = New-Object -ComObject WScript.Shell; "
            f"$sc = $ws.CreateShortcut('{str(lnk_path).replace(chr(39), chr(39)*2)}'); "
            f"$sc.TargetPath = '{target.replace(chr(39), chr(39)*2)}'; "
            f"$sc.Arguments = '{args}'; "
            f"$sc.WorkingDirectory = '{str(pkg_root).replace(chr(39), chr(39)*2)}'; "
            f"$sc.IconLocation = '{icon.replace(chr(39), chr(39)*2)},0'; "
            "$sc.Description = 'Launch Wuchang: Fallen Feathers with WMM'; "
            "$sc.Save()"
        )
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_script],
            check=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return lnk_path

    desktop_file = desktop / "wuchang-wmm.desktop"
    exe = Path(sys.executable)
    exec_cmd = f'"{exe}" cli play' if getattr(sys, "frozen", False) else f'"{exe}" -m wmm cli play'
    desktop_file.write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Wuchang (WMM)\n"
        f"Path={pkg_root}\n"
        f"Exec={exec_cmd}\n"
        "Terminal=false\n",
        encoding="utf-8",
        newline="\n",
    )
    return desktop_file


def cmd_play(args: argparse.Namespace, store: Store, t: Translator) -> int:
    """Apply active WMM profile and launch the game directly via its detected launcher."""
    from .platform import get_platform

    game = _resolve_game(store, args.game)
    if game is None:
        print(t.tr("cli.game_not_found"), file=sys.stderr)
        return 1
    mgr = Manager(store, game, tools_dir=_package_root() / "tools")
    try:
        mgr.apply_profile()
    except DeployError as exc:
        print(f"[WMM] play pre-apply skipped: {exc}", file=sys.stderr)
    plat = get_platform()
    kind, target = game.launch_command()
    if kind == "uri":
        plat.launch_uri(target)
    else:
        plat.launch_exe(Path(target))
    return 0


def cmd_launch(args: argparse.Namespace, store: Store, t: Translator) -> int:
    """Steam Launch Options wrapper: apply active WMM profile, then run the game command."""
    game = _resolve_game(store, args.game)
    if game is not None:
        mgr = Manager(store, game, tools_dir=_package_root() / "tools")
        try:
            mgr.apply_profile()
        except DeployError as exc:
            print(f"[WMM] launch pre-apply skipped: {exc}", file=sys.stderr)

    cmd = list(args.cmd or [])
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        if game is None:
            print(t.tr("cli.game_not_found"), file=sys.stderr)
            return 1
        cmd = [str(game.exe_path)]
    res = subprocess.run(cmd)
    return int(res.returncode)


def cmd_lang_template(args: argparse.Namespace, store: Store, t: Translator) -> int:
    root = _package_root()
    en_path, added = update_en_template(root / "wmm", root / "lang")
    print(f"Updated {en_path} ({len(added)} new keys).")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="wmm", description="Wuchang Mod Manager CLI")
    p.add_argument("--version", action="version", version=f"WMM {__version__}")
    p.add_argument("--data", type=str, default=None, help="Path to portable data/ directory")
    p.add_argument("--game", type=str, default=None, help="Override game root directory")

    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("detect-game", help="Detect Wuchang installs across Steam/Epic/GOG/Game Pass")
    sub.add_parser("base-status", help="Show mod base status (UE4SS, enabler, BP loader, Engine.ini, foreign proxies)")

    sp_insp = sub.add_parser("inspect", help="Inspect an archive or folder and classify mod components")
    sp_insp.add_argument("path", help="Archive (.zip/.7z/.rar) or directory")
    sp_insp.add_argument("--json", action="store_true", help="Output JSON")

    sp_add = sub.add_parser("add", help="Add a mod archive or folder to the store")
    sp_add.add_argument("path", help="Archive or directory")
    sp_add.add_argument("--name", default=None)
    sp_add.add_argument("--version", default="")
    sp_add.add_argument("--enable", action="store_true")
    sp_add.add_argument("--allow-loaders", action="store_true")

    sp_en = sub.add_parser("enable", help="Enable a mod and deploy")
    sp_en.add_argument("mod_id")

    sp_dis = sub.add_parser("disable", help="Disable a mod and undeploy")
    sp_dis.add_argument("mod_id")

    sub.add_parser("list", help="List managed mods")
    sub.add_parser("conflicts", help="Analyze pak overrides and config conflicts in ~mods")

    sp_re = sub.add_parser("reorder", help="Set custom pak order (lowest -> highest)")
    sp_re.add_argument("pak_keys", nargs="+", help="mod_id:original_pak_name entries")

    sp_prof = sub.add_parser("profile", help="Manage profiles")
    sp_prof.add_argument("action", choices=["list", "create", "use"])
    sp_prof.add_argument("name", nargs="?", default="default")

    sp_adopt = sub.add_parser("adopt", help="Adopt an unmanaged pak or UE4SS mod folder")
    sp_adopt.add_argument("path")
    sp_adopt.add_argument("--name", default=None)

    sub.add_parser("remove-all", help="Undo all managed deployments and restore backups")
    sub.add_parser("lang-template", help="Sync lang/en.json from tr() calls in source")

    sub.add_parser("play", help="Apply active profile and launch the game directly")
    sp_launch = sub.add_parser("launch", help="Apply active profile then exec game command (for Steam Launch Options)")
    sp_launch.add_argument("cmd", nargs=argparse.REMAINDER, help="Command passed by Steam (after --)")

    return p


def main(argv: Optional[list[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # No args or 'gui' -> launch PySide6 GUI
    if not argv or argv[0] == "gui":
        from .ui import launch_gui
        return launch_gui()

    # Support optional leading 'cli' subcommand (`WMM.exe cli <verb>`)
    if argv[0] == "cli":
        argv = argv[1:]

    parser = build_parser()
    args = parser.parse_args(argv)

    root = _package_root()
    data_dir = Path(args.data) if args.data else (root / "data")
    store = Store(data_dir)
    lang = store.load_settings().get("language", "en")
    t = Translator(root / "lang", language=lang)

    dispatch = {
        "detect-game": cmd_detect_game,
        "base-status": cmd_base_status,
        "inspect": cmd_inspect,
        "add": cmd_add,
        "enable": lambda a, s, tr: cmd_toggle(a, s, tr, True),
        "disable": lambda a, s, tr: cmd_toggle(a, s, tr, False),
        "list": cmd_list,
        "conflicts": cmd_conflicts,
        "reorder": cmd_reorder,
        "profile": cmd_profile,
        "adopt": cmd_adopt,
        "remove-all": cmd_remove_all,
        "play": cmd_play,
        "launch": cmd_launch,
        "lang-template": cmd_lang_template,
    }
    return dispatch[args.command](args, store, t)
