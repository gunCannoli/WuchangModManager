"""Unit tests for Engine.ini safe updates, deduplication, and WFF migration (§4.4)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from wmm.engineini import (
    WFF_BP_SECTION,
    WMM_BP_SECTION,
    apply_ini_tweak_block,
    apply_perf_preset,
    migrate_wff_bploader_entries,
    read_perf_preset_mode,
    read_section_entries,
    update_wmm_bp_section,
)


class TestEngineIni(unittest.TestCase):
    def test_update_in_place_preserves_other_sections_and_dedupes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ini = Path(td) / "Engine.ini"
            bdir = Path(td) / "backup"
            original = (
                "[Core.System]\r\n"
                "Paths=../../../Engine/Content\r\n"
                "\r\n"
                f"[{WMM_BP_SECTION}]\r\n"
                "Mods=OldMod\r\n"
                "\r\n"
                f"[{WMM_BP_SECTION}]\r\n"
                "Mods=DuplicateMod\r\n"
            )
            ini.write_bytes(original.encode("utf-8"))

            update_wmm_bp_section(ini, ["SkipIntroMessages", "CoolBP"], backup_dir=bdir)
            # Backup was created
            self.assertTrue(any(bdir.rglob("Engine.ini")))
            raw = ini.read_bytes()
            # Preserved CRLF
            self.assertIn(b"\r\n", raw)
            text = raw.decode("utf-8")
            self.assertIn("[Core.System]", text)
            self.assertEqual(text.count(f"[{WMM_BP_SECTION}]"), 1)
            self.assertEqual(read_section_entries(ini, WMM_BP_SECTION), ["SkipIntroMessages", "CoolBP"])

    def test_wff_migration_filters_non_bp_entries(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ini = Path(td) / "Engine.ini"
            ini.write_text(
                f"[{WFF_BP_SECTION}]\n"
                "Mods=SkipIntroMessages\n"
                "Mods=ZZZ_UncensorMod_4_P\n"
                "Mods=WFF_FastLadders_P\n"
                ";Mods=CommentedOut\n",
                encoding="utf-8",
            )
            migrated = migrate_wff_bploader_entries(ini, {"SkipIntroMessages", "OtherBP"})
            self.assertEqual(migrated, ["SkipIntroMessages"])

    def test_ini_tweak_block_add_and_remove(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ini = Path(td) / "Engine.ini"
            ini.write_text("[Core.System]\nPaths=Foo\n", encoding="utf-8")
            apply_ini_tweak_block(ini, "tweak1", "[SystemSettings]\nr.BloomQuality=0\n")
            self.assertIn("r.BloomQuality=0", ini.read_text(encoding="utf-8"))
            apply_ini_tweak_block(ini, "tweak1", None)
            txt = ini.read_text(encoding="utf-8")
            self.assertNotIn("r.BloomQuality", txt)
            self.assertIn("[Core.System]", txt)

    def test_perf_preset_vrr_no_vrr_and_remove(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ini = Path(td) / "Engine.ini"
            ini.write_text("[Core.System]\nPaths=Base\n", encoding="utf-8")
            self.assertEqual(read_perf_preset_mode(ini), "none")

            apply_perf_preset(ini, "vrr")
            self.assertEqual(read_perf_preset_mode(ini), "vrr")
            self.assertIn("r.D3D12.UseAllowTearing=1", ini.read_text(encoding="utf-8"))

            apply_perf_preset(ini, "default")
            self.assertEqual(read_perf_preset_mode(ini), "default")
            self.assertNotIn("r.D3D12.UseAllowTearing=1", ini.read_text(encoding="utf-8"))
            self.assertIn("r.ShaderPipelineCache.Enabled=1", ini.read_text(encoding="utf-8"))

            apply_perf_preset(ini, "none")
            self.assertEqual(read_perf_preset_mode(ini), "none")
            self.assertIn("[Core.System]", ini.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
