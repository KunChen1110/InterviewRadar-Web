"""Validation and short-lived approval previews for manual question imports."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import secrets
import time

MAX_BYTES = 2 * 1024 * 1024
MAX_ROWS = 500
PREVIEW_TTL_SECONDS = 600
ALLOWED_STAGES = {"自我介绍", "项目深挖", "能力匹配", "场景问题"}
_PREVIEWS: dict[str, dict] = {}


class ImportValidationError(ValueError):
    pass


class PreviewAccessError(ValueError):
    pass


def normalize_question(text: object) -> str:
    return re.sub(r"\s+", "", str(text or "")).lower()


def content_hash(question: object) -> str:
    normalized = normalize_question(question)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _role_tags(value: object) -> list[str]:
    if isinstance(value, list):
        raw = value
    else:
        raw = re.split(r"[|,，;；]", str(value or ""))
    return list(dict.fromkeys(item.strip()[:80] for item in raw if item and item.strip()))[:8]


def _parse(filename: str, payload: bytes) -> list[dict]:
    if len(payload) > MAX_BYTES:
        raise ImportValidationError("导入文件不能超过 2 MB")
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ImportValidationError("文件必须使用 UTF-8 编码") from exc
    if filename.lower().endswith(".json"):
        try:
            rows = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ImportValidationError("JSON 格式无效") from exc
        if not isinstance(rows, list):
            raise ImportValidationError("JSON 顶层必须是题目对象数组")
    elif filename.lower().endswith(".csv"):
        rows = list(csv.DictReader(io.StringIO(text)))
    else:
        raise ImportValidationError("仅支持 CSV 或 JSON 题库文件")
    if not rows or len(rows) > MAX_ROWS:
        raise ImportValidationError(f"单次导入题目数必须在 1 到 {MAX_ROWS} 条之间")
    return [row if isinstance(row, dict) else {} for row in rows]


def _normalize_row(raw: dict, index: int) -> tuple[dict | None, str | None]:
    question = str(raw.get("question") or "").strip()[:1000]
    tags = _role_tags(raw.get("role_tags") or raw.get("role") or raw.get("tags"))
    stage = str(raw.get("stage") or "").strip()
    topic = str(raw.get("topic") or "通用能力").strip()[:120]
    source_name = str(raw.get("source_name") or "").strip()[:160]
    source_url = str(raw.get("source_url") or "").strip()[:500]
    if len(question) < 8:
        return None, f"第 {index} 条：题干至少 8 个字符"
    if not tags:
        return None, f"第 {index} 条：缺少 role_tags"
    if stage not in ALLOWED_STAGES:
        return None, f"第 {index} 条：stage 必须是 {'、'.join(sorted(ALLOWED_STAGES))}"
    if not source_name:
        return None, f"第 {index} 条：缺少 source_name"
    if source_url and not re.match(r"^https?://", source_url):
        return None, f"第 {index} 条：source_url 必须是 http(s) 地址"
    return {
        "question": question, "role_tags": tags, "stage": stage, "topic": topic,
        "source_name": source_name, "source_url": source_url,
        "published_at": str(raw.get("published_at") or "").strip()[:10] or None,
        "answer_signals": _role_tags(raw.get("answer_signals")),
        "followup_templates": _role_tags(raw.get("followup_templates")),
        "content_hash": content_hash(question), "source_type": "manual_import",
    }, None


def create_preview(owner_id: str, filename: str, payload: bytes, existing_hashes: set[str]) -> dict:
    rows, errors, duplicate_count, batch_seen = [], [], 0, set()
    for index, raw in enumerate(_parse(filename, payload), start=1):
        row, error = _normalize_row(raw, index)
        if error:
            errors.append(error)
            continue
        if row["content_hash"] in batch_seen or row["content_hash"] in existing_hashes:
            duplicate_count += 1
            continue
        batch_seen.add(row["content_hash"])
        rows.append(row)
    preview_id = secrets.token_urlsafe(18)
    expires_at = time.time() + PREVIEW_TTL_SECONDS
    _PREVIEWS[preview_id] = {"owner_id": owner_id, "expires_at": expires_at, "rows": rows}
    return {"preview_id": preview_id, "valid_count": len(rows), "duplicate_count": duplicate_count,
            "errors": errors, "expires_at": expires_at, "rows": rows}


def get_preview(preview_id: str, owner_id: str, consume: bool = False) -> dict:
    preview = _PREVIEWS.get(preview_id)
    if not preview or preview["owner_id"] != owner_id or time.time() > preview["expires_at"]:
        _PREVIEWS.pop(preview_id, None)
        raise PreviewAccessError("导入预览不存在、已过期或不属于当前管理员")
    if consume:
        _PREVIEWS.pop(preview_id, None)
    return preview
