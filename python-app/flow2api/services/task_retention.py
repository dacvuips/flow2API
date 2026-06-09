"""Drop finished tasks (and their media) beyond the dashboard retention window."""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from flow2api.config import ACTIVITY_LIST_LIMIT, VIDEOS_DIR
from flow2api.db.models import RequestRecord, SessionLocal

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


def _delete_local_video(media_id: str) -> None:
    path = VIDEOS_DIR / f"{media_id}.mp4"
    if not path.is_file():
        return
    try:
        path.unlink()
        logger.info("purged local video %s", media_id[:8])
    except OSError as exc:
        logger.warning("failed to delete video %s: %s", media_id[:8], exc)


def purge_old_requests(keep: int | None = None) -> int:
    """Delete finished tasks older than the newest `keep` rows (+ their media)."""
    limit = max(1, int(keep or ACTIVITY_LIST_LIMIT))
    db = SessionLocal()
    deleted = 0
    try:
        total = db.query(RequestRecord).count()
        if total <= limit:
            return 0

        keep_rows = (
            db.query(RequestRecord)
            .order_by(RequestRecord.created_at.desc())
            .limit(limit)
            .all()
        )
        keep_ids = {row.id for row in keep_rows}
        remaining_media: set[str] = set()
        for row in keep_rows:
            remaining_media |= collect_request_media_ids(row)

        old_rows = (
            db.query(RequestRecord)
            .order_by(RequestRecord.created_at.desc())
            .offset(limit)
            .all()
        )
        for row in old_rows:
            if row.id in keep_ids:
                continue
            if row.status in _ACTIVE_STATUSES:
                continue

            purge_media = collect_request_media_ids(row) - remaining_media
            for mid in purge_media:
                _delete_local_video(mid)

            db.delete(row)
            deleted += 1

        if deleted:
            db.commit()
            logger.info("purged %s task(s) beyond retention limit %s", deleted, limit)
        return deleted
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
