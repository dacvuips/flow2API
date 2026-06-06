from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException

from flow2api.services.auth_keys import lookup_api_key
from flow2api.services.extension_pool import get_extension_pool

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _bearer(authorization: str | None = Header(default=None)) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "missing_bearer_token")
    return authorization.split(" ", 1)[1].strip()


@router.get("/key")
async def key_info(token: str = Depends(_bearer)):
    from datetime import datetime

    row = lookup_api_key(token)
    if not row:
        raise HTTPException(401, detail="invalid_api_key")
    remaining = None
    if row.expires_at:
        remaining = max(0, int((row.expires_at - datetime.utcnow()).total_seconds()))
    if row.status != "active":
        key_status = "revoked"
    elif row.expires_at and remaining == 0:
        key_status = "expired"
    else:
        key_status = "active"
    return {
        "label": row.label,
        "status": key_status,
        "expires_at": row.expires_at.isoformat() + "Z" if row.expires_at else None,
        "remaining_seconds": remaining,
        "unlimited": row.expires_at is None,
        "valid": key_status == "active",
    }


@router.get("/me")
async def auth_me(_: str = Depends(_bearer)):
    pool = get_extension_pool()
    ready = pool.ready_sessions()
    first = ready[0] if ready else None
    return {
        "extension_connected": pool.any_connected(),
        "profiles_online": pool.online_count(),
        "profiles_ready": pool.ready_count(),
        "flow_key_present": bool(first and first.flow_key),
        "paygate_tier": first.paygate_tier if first else None,
        "user": first.user_info if first else {},
    }


@router.get("/accounts")
async def auth_accounts(_: str = Depends(_bearer)):
    pool = get_extension_pool()
    accounts = []
    for item in pool.list_public():
        if item.get("profile_id") == "_placeholder":
            continue
        accounts.append(
            {
                "profile_id": item.get("profile_id"),
                "profile_label": item.get("profile_label"),
                "display_name": item.get("display_name"),
                "online": item.get("online"),
                "ready": item.get("ready"),
                "flow_key_present": item.get("flow_key_present"),
                "paygate_tier": item.get("paygate_tier"),
                "email": item.get("email"),
                "active_jobs": item.get("active_jobs"),
            }
        )
    ready = [a for a in accounts if a.get("ready")]
    return {
        "accounts": accounts,
        "count": len(accounts),
        "online_count": sum(1 for a in accounts if a.get("online")),
        "ready_count": len(ready),
    }


@router.post("/scan")
async def auth_scan(_: str = Depends(_bearer)):
    pool = get_extension_pool()
    await pool.broadcast({"type": "please_resend_userinfo"})
    return {"ok": True, "profiles": pool.online_count()}


@router.post("/logout")
async def auth_logout(_: str = Depends(_bearer)):
    pool = get_extension_pool()
    pool.clear_all_credentials()
    await pool.broadcast({"type": "logout"})
    return {"ok": True}
