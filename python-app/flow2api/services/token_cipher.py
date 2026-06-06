from __future__ import annotations

import base64
import os
from pathlib import Path

from flow2api.config import STORAGE_DIR

_SECRET_FILE = STORAGE_DIR / ".token_secret"


def _secret() -> bytes:
    if _SECRET_FILE.exists():
        return _SECRET_FILE.read_bytes()
    key = os.urandom(32)
    _SECRET_FILE.write_bytes(key)
    return key


def encrypt_token(plain: str) -> str:
    key = _secret()
    data = plain.encode("utf-8")
    out = bytes(b ^ key[i % len(key)] for i, b in enumerate(data))
    return base64.urlsafe_b64encode(out).decode("ascii")


def decrypt_token(enc: str) -> str:
    key = _secret()
    data = base64.urlsafe_b64decode(enc.encode("ascii"))
    out = bytes(b ^ key[i % len(key)] for i, b in enumerate(data))
    return out.decode("utf-8")
