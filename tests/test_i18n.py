"""Unit tests for i18n translations and en.json completeness (§2.2)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from wmm.i18n import Translator, scan_code_keys


class TestI18n(unittest.TestCase):
    def test_all_code_keys_exist_in_en_json(self) -> None:
        root = Path(__file__).resolve().parent.parent
        en_path = root / "lang" / "en.json"
        en_data = json.loads(en_path.read_text(encoding="utf-8"))
        used_keys = scan_code_keys(root / "wmm")
        missing = sorted(k for k in used_keys if k not in en_data)
        self.assertEqual(missing, [], f"Keys used in code but missing from lang/en.json: {missing}")

    def test_fallback_and_placeholders(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ldir = Path(td)
            (ldir / "en.json").write_text(
                json.dumps({"_language": "English", "a": "Hello {name}", "b": "Only English"}),
                encoding="utf-8",
            )
            (ldir / "pt-BR.json").write_text(
                json.dumps({"_language": "Português (Brasil)", "a": "Olá {name}"}),
                encoding="utf-8",
            )
            t = Translator(ldir, language="pt-BR")
            self.assertEqual(t.tr("a", name="Wuchang"), "Olá Wuchang")
            self.assertEqual(t.tr("b"), "Only English")
            self.assertEqual(len(t.available_languages()), 2)


if __name__ == "__main__":
    unittest.main()
