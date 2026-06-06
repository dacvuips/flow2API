from __future__ import annotations

import hashlib
import secrets
from typing import Optional

from flow2api.db.models import AdminConfig, SessionLocal


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 200_000)
    return f"{salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt, expected = stored.split("$", 1)
    except ValueError:
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 200_000)
    return secrets.compare_digest(digest.hex(), expected)


def is_admin_configured() -> bool:
    db = SessionLocal()
    try:
        return db.query(AdminConfig).filter(AdminConfig.id == 1).first() is not None
    finally:
        db.close()


def get_admin_config() -> Optional[AdminConfig]:
    db = SessionLocal()
    try:
        return db.query(AdminConfig).filter(AdminConfig.id == 1).first()
    finally:
        db.close()


def setup_admin(username: str, password: str) -> None:
    username = username.strip() or "admin"
    if len(password) < 6:
        raise ValueError("password_too_short")
    db = SessionLocal()
    try:
        if db.query(AdminConfig).filter(AdminConfig.id == 1).first():
            raise ValueError("already_configured")
        row = AdminConfig(id=1, username=username, password_hash=hash_password(password))
        db.add(row)
        db.commit()
    finally:
        db.close()


def verify_admin_login(username: str, password: str) -> bool:
    row = get_admin_config()
    if not row:
        return False
    if username.strip() != row.username:
        return False
    return verify_password(password, row.password_hash)
