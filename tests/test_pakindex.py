"""Unit tests for UE Pak V3-V11 index reader."""

from __future__ import annotations

import struct
import tempfile
import unittest
from pathlib import Path

from wmm.pakindex import PAK_MAGIC, PakReadError, normalize_pak_path, read_pak_index


def _pack_fstring(s: str) -> bytes:
    raw = s.encode("utf-8") + b"\x00"
    return struct.pack("<i", len(raw)) + raw


def build_synthetic_v3_pak(path: Path, mount_point: str, filenames: list[str]) -> Path:
    """Build a minimal valid UE Pak V3 file for testing."""
    data_payload = b"TESTDATA"
    idx = bytearray()
    idx += _pack_fstring(mount_point)
    idx += struct.pack("<I", len(filenames))
    for fn in filenames:
        idx += _pack_fstring(fn)
        idx += struct.pack("<QQQI", 0, 8, 8, 0)  # offset, size, uncomp, comp_method=0
        idx += b"\x00" * 20  # sha1
        idx += struct.pack("<BI", 0, 0)  # flags, comp_block_size
    index_offset = len(data_payload)
    index_size = len(idx)
    footer = struct.pack("<IIQQ", PAK_MAGIC, 3, index_offset, index_size) + (b"\x00" * 20)
    path.write_bytes(data_payload + bytes(idx) + footer)
    return path


def build_synthetic_v11_pak(path: Path, mount_point: str, dir_files: dict[str, list[str]]) -> Path:
    """Build a minimal valid UE Pak V11 file with FullDirectoryIndex."""
    data_payload = b"TESTDATA"
    # Build FullDirectoryIndex
    fdi = bytearray()
    fdi += struct.pack("<I", len(dir_files))
    for dname, files in dir_files.items():
        fdi += _pack_fstring(dname)
        fdi += struct.pack("<I", len(files))
        for fn in files:
            fdi += _pack_fstring(fn)
            fdi += struct.pack("<i", 0)

    fdi_offset = len(data_payload)
    fdi_size = len(fdi)

    # Primary index
    idx = bytearray()
    idx += _pack_fstring(mount_point)
    total_files = sum(len(v) for v in dir_files.values())
    idx += struct.pack("<iQ", total_files, 0)  # num_entries, path_hash_seed
    idx += struct.pack("<i", 0)  # has_path_hash_index = 0
    idx += struct.pack("<i", 1)  # has_full_directory_index = 1
    idx += struct.pack("<QQ", fdi_offset, fdi_size) + (b"\x00" * 20)
    idx += struct.pack("<ii", 0, 0)  # encoded_entries_size, files_count

    index_offset = fdi_offset + fdi_size
    index_size = len(idx)

    # V11 footer: guid(16) + enc(1) + magic(4) + ver(4) + off(8) + size(8) + hash(20) + 5*32 comp names
    footer = (
        (b"\x00" * 16)
        + b"\x00"  # encrypted_index = False
        + struct.pack("<IIQQ", PAK_MAGIC, 11, index_offset, index_size)
        + (b"\x00" * 20)
        + (b"\x00" * 160)
    )
    path.write_bytes(data_payload + bytes(fdi) + bytes(idx) + footer)
    return path


class TestPakIndex(unittest.TestCase):
    def test_normalize_pak_path(self) -> None:
        self.assertEqual(
            normalize_pak_path("../../../", "Project_Plague/Content/Mods/Foo/ModActor.uasset"),
            "Project_Plague/Content/Mods/Foo/ModActor.uasset",
        )
        self.assertEqual(
            normalize_pak_path("../../../Project_Plague/Config/", "UserEngine.ini"),
            "Project_Plague/Config/UserEngine.ini",
        )

    def test_v3_bp_and_config(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p1 = build_synthetic_v3_pak(
                Path(td) / "MyBP.pak",
                "../../../",
                [
                    "Project_Plague/Content/Mods/SkipIntroMessages/ModActor.uasset",
                    "Project_Plague/Content/Mods/SkipIntroMessages/ModActor.uexp",
                ],
            )
            idx1 = read_pak_index(p1)
            self.assertEqual(idx1.version, 3)
            self.assertEqual(idx1.bp_mod_names(), ["SkipIntroMessages"])
            self.assertEqual(idx1.config_override_files(), [])

            p2 = build_synthetic_v3_pak(
                Path(td) / "Loader_P.pak",
                "../../../Project_Plague/Config/",
                ["UserEngine.ini"],
            )
            idx2 = read_pak_index(p2)
            self.assertEqual(idx2.config_override_files(), ["Project_Plague/Config/UserEngine.ini"])

    def test_v11_full_directory_index(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = build_synthetic_v11_pak(
                Path(td) / "Test_999_P.pak",
                "../../../",
                {
                    "Project_Plague/Content/DataTables/": ["DT_Shop.uasset", "DT_Shop.uexp"],
                    "Project_Plague/Content/Mods/CoolBP/": ["ModActor.uasset"],
                },
            )
            idx = read_pak_index(p)
            self.assertEqual(idx.version, 11)
            self.assertEqual(len(idx.files), 3)
            self.assertIn("Project_Plague/Content/DataTables/DT_Shop.uasset", idx.files)
            self.assertEqual(idx.bp_mod_names(), ["CoolBP"])

    def test_invalid_pak_raises(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            bad = Path(td) / "not_a_pak.pak"
            bad.write_bytes(b"hello world")
            with self.assertRaises(PakReadError):
                read_pak_index(bad)


if __name__ == "__main__":
    unittest.main()
