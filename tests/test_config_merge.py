"""Unit tests for Lua config.lua update merger."""

from __future__ import annotations

import unittest

from wmm.config_merge import merge_lua_config


class TestConfigMerge(unittest.TestCase):
    def test_preserves_user_values_injects_new_prunes_removed_lf(self) -> None:
        user_cfg = (
            "return {\r\n"
            "    DebugLogging = true,\r\n"
            "    DirtMultiplier = 0.75,\r\n"
            "    CleaningMode = \"persistent\",\r\n"
            "    OldDeprecatedKey = 42,\r\n"
            "    Combat = {\r\n"
            "        MaxBlood = 0.6,\r\n"
            "    },\r\n"
            "}\r\n"
        )
        upstream_cfg = (
            "return {\n"
            "    DebugLogging = false,\n"
            "    DirtMultiplier = 0.5,\n"
            "    CleaningMode = \"reactive\",\n"
            "    NewFeature = true,\n"
            "    Combat = {\n"
            "        MaxBlood = 0.8,\n"
            "        NewCombatCap = 1.0,\n"
            "    },\n"
            "}\n"
        )
        merged, summary = merge_lua_config(user_cfg, upstream_cfg)
        self.assertNotIn("\r\n", merged)
        self.assertIn("DebugLogging = true,", merged)
        self.assertIn("DirtMultiplier = 0.75,", merged)
        self.assertIn('CleaningMode = "persistent",', merged)
        self.assertIn("NewFeature = true,", merged)
        self.assertIn("MaxBlood = 0.6,", merged)
        self.assertIn("NewCombatCap = 1.0,", merged)
        self.assertNotIn("OldDeprecatedKey", merged)
        self.assertIn("OldDeprecatedKey", summary["removed"])
        self.assertIn("NewFeature", summary["added"])
        self.assertIn("Combat.MaxBlood", summary["kept"])


if __name__ == "__main__":
    unittest.main()
