"""Safe archive extraction (zip via stdlib; 7z/rar via bundled or PATH 7z)."""

from __future__ import annotations

import shutil
import subprocess
import zipfile
from pathlib import Path
from typing import Optional


class ExtractError(Exception):
    """Raised when an archive cannot be extracted safely."""


def find_7z(tools_dir: Optional[Path] = None) -> Optional[Path]:
    """Locate 7z executable: bundled tools/7z.exe first, then PATH."""
    if tools_dir is not None:
        for name in ("7z.exe", "7z", "7zz"):
            cand = tools_dir / name
            if cand.is_file():
                return cand
    for name in ("7z", "7zz", "7za"):
        found = shutil.which(name)
        if found:
            return Path(found)
    return None


def _safe_join(dest: Path, member: str) -> Path:
    dest_resolved = dest.resolve()
    target = (dest_resolved / member).resolve()
    if target != dest_resolved and dest_resolved not in target.parents:
        raise ExtractError(f"Path traversal blocked in archive entry: {member!r}")
    return target


def extract_archive(
    archive_path: Path,
    dest_dir: Path,
    tools_dir: Optional[Path] = None,
) -> Path:
    """Extract archive_path into a fresh dest_dir. Nothing in the archive is executed."""
    archive_path = Path(archive_path)
    dest_dir = Path(dest_dir)
    if not archive_path.is_file():
        raise ExtractError(f"Archive not found: {archive_path}")

    dest_dir.mkdir(parents=True, exist_ok=True)
    ext = archive_path.suffix.lower()

    if ext == ".zip":
        try:
            with zipfile.ZipFile(archive_path, "r") as zf:
                for info in zf.infolist():
                    name = info.filename.replace("\\", "/")
                    if not name or name.endswith("/"):
                        if name:
                            _safe_join(dest_dir, name).mkdir(parents=True, exist_ok=True)
                        continue
                    target = _safe_join(dest_dir, name)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(info, "r") as src, target.open("wb") as dst:
                        shutil.copyfileobj(src, dst)
            return dest_dir
        except zipfile.BadZipFile as exc:
            # Some archives have .zip extension but are actually 7z/rar; fall through to 7z
            seven_z = find_7z(tools_dir)
            if seven_z is None:
                raise ExtractError(f"Invalid zip file: {archive_path.name}") from exc
    elif ext in (".7z", ".rar"):
        seven_z = find_7z(tools_dir)
        if seven_z is None:
            raise ExtractError(f"7-Zip is required to extract {ext} archives (not found in tools/ or PATH)")
    else:
        raise ExtractError(f"Unsupported archive format: {ext}")

    # Extract via 7z
    seven_z = find_7z(tools_dir)
    if seven_z is None:
        raise ExtractError("7-Zip not found")
    res = subprocess.run(
        [str(seven_z), "x", "-y", f"-o{dest_dir}", str(archive_path)],
        capture_output=True,
        text=True,
        timeout=300,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if res.returncode != 0:
        raise ExtractError(f"7z failed ({res.returncode}): {res.stderr.strip() or res.stdout.strip()}")
    return dest_dir
