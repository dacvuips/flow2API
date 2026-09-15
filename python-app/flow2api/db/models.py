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
    cookies_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    paygate_tier: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # batchexecute session (flow.google.com no longer issues OAuth access_token —
    # generation goes through flow.google.com's own batchexecute RPC instead, which
    # needs a project id + the f.sid/bl/at triple read from the page's own
    # window.WIZ_global_data). These are captured once from a live CDP tab and can
    # be reused across many generate calls without keeping that tab open — see
    # flow_batchexecute_client.py for details on what each field is and why.
    flow_project_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    flow_fsid: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    flow_bl: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    flow_at: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    flow_session_captured_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class FsidSessionHistory(Base):
    """Vòng đời 1 phiên fsid/bl/at của 1 profile — bắt đầu khi capture thành
    công, kết thúc khi phát hiện 401 (session hết hạn). Cuốn chiếu: chỉ giữ
    5 bản ghi gần nhất mỗi profile — xem end_fsid_session/start_fsid_session
    trong flow_profile_service.py."""

    __tablename__ = "flow_fsid_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    profile_id: Mapped[str] = mapped_column(String(128), index=True)
    fsid: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    duration_seconds: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    end_reason: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)


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
        if fp_cols and "cookies_expires_at" not in fp_cols:
            conn.execute(text("ALTER TABLE flow_profiles ADD COLUMN cookies_expires_at DATETIME"))
        if fp_cols and "flow_project_id" not in fp_cols:
            conn.execute(text("ALTER TABLE flow_profiles ADD COLUMN flow_project_id VARCHAR(64)"))
        if fp_cols and "flow_fsid" not in fp_cols:
            conn.execute(text("ALTER TABLE flow_profiles ADD COLUMN flow_fsid VARCHAR(64)"))
        if fp_cols and "flow_bl" not in fp_cols:
            conn.execute(text("ALTER TABLE flow_profiles ADD COLUMN flow_bl VARCHAR(128)"))
        if fp_cols and "flow_at" not in fp_cols:
            conn.execute(text("ALTER TABLE flow_profiles ADD COLUMN flow_at VARCHAR(128)"))
        if fp_cols and "flow_session_captured_at" not in fp_cols:
            conn.execute(text("ALTER TABLE flow_profiles ADD COLUMN flow_session_captured_at DATETIME"))
