"""Small, bounded facts-and-gaps memory used between interview turns."""
from __future__ import annotations

import re

FIELDS = ("verified_claims", "strengths", "gaps", "followup_targets", "covered_terms")
LIMIT = 6


def _clean(values) -> list[str]:
    result = []
    for value in values or []:
        text = re.sub(r"\s+", " ", str(value).strip())[:100]
        if text and text not in result:
            result.append(text)
        if len(result) >= LIMIT:
            break
    return result


def extract_update(answer: str) -> dict:
    """Rule fallback: retain evidence and concrete missing-information signals."""
    answer = answer.strip()
    gaps = []
    targets = []
    for word in ("样本量", "归因", "指标口径", "对照组", "成本", "时间", "数据来源"):
        if word in answer and any(marker in answer for marker in ("没有", "未", "缺", "不清楚")):
            gaps.append(f"{word}未说明")
            targets.append(word)
    if len(answer) < 60:
        gaps.append("回答缺少具体场景、动作或结果")
        targets.append("具体场景与结果")
    claims = re.findall(r"[^。；，]{2,80}(?:\d+(?:\.\d+)?%|提升|降低|负责|实现)[^。；，]{0,60}", answer)
    return {
        "verified_claims": _clean(claims),
        "strengths": _clean(["回答包含量化或职责证据"] if claims else []),
        "gaps": _clean(gaps),
        "followup_targets": _clean(targets),
        "covered_terms": _clean(re.findall(r"[A-Za-z0-9+#./-]{2,}|[\u4e00-\u9fff]{2,}", answer)[:4]),
    }


def merge_memory(current: dict | None, update: dict | None) -> dict:
    current, update = current or {}, update or {}
    return {field: _clean(list(current.get(field) or []) + list(update.get(field) or [])) for field in FIELDS}


def query_terms(memory: dict | None) -> list[str]:
    memory = memory or {}
    return _clean(list(memory.get("gaps") or []) + list(memory.get("followup_targets") or []))
