from __future__ import annotations

import html
import secrets
from datetime import datetime

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from flow2api.config import ADMIN_SESSION_COOKIE, ADMIN_USER
from flow2api.services.admin_auth import (
    is_admin_configured,
    setup_admin,
    verify_admin_login,
)
from flow2api.services.auth_keys import (
    create_api_key,
    delete_key,
    get_key_token,
    list_api_keys,
    revoke_key,
)

router = APIRouter(tags=["admin"])

_sessions: set[str] = set()

_ADMIN_CSS = """
*{margin:0;padding:0;box-sizing:border-box}
:root{--bg:#F4F4F5;--surface:#FFF;--surface2:#FAFAFA;--border:#E4E4E7;--primary:#006FEE;--primary-soft:rgba(0,111,238,.10);
--ok:#17C964;--ok-soft:rgba(23,201,100,.10);--err:#F31260;--err-soft:rgba(243,18,96,.10);--warn:#F5A524;--accent-soft:rgba(245,165,36,.10);
--t1:#11181C;--t2:#3F3F46;--t3:#52525B;--t4:#71717A;--radius:16px;--radius-s:12px}
body{font-family:'Fira Sans',system-ui,sans-serif;background:var(--bg);color:var(--t1);min-height:100vh;font-size:15px;line-height:1.5}
.hdr{position:sticky;top:0;z-index:50;background:rgba(255,255,255,.9);backdrop-filter:blur(16px);border-bottom:1px solid var(--border);
padding:0 32px;height:64px;display:flex;align-items:center;justify-content:space-between}
.hdr h1{font-size:22px;font-weight:700;letter-spacing:-.4px;margin:0}
.hdr h1 b{color:var(--primary)}
.hdr-right{display:flex;align-items:center;gap:12px;font-size:14px;color:var(--t3)}
.hdr-right a{color:var(--primary);font-weight:600;text-decoration:none}
.wrap{width:min(1280px,calc(100% - 48px));margin:0 auto;padding:28px 0}
.card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:24px;box-shadow:0 4px 12px rgba(0,0,0,.02)}
.card-top{display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;flex-wrap:wrap;gap:12px}
.card-title{font-size:13px;font-weight:600;color:var(--t3);text-transform:uppercase;letter-spacing:1.2px}
.controls{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.lbl{font-size:13px;color:var(--t3);display:flex;align-items:center;gap:6px}
.inp{background:var(--surface2);border:1px solid var(--border);color:var(--t1);padding:8px 12px;border-radius:var(--radius-s);
font-family:'Fira Code',monospace;font-size:14px;outline:0}
.inp:focus{border-color:var(--primary);box-shadow:0 0 0 3px var(--primary-soft)}
.sel{height:38px;border:1px solid var(--border);border-radius:var(--radius-s);background:#fff;color:var(--t1);font:600 13px 'Fira Sans';padding:0 10px}
.btn{display:inline-flex;align-items:center;gap:8px;padding:10px 16px;border-radius:var(--radius-s);border:1px solid var(--border);
background:var(--surface2);color:var(--t2);font:600 14px 'Fira Sans';cursor:pointer;text-decoration:none;transition:.2s}
.btn:hover{border-color:var(--primary);color:var(--t1);background:var(--primary-soft)}
.btn-p{background:var(--primary);border-color:var(--primary);color:#fff}
.btn-p:hover{background:#2563EB;color:#fff;box-shadow:0 4px 18px var(--primary-soft)}
.btn-stop{padding:6px 12px;font-size:13px;font-weight:700;background:var(--err-soft);color:var(--err);border-color:rgba(243,18,96,.28)}
.btn-stop:hover{background:var(--err);color:#fff;border-color:var(--err)}
.btn-sm{padding:6px 12px;font-size:13px}
.tbl-wrap{overflow-x:auto;margin-top:18px}
table{width:100%;border-collapse:collapse}
th{text-align:left;padding:14px 16px;font-size:12px;font-weight:600;color:var(--t4);text-transform:uppercase;letter-spacing:1.2px;border-bottom:1px solid var(--border)}
td{padding:16px;font-size:15px;border-bottom:1px solid #F1F5F9;color:var(--t2);vertical-align:middle}
tr:hover td{background:rgba(59,130,246,.04)}
.mono{font-family:'Fira Code',monospace;font-size:13px}
.ind{display:inline-flex;align-items:center;gap:8px;font-size:14px;font-weight:600;border-radius:999px;padding:4px 10px}
.dot{width:9px;height:9px;border-radius:50%;flex-shrink:0}
.status-done{background:var(--ok-soft);color:var(--ok);border:1px solid rgba(23,201,100,.22)}
.status-failed{background:var(--err-soft);color:var(--err);border:1px solid rgba(243,18,96,.22)}
.status-offline{background:#F4F4F5;color:var(--t4);border:1px solid var(--border)}
.dot-on{background:var(--ok)}.dot-off{background:var(--err)}
.summary-bar{margin-bottom:14px;padding:12px 14px;border:1px solid var(--border);border-radius:12px;background:var(--surface2);font-size:13px;color:var(--t3)}
.key-cell{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.key-mask{max-width:220px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.auth-wrap{width:min(420px,calc(100% - 32px));margin:80px auto;padding:24px}
.auth-card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:28px;box-shadow:0 8px 24px rgba(0,0,0,.06)}
.auth-card h1{font-size:24px;margin-bottom:8px}
.auth-card p{color:var(--t3);margin-bottom:20px;font-size:14px}
.field{margin-bottom:14px}
.field label{display:block;font-size:12px;font-weight:700;color:var(--t4);text-transform:uppercase;letter-spacing:.8px;margin-bottom:6px}
.field input{width:100%;padding:11px 14px;border:1px solid var(--border);border-radius:var(--radius-s);font-size:15px}
.field input:focus{border-color:var(--primary);box-shadow:0 0 0 3px var(--primary-soft);outline:0}
.err{color:var(--err);font-size:13px;margin-bottom:12px}
.modal{position:fixed;inset:0;background:rgba(15,23,42,.48);backdrop-filter:blur(6px);display:none;align-items:center;justify-content:center;z-index:9998;padding:24px}
.modal.on{display:flex}
.modal-box{width:min(560px,96vw);background:#fff;border:1px solid var(--border);border-radius:20px;box-shadow:0 24px 80px rgba(15,23,42,.28)}
.modal-hd{padding:18px 22px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between}
.modal-hd h3{margin:0;font-size:18px}
.modal-body{padding:22px}
.modal-close{border:1px solid var(--border);background:var(--surface2);border-radius:10px;padding:8px 12px;cursor:pointer;font-weight:700}
.token-pre{white-space:pre-wrap;word-break:break-all;background:#0F172A;color:#E2E8F0;border-radius:14px;padding:16px;font-family:'Fira Code',monospace;font-size:13px}
.modal-actions{display:flex;justify-content:flex-end;gap:10px;margin-top:16px}
@media(max-width:640px){.hdr{padding:0 16px}.wrap{width:calc(100% - 24px)}}
"""


