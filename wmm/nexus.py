"""Nexus Mods API client, nxm:// parser, update check, and config-preserving updates (§6)."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from . import APP_NAME, __version__
from .config_merge import merge_lua_config_files
from .deploy import Manager
from .store import ModRecord

NEXUS_API_V1 = "https://api.nexusmods.com/v1"
NEXUS_WEB_BASE = "https://www.nexusmods.com"
DEFAULT_GAME_DOMAIN = "wuchangfallenfeathers"

# Matches Nexus manual/manager archive naming:
#   "WMF-500-1-0-0-1732078800.zip"  or  "WMF 500 1.0.0 2026-11-20T05-00Z yPd43amsq.zip"
_NEXUS_DASH_ID_RE = re.compile(r"-(?P<id>\d{1,7})-(?:v?\d+(?:[._-]\d+)+|\d{9,})")
_NEXUS_SPACE_ID_RE = re.compile(r"\s(?P<id>\d{1,7})\s+v?\d+\.\d+")


def sanitize_url(url: str) -> str:
    """Percent-encode spaces and other unsafe characters in a CDN URL (existing %XX escapes are kept)."""
    parts = urllib.parse.urlsplit(url.strip())
    safe = "/%:@!$&'()*+,;=-._~"
    return urllib.parse.urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            urllib.parse.quote(parts.path, safe=safe),
            urllib.parse.quote(parts.query, safe=safe + "?"),
            urllib.parse.quote(parts.fragment, safe=safe + "?"),
        )
    )


def version_key(v: str) -> tuple:
    """Natural sort key for version strings (1.2.10 > 1.2.9; leading v ignored)."""
    return tuple((0, int(p)) if p.isdigit() else (1, p.lower()) for p in re.findall(r"\d+|[A-Za-z]+", v or ""))


def latest_main_file(files_json: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Newest MAIN file (falls back to any file) from a files.json response."""
    files = files_json.get("files", [])
    main = [f for f in files if f.get("category_name") == "MAIN" or f.get("category_id") == 1] or files
    if not main:
        return None
    return max(main, key=lambda f: int(f.get("file_id", 0)))


def is_update_available(
    latest_version: str,
    latest_file_id: Optional[int],
    current_version: str,
    current_file_id: Optional[int],
) -> bool:
    """True when Nexus has a newer main file than the installed one."""
    lv, cv = version_key(latest_version), version_key(current_version)
    if current_file_id and latest_file_id:
        return int(latest_file_id) > int(current_file_id) and (lv != cv or not cv)
    return bool(lv) and lv > cv


def nexus_mod_url(mod_id: int, domain: str = DEFAULT_GAME_DOMAIN) -> str:
    return f"{NEXUS_WEB_BASE}/{domain}/mods/{mod_id}"


_NEXUS_MANUAL_RE = re.compile(
    r"^(?P<name>.+?)\s(?P<id>\d{1,7})\s(?P<ver>\S+)\s\d{4}-\d{2}-\d{2}T\d{2}-\d{2}Z(?:\s\S+)?$"
)
_NEXUS_DASH_RE = re.compile(r"^(?P<name>.+?)-(?P<id>\d{1,7})-(?P<ver>\d+(?:-\d+)*)-(?P<ts>\d{9,})$")


def parse_nexus_archive_name(filename: str) -> Optional[tuple[str, int, str]]:
    """Split a Nexus manual-download archive name into (mod name, mod id, version).

    Handles "UncensorMod 411 4 2026-10-11T21-53Z AMLpw5goK" and "Casting Speed x3-386-1-0-1772254188";
    returns None for any other file name.
    """
    stem = Path(filename).stem if Path(filename).suffix.lower() in (".zip", ".7z", ".rar", ".pak") else filename
    m = _NEXUS_MANUAL_RE.match(stem)
    if m:
        ver = m.group("ver")
    else:
        m = _NEXUS_DASH_RE.match(stem)
        if not m:
            return None
        ver = m.group("ver").replace("-", ".")
    name = m.group("name").strip()
    mod_id = int(m.group("id"))
    if not name or mod_id <= 0:
        return None
    return name, mod_id, ver


