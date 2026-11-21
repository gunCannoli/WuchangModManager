"""Unit tests for version-agnostic & cross-platform game detection (§9.4)."""

from __future__ import annotations

import json
import struct
import tempfile
import unittest
from pathlib import Path
from typing import Optional

from wmm.game import (
    detect_epic_installs,
    detect_gamepass_installs,
    detect_steam_installs,
    parse_acf_buildid,
    parse_vdf_paths,
    read_engine_version,
    validate_game_root,
)
from wmm.platform import Platform


class FakePlatform(Platform):
    def __init__(self, os_name: str = "windows", local_appdata: Optional[Path] = None, running: Optional[list[str]] = None):
        self._name = os_name
        self._local_appdata = local_appdata
        self._running = running or []

    @property
    def name(self) -> str:
        return self._name

    def create_dir_link(self, link_path: Path, target_path: Path) -> None:
        raise NotImplementedError

    def is_dir_link(self, path: Path) -> bool:
        return False

    def remove_dir_link(self, link_path: Path) -> None:
        raise NotImplementedError

    def same_volume(self, a: Path, b: Path) -> bool:
        return True

    def hardlink_or_move(self, src: Path, dst: Path) -> str:
        raise NotImplementedError

    def default_local_appdata(self) -> Optional[Path]:
        return self._local_appdata

    def is_process_running(self, exe_names: list[str]) -> list[str]:
        low = {e.lower() for e in exe_names}
        return [r for r in self._running if r.lower() in low]

    def launch_uri(self, uri: str) -> None:
        pass

    def launch_exe(self, exe_path: Path, args: Optional[list[str]] = None) -> None:
        pass

    def protect_secret(self, plaintext: str) -> str:
        return plaintext

    def unprotect_secret(self, token: str) -> str:
        return token

    def register_nxm(self, wmm_exe: Path) -> Optional[str]:
        return None

    def unregister_nxm(self, previous_handler: Optional[str] = None) -> None:
        pass

    def set_dll_override(self, steam_app_id: str, dll_name: str, steam_root: Optional[Path] = None) -> bool:
        return False

    def ui_language(self) -> str:
        return "en"


def _make_fake_exe(exe_path: Path, engine_ver: str = "++UE5+Release-5.1-CL-0", timestamp: int = 0x67001234) -> None:
    exe_path.parent.mkdir(parents=True, exist_ok=True)
    # Minimal valid DOS + PE header + UTF-16LE ProductVersion string
    dos = bytearray(0x80)
    dos[0:2] = b"MZ"
    struct.pack_into("<I", dos, 0x3C, 0x80)
    pe = bytearray(24)
    pe[0:4] = b"PE\x00\x00"
    struct.pack_into("<I", pe, 8, timestamp)
    ver_bytes = engine_ver.encode("utf-16-le") + b"\x00\x00"
    exe_path.write_bytes(bytes(dos) + bytes(pe) + b"\x00" * 64 + ver_bytes)