def _page(title: str, body: str) -> str:
    return f"""<!doctype html><html lang='vi'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<title>{html.escape(title)}</title>
<link rel='preconnect' href='https://fonts.googleapis.com'><link rel='preconnect' href='https://fonts.gstatic.com' crossorigin>
<link href='https://fonts.googleapis.com/css2?family=Fira+Code:wght@400;500;600&family=Fira+Sans:wght@400;500;600;700&display=swap' rel='stylesheet'>
<style>{_ADMIN_CSS}</style></head><body>{body}</body></html>"""


def _is_admin(request: Request) -> bool:
    token = request.cookies.get(ADMIN_SESSION_COOKIE)
    return bool(token and token in _sessions)


def _require_admin(request: Request, *, api: bool = False) -> None:
    if not is_admin_configured():
        if api:
            raise HTTPException(401, "setup_required")
        raise HTTPException(303, headers={"Location": "/admin/setup"})
    if not _is_admin(request):
        if api:
            raise HTTPException(401, "unauthorized")
        raise HTTPException(303, headers={"Location": "/admin/login"})


def _status_pill(status: str) -> str:
    s = (status or "").lower()
    if s == "active":
        cls, dot = "status-done", "dot-on"
    elif s == "revoked":
        cls, dot = "status-failed", "dot-off"
    else:
        cls, dot = "status-offline", "dot-off"
    return f"<span class='ind {cls}'><span class='dot {dot}'></span>{html.escape(status)}</span>"


def _remaining_label(expires_at: datetime | None) -> str:
    if not expires_at:
        return "Vô hạn"
    sec = max(0, int((expires_at - datetime.utcnow()).total_seconds()))
    days, rem = divmod(sec, 86400)
    return f"{days} ngày {rem // 3600:02d}:{(rem % 3600) // 60:02d}:{rem % 60:02d}"


