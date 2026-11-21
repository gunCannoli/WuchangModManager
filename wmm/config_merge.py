"""Config-preserving Lua config.lua merger (§1.9, §6).

Updates a user's config.lua from a new upstream template while:
- Preserving every user-customized scalar value (bool, number, string)
- Injecting new upstream keys and sections
- Pruning keys removed upstream
- Enforcing strict LF line endings
"""

from __future__ import annotations

import re
from pathlib import Path

# Matches simple scalar assignments inside a Lua table:
#   Key = value,   -- optional comment
_ASSIGN_RE = re.compile(
    r"^(?P<indent>[ \t]*)(?P<key>[A-Za-z_][A-Za-z0-9_]*)(?P<eq>[ \t]*=[ \t]*)"
    r"(?P<val>true|false|-?\d+(?:\.\d+)?|\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*')"
    r"(?P<tail>[ \t]*,?[ \t]*(?:--.*)?)$"
)

# Matches nested table header:   SectionName = {
_TABLE_OPEN_RE = re.compile(r"^[ \t]*([A-Za-z_][A-Za-z0-9_]*)[ \t]*=[ \t]*\{[ \t]*(?:--.*)?$")
_TABLE_CLOSE_RE = re.compile(r"^[ \t]*\}[ \t]*,?[ \t]*(?:--.*)?$")


def _extract_scalars(lua_text: str) -> dict[tuple[str, ...], str]:
    """Extract {(section..., key): raw_value_literal} from a Lua config table."""
    values: dict[tuple[str, ...], str] = {}
    stack: list[str] = []
    for line in lua_text.splitlines():
        s = line.strip()
        if not s or s.startswith("--"):
            continue
        m_open = _TABLE_OPEN_RE.match(line)
        if m_open:
            stack.append(m_open.group(1))
            continue
        if _TABLE_CLOSE_RE.match(line):
            if stack:
                stack.pop()
            continue
        m_assign = _ASSIGN_RE.match(line)
        if m_assign:
            path = tuple(stack + [m_assign.group("key")])
            values[path] = m_assign.group("val")
    return values


def merge_lua_config(user_text: str, upstream_text: str) -> tuple[str, dict]:
    """Merge user values into upstream_text.

    Returns (merged_text_lf, summary_dict) where summary_dict has:
      - kept: list of dotted keys preserved from user
      - added: list of dotted keys newly introduced by upstream
      - removed: list of dotted keys present in user but removed upstream
    """
    user_vals = _extract_scalars(user_text)
    upstream_vals = _extract_scalars(upstream_text)

    kept: list[str] = []
    added: list[str] = []
    removed: list[str] = sorted(".".join(k) for k in user_vals if k not in upstream_vals)

    out_lines: list[str] = []
    stack: list[str] = []
    for line in upstream_text.replace("\r\n", "\n").splitlines():
        m_open = _TABLE_OPEN_RE.match(line)
        if m_open:
            stack.append(m_open.group(1))
            out_lines.append(line)
            continue
        if _TABLE_CLOSE_RE.match(line):
            if stack:
                stack.pop()
            out_lines.append(line)
            continue
        m_assign = _ASSIGN_RE.match(line)
        if m_assign:
            key = m_assign.group("key")
            path = tuple(stack + [key])
            dotted = ".".join(path)
            if path in user_vals:
                kept.append(dotted)
                new_line = f"{m_assign.group('indent')}{key}{m_assign.group('eq')}{user_vals[path]}{m_assign.group('tail')}"
                out_lines.append(new_line)
                continue
            else:
                added.append(dotted)
        out_lines.append(line)

    merged = "\n".join(out_lines) + "\n"
    return merged, {"kept": kept, "added": added, "removed": removed}


def merge_lua_config_files(user_path: Path, upstream_path: Path, dest_path: Path) -> dict:
    """Read user_path and upstream_path, write LF-only merged config to dest_path."""
    user_text = user_path.read_text(encoding="utf-8", errors="replace") if user_path.is_file() else ""
    upstream_text = upstream_path.read_text(encoding="utf-8", errors="replace")
    merged, summary = merge_lua_config(user_text, upstream_text)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    dest_path.write_bytes(merged.encode("utf-8"))
    return summary
