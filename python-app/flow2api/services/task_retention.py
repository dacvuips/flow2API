"""Tiered task retention: media for newest N rows, metadata-only for M rows."""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from flow2api.config import (
    ACTIVITY_LIST_LIMIT,
    ACTIVITY_META_LIMIT,
    INPUTS_DIR,
    VIDEOS_DIR,
)
from flow2api.db.models import RequestRecord, SessionLocal
from flow2api.services.stored_media import delete_output_dir, purge_expired_outputs

logger = logging.getLogger(__name__)

_MEDIA_ID_IN_PATH_RE = re.compile(r"/media/([0-9a-fA-F-]{36})")
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_ACTIVE_STATUSES = frozenset({"queued", "running"})


def _media_id_from_value(value: Any) -> str | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    m = _MEDIA_ID_IN_PATH_RE.search(text)
    if m:
        return m.group(1)
    if _UUID_RE.match(text):
        return text
    return None


def collect_request_media_ids(row: RequestRecord) -> set[str]:
    ids: set[str] = set()
    try:
        result = json.loads(row.result_json or "{}")
    except json.JSONDecodeError:
        result = {}
    try:
        params = json.loads(row.params_json or "{}")
    except json.JSONDecodeError:
        params = {}

    for mid in result.get("media_ids") or []:
        parsed = _media_id_from_value(mid)
        if parsed:
            ids.add(parsed)

    for key in ("video_urls", "image_urls", "local_files"):
        for item in result.get(key) or []:
            parsed = _media_id_from_value(item)
            if parsed:
                ids.add(parsed)

    for entry in result.get("media_entries") or []:
        if not isinstance(entry, dict):
            continue
        for key in ("media_id", "mediaId", "url", "local_url", "local_path"):
            parsed = _media_id_from_value(entry.get(key))
            if parsed:
                ids.add(parsed)

    for key in ("start_media_id", "end_media_id"):
        parsed = _media_id_from_value(params.get(key))
        if parsed:
            ids.add(parsed)
    for mid in params.get("reference_media_ids") or []:
        parsed = _media_id_from_value(mid)
        if parsed:
            ids.add(parsed)

    return ids


def slim_params_for_retention(params: dict[str, Any]) -> dict[str, Any]:
    """Keep only profile_id for upscale fallback after media purge."""
    pid = str((params or {}).get("profile_id") or "").strip()
    return {"profile_id": pid} if pid else {}


def strip_result_to_metadata(
    result: dict[str, Any],
    *,
    profile_id: str = "",
) -> dict[str, Any]:
    """Keep only upscale fields; drop URLs, traces, logs, and other result blobs."""
    out: dict[str, Any] = {"media_purged": True}

    media_ids = [
        str(m).strip()
        for m in (result.get("media_ids") or [])
        if str(m).strip()
    ]
    if media_ids:
        out["media_ids"] = media_ids

    project_id = str(result.get("project_id") or "").strip()
    if project_id:
        out["project_id"] = project_id

    prof = str(result.get("profile_id") or profile_id or "").strip()
    if prof:
        out["profile_id"] = prof

    return out


def _delete_local_video(media_id: str) -> None:
    path = VIDEOS_DIR / f"{media_id}.mp4"
    if not path.is_file():
        return
    try:
        path.unlink()
        logger.info("purged local video %s", media_id[:8])
    except OSError as exc:
        logger.warning("failed to delete video %s: %s", media_id[:8], exc)


def _delete_input_previews(request_id: str) -> None:
    import shutil

    dir_path = INPUTS_DIR / request_id
    if not dir_path.is_dir():
        return
    try:
        shutil.rmtree(dir_path)
        logger.info("purged input previews %s", request_id[:8])
    except OSError as exc:
        logger.warning("failed to delete input previews %s: %s", request_id[:8], exc)


def _purge_row_files(
    row: RequestRecord,
    purge_media_ids: set[str],
    *,
    delete_inputs: bool = True,
) -> None:
    for mid in purge_media_ids:
        _delete_local_video(mid)
    if delete_inputs:
        _delete_input_previews(row.id)
    delete_output_dir(row.id)