def _auth_header(logout: bool = True) -> str:
    links = "<a href='/'>Dashboard</a>"
    if logout:
        links = f"<a href='/admin/logout'>Đăng xuất</a> · {links}"
    return f"<header class='hdr'><h1>Flow<b>2API</b> Admin</h1><div class='hdr-right'>{links}</div></header>"


@router.get("/admin/setup", response_class=HTMLResponse)
async def admin_setup_page():
    if is_admin_configured():
        return RedirectResponse("/admin/login")
    err = ""
    body = f"""{_auth_header(logout=False)}
<div class='auth-wrap'><div class='auth-card'>
<h1>Thiết lập Admin</h1>
<p>Lần đầu cài đặt — tạo tài khoản quản trị để bảo vệ trang /admin.</p>
{err}
<form method='post' action='/admin/setup'>
<div class='field'><label>Tên đăng nhập</label><input name='username' value='{html.escape(ADMIN_USER)}' required autocomplete='username'></div>
<div class='field'><label>Mật khẩu</label><input name='password' type='password' required minlength='6' autocomplete='new-password' placeholder='Tối thiểu 6 ký tự'></div>
<div class='field'><label>Nhập lại mật khẩu</label><input name='password2' type='password' required minlength='6' autocomplete='new-password'></div>
<button class='btn btn-p' style='width:100%;margin-top:8px' type='submit'>Lưu &amp; tiếp tục</button>
</form></div></div>"""
    return HTMLResponse(_page("Thiết lập Admin", body))


@router.post("/admin/setup")
async def admin_setup_submit(
    username: str = Form(...),
    password: str = Form(...),
    password2: str = Form(...),
):
    if is_admin_configured():
        return RedirectResponse("/admin/login", status_code=303)
    err = ""
    if password != password2:
        err = "<div class='err'>Mật khẩu nhập lại không khớp.</div>"
    elif len(password) < 6:
        err = "<div class='err'>Mật khẩu tối thiểu 6 ký tự.</div>"
    else:
        try:
            setup_admin(username, password)
            return RedirectResponse("/admin/login", status_code=303)
        except ValueError as e:
            if str(e) == "password_too_short":
                err = "<div class='err'>Mật khẩu tối thiểu 6 ký tự.</div>"
            else:
                err = "<div class='err'>Đã được cấu hình trước đó.</div>"
    body = f"""{_auth_header(logout=False)}
<div class='auth-wrap'><div class='auth-card'>
<h1>Thiết lập Admin</h1>
<p>Lần đầu cài đặt — tạo tài khoản quản trị để bảo vệ trang /admin.</p>
{err}
<form method='post' action='/admin/setup'>
<div class='field'><label>Tên đăng nhập</label><input name='username' value='{html.escape(username)}' required autocomplete='username'></div>
<div class='field'><label>Mật khẩu</label><input name='password' type='password' required minlength='6' autocomplete='new-password'></div>
<div class='field'><label>Nhập lại mật khẩu</label><input name='password2' type='password' required minlength='6' autocomplete='new-password'></div>
<button class='btn btn-p' style='width:100%;margin-top:8px' type='submit'>Lưu &amp; tiếp tục</button>
</form></div></div>"""
    return HTMLResponse(_page("Thiết lập Admin", body))


@router.get("/admin/login", response_class=HTMLResponse)
async def admin_login_page():
    if not is_admin_configured():
        return RedirectResponse("/admin/setup")
    body = f"""{_auth_header(logout=False)}
<div class='auth-wrap'><div class='auth-card'>
<h1>Đăng nhập Admin</h1>
<p>Quản lý API key Flow2API</p>
<form method='post' action='/admin/login'>
<div class='field'><label>Tên đăng nhập</label><input name='username' value='admin' required autocomplete='username'></div>
<div class='field'><label>Mật khẩu</label><input name='password' type='password' required autocomplete='current-password'></div>
<button class='btn btn-p' style='width:100%;margin-top:8px' type='submit'>Đăng nhập</button>
</form></div></div>"""
    return HTMLResponse(_page("Đăng nhập Admin", body))


