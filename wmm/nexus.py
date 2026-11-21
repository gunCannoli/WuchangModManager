"""Nexus Mods API client, nxm:// parser, update check, and config-preserving updates (§6)."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from . import APP_NAME, __version__
from .config_merge import merge_lua_config_files
from .deploy import Manager
from .store import ModRecord

NEXUS_API_V1 = "https://api.nexusmods.com/v1"
DEFAULT_GAME_DOMAIN = "wuchangfallenfeathers"


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

    def download_url(self, url: str, dest_dir: Path, filename: Optional[str] = None) -> Path:
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        if not filename:
            parsed = urllib.parse.urlparse(url)
            filename = Path(urllib.parse.unquote(parsed.path)).name or "download.zip"
        dest = dest_dir / filename
        req = urllib.request.Request(url, headers={"User-Agent": f"{APP_NAME}/{__version__}"})
        open_fn = self._opener.open if self._opener else urllib.request.urlopen
        with open_fn(req, timeout=120) as resp, dest.open("wb") as out:
            shutil.copyfileobj(resp, out)
        return dest


def update_mod_preserving_configs(
    manager: Manager,
    mod_id: str,
    new_source: Path,
    new_version: str = "",
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

    prof = manager.store.load_profile()
    was_enabled = mod_id in prof.enabled_mods
    if was_enabled:
        manager.set_enabled(mod_id, False)

    # Add the new version to a temp mod_id, then swap into old mod_id
    temp_rec = manager.add_mod(new_source, name=old_mod.name, version=new_version or old_mod.version)
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
    temp_rec.nexus_mod_id = old_mod.nexus_mod_id
    manager.store.save_mod(temp_rec)

    if was_enabled:
        manager.set_enabled(mod_id, True)

    return temp_rec, merge_summaries
