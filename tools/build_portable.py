"""Build portable WMM.exe (PyInstaller one-folder) and package portable release zip (§2, §7 Phase 6)."""

from __future__ import annotations

import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = ROOT.parent
sys.path.insert(0, str(ROOT))

from wmm import __version__


def main() -> int:
    py = sys.executable
    dist_dir = ROOT / "dist"
    build_dir = ROOT / "build"
    out_app = dist_dir / "WMM"
    icon_ico = ROOT / "assets" / "wmm.ico"

    if not icon_ico.is_file():
        subprocess.run([py, str(ROOT / "tools" / "make_icon.py")], check=True)

    # Build one-folder PyInstaller bundle (windowed, with our feather icon)
    cmd = [
        py,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onedir",
        "--windowed",
        "--name",
        "WMM",
        "--icon",
        str(icon_ico),
        "--exclude-module",
        "PySide6.QtWebEngineCore",
        "--exclude-module",
        "PySide6.QtWebEngineWidgets",
        "--exclude-module",
        "PySide6.QtPdf",
        "--exclude-module",
        "PySide6.Qt3DCore",
        "--exclude-module",
        "PySide6.QtMultimedia",
        "--exclude-module",
        "PySide6.QtQuick",
        "--exclude-module",
        "PySide6.QtQml",
        str(ROOT / "wmm_entry.py"),
    ]
    subprocess.run(cmd, cwd=str(ROOT), check=True)

    # Copy portable runtime folders next to WMM.exe so all settings/lang/base stay local
    for sub in ("lang", "assets"):
        dst = out_app / sub
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(ROOT / sub, dst)

    # Bundle standalone WMM Pak Enabler dsound.dll inside base/sigfix/
    enabler_src = ROOT / "base" / "sigfix" / "dsound.dll"
    if not enabler_src.is_file():
        enabler_src = ROOT / "native" / "pak_enabler" / "build" / "dsound.dll"
    if enabler_src.is_file():
        sigfix_dst = out_app / "base" / "sigfix"
        sigfix_dst.mkdir(parents=True, exist_ok=True)
        shutil.copy2(enabler_src, sigfix_dst / "dsound.dll")

    # Bundle 7z.exe + 7z.dll if a real (non-WindowsApps alias) copy is found
    sz_candidates = [
        Path(r"C:\Program Files\7-Zip\7z.exe"),
        Path(r"C:\Program Files (x86)\7-Zip\7z.exe"),
    ]
    found_7z = shutil.which("7z")
    if found_7z:
        sz_candidates.append(Path(found_7z))
    for cand in sz_candidates:
        if not cand.is_file() or "WindowsApps" in str(cand):
            continue
        try:
            tools_dst = out_app / "tools"
            tools_dst.mkdir(parents=True, exist_ok=True)
            shutil.copy2(cand, tools_dst / "7z.exe")
            sz_dll = cand.with_name("7z.dll")
            if sz_dll.is_file():
                shutil.copy2(sz_dll, tools_dst / "7z.dll")
            break
        except OSError:
            continue

    # Package portable zip into releases/WMM/
    rel_dir = REPO_ROOT / "releases" / "WMM"
    rel_dir.mkdir(parents=True, exist_ok=True)
    zip_path = rel_dir / f"WuchangModManager_v{__version__}_portable.zip"
    if zip_path.exists():
        zip_path.unlink()

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for p in sorted(out_app.rglob("*")):
            if p.is_file():
                arcname = f"WMM/{p.relative_to(out_app).as_posix()}"
                zf.write(p, arcname)

    # Also copy optional WMM_Custom_UE4SS.zip into releases/WMM/ if present
    ue4ss_zip = ROOT / "base" / "WMM_Custom_UE4SS.zip"
    if ue4ss_zip.is_file():
        shutil.copy2(ue4ss_zip, rel_dir / "WMM_Custom_UE4SS.zip")

    print(f"Portable zip created: {zip_path} ({zip_path.stat().st_size / (1024*1024):.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