@router.post("/admin/login")
async def admin_login(username: str = Form(...), password: str = Form(...)):
    if not is_admin_configured():
        return RedirectResponse("/admin/setup", status_code=303)
    if not verify_admin_login(username, password):
        body = f"""{_auth_header(logout=False)}
<div class='auth-wrap'><div class='auth-card'>
<h1>Đăng nhập Admin</h1>
<p>Quản lý API key Flow2API</p>
<div class='err'>Sai tên đăng nhập hoặc mật khẩu.</div>
<form method='post' action='/admin/login'>
<div class='field'><label>Tên đăng nhập</label><input name='username' value='{html.escape(username)}' required autocomplete='username'></div>
<div class='field'><label>Mật khẩu</label><input name='password' type='password' required autocomplete='current-password'></div>
<button class='btn btn-p' style='width:100%;margin-top:8px' type='submit'>Đăng nhập</button>
</form></div></div>"""
        return HTMLResponse(_page("Đăng nhập Admin", body), status_code=401)
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
    if not is_admin_configured():
        return RedirectResponse("/admin/setup")
    if not _is_admin(request):
        return RedirectResponse("/admin/login")
    keys = list_api_keys()
    active = sum(1 for k in keys if k.status == "active")
    rows = []
    for k in keys:
        last_used = k.last_used_at.strftime("%Y-%m-%d %H:%M") if k.last_used_at else "—"
        can_view = "1" if k.token_enc else "0"
        label_js = k.label.replace("\\", "\\\\").replace("'", "\\'")
        rows.append(
            f"<tr>"
            f"<td class='mono'>{k.id}</td>"
            f"<td>{html.escape(k.label)}</td>"
            f"<td><div class='key-cell'><code class='key-mask mono' id='keymask-{k.id}'>{html.escape(k.token_prefix)}</code>"
            f"<button class='btn btn-sm' type='button' data-view-key='{k.id}' data-can-view='{can_view}'>Xem</button>"
            f"<button class='btn btn-sm' type='button' data-copy-key='{k.id}' data-can-view='{can_view}'>Copy</button></div></td>"
            f"<td>{html.escape(str(k.package_days) if k.package_days is not None else '∞')}</td>"
            f"<td>{_status_pill(k.status)}</td>"
            f"<td>{k.created_at:%Y-%m-%d %H:%M}</td>"
            f"<td class='mono'>{html.escape(_remaining_label(k.expires_at))}</td>"
            f"<td>{last_used}</td>"
            f"<td><div style='display:flex;gap:6px;flex-wrap:wrap'>"
            + (
                ""
                if k.status == "revoked"
                else f"<form method='post' action='/api/admin/keys/{k.id}/revoke' style='display:inline'>"
                f"<button class='btn btn-stop btn-sm' type='submit'>Revoke</button></form>"
            )
            + f"<form method='post' action='/api/admin/keys/{k.id}/delete' style='display:inline' "
            f"onsubmit=\"return confirm('Xóa vĩnh viễn API key {label_js}?')\">"
            f"<button class='btn btn-stop btn-sm' type='submit'>Xóa</button></form>"
            f"</div></td></tr>"
        )
    table = "".join(rows) or "<tr><td colspan='9' style='text-align:center;color:var(--t4);padding:40px'>Chưa có API key — tạo key mới ở góc phải.</td></tr>"
    body = f"""{_auth_header()}
<div class='wrap'><div class='card'>
<div class='card-top'>
<span class='card-title'>API Keys</span>
<div class='controls'>
<form method='post' action='/api/admin/keys' style='display:flex;align-items:center;gap:10px;flex-wrap:wrap'>
<label class='lbl'>Tên <input class='inp' name='label' placeholder='Khách A' style='width:140px'></label>
<label class='lbl'>Gói
<select class='sel' name='package_days'>
<option value='30'>30 ngày</option>
<option value='180'>180 ngày</option>
<option value=''>Vô hạn</option>
</select></label>
<button class='btn btn-p' type='submit'>Tạo API Key</button>
</form>
<button class='btn' type='button' onclick='location.reload()'>Refresh</button>
</div></div>
<div class='summary-bar'><strong>Tổng:</strong> {len(keys)} key · <strong>Active:</strong> {active}</div>
<div class='tbl-wrap'><table>
<thead><tr><th>ID</th><th>Tên</th><th>Key</th><th>Gói</th><th>Trạng thái</th><th>Tạo lúc</th><th>Còn lại</th><th>Last used</th><th></th></tr></thead>
<tbody>{table}</tbody>
</table></div></div></div>
<div class='modal' id='keyModal' onclick="if(event.target===this)closeKeyModal()">
<div class='modal-box'><div class='modal-hd'><h3>API Key</h3><button class='modal-close' type='button' onclick='closeKeyModal()'>Đóng</button></div>
<div class='modal-body'><pre class='token-pre' id='keyModalText'>...</pre>
<div class='modal-actions'><button class='btn' type='button' onclick='closeKeyModal()'>Đóng</button>
<button class='btn btn-p' type='button' id='keyModalCopy'>Copy</button></div></div></div></div>
<script>
let modalToken='';
function closeKeyModal(){{document.getElementById('keyModal').classList.remove('on')}}
async function fetchKey(id){{
  const r=await fetch('/api/admin/keys/'+id+'/token',{{credentials:'same-origin'}});
  if(!r.ok) throw new Error(await r.text()||r.status);
  const j=await r.json();
  return j.token||'';
}}
document.querySelectorAll('[data-view-key]').forEach(btn=>{{
  btn.addEventListener('click',async()=>{{
    const id=btn.getAttribute('data-view-key');
    if(btn.getAttribute('data-can-view')!=='1'){{alert('Key cũ không lưu được — chỉ hiện prefix. Tạo key mới để xem lại.');return}}
    try{{
      modalToken=await fetchKey(id);
      document.getElementById('keyModalText').textContent=modalToken;
      document.getElementById('keyModal').classList.add('on');
    }}catch(e){{alert('Không tải được key: '+(e.message||e))}}
  }});
}});
document.querySelectorAll('[data-copy-key]').forEach(btn=>{{
  btn.addEventListener('click',async()=>{{
    const id=btn.getAttribute('data-copy-key');
    if(btn.getAttribute('data-can-view')!=='1'){{alert('Key cũ không lưu được. Tạo key mới.');return}}
    try{{
      const t=await fetchKey(id);
      await navigator.clipboard.writeText(t);
      btn.textContent='Đã copy';
      setTimeout(()=>btn.textContent='Copy',1200);
    }}catch(e){{alert('Copy lỗi: '+(e.message||e))}}
  }});
}});
document.getElementById('keyModalCopy')?.addEventListener('click',async()=>{{
  if(!modalToken) return;
  try{{await navigator.clipboard.writeText(modalToken);document.getElementById('keyModalCopy').textContent='Đã copy';setTimeout(()=>document.getElementById('keyModalCopy').textContent='Copy',1200)}}catch(e){{prompt('Copy:',modalToken)}}
}});
document.addEventListener('keydown',e=>{{if(e.key==='Escape')closeKeyModal()}});
</script>"""
    return HTMLResponse(_page("Quản lý API Key", body))