def parse_nexus_mod_id_from_filename(filename: str) -> Optional[int]:
    """Best-effort extraction of a Nexus mod ID from a downloaded archive filename."""
    stem = Path(filename).stem
    for rx in (_NEXUS_DASH_ID_RE, _NEXUS_SPACE_ID_RE):
        m = rx.search(stem)
        if m:
            val = int(m.group("id"))
            if val > 0 and not (1990 <= val <= 2099):
                return val
    return None


@dataclass
class NxmLink:
    domain: str
    mod_id: int
    file_id: int
    key: Optional[str] = None
    expires: Optional[int] = None
    user_id: Optional[int] = None


class NexusError(Exception):
    """Raised on Nexus API or nxm:// errors."""


def parse_nxm_uri(uri: str) -> NxmLink:
    """Parse nxm://<domain>/mods/<mod_id>/files/<file_id>?key=...&expires=..."""
    parsed = urllib.parse.urlparse(uri.strip())
    if parsed.scheme.lower() != "nxm":
        raise NexusError(f"Not an nxm:// URI: {uri}")
    domain = parsed.netloc.strip()
    m = re.match(r"^/mods/(\d+)/files/(\d+)/?$", parsed.path)
    if not domain or not m:
        raise NexusError(f"Malformed nxm:// URI path: {uri}")
    qs = urllib.parse.parse_qs(parsed.query)
    key = qs.get("key", [None])[0]
    exp_raw = qs.get("expires", [None])[0]
    uid_raw = qs.get("user_id", [None])[0]
    return NxmLink(
        domain=domain,
        mod_id=int(m.group(1)),
        file_id=int(m.group(2)),
        key=key,
        expires=int(exp_raw) if exp_raw and exp_raw.isdigit() else None,
        user_id=int(uid_raw) if uid_raw and uid_raw.isdigit() else None,
    )


def find_nexus_api_key(explicit_key: Optional[str] = None) -> Optional[str]:
    """Reuse the key lookup convention of D:\\modding\\nexus\\nexus_publish.py."""
    if explicit_key and explicit_key.strip():
        return explicit_key.strip()
    env = os.environ.get("NEXUSMODS_API_KEY") or os.environ.get("NEXUS_API_KEY")
    if env and env.strip():
        return env.strip()
    return None


