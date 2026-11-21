"""Flat JSON translation loader and template sync (§2.2)."""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Optional


class Translator:
    def __init__(self, lang_dir: Path, language: str = "en"):
        self.lang_dir = Path(lang_dir)
        self.fallback: dict[str, str] = self._load_file("en")
        self.language = language
        self.strings: dict[str, str] = self._load_file(language) if language != "en" else dict(self.fallback)

    def _load_file(self, code: str) -> dict[str, str]:
        p = self.lang_dir / f"{code}.json"
        if not p.is_file():
            # Try base language (e.g. 'pt-BR' -> 'pt')
            if "-" in code:
                p = self.lang_dir / f"{code.split('-', 1)[0]}.json"
        if not p.is_file():
            return {}
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            return {k: str(v) for k, v in data.items() if isinstance(k, str)}
        except (OSError, json.JSONDecodeError):
            return {}

    def available_languages(self) -> list[tuple[str, str]]:
        """Return [(code, display_name)] for every lang/*.json present."""
        langs: list[tuple[str, str]] = []
        if not self.lang_dir.is_dir():
            return langs
        for p in sorted(self.lang_dir.glob("*.json")):
            data = self._load_file(p.stem)
            langs.append((p.stem, data.get("_language", p.stem)))
        return langs

    def tr(self, key: str, **kwargs) -> str:
        tmpl = self.strings.get(key) or self.fallback.get(key) or key
        if kwargs:
            try:
                return tmpl.format(**kwargs)
            except KeyError:
                return tmpl
        return tmpl


def scan_code_keys(code_root: Path) -> set[str]:
    """Scan Python source under code_root for all tr('key') calls via AST (ignores comments/docstrings)."""
    keys: set[str] = set()
    for py in Path(code_root).rglob("*.py"):
        try:
            tree = ast.parse(py.read_text(encoding="utf-8", errors="ignore"), filename=str(py))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            is_tr = (isinstance(func, ast.Name) and func.id == "tr") or (
                isinstance(func, ast.Attribute) and func.attr == "tr"
            )
            if is_tr and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                keys.add(node.args[0].value)
    return keys


def update_en_template(code_root: Path, lang_dir: Path) -> tuple[Path, list[str]]:
    """Sync lang/en.json with all keys used in code_root, preserving existing English text."""
    en_path = Path(lang_dir) / "en.json"
    existing: dict[str, str] = {"_language": "English"}
    if en_path.is_file():
        try:
            existing.update(json.loads(en_path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            pass

    used = scan_code_keys(code_root)
    added: list[str] = []
    for k in sorted(used):
        if k not in existing:
            existing[k] = k
            added.append(k)

    ordered = {"_language": existing.get("_language", "English")}
    for k in sorted(k for k in existing if k != "_language"):
        ordered[k] = existing[k]

    en_path.parent.mkdir(parents=True, exist_ok=True)
    en_path.write_text(json.dumps(ordered, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    return en_path, added
