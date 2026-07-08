from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Integer, String, Text, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from flow2api.config import DB_PATH


class Base(DeclarativeBase):
    pass


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    label: Mapped[str] = mapped_column(String(255), default="")
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    token_prefix: Mapped[str] = mapped_column(String(32), default="")
    status: Mapped[str] = mapped_column(String(32), default="active")
    package_days: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    activated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    token_enc: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class AdminConfig(Base):
    __tablename__ = "admin_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), default="admin")
    password_hash: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class RequestRecord(Base):
    __tablename__ = "requests"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    type: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    prompt: Mapped[str] = mapped_column(Text, default="")
    model: Mapped[str] = mapped_column(String(64), default="")
    params_json: Mapped[str] = mapped_column(Text, default="{}")
    result_json: Mapped[str] = mapped_column(Text, default="{}")
    logs_json: Mapped[str] = mapped_column(Text, default="[]")
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    api_key_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class AdminSession(Base):
    __tablename__ = "admin_sessions"

    token: Mapped[str] = mapped_column(String(64), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class FlowProfile(Base):
    """Per Chrome profile — persisted Flow access token (ya29), parity Veo3Studio profile.accessToken."""

    __tablename__ = "flow_profiles"

    profile_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    profile_label: Mapped[str] = mapped_column(String(255), default="")
    email: Mapped[str] = mapped_column(String(255), default="")
    access_token_enc: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    token_captured_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    access_token_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    cookies_enc: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    cookies_captured_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    paygate_tier: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
    from sqlalchemy import text

    with engine.begin() as conn:
        conn.execute(text("PRAGMA journal_mode=WAL"))
        conn.execute(text("PRAGMA synchronous=NORMAL"))
        conn.execute(text("PRAGMA busy_timeout=5000"))
        cols = {row[1] for row in conn.execute(text("PRAGMA table_info(requests)")).fetchall()}
        if "logs_json" not in cols:
            conn.execute(text("ALTER TABLE requests ADD COLUMN logs_json TEXT DEFAULT '[]'"))
        key_cols = {row[1] for row in conn.execute(text("PRAGMA table_info(api_keys)")).fetchall()}
        if "token_enc" not in key_cols:
            conn.execute(text("ALTER TABLE api_keys ADD COLUMN token_enc TEXT"))
        fp_cols = {row[1] for row in conn.execute(text("PRAGMA table_info(flow_profiles)")).fetchall()}
        if fp_cols and "cookies_enc" not in fp_cols:
            conn.execute(text("ALTER TABLE flow_profiles ADD COLUMN cookies_enc TEXT"))
        if fp_cols and "cookies_captured_at" not in fp_cols:
            conn.execute(text("ALTER TABLE flow_profiles ADD COLUMN cookies_captured_at DATETIME"))
