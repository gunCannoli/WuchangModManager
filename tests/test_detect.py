"""Unit tests for mod detection and safe archive extraction (§3)."""

from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path

from tests.test_pakindex import build_synthetic_v3_pak, build_synthetic_v11_pak
from wmm.detect import detect_mod
from wmm.extract import ExtractError, extract_archive


class TestDetectAndExtract(unittest.TestCase):
    def test_zip_slip_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            zpath = Path(td) / "evil.zip"
            with zipfile.ZipFile(zpath, "w") as zf:
                zf.writestr("../../escaped.txt", "boom")
            with self.assertRaises(ExtractError):
                extract_archive(zpath, Path(td) / "out")

    def test_detect_combined_pak_and_ue4ss_with_shared(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            mod_lua = root / "Project_Plague" / "Binaries" / "Win64" / "ue4ss" / "Mods" / "MyMod" / "Scripts" / "main.lua"
            mod_lua.parent.mkdir(parents=True)
            mod_lua.write_text("print('hi')\n", encoding="utf-8")

            shared = root / "Project_Plague" / "Binaries" / "Win64" / "ue4ss" / "Mods" / "shared" / "WMF" / "init.lua"
            shared.parent.mkdir(parents=True)
            shared.write_text("return {}\n", encoding="utf-8")

            pak_dir = root / "Project_Plague" / "Content" / "Paks" / "~mods"
            pak_dir.mkdir(parents=True)
            build_synthetic_v11_pak(pak_dir / "zz_MyMod_P.pak", "../../../", {"Project_Plague/Content/": ["A.uasset"]})

            det = detect_mod(root)
            self.assertEqual(det.badge, "PAK+UE4SS")
            kinds = sorted(c.kind for c in det.components)
            self.assertEqual(kinds, ["pak", "ue4ss_mod", "ue4ss_shared"])

    def test_detect_bp_mod_and_logicmods_target(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            lm = root / "Project_Plague" / "Content" / "Paks" / "LogicMods"
            lm.mkdir(parents=True)
            build_synthetic_v3_pak(
                lm / "CoolMod.pak",
                "../../../",
                ["Project_Plague/Content/Mods/CoolMod/ModActor.uasset"],
            )
            det = detect_mod(root)
            self.assertEqual(det.badge, "BP")
            self.assertEqual(det.components[0].target_kind, "LogicMods")
            self.assertEqual(det.components[0].bp_mod_names, ["CoolMod"])

    def test_detect_loader_flagged(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "dwmapi.dll").write_bytes(b"MZ")
            (root / "UE4SS.dll").write_bytes(b"MZ")
            det = detect_mod(root)
            self.assertTrue(det.has_loader)
            self.assertEqual(det.badge, "LOADER")
            self.assertTrue(all(c.requires_confirmation for c in det.components))

    def test_detect_variants_in_subfolders_and_duplicate_ue4ss(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            # Variant A & B folders with paks
            for opt in ("OptionA", "OptionB"):
                d = root / opt
                d.mkdir()
                build_synthetic_v3_pak(d / f"Mod_{opt}_P.pak", "../../../", ["A.uasset"])
            det = detect_mod(root)
            self.assertTrue(bool(det.variants))

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for opt in ("SheatheOnly", "SheathePlus"):
                p = root / opt / "Mods" / "WeaponSwitchUnarmed" / "Scripts" / "main.lua"
                p.parent.mkdir(parents=True)
                p.write_text("-- lua\n", encoding="utf-8")
            det = detect_mod(root)
            self.assertIn("ue4ss:WeaponSwitchUnarmed", det.variants)
            self.assertEqual(len(det.variants["ue4ss:WeaponSwitchUnarmed"]), 2)


if __name__ == "__main__":
    unittest.main()
