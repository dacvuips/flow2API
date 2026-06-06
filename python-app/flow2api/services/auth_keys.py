from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta
from typing import Optional

from flow2api.db.models import ApiKey, SessionLocal
from flow2api.services.token_cipher import decrypt_token, encrypt_token


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_api_key(label: str, package_days: Optional[int]) -> tuple[ApiKey, str]:
    raw = f"f2api_{secrets.token_urlsafe(32)}"
    db = SessionLocal()
    try:
        now = datetime.utcnow()
        expires = None if package_days is None else now + timedelta(days=package_days)
        row = ApiKey(
            label=label.strip() or "key",
            token_hash=_hash(raw),
            token_prefix=raw[:16] + "...",
            token_enc=encrypt_token(raw),
            package_days=package_days,
            expires_at=expires,
            status="active",
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return row, raw
    finally:
        db.close()


def list_api_keys() -> list[ApiKey]:
    db = SessionLocal()
    try:
        return db.query(ApiKey).order_by(ApiKey.id.desc()).all()
    finally:
        db.close()


def lookup_api_key(token: str) -> Optional[ApiKey]:
    """Find key by token without mutating last_used (for /api/auth/key display)."""
    if not token or not token.startswith("f2api_"):
        return None
    db = SessionLocal()
    try:
        return db.query(ApiKey).filter(ApiKey.token_hash == _hash(token)).first()
    finally:
        db.close()


def get_api_key_by_token(token: str) -> Optional[int]:
    """Validate token and return api_keys.id (updates last_used_at)."""
    if not token or not token.startswith("f2api_"):
        return None
    row = lookup_api_key(token)
    if not row or row.status != "active":
        return None
    if row.expires_at and row.expires_at < datetime.utcnow():
        return None
    db = SessionLocal()
    try:
        row = db.query(ApiKey).filter(ApiKey.token_hash == _hash(token)).first()
        if not row:
            return None
        if not row.activated_at:
            row.activated_at = datetime.utcnow()
        row.last_used_at = datetime.utcnow()
        key_id = row.id
        db.commit()
        return key_id
    finally:
        db.close()


def revoke_key(key_id: int) -> None:
    db = SessionLocal()
    try:
        row = db.get(ApiKey, key_id)
        if row:
            row.status = "revoked"
            db.commit()
    finally:
        db.close()


def extend_key(key_id: int, days: int) -> None:
    db = SessionLocal()
    try:
        row = db.get(ApiKey, key_id)
        if not row:
            return
        base = row.expires_at or datetime.utcnow()
        if base < datetime.utcnow():
            base = datetime.utcnow()
        row.expires_at = base + timedelta(days=days)
        row.status = "active"
        db.commit()
    finally:
        db.close()


def get_key_token(key_id: int) -> Optional[str]:
    db = SessionLocal()
    try:
        row = db.get(ApiKey, key_id)
        if not row or not row.token_enc:
            return None
        try:
            return decrypt_token(row.token_enc)
        except Exception:
            return None
    finally:
        db.close()


def delete_key(key_id: int) -> None:
    db = SessionLocal()
    try:
        row = db.get(ApiKey, key_id)
        if row:
            db.delete(row)
            db.commit()
    finally:
        db.close()
