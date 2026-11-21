"""Unit tests for mod base detection/setup (§5) and Nexus integration (§6)."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path

from tests.test_game import _make_fake_exe
from wmm.base import (
    detect_mod_base,
    install_ue4ss_base,
    install_wmm_lua_bploader,
)
from wmm.deploy import Manager
from wmm.game import validate_game_root
from wmm.nexus import (
    NexusClient,
    NexusError,
    parse_nxm_uri,
    update_mod_preserving_configs,
)
from wmm.store import Store


class _FakeResponse(io.BytesIO):
    def __init__(self, payload: bytes, headers: dict[str, str]):
        super().__init__(payload)
        self.headers = headers

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _FakeOpener:
    def __init__(self, responses: dict[str, bytes]):
        self.responses = responses
        self.last_request = None

    def open(self, req, timeout=30):
        self.last_request = req
        for substr, payload in self.responses.items():
            if substr in req.full_url:
                return _FakeResponse(payload, {"x-rl-hourly-remaining": "99", "x-rl-daily-remaining": "2400"})
        raise RuntimeError(f"Unmocked URL: {req.full_url}")


class TestBaseAndNexus(unittest.TestCase):
    def test_mod_base_detect_and_lua_bploader_install(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            game_root = root / "Game"
            bdir = game_root / "Project_Plague" / "Binaries" / "Win64"
            _make_fake_exe(bdir / "Project_Plague-Win64-Shipping.exe")
            (bdir / "OptiScaler.ini").write_text("[OptiScaler]\n", encoding="utf-8")
            (bdir / "dxgi.dll").write_bytes(b"MZ")
            (bdir / "bitfix").mkdir()
            (bdir / "dsound.dll").write_bytes(b"MZ")
            (game_root / "Project_Plague" / "Content" / "Paks" / "~mods").mkdir(parents=True)

            game = validate_game_root(game_root)
            assert game is not None
            store = Store(root / "data")

            # Install synthetic UE4SS payload
            payload = root / "ue4ss_payload"
            (payload / "ue4ss").mkdir(parents=True)
            (payload / "dwmapi.dll").write_bytes(b"MZ")
            (payload / "ue4ss" / "UE4SS.dll").write_bytes(b"MZ")
            install_ue4ss_base(game, store, payload)

            install_wmm_lua_bploader(game, store, bp_mod_names=["SkipIntroMessages"])

            st = detect_mod_base(game)
            self.assertTrue(st.ue4ss.installed)
            self.assertTrue(st.enabler.present)
            self.assertEqual(st.enabler.kind, "bitfix")
            self.assertEqual(st.bp_loader.kind, "wmm_lua")
            self.assertIn("OptiScaler (dxgi.dll)", st.foreign_proxies)
            self.assertNotIn(b"\r\n", game.ue4ss_mods_txt.read_bytes())

    def test_parse_nxm_uri(self) -> None:
        link = parse_nxm_uri("nxm://wuchangfallenfeathers/mods/437/files/1234?key=abc&expires=1799999999&user_id=7")
        self.assertEqual(link.domain, "wuchangfallenfeathers")
        self.assertEqual(link.mod_id, 437)
        self.assertEqual(link.file_id, 1234)
        self.assertEqual(link.key, "abc")
        self.assertEqual(link.expires, 1799999999)

        with self.assertRaises(NexusError):
            parse_nxm_uri("https://nexusmods.com/bad")

    def test_nexus_client_check_update_and_rate_limits(self) -> None:
        opener = _FakeOpener(
            {
                "/files.json": json.dumps(
                    {
                        "files": [
                            {"file_id": 100, "category_name": "MAIN", "version": "1.0"},
                            {"file_id": 150, "category_name": "MAIN", "version": "1.2"},
                            {"file_id": 160, "category_name": "OPTIONAL", "version": "1.2-opt"},
                        ]
                    }
                ).encode("utf-8")
            }
        )
        client = NexusClient("dummy_key", opener=opener)
        newer = client.check_update(mod_id=42, current_file_id=100)
        self.assertIsNotNone(newer)
        assert newer is not None
        self.assertEqual(newer["file_id"], 150)
        self.assertEqual(client.rate_limits["x-rl-hourly-remaining"], "99")
        self.assertIsNone(client.check_update(mod_id=42, current_file_id=150))

    def test_update_mod_preserving_configs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            game_root = root / "Game"
            _make_fake_exe(game_root / "Project_Plague" / "Binaries" / "Win64" / "Project_Plague-Win64-Shipping.exe")
            (game_root / "Project_Plague" / "Binaries" / "Win64" / "ue4ss" / "Mods").mkdir(parents=True)
            game = validate_game_root(game_root)
            assert game is not None
            store = Store(root / "data")
            mgr = Manager(store, game)

            # v1 mod
            v1 = root / "v1" / "MyMod"
            (v1 / "Scripts").mkdir(parents=True)
            (v1 / "Scripts" / "main.lua").write_text("-- v1\n", encoding="utf-8")
            (v1 / "config.lua").write_text("return {\n    Scale = 1.0,\n    OldKey = true,\n}\n", encoding="utf-8")
            rec = mgr.add_mod(v1.parent, name="MyMod", version="1.0")
            mgr.set_enabled(rec.id, True)

            # User customizes config.lua through the live junction
            live_cfg = game.ue4ss_mods_dir / "MyMod" / "config.lua"
            live_cfg.write_text("return {\n    Scale = 2.5,\n    OldKey = false,\n}\n", encoding="utf-8")

            # v2 mod arrives
            v2 = root / "v2" / "MyMod"
            (v2 / "Scripts").mkdir(parents=True)
            (v2 / "Scripts" / "main.lua").write_text("-- v2\n", encoding="utf-8")
            (v2 / "config.lua").write_text("return {\n    Scale = 1.0,\n    NewKey = 7,\n}\n", encoding="utf-8")

            updated_rec, summaries = update_mod_preserving_configs(mgr, rec.id, v2.parent, new_version="2.0")
            self.assertEqual(updated_rec.version, "2.0")
            final_cfg = live_cfg.read_text(encoding="utf-8")
            self.assertIn("Scale = 2.5,", final_cfg)
            self.assertIn("NewKey = 7,", final_cfg)
            self.assertNotIn("OldKey", final_cfg)
            self.assertNotIn(b"\r\n", live_cfg.read_bytes())
            self.assertTrue(bool(summaries))


if __name__ == "__main__":
    unittest.main()
