"""Unit tests for pak priority, conflict graph, and custom order renaming (§4.2)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests.test_pakindex import build_synthetic_v11_pak
from wmm.pakindex import read_pak_index
from wmm.paks import (
    PakOrderRule,
    analyze_pak_conflicts,
    assign_custom_order_names,
    parse_pak_priority,
)


class TestPaks(unittest.TestCase):
    def test_priority_chunk_and_p_suffix(self) -> None:
        rule = PakOrderRule(verified=True, higher_chunk_wins=True, name_ascending_later_wins=True)
        k_plain = parse_pak_priority("Foo.pak", "~mods", rule).sort_key
        k_p = parse_pak_priority("Foo_P.pak", "~mods", rule).sort_key
        k_1 = parse_pak_priority("Foo_1_P.pak", "~mods", rule).sort_key
        k_999 = parse_pak_priority("Foo_999_P.pak", "~mods", rule).sort_key
        self.assertLess(k_plain, k_p)
        self.assertLess(k_p, k_1)
        self.assertLess(k_1, k_999)

    def test_custom_order_keeps_names_when_already_ordered(self) -> None:
        spec = [
            {"id": "a", "original_name": "AAA_P.pak", "folder": "~mods", "locked": False},
            {"id": "b", "original_name": "ZZZ_P.pak", "folder": "~mods", "locked": False},
        ]
        out = assign_custom_order_names(spec)
        self.assertEqual(out["a"], "AAA_P.pak")
        self.assertEqual(out["b"], "ZZZ_P.pak")

    def test_custom_order_renames_to_realise_reversed_order(self) -> None:
        spec = [
            {"id": "z", "original_name": "ZZZ_P.pak", "folder": "~mods", "locked": False},
            {"id": "a", "original_name": "AAA_P.pak", "folder": "~mods", "locked": False},
            {"id": "l", "original_name": "LogicMod.pak", "folder": "LogicMods", "locked": True},
        ]
        out = assign_custom_order_names(spec)
        self.assertEqual(out["z"], "ZZZ_9000_P.pak")
        self.assertEqual(out["a"], "AAA_9010_P.pak")
        # LogicMods never renamed
        self.assertEqual(out["l"], "LogicMod.pak")
        # Verify the resulting names sort in the requested order (z < a)
        self.assertLess(
            parse_pak_priority(out["z"]).sort_key,
            parse_pak_priority(out["a"]).sort_key,
        )

    def test_conflict_analysis(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p1 = build_synthetic_v11_pak(
                Path(td) / "Low_P.pak",
                "../../../",
                {"Project_Plague/Content/": ["Shared.uasset", "OnlyLow.uasset"]},
            )
            p2 = build_synthetic_v11_pak(
                Path(td) / "High_999_P.pak",
                "../../../",
                {"Project_Plague/Content/": ["Shared.uasset", "OnlyHigh.uasset"]},
            )
            rep = analyze_pak_conflicts(
                [
                    ("Low_P.pak", "~mods", read_pak_index(p1)),
                    ("High_999_P.pak", "~mods", read_pak_index(p2)),
                ],
                rule=PakOrderRule(verified=False),
            )
            self.assertFalse(rep.verified)
            self.assertEqual(rep.per_pak["High_999_P.pak"]["assets_won"], 1)
            self.assertEqual(rep.per_pak["High_999_P.pak"]["wins"], ["Low_P.pak"])
            self.assertEqual(rep.per_pak["Low_P.pak"]["assets_lost"], 1)
            self.assertEqual(rep.per_pak["Low_P.pak"]["loses"], ["High_999_P.pak"])


if __name__ == "__main__":
    unittest.main()
