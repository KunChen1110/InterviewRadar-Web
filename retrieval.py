"""Source-grounded, hybrid-ready question retrieval.

The initial production path is deterministic keyword ranking.  Its response shape
contains retrieval evidence so a future embedding scorer can be added without
changing plan consumers.
"""
from __future__ import annotations

import re
import math

from embeddings import cosine_similarity
from profile import project_names


def _terms(profile: dict) -> dict[str, list[str]]:
    values = {
        "skills": list(profile.get("skills") or []),
        "risks": list(profile.get("risks") or []),
        "projects": project_names(profile),
    }
    for project in profile.get("projects") or []:
        if isinstance(project, dict):
            values["projects"].extend(project.get("stack") or [])
            values["risks"].extend(project.get("evidence_gaps") or [])
    return {
        field: [term for value in items for term in re.findall(r"[A-Za-z0-9+#./-]{2,}|[\u4e00-\u9fff]{2,}", str(value))]
        for field, items in values.items()
    }


def has_source(question: dict) -> bool:
    sources = question.get("_sources") or question.get("sources") or question.get("supporting_sources") or []
    return any(source.get("name") or source.get("url") or source.get("source_url") for source in sources)


def _score(profile: dict, role: str, question: dict, query_embedding: list[float] | None = None) -> tuple[float, list[str], list[str], float | None]:
    text = " ".join(str(question.get(key, "")) for key in ("role", "stage", "topic", "question", "tags"))
    terms = _terms(profile)
    matched_fields, query_terms = [], []
    score = int(question.get("frequency", 1))
    if role and role.lower() in str(question.get("role", "")).lower():
        score += 12
    for field, field_terms in terms.items():
        hits = [term for term in field_terms if term.lower() in text.lower()]
        if hits:
            matched_fields.append(field)
            query_terms.extend(hits)
            score += len(set(hits)) * (8 if field == "risks" else 5)
    semantic_score = cosine_similarity(query_embedding, question.get("embedding"))
    if semantic_score is not None:
        try:
            retrieval_weight = float(question.get("retrieval_weight", 1.0))
            if not math.isfinite(retrieval_weight):
                retrieval_weight = 1.0
        except (TypeError, ValueError):
            retrieval_weight = 1.0
        score += semantic_score * 20 * max(0.0, min(retrieval_weight, 2.0))
    return score, matched_fields, list(dict.fromkeys(query_terms))[:12], semantic_score


def rank_questions(profile: dict, role: str, stage: str, questions: list[dict], query_embedding: list[float] | None = None) -> list[dict]:
    """Return only sourced questions, with deterministic rank evidence."""
    ranked = []
    seen = set()
    for source in questions:
        if source.get("stage") != stage or not has_source(source):
            continue
        text = re.sub(r"\s+", "", str(source.get("question", "")))
        if not text or text in seen:
            continue
        seen.add(text)
        score, fields, terms, semantic_score = _score(profile, role, source, query_embedding)
        item = dict(source)
        item["retrieval"] = {
            "query_terms": terms,
            "matched_profile_fields": fields,
            "rank_score": score,
            "retrieval_mode": "hybrid" if semantic_score is not None else "keyword",
            "semantic_score": round(semantic_score, 4) if semantic_score is not None else None,
        }
        ranked.append(item)
    return sorted(ranked, key=lambda item: (-item["retrieval"]["rank_score"], item["question"]))


def select_stage_questions(profile: dict, role: str, stage: str, questions: list[dict], limit: int, query_embedding: list[float] | None = None) -> list[dict]:
    ranked = rank_questions(profile, role, stage, questions, query_embedding)
    selected, seen_topics = [], set()
    for item in ranked:
        if item.get("topic") not in seen_topics:
            selected.append(item)
            seen_topics.add(item.get("topic"))
        if len(selected) >= limit:
            return selected
    for item in ranked:
        if item not in selected:
            selected.append(item)
        if len(selected) >= limit:
            break
    return selected
