# Changelog

All notable changes to **Wuchang Mod Manager (WMM)** are documented here.

## v0.6.0-beta (2026-11-22)

- Icon toolbar, mod filter box, sortable column titles; Refresh button.
- Mod order column; drag and drop or Move / Set order for mods and Paks.
- Multi-select in the right panel with bulk actions: enable, disable, delete downloads. Space toggles the selected rows.
- Enable all / Enable selected / Disable selected / Disable all.
- Double-click a row to open its details.
- Nexus update badge on the mod version.
- Paks tab shows the original pak names.
- Clean mod names for manual Nexus downloads; rename in details.
- Fixed: checkbox toggling and multi-select; Nexus download URLs with spaces; row selection losing its colours.
- Faster reorder (native process check).

## v0.5.0-beta (2026-11-22)

- Settings: the *Show UE4SS core mods* and *Handle nxm:// links* checkboxes now line up on the left with the others.

### UI & Profiles
- Added a centered running-game lock modal (`WMM is locked while the game is running`) with a manual **Unlock** button; background `nxm://` downloads continue while locked, the lock auto-closes when the game exits, and enabling mods while unlocked alerts that changes apply after restarting the game.
- Replaced the toolbar `+` button with a **Profiles** manager dialog supporting inline profile creation (28px row height for readable inline editing), cloning, and deletion.
- Organized Settings into consistent grouped sections (`General`, `Nexus Mods`, `Downloads`, `Setup`) with tightly-fitted uniform left-aligned labels, uniform button widths, left-scrolled text inputs, and `Re-run onboarding` (removed unnecessary Desktop shortcut & Steam Launch Options since NTFS junctions/hardlinks stay active without WMM running).
- Rebuilt the right-hand pane into MO2-style bottom tabs (`UE4SS`, `Paks`, `Downloads`) with responsive column sizing, per-mod enable/disable checkboxes that unlink disabled mods from the game folder, locked `Core` row for `Keybinds`, bold-italic `Unmanaged` styling with tooltips, unified `State` column on `Paks` (`● OK`, `● Overridden`, `● Conflict`, `● Unmanaged`), and right-click context menus (`Details...`, `Open folder`, `Open config`, `Visit Nexus page`, `Endorse on Nexus`, `Install archive`, `Delete download`).
- Redesigned **Mod Details** into a grouped modal (`Overview`, `Components & Deployed`, `Overrides & Conflicts`) showing detailed winning overrides, losing overrides, config conflicts, and Nexus Mod ID actions.
- Added Nexus mod ID auto-detection (from `nxm://` and manually downloaded Nexus archive filenames), storing `nexus_mod_id` on installed mods, plus update-on-top confirmation with a `Back up existing config files before updating` checkbox.
- Fixed red/inactive cell highlight artifacts when switching right-panel tabs by switching table selection styling to neutral dark slate and clearing selection on tab change.
- Added live download progress bar and transfer speed in the `Downloads` tab, non-blocking background downloads, and Settings toggles for auto-extract and keeping/deleting downloaded archives.

### Core & Deployment
- Fixed `WMM_BPLoader` not spawning Blueprint mods like `Skip intro messages`: `apply_profile()` now syncs `WMM_BPLoader/wmm_bp_mods.txt`, `WMM_BPLoader/Scripts/main.lua` resolves `wmm_bp_mods.txt` relative to its own script directory (CWD-independent), and no longer skips startup/intro maps.
- Improved responsive table column resizing across both left and right panels: shrinking a column expands the largest other column (or second-largest when shrinking the largest, with ties preferring `State`/`Status`) so no dead space appears.
- Renamed the default downloads folder from `wmm\mods\` to `wmm\downloads\` (avoiding confusion with `wmm\data\mods\`) and updated `slugify_id()` to preserve spaces in mod/profile folder names instead of replacing them with underscores.
- Nexus `nxm://` downloads now fetch the real archive filename via `get_mod_file_info()` instead of saving CDN UUID filenames.
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
