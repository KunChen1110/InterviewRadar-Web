"""Convert historical collection artifacts into review-only admin import CSVs."""
from __future__ import annotations

import csv
from pathlib import Path
import re


_ROLE_ALIASES = {
    "人力资源": "人力资源",
    "HR": "人力资源",
    "招聘": "人力资源",
    "HRBP": "人力资源",
}


def _stage(question: str) -> str:
    if any(term in question for term in ("自我介绍", "介绍自己", "个人经历")):
        return "自我介绍"
    if any(term in question for term in ("项目", "实习", "负责过", "做过")):
        return "项目深挖"
    if any(term in question for term in ("如何", "怎么", "如果", "怎样", "怎么办")):
        return "场景问题"
    return "能力匹配"


def _question_text(value: object) -> str:
    """Drop only collector-added post labels, leaving the source question intact."""
    text = str(value or "").strip()
    marker = re.search(r"(?:可见题目|面试题目|题目)\s*[：:]\s*(.+)$", text)
    return marker.group(1).strip() if marker else text


def historical_questions_to_import_rows(questions: list[dict]) -> list[dict]:
    """Produce bounded review rows; this function never writes to the database."""
    rows, seen = [], set()
    for item in questions:
        question = _question_text(item.get("text"))
        if len(question) < 8 or question in seen:
            continue
        seen.add(question)
        raw_tags = [str(tag).strip() for tag in item.get("role_tags") or [] if str(tag).strip()]
        canonical = next((_ROLE_ALIASES[tag] for tag in raw_tags if tag in _ROLE_ALIASES), "")
        tags = list(dict.fromkeys(([canonical] if canonical else []) + raw_tags))[:8]
        source_urls = [str(url).strip() for url in item.get("source_refs") or [] if str(url).strip()]
        if not tags or not source_urls:
            continue
        rows.append({
            "question": question,
            "role_tags": "|".join(tags),
            "stage": _stage(question),
            "topic": str(item.get("topic") or "历史采集题").strip()[:120],
            "source_name": "历史采集候选（待人工审核）",
            "source_url": source_urls[0],
            "published_at": str(item.get("latest_posted_at") or "").strip()[:10],
        })
    return rows


def write_import_csv(rows: list[dict], output_path: Path) -> None:
    """Write an administrator-review file compatible with the existing importer."""
    fields = ["question", "role_tags", "stage", "topic", "source_name", "source_url", "published_at"]
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