@router.get("/api/admin/keys/{key_id}/token")
async def admin_reveal_key(request: Request, key_id: int):
    _require_admin(request, api=True)
    token = get_key_token(key_id)
    if not token:
        raise HTTPException(404, "token_not_available")
    return JSONResponse({"token": token})


@router.post("/api/admin/keys")
async def admin_create_key(request: Request, label: str = Form(""), package_days: str = Form("")):
    _require_admin(request, api=False)
    days = int(package_days) if package_days.strip().isdigit() else None
    row, raw = create_api_key(label, days)
    safe = html.escape(raw)
    body = f"""{_auth_header()}
<div class='wrap'><div class='card'>
<h2 style='margin-bottom:8px'>API Key mới</h2>
<p style='color:var(--t3);margin-bottom:16px'>Key đã được lưu — có thể xem lại bất cứ lúc nào trong danh sách.</p>
<pre class='token-pre' id='newKeyText'>{safe}</pre>
<div style='margin-top:16px;display:flex;gap:10px'>
<button class='btn btn-p' type='button' id='newKeyCopy'>Copy</button>
<a class='btn' href='/admin'>Quay lại danh sách</a>
</div></div></div>
<script>document.getElementById('newKeyCopy')?.addEventListener('click',async()=>{{
  const t=document.getElementById('newKeyText')?.textContent||'';
  try{{await navigator.clipboard.writeText(t)}}catch(e){{prompt('Copy:',t)}}
}});</script>"""
    return HTMLResponse(_page("Key mới", body))


@router.post("/api/admin/keys/{key_id}/revoke")
async def admin_revoke(request: Request, key_id: int):
    _require_admin(request, api=False)
    revoke_key(key_id)
    return RedirectResponse("/admin", status_code=303)


@router.post("/api/admin/keys/{key_id}/delete")
async def admin_delete(request: Request, key_id: int):
    _require_admin(request, api=False)
    delete_key(key_id)
    return RedirectResponse("/admin", status_code=303)
