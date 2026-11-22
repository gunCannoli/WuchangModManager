# WMM — Wuchang Mod Manager

A lightweight, portable mod manager built specifically for **Wuchang: Fallen Feathers** (UE 5.1).

Zero installer, zero duplicate mod copies, and zero edits to your game executable.

## Features

- **Portable**: extract anywhere; all settings, profiles, backups, and stored mods live locally in `data\`.
- **Guided setup (Onboarding)**: auto-detects Steam installs (with experimental Epic / Game Pass detection and manual folder pick), validates the Shipping executable, configures your mods folder, optional UE 5.1 `Engine.ini` performance-only presets (`Default` / `VRR`, no visual changes, with automatic backup), and installs the mod base (`Full setup` or `Paks only`).
- **Built-in unsigned pak support**:
  - **Full setup** uses our custom [gunCannoli/RE-UE4SS](https://github.com/gunCannoli/RE-UE4SS) fork (`dwmapi.dll` with `EnableUnsignedPaks = 1` built in — zero extra proxy DLLs) plus WMM's Lua Blueprint Mod Loader.
  - **Paks-only mode** uses WMM's standalone 60 KB `dsound.dll` pak enabler (version-agnostic RSA-2048 thunk resolver, memory-only, never patches the `.exe` on disk).
- **Automatic mod detection**: classifies `.pak`, Blueprint `ModActor` mods, UE4SS Lua/C++ mods, shared libraries, `Engine.ini` tweaks, variants, and flags loader DLLs before installing.
- **One copy per file**: deploys paks via hardlink/move and UE4SS mods via NTFS directory junctions (so `config.lua` and logs survive disable/enable and profile switches).
- **Pak conflict viewer & custom order**: reads UE Pak V3–V11 indexes directly, shows which pak overrides which, flags `UserEngine.ini` conflicts, and reorders paks cleanly.
- **Unmanaged mod adoption & safety**: lists mods already installed in the game folder (`Adopt into WMM`), refuses changes while the game is running, and leaves foreign proxies (OptiScaler, ReShade, fakenvapi) untouched.
- **Steam Launch Options & Desktop shortcut**: launch directly from Steam (`cli launch -- %command%`) or a Desktop shortcut while keeping WMM profiles applied.
- **User translations**: flat JSON files under `lang/` — copy `lang/en.json` to `lang/<code>.json` and translate the values.

## Quick Start

1. Download **`WuchangModManager_v0.6.0_portable.zip`** and extract it anywhere (e.g. `D:\WMM\`).
2. *(Optional, for UE4SS mods)* Download **`WMM_Custom_UE4SS.zip`** and drop it into the `WMM\` folder (or your chosen Mods folder).
3. Run **`WMM.exe`** and complete the setup screen.

## Building from Source

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt pyinstaller
.\.venv\Scripts\python.exe -m unittest discover -s tests
.\.venv\Scripts\python.exe tools/build_portable.py
```

## Credits & References

- **Mod Organizer 2**: inspiration for profile management, conflict inspection, and `nxm://` workflow — https://github.com/ModOrganizer2/modorganizer
- **Wuchang FMM Launcher** (`ArjunLee`): inspiration for link-based deployment and multi-launcher detection — https://github.com/ArjunLee/Wuchang_FMM_Launcher
- **Custom UE4SS Fork (`gunCannoli/RE-UE4SS`)**: Wuchang stability/performance fixes and built-in `dwmapi.dll` unsigned-pak support — https://github.com/gunCannoli/RE-UE4SS
- **RE-UE4SS (Original)**: the Unreal Engine 4/5 scripting system powering Lua & Blueprint mods — https://github.com/UE4SS-RE/RE-UE4SS

## License

MIT
