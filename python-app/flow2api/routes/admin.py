from __future__ import annotations

import secrets
from datetime import datetime

from fastapi import APIRouter, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from flow2api.config import ADMIN_PASSWORD, ADMIN_SESSION_COOKIE, ADMIN_USER
from flow2api.db.models import SessionLocal
from flow2api.services.auth_keys import (
    create_api_key,
    delete_key,
    extend_key,
    list_api_keys,
    revoke_key,
)

router = APIRouter(tags=["admin"])

_sessions: set[str] = set()


def _is_admin(request: Request) -> bool:
    token = request.cookies.get(ADMIN_SESSION_COOKIE)
    return bool(token and token in _sessions)


def _require_admin(request: Request) -> None:
    if not _is_admin(request):
        raise HTTPException(303, headers={"Location": "/admin/login"})


@router.get("/admin/login", response_class=HTMLResponse)
async def admin_login_page():
    return HTMLResponse(
        """<!doctype html><html><head><meta charset='utf-8'><title>Flow2API Admin</title>
        <style>body{font-family:system-ui;max-width:420px;margin:80px auto;padding:24px}
        input,button{width:100%;padding:10px;margin:8px 0}button{background:#006FEE;color:#fff;border:0;border-radius:8px}</style></head>
        <body><h1>Flow2API Admin</h1>
        <form method='post' action='/admin/login'>
        <input name='username' placeholder='User' value='admin'>
        <input name='password' type='password' placeholder='Password'>
        <button>Đăng nhập</button></form></body></html>"""
    )


@router.post("/admin/login")
async def admin_login(username: str = Form(...), password: str = Form(...)):
    if username != ADMIN_USER or password != ADMIN_PASSWORD:
        raise HTTPException(401, "invalid_credentials")
    token = secrets.token_urlsafe(32)
    _sessions.add(token)
    resp = RedirectResponse("/admin", status_code=303)
    resp.set_cookie(ADMIN_SESSION_COOKIE, token, httponly=True, samesite="lax")
    return resp


@router.get("/admin/logout")
async def admin_logout(request: Request):
    token = request.cookies.get(ADMIN_SESSION_COOKIE)
    if token:
        _sessions.discard(token)
    resp = RedirectResponse("/admin/login", status_code=303)
    resp.delete_cookie(ADMIN_SESSION_COOKIE)
    return resp


@router.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    if not _is_admin(request):
        return RedirectResponse("/admin/login")
    keys = list_api_keys()
    rows = []
    for k in keys:
        remaining = "Vô hạn"
        if k.expires_at:
            sec = max(0, int((k.expires_at - datetime.utcnow()).total_seconds()))
            days, rem = divmod(sec, 86400)
            remaining = f"{days} ngày {rem // 3600:02d}:{(rem % 3600) // 60:02d}:{rem % 60:02d}"
        rows.append(
            f"<tr><td>{k.label}</td><td><code>{k.token_prefix}</code></td>"
            f"<td>{k.status}</td><td>{k.created_at:%Y-%m-%d %H:%M}</td>"
            f"<td>{k.package_days or '∞'}</td><td>{remaining}</td>"
            f"<td>{k.last_used_at or '—'}</td>"
            f"<td><form method='post' action='/api/admin/keys/{k.id}/revoke' style='display:inline'><button>Revoke</button></form></td></tr>"
        )
    table = "".join(rows) or "<tr><td colspan='8'>Chưa có key</td></tr>"
    return HTMLResponse(
        f"""<!doctype html><html><head><meta charset='utf-8'><title>Flow2API Admin</title>
        <style>body{{font-family:system-ui;margin:24px}}table{{border-collapse:collapse;width:100%}}
        th,td{{border:1px solid #ddd;padding:8px}} .bar{{display:flex;gap:12px;margin-bottom:20px}}
        input,select,button{{padding:8px}} button{{background:#006FEE;color:#fff;border:0;border-radius:6px}}</style></head>
        <body><h1>Quản lý API Key</h1><p><a href='/admin/logout'>Đăng xuất</a> · <a href='/'>Dashboard</a></p>
        <form class='bar' method='post' action='/api/admin/keys'>
        <input name='label' placeholder='Tên key, ví dụ khách A'>
        <select name='package_days'><option value='30'>30 ngày</option><option value='180'>180 ngày</option><option value=''>Vô hạn</option></select>
        <button>Tạo API Key</button></form>
        <table><thead><tr><th>Tên</th><th>Key</th><th>Trạng thái</th><th>Tạo lúc</th><th>Gói</th><th>Còn lại</th><th>Last used</th><th></th></tr></thead>
        <tbody>{table}</tbody></table></body></html>"""
    )


@router.post("/api/admin/keys")
async def admin_create_key(request: Request, label: str = Form(""), package_days: str = Form("")):
    if not _is_admin(request):
        raise HTTPException(401)
    days = int(package_days) if package_days.strip().isdigit() else None
    row, raw = create_api_key(label, days)
    return HTMLResponse(
        f"<html><body><h2>Key mới (lưu ngay, chỉ hiện 1 lần)</h2><pre>{raw}</pre><p><a href='/admin'>Quay lại</a></p></body></html>"
    )


@router.post("/api/admin/keys/{key_id}/revoke")
async def admin_revoke(request: Request, key_id: int):
    if not _is_admin(request):
        raise HTTPException(401)
    revoke_key(key_id)
    return RedirectResponse("/admin", status_code=303)