def purge_tiered_requests(
    media_keep: int | None = None,
    meta_keep: int | None = None,
) -> tuple[int, int, set[str]]:
    """
    Tier 1 (newest media_keep): keep DB row + on-disk media.
    Tier 2 (media_keep..meta_keep): keep DB row, strip media files + heavy result fields.
    Tier 3 (beyond meta_keep): delete DB row (+ orphan media).
    Returns (metadata_stripped, rows_deleted, protected_request_ids).
    """
    media_limit = max(1, int(media_keep or ACTIVITY_LIST_LIMIT))
    meta_limit = max(media_limit, int(meta_keep or ACTIVITY_META_LIMIT))

    db = SessionLocal()
    stripped = 0
    deleted = 0
    protected_request_ids: set[str] = set()
    try:
        all_rows = (
            db.query(RequestRecord)
            .order_by(RequestRecord.created_at.desc())
            .all()
        )
        if not all_rows:
            return 0, 0, protected_request_ids

        media_rows = all_rows[:media_limit]
        meta_rows = all_rows[media_limit:meta_limit]
        drop_rows = all_rows[meta_limit:]

        protected_request_ids = {row.id for row in media_rows}
        protected_media: set[str] = set()
        for row in media_rows:
            protected_media |= collect_request_media_ids(row)

        for row in meta_rows:
            if row.status in _ACTIVE_STATUSES:
                continue
            try:
                result = json.loads(row.result_json or "{}")
            except json.JSONDecodeError:
                result = {}
            try:
                params = json.loads(row.params_json or "{}")
            except json.JSONDecodeError:
                params = {}
            prof = str(
                (result.get("profile_id") if isinstance(result, dict) else "")
                or params.get("profile_id")
                or ""
            ).strip()
            if result.get("media_purged"):
                changed = False
                if (row.logs_json or "[]").strip() not in ("", "[]"):
                    row.logs_json = "[]"
                    changed = True
                slim_params = slim_params_for_retention(params)
                if params != slim_params:
                    row.params_json = json.dumps(slim_params, ensure_ascii=False)
                    changed = True
                slim_result = strip_result_to_metadata(
                    result if isinstance(result, dict) else {},
                    profile_id=prof,
                )
                if result != slim_result:
                    row.result_json = json.dumps(slim_result, ensure_ascii=False)
                    changed = True
                if changed:
                    stripped += 1
                continue

            orphan_media = collect_request_media_ids(row) - protected_media
            _purge_row_files(row, orphan_media, delete_inputs=True)

            slim = strip_result_to_metadata(
                result if isinstance(result, dict) else {},
                profile_id=prof,
            )
            if not slim.get("media_ids") and isinstance(result, dict):
                mids = [
                    str(m).strip()
                    for m in (result.get("media_ids") or [])
                    if str(m).strip()
                ]
                if mids:
                    slim["media_ids"] = mids
            row.result_json = json.dumps(slim, ensure_ascii=False)
            row.logs_json = "[]"
            row.params_json = json.dumps(
                slim_params_for_retention(params), ensure_ascii=False
            )
            stripped += 1

        keep_ids = {row.id for row in all_rows[:meta_limit]}
        remaining_media = set(protected_media)
        for row in all_rows[:meta_limit]:
            remaining_media |= collect_request_media_ids(row)

        for row in drop_rows:
            if row.id in keep_ids:
                continue
            if row.status in _ACTIVE_STATUSES:
                continue

            purge_media = collect_request_media_ids(row) - remaining_media
            _purge_row_files(row, purge_media, delete_inputs=True)
            db.delete(row)
            deleted += 1

        if stripped or deleted:
            db.commit()
            if stripped:
                logger.info(
                    "stripped media from %s task(s) (tier 2, meta limit %s)",
                    stripped,
                    meta_limit,
                )
            if deleted:
                logger.info(
                    "purged %s task row(s) beyond meta limit %s",
                    deleted,
                    meta_limit,
                )
        return stripped, deleted, protected_request_ids
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def purge_old_requests(keep: int | None = None) -> int:
    """Back-compat: full delete beyond `keep` rows."""
    _, deleted, _ = purge_tiered_requests(media_keep=keep, meta_keep=keep)
    return deleted


def purge_storage(
    media_keep: int | None = None,
    meta_keep: int | None = None,
) -> tuple[int, int, int]:
    """Purge tier-2 media, tier-3 rows, then expired outputs (skipping protected ids)."""
    stripped, deleted, protected = purge_tiered_requests(media_keep, meta_keep)
    expired = 0
    try:
        expired = purge_expired_outputs(protected_request_ids=protected)
    except Exception as exc:
        logger.warning("purge_expired_outputs failed: %s", exc)
    return stripped, deleted, expired
