"""Unit tests for UE4SS mods.txt LF-only writer and detection (§4.3)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from wmm.ue4ss import detect_ue4ss, parse_mods_txt, write_mods_txt


class TestUE4SS(unittest.TestCase):
    def test_write_mods_txt_lf_only_order_and_unmanaged(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            mods_dir = Path(td)
            txt = mods_dir / "mods.txt"
            # Seed with CRLF and an unmanaged mod + base mod
            txt.write_bytes(b"BPModLoaderMod : 1\r\nManualMod : 1\r\nKeybinds : 1\r\n")
            mjson = mods_dir / "mods.json"
            mjson.write_text(json.dumps([{"mod_name": "Surprise", "mod_enabled": False}]), encoding="utf-8")

            ordered = write_mods_txt(txt, [("WMF", True), ("Surprise", True)])
            raw = txt.read_bytes()
            self.assertNotIn(b"\r\n", raw, "mods.txt must be LF only!")
            names = [n for n, _ in ordered]
            self.assertEqual(names[0], "BPModLoaderMod")
            self.assertEqual(names[-1], "Keybinds")
            self.assertIn("ManualMod", names)
            self.assertIn("WMF", names)
            self.assertIn("Surprise", names)

            # Verify mods.json synced
            synced = json.loads(mjson.read_text(encoding="utf-8"))
            self.assertTrue(synced[0]["mod_enabled"])

            # Round-trip parse
            parsed = parse_mods_txt(txt)
            self.assertEqual(parsed, ordered)

    def test_detect_ue4ss_log_version(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            bdir = Path(td)
            (bdir / "dwmapi.dll").write_bytes(b"MZ")
            ue = bdir / "ue4ss"
            ue.mkdir()
            (ue / "UE4SS.dll").write_bytes(b"MZ")
            (ue / "UE4SS.log").write_text(
                "[2026-11-21] Console created\n[2026-11-21] UE4SS - v3.0.1 Experimental - Git SHA #a076aea6\n",
                encoding="utf-8",
            )
            st = detect_ue4ss(bdir)
            self.assertTrue(st.installed)
            self.assertTrue(st.is_fork)
            self.assertIn("v3.0.1", st.version_line)


if __name__ == "__main__":
    unittest.main()