class NexusClient:
    """Thin stdlib Nexus v1 API client with rate-limit tracking (never logs API key)."""

    def __init__(self, api_key: str, opener=None):
        if not api_key:
            raise NexusError("Missing Nexus API key")
        self._api_key = api_key
        self._opener = opener
        self.rate_limits: dict[str, str] = {}

    def _headers(self) -> dict[str, str]:
        return {
            "apikey": self._api_key,
            "Accept": "application/json",
            "Application-Name": APP_NAME,
            "Application-Version": __version__,
            "User-Agent": f"{APP_NAME}/{__version__}",
        }

    def _get_json(self, path: str, query: Optional[dict[str, Any]] = None) -> Any:
        url = f"{NEXUS_API_V1}{path}"
        if query:
            clean = {k: v for k, v in query.items() if v is not None}
            if clean:
                url += "?" + urllib.parse.urlencode(clean)
        req = urllib.request.Request(url, headers=self._headers(), method="GET")
        try:
            open_fn = self._opener.open if self._opener else urllib.request.urlopen
            with open_fn(req, timeout=30) as resp:
                for hdr in ("x-rl-hourly-remaining", "x-rl-daily-remaining", "x-rl-hourly-limit", "x-rl-daily-limit"):
                    val = resp.headers.get(hdr)
                    if val is not None:
                        self.rate_limits[hdr] = val
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise NexusError(f"Nexus HTTP {exc.code} for {path}") from exc
        except urllib.error.URLError as exc:
            raise NexusError(f"Nexus network error: {exc.reason}") from exc

    def get_mod_info(self, mod_id: int, domain: str = DEFAULT_GAME_DOMAIN) -> dict[str, Any]:
        return self._get_json(f"/games/{domain}/mods/{mod_id}.json")

    def get_mod_files(self, mod_id: int, domain: str = DEFAULT_GAME_DOMAIN) -> dict[str, Any]:
        return self._get_json(f"/games/{domain}/mods/{mod_id}/files.json")

    def get_mod_file_info(
        self,
        mod_id: int,
        file_id: int,
        domain: str = DEFAULT_GAME_DOMAIN,
    ) -> dict[str, Any]:
        return self._get_json(f"/games/{domain}/mods/{mod_id}/files/{file_id}.json")

    def endorse_mod(
        self,
        mod_id: int,
        version: str = "",
        domain: str = DEFAULT_GAME_DOMAIN,
    ) -> dict[str, Any]:
        """Endorse a mod on Nexus Mods (POST /v1/games/{domain}/mods/{mod_id}/endorse.json)."""
        path = f"/games/{domain}/mods/{mod_id}/endorse.json"
        url = f"{NEXUS_API_V1}{path}"
        headers = self._headers()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        body = urllib.parse.urlencode({"version": version or "1.0"}).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            open_fn = self._opener.open if self._opener else urllib.request.urlopen
            with open_fn(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                err_body = json.loads(exc.read().decode("utf-8", errors="ignore"))
                detail = err_body.get("message") or ""
            except Exception:
                pass
            raise NexusError(detail or f"Nexus HTTP {exc.code} for {path}") from exc
        except urllib.error.URLError as exc:
            raise NexusError(f"Nexus network error: {exc.reason}") from exc

    def get_download_links(
        self,
        mod_id: int,
        file_id: int,
        domain: str = DEFAULT_GAME_DOMAIN,
        key: Optional[str] = None,
        expires: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        query = {"key": key, "expires": expires} if key and expires else None
        return self._get_json(f"/games/{domain}/mods/{mod_id}/files/{file_id}/download_link.json", query=query)

    def check_update(
        self,
        mod_id: int,
        current_file_id: int,
        domain: str = DEFAULT_GAME_DOMAIN,
    ) -> Optional[dict[str, Any]]:
        """Return newer MAIN file dict if a newer file_id exists on Nexus, else None."""
        data = self.get_mod_files(mod_id, domain=domain)
        files = data.get("files", [])
        main_files = [f for f in files if f.get("category_name") == "MAIN" or f.get("category_id") == 1]
        if not main_files:
            main_files = files
        newer = [f for f in main_files if int(f.get("file_id", 0)) > int(current_file_id)]
        if not newer:
            return None
        newer.sort(key=lambda f: int(f.get("file_id", 0)), reverse=True)
        return newer[0]

    def download_url(
        self,
        url: str,
        dest_dir: Path,
        filename: Optional[str] = None,
        progress_cb: Optional[Callable[[str, int, int, float], None]] = None,
    ) -> Path:
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        req = urllib.request.Request(sanitize_url(url), headers={"User-Agent": f"{APP_NAME}/{__version__}"})
        open_fn = self._opener.open if self._opener else urllib.request.urlopen
        with open_fn(req, timeout=120) as resp:
            if not filename:
                # 1. Try Content-Disposition header (Nexus CDN URLs often have UUID paths)
                cd = resp.headers.get("Content-Disposition") or resp.headers.get("content-disposition") or ""
                m_star = re.search(r"filename\*\s*=\s*(?:UTF-8''|utf-8'')([^;]+)", cd)
                m_plain = re.search(r'filename\s*=\s*"?([^";]+)"?', cd)
                if m_star:
                    filename = Path(urllib.parse.unquote(m_star.group(1).strip())).name
                elif m_plain:
                    filename = Path(m_plain.group(1).strip()).name
            if not filename:
                parsed = urllib.parse.urlparse(url)
                filename = Path(urllib.parse.unquote(parsed.path)).name or "download.zip"
            # Sanitize invalid Windows filename characters
            filename = re.sub(r'[<>:"/\\|?*]+', "_", filename).strip(" .") or "download.zip"
            dest = dest_dir / filename

            clen_raw = resp.headers.get("Content-Length") or resp.headers.get("content-length") or "0"
            total_bytes = int(clen_raw) if str(clen_raw).isdigit() else 0
            downloaded = 0
            t_start = time.monotonic()
            last_emit = 0.0

            with dest.open("wb") as out:
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    out.write(chunk)
                    downloaded += len(chunk)
                    if progress_cb is not None:
                        now = time.monotonic()
                        if now - last_emit >= 0.08 or (total_bytes and downloaded >= total_bytes):
                            elapsed = max(0.001, now - t_start)
                            progress_cb(filename, downloaded, total_bytes, downloaded / elapsed)
                            last_emit = now

        # 2. If still missing archive extension (e.g. UUID filename), sniff magic bytes and rename
        if dest.suffix.lower() not in (".zip", ".7z", ".rar", ".pak"):
            from .extract import sniff_archive_format
            ext = sniff_archive_format(dest)
            if ext:
                renamed = dest.with_name(dest.name + ext)
                if renamed.exists():
                    renamed.unlink()
                dest.rename(renamed)
                dest = renamed
        return dest


def update_mod_preserving_configs(
    manager: Manager,
    mod_id: str,
    new_source: Path,
    new_version: str = "",
    backup_configs: bool = False,
    nexus_mod_id: Optional[int] = None,
) -> tuple[ModRecord, dict[str, dict]]:
    """Update an existing mod from new_source while preserving every user config.lua value (§1.9, §6)."""
    old_mod = manager.store.load_mod(mod_id)
    if old_mod is None:
        raise NexusError(f"Mod not found: {mod_id}")

    old_mod_dir = manager.store.mod_dir(mod_id)
    # Snapshot existing user config.lua files before replacing
    saved_configs: dict[str, str] = {}
    for cfg in old_mod_dir.rglob("config.lua"):
        rel = str(cfg.relative_to(old_mod_dir)).replace("\\", "/")
        saved_configs[rel] = cfg.read_text(encoding="utf-8", errors="replace")

    if backup_configs:
        cfg_files = [
            p
            for p in old_mod_dir.rglob("*")
            if p.is_file()
            and p.name.lower() in ("config.lua", "config.ini", "config.json", "settings.ini")
        ]
        if cfg_files:
            stamp = time.strftime("%Y%m%d_%H%M%S")
            bdir = manager.store.backup_dir / f"config_{mod_id}_{stamp}"
            for cf in cfg_files:
                rel = cf.relative_to(old_mod_dir)
                dst = bdir / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(cf, dst)

    prof = manager.store.load_profile()
    was_enabled = mod_id in prof.enabled_mods
    if was_enabled:
        manager.set_enabled(mod_id, False)

    # Add the new version to a temp mod_id, then swap into old mod_id
    temp_rec = manager.add_mod(
        new_source,
        name=old_mod.name,
        version=new_version or old_mod.version,
        nexus_mod_id=nexus_mod_id or old_mod.nexus_mod_id,
    )
    temp_dir = manager.store.mod_dir(temp_rec.id)

    merge_summaries: dict[str, dict] = {}
    for rel, user_text in saved_configs.items():
        upstream_cfg = temp_dir / rel
        if upstream_cfg.is_file():
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, suffix=".lua") as tf:
                tf.write(user_text)
                tmp_user = Path(tf.name)
            try:
                merge_summaries[rel] = merge_lua_config_files(tmp_user, upstream_cfg, upstream_cfg)
            finally:
                tmp_user.unlink(missing_ok=True)

    # Replace old store dir with merged new dir under original mod_id
    shutil.rmtree(old_mod_dir)
    shutil.move(str(temp_dir), str(old_mod_dir))
    temp_rec.id = mod_id
    temp_rec.nexus_mod_id = nexus_mod_id or old_mod.nexus_mod_id or temp_rec.nexus_mod_id
    manager.store.save_mod(temp_rec)

    if was_enabled:
        manager.set_enabled(mod_id, True)

    return temp_rec, merge_summaries
