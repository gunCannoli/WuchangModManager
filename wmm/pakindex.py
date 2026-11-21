"""Pure-Python Unreal Engine Pak V3-V11 index reader.

Reads file listings and mount points from mod paks (unencrypted) and optionally
encrypted base paks when an AES-256 hex key is supplied from config (never hardcoded).
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

PAK_MAGIC = 0x5A6F12E1


@dataclass
class PakIndex:
    path: Path
    version: int
    encrypted_index: bool
    mount_point: str
    files: list[str] = field(default_factory=list)  # normalized game-relative paths

    def bp_mod_names(self) -> list[str]:
        """Return Blueprint ModActor folder names (/Game/Mods/<Name>/ModActor.uasset)."""
        names: list[str] = []
        seen: set[str] = set()
        for f in self.files:
            low = f.lower()
            # Normalized paths look like Project_Plague/Content/Mods/<Name>/ModActor.uasset
            # or Mods/<Name>/ModActor.uasset
            for marker in ("/content/mods/", "content/mods/", "/game/mods/"):
                idx = low.find(marker)
                if idx != -1:
                    rest = f[idx + len(marker):]
                    parts = rest.split("/")
                    if len(parts) == 2 and parts[1].lower() == "modactor.uasset":
                        mod_name = parts[0]
                        if mod_name.lower() not in seen:
                            seen.add(mod_name.lower())
                            names.append(mod_name)
        return names

    def config_override_files(self) -> list[str]:
        """Return any Config/UserEngine.ini or Config/DefaultEngine.ini paths inside the pak."""
        out: list[str] = []
        for f in self.files:
            low = f.lower()
            if low.endswith("config/userengine.ini") or low.endswith("config/defaultengine.ini"):
                out.append(f)
        return out


class PakReadError(Exception):
    """Raised when a pak file cannot be parsed."""


def _aes_ecb_decrypt(data: bytes, key_bytes: bytes) -> bytes:
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError as exc:
        raise PakReadError("Encrypted pak index requires the 'cryptography' package") from exc
    if len(data) % 16 != 0:
        raise PakReadError(f"Encrypted block length {len(data)} is not a multiple of 16")
    cipher = Cipher(algorithms.AES(key_bytes), modes.ECB())
    dec = cipher.decryptor()
    return dec.update(data) + dec.finalize()


def _parse_aes_key(aes_key_hex: Optional[str]) -> Optional[bytes]:
    if not aes_key_hex:
        return None
    s = aes_key_hex.strip()
    if s.lower().startswith("0x"):
        s = s[2:]
    return bytes.fromhex(s)


class _Reader:
    __slots__ = ("buf", "pos")

    def __init__(self, buf: bytes):
        self.buf = buf
        self.pos = 0

    def read_u8(self) -> int:
        v = self.buf[self.pos]
        self.pos += 1
        return v

    def read_i32(self) -> int:
        v = struct.unpack_from("<i", self.buf, self.pos)[0]
        self.pos += 4
        return v

    def read_u32(self) -> int:
        v = struct.unpack_from("<I", self.buf, self.pos)[0]
        self.pos += 4
        return v

    def read_u64(self) -> int:
        v = struct.unpack_from("<Q", self.buf, self.pos)[0]
        self.pos += 8
        return v

    def skip(self, n: int) -> None:
        self.pos += n

    def read_fstring(self) -> str:
        length = self.read_i32()
        if length == 0:
            return ""
        if length > 0:
            raw = self.buf[self.pos : self.pos + length]
            self.pos += length
            return raw.rstrip(b"\x00").decode("utf-8", errors="replace")
        # UTF-16LE
        byte_len = (-length) * 2
        raw = self.buf[self.pos : self.pos + byte_len]
        self.pos += byte_len
        return raw.decode("utf-16-le", errors="replace").rstrip("\x00")


def normalize_pak_path(mount_point: str, rel_path: str) -> str:
    """Combine mount_point and entry path into a canonical forward-slash game-relative path."""
    mp = mount_point.replace("\\", "/")
    rp = rel_path.replace("\\", "/")
    # Strip leading "../../../" segments that UE uses for engine-relative mount points
    while mp.startswith("../"):
        mp = mp[3:]
    mp = mp.strip("/")
    rp = rp.lstrip("/")
    if mp and rp:
        full = f"{mp}/{rp}"
    else:
        full = mp or rp
    # Collapse any duplicate slashes
    while "//" in full:
        full = full.replace("//", "/")
    return full


def read_pak_index(pak_path: Path, aes_key_hex: Optional[str] = None) -> PakIndex:
    """Read the file list from a UE Pak V3-V11 file."""
    pak_path = Path(pak_path)
    key_bytes = _parse_aes_key(aes_key_hex)

    with pak_path.open("rb") as f:
        f.seek(0, 2)
        file_size = f.tell()
        tail_size = min(file_size, 256)
        f.seek(file_size - tail_size)
        tail = f.read(tail_size)

        magic_bytes = struct.pack("<I", PAK_MAGIC)
        magic_pos = tail.rfind(magic_bytes)
        if magic_pos == -1:
            raise PakReadError(f"Not a valid UE pak file (magic not found): {pak_path.name}")

        footer_off = file_size - tail_size + magic_pos
        if magic_pos + 24 > len(tail):
            raise PakReadError(f"Truncated pak footer: {pak_path.name}")

        _magic, version, index_offset, index_size = struct.unpack_from("<IIQQ", tail, magic_pos)
        encrypted_index = False
        if version >= 4 and magic_pos >= 1:
            encrypted_index = tail[magic_pos - 1] != 0

        if index_offset + index_size > file_size:
            raise PakReadError(f"Index out of bounds in {pak_path.name}")

        f.seek(index_offset)
        idx_raw = f.read(index_size)
        if encrypted_index:
            if key_bytes is None:
                return PakIndex(
                    path=pak_path,
                    version=version,
                    encrypted_index=True,
                    mount_point="",
                    files=[],
                )
            idx_raw = _aes_ecb_decrypt(idx_raw, key_bytes)

        r = _Reader(idx_raw)
        mount_point = r.read_fstring()

        files: list[str] = []
        if version >= 10:
            _num_entries = r.read_i32()
            _path_hash_seed = r.read_u64()
            has_path_hash = r.read_i32()
            if has_path_hash != 0:
                r.skip(8 + 8 + 20)
            has_full_dir = r.read_i32()
            if has_full_dir != 0:
                full_dir_off = r.read_u64()
                full_dir_size = r.read_u64()
                r.skip(20)  # sha1

                f.seek(full_dir_off)
                dir_raw = f.read(full_dir_size)
                if encrypted_index:
                    dir_raw = _aes_ecb_decrypt(dir_raw, key_bytes)  # type: ignore[arg-type]
                dr = _Reader(dir_raw)
                dir_count = dr.read_u32()
                for _ in range(dir_count):
                    dir_name = dr.read_fstring()
                    file_count = dr.read_u32()
                    for _ in range(file_count):
                        fname = dr.read_fstring()
                        _enc_off = dr.read_i32()
                        combined = f"{dir_name}/{fname}" if dir_name and dir_name != "/" else fname
                        files.append(normalize_pak_path(mount_point, combined))
        else:
            # V3 - V9 flat entry list
            record_count = r.read_u32()
            for _ in range(record_count):
                fname = r.read_fstring()
                files.append(normalize_pak_path(mount_point, fname))
                # Skip FPakEntry metadata
                r.skip(24)  # offset(8) + size(8) + uncompressed_size(8)
                comp_method = r.read_u32()
                if version == 1:
                    r.skip(8)  # timestamp
                r.skip(20)  # sha1
                if version >= 3:
                    if comp_method != 0:
                        block_count = r.read_u32()
                        r.skip(block_count * 16)
                    r.skip(1)  # flags
                    r.skip(4)  # compression_block_size

        return PakIndex(
            path=pak_path,
            version=version,
            encrypted_index=encrypted_index,
            mount_point=mount_point,
            files=files,
        )
