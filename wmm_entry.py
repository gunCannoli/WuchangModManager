"""PyInstaller entry point for WMM.exe."""

from __future__ import annotations

import sys

from wmm.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