class TestGameDetection(unittest.TestCase):
    def test_vdf_and_acf_parsers(self) -> None:
        vdf = '"libraryfolders"\n{\n  "0" { "path" "C:\\\\Program Files\\\\Steam" }\n  "1" { "path" "D:\\\\SteamLib" }\n}'
        self.assertEqual(parse_vdf_paths(vdf), [r"C:\Program Files\Steam", r"D:\SteamLib"])
        acf = '"AppState"\n{\n  "appid" "2277560"\n  "buildid" "20781879"\n}'
        self.assertEqual(parse_acf_buildid(acf), "20781879")

    def test_steam_multi_library_windows(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            steam1 = root / "SteamMain"
            steam2 = root / "SteamGames"
            (steam1 / "steamapps").mkdir(parents=True)
            vdf_path = steam1 / "steamapps" / "libraryfolders.vdf"
            vdf_path.write_text(
                f'"libraryfolders"\n{{\n  "0" {{ "path" "{str(steam1).replace(chr(92), chr(92)*2)}" }}\n'
                f'  "1" {{ "path" "{str(steam2).replace(chr(92), chr(92)*2)}" }}\n}}',
                encoding="utf-8",
            )
            game_root = steam2 / "steamapps" / "common" / "Wuchang Fallen Feathers"
            _make_fake_exe(game_root / "Project_Plague" / "Binaries" / "Win64" / "Project_Plague-Win64-Shipping.exe")
            (steam2 / "steamapps" / "appmanifest_2277560.acf").write_text(
                '"AppState" { "buildid" "20781879" "installdir" "Wuchang Fallen Feathers" }',
                encoding="utf-8",
            )
            appdata = root / "LocalAppData"
            plat = FakePlatform("windows", local_appdata=appdata)

            found = detect_steam_installs(steam_roots=[steam1], platform=plat)
            self.assertEqual(len(found), 1)
            inst = found[0]
            self.assertEqual(inst.launcher, "steam")
            self.assertEqual(inst.build_id, "20781879")
            self.assertEqual(inst.engine_version, "++UE5+Release-5.1-CL-0")
            self.assertEqual(inst.binaries_dir.name, "Win64")
            self.assertEqual(inst.config_dir, appdata / "Project_Plague" / "Saved" / "Config" / "Windows")
            self.assertEqual(inst.launch_command(), ("uri", "steam://rungameid/2277560"))

    def test_steam_proton_linux(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            steam = root / "Steam"
            game_root = steam / "steamapps" / "common" / "Wuchang Fallen Feathers"
            _make_fake_exe(game_root / "Project_Plague" / "Binaries" / "Win64" / "Project_Plague-Win64-Shipping.exe")
            (steam / "steamapps" / "appmanifest_2277560.acf").write_text('"buildid" "99999"', encoding="utf-8")

            plat = FakePlatform("linux")
            found = detect_steam_installs(steam_roots=[steam], platform=plat)
            self.assertEqual(len(found), 1)
            inst = found[0]
            self.assertTrue(inst.is_proton)
            self.assertEqual(inst.build_id, "99999")
            self.assertIn("compatdata", str(inst.config_dir))
            self.assertTrue(str(inst.config_dir).endswith(str(Path("Project_Plague/Saved/Config/Windows"))))

    def test_epic_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifests = root / "Manifests"
            manifests.mkdir()
            game_root = root / "EpicWuchang"
            _make_fake_exe(game_root / "Project_Plague" / "Binaries" / "Win64" / "Project_Plague-Win64-Shipping.exe")
            (manifests / "wuchang.item").write_text(
                json.dumps({"InstallLocation": str(game_root), "AppName": "WuchangApp"}),
                encoding="utf-8",
            )
            plat = FakePlatform("windows", local_appdata=root / "AppData")
            found = detect_epic_installs(manifests_dir=manifests, platform=plat)
            self.assertEqual(len(found), 1)
            self.assertEqual(found[0].launcher, "epic")
            self.assertTrue(found[0].build_id.startswith("pe-67001234-"))
            self.assertEqual(found[0].launch_command(), ("uri", "com.epicgames.launcher://apps/WuchangApp?action=launch"))

    def test_gamepass_wingdk(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            xbox = root / "XboxGames"
            game_root = xbox / "Wuchang" / "Content"
            _make_fake_exe(
                game_root / "Project_Plague" / "Binaries" / "WinGDK" / "Project_Plague-WinGDK-Shipping.exe",
                engine_ver="++UE5+Release-5.2-CL-1",
            )
            (game_root / "gamelaunchhelper.exe").write_bytes(b"MZ")
            appdata = root / "LocalAppData"
            plat = FakePlatform("windows", local_appdata=appdata)

            found = detect_gamepass_installs(xbox_roots=[xbox], platform=plat)
            self.assertEqual(len(found), 1)
            inst = found[0]
            self.assertEqual(inst.launcher, "gamepass")
            self.assertEqual(inst.binaries_dir.name, "WinGDK")
            self.assertEqual(inst.engine_version, "++UE5+Release-5.2-CL-1")
            self.assertEqual(inst.config_dir, appdata / "Project_Plague" / "Saved" / "Config" / "WinGDK")
            self.assertEqual(inst.launch_command()[0], "exe")
            self.assertTrue(inst.launch_command()[1].endswith("gamelaunchhelper.exe"))

    def test_read_engine_version(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            exe = Path(td) / "game.exe"
            _make_fake_exe(exe, engine_ver="++UE5+Release-5.1-CL-0")
            self.assertEqual(read_engine_version(exe), "++UE5+Release-5.1-CL-0")

    def test_invalid_dir_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(validate_game_root(Path(td)))


if __name__ == "__main__":
    unittest.main()
