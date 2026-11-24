"""Encryption at rest for memory text and conversation logs.

The data key is random (Fernet: AES-128-CBC + HMAC-SHA256). On Windows it is
wrapped with DPAPI (CryptProtectData, current-user scope), so the key file is
useless if copied to another account or PC. Elsewhere the key file is kept
with 0600 permissions (weaker; documented).

Encrypted values carry the prefix "enc1:" so old plaintext rows keep working
and can be migrated in place with MemoryStore.encrypt_existing().
What is NOT encrypted: embeddings, knowledge-graph triples (queried by SQL),
metrics and the audit log. See docs/SECURITY.md.
"""
from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path

PREFIX = "enc1:"


class NullCipher:
    enabled = False

    def encrypt(self, text: str) -> str:
        return text

    def decrypt(self, text: str) -> str:
        if text and text.startswith(PREFIX):
            raise RuntimeError("This database holds encrypted memories but encryption is off or the key is missing.")
        return text


class Cipher:
    enabled = True

    def __init__(self, key: bytes):
        from cryptography.fernet import Fernet
        self.f = Fernet(key)

    def encrypt(self, text: str) -> str:
        if text is None or text.startswith(PREFIX):
            return text
        return PREFIX + self.f.encrypt(text.encode("utf-8")).decode("ascii")

    def decrypt(self, text: str) -> str:
        if not text or not text.startswith(PREFIX):
            return text
        return self.f.decrypt(text[len(PREFIX):].encode("ascii")).decode("utf-8")


# ------------------------------------------------------------------ DPAPI (Windows)
class _Blob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi(data: bytes, protect: bool) -> bytes:
    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32  # type: ignore[attr-defined]
    buf = ctypes.create_string_buffer(data, len(data))
    src, out = _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), _Blob()
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    if not fn(ctypes.byref(src), None, None, None, None, 0x1, ctypes.byref(out)):  # 0x1 = UI_FORBIDDEN
        raise OSError("DPAPI call failed; the key belongs to another Windows user or PC.")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


def load_cipher(home: Path, enabled: bool = True):
    if not enabled:
        return NullCipher()
    try:
        from cryptography.fernet import Fernet
    except ImportError:
        from .logging import log
        log.warning("cryptography is not installed; memories are stored unencrypted.")
        return NullCipher()
    path = Path(home) / "aether.key"
    windows = sys.platform == "win32"
    if path.exists():
        raw = path.read_bytes()
        key = _dpapi(raw, protect=False) if windows else raw
    else:
        key = Fernet.generate_key()
        path.write_bytes(_dpapi(key, protect=True) if windows else key)
        if not windows:
            os.chmod(path, 0o600)
    return Cipher(key)
