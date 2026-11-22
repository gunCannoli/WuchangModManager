# Changelog

All notable changes to **Wuchang Mod Manager (WMM)** are documented here.

## Unreleased (post v0.1.0-beta)

### UI & Profiles
- Replaced the toolbar `+` button with a **Profiles** manager dialog supporting inline profile creation, cloning, and deletion.
- Organized Settings into consistent grouped sections (`General`, `Nexus Mods`, `Downloads`, `Launch & Setup`) with aligned left-justified labels, uniform button widths, left-scrolled text inputs, and shorter `Steam launch` / `Re-run onboarding` labels.
- Rebuilt the right-hand pane into MO2-style bottom tabs (`UE4SS`, `Paks`, `Downloads`) with responsive column sizing, per-mod enable/disable checkboxes that unlink disabled mods from the game folder, locked `Core` row for `Keybinds`, bold-italic `Unmanaged` styling with tooltips, and right-click context menus (`Open folder`, `Open config`, `Install archive`, `Delete download`).
- Fixed red/inactive cell highlight artifacts when switching right-panel tabs by switching table selection styling to neutral dark slate and clearing selection on tab change.
- Added live download progress bar and transfer speed in the `Downloads` tab, non-blocking background downloads, and Settings toggles for auto-extract and keeping/deleting downloaded archives.

### Core & Deployment
- Fixed profile switching leaking disabled managed UE4SS mods into `mods.txt` as `Unmanaged`: inactive managed UE4SS mods are now pruned cleanly from `mods.txt` while preserving unmanaged entries and keeping `Keybinds : 1` last.
- Fixed Nexus `nxm://` CDN downloads with extensionless UUID filenames via `Content-Disposition` parsing and archive magic-byte sniffing.
- Replaced VRR / No VRR `Engine.ini` presets with performance-only `Performance (Default)` and `Performance (VRR)` presets (no visual/sharpness changes) and added an `Engine.ini` preset selector in Settings.
- Added `nxm://` protocol handler registration and IPC forwarding to a running WMM instance.

## v0.1.0-beta (2026-11-21)

- Initial public beta release of Wuchang Mod Manager for *Wuchang: Fallen Feathers* (UE 5.1).
- One-copy-per-file deployment using hardlinks/moves for `.pak` mods and directory junctions/symlinks for UE4SS mods and shared libraries.
- Built-in pak index reader (V3–V11), conflict detection, and verified `_P` chunk load-order management.
- Version-agnostic WMM Pak Enabler (`dsound.dll`) and custom UE4SS fork integration (`WMM_Custom_UE4SS.zip`).
- Guided onboarding wizard, portable single-folder build, Steam/Epic/Game Pass detection, and Linux/Steam Deck support.
