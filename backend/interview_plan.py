"""Resume-grounded interview planning with source validation."""
from __future__ import annotations

import re

from . import knowledge_plan
from .profile import project_names


def _terms(profile: dict) -> list[str]:
    values = list(profile.get("skills") or []) + project_names(profile)
    terms: list[str] = []
    for value in values:
        for term in re.findall(r"[A-Za-z0-9+#.-]{2,}|[\u4e00-\u9fff]{2,}", value):
            if term not in terms:
                terms.append(term)
    return terms


def _has_source(item: dict) -> bool:
    return any((source.get("url") or source.get("name")) for source in item.get("sources", []))


def _opening_question(role: str, project: str, profile: dict) -> str:
    """Natural first prompt: set context without pretending the source corpus wrote it."""
    skills = "、".join(str(item) for item in (profile.get("skills") or [])[:2])
    project_clause = f"我看到你在简历中提到「{project[:50]}」。" if project else ""
    skill_clause = f"尤其涉及 {skills}。" if skills else ""
    return (
        f"我们先从你的求职方向聊起：你为什么想做{role}？"
        f"{project_clause}{skill_clause}请用 1–2 分钟讲清楚你在其中负责什么、做成了什么，以及它和这个岗位有什么关联。"
    )


def _project_deep_dive_question(project: str, source_question: str) -> str:
    """Keep the sourced interview intent, without making another project sound like theirs."""
    source = source_question.replace(" ", "")
    emphasis = ""
    if any(word in source for word in ("阻力", "挑战", "困难", "问题")):
        emphasis = "尤其想了解你遇到的最大挑战、如何推进，以及最后怎样验证改进。"
    elif any(word in source for word in ("目标", "成交", "指标", "结果")):
        emphasis = "重点讲清楚项目目标、衡量指标，以及结果是怎样验证的。"
    elif any(word in source for word in ("实习", "职责", "负责", "角色")):
        emphasis = "重点讲清楚你的个人职责、协作边界和实际产出。"
    else:
        emphasis = "请说明最大的挑战，以及你如何验证最终结果。"
    return (
        f"请展开讲讲你简历中的「{project[:50]}」：它解决了什么问题？"
        f"你做了哪些关键决策？{emphasis}"
    )


def build_personalized_plan(role: str, mode: str, profile: dict, planner=None) -> list[dict]:
    """Build a source-grounded plan and anchor its opening in the resume."""
    base = knowledge_plan.build_plan(role, mode, profile) or []
    sourced = [item for item in base if _has_source(item)]
    if not sourced:
        return []

    terms = _terms(profile)
    def score(item: dict) -> int:
        text = " ".join(str(item.get(key, "")) for key in ("question", "topic", "desc"))
        return sum(3 for term in terms if term.lower() in text.lower()) + int(item.get("frequency", 1))

    project = (project_names(profile) or [""])[0]
    ranked = sorted(sourced, key=score, reverse=True)
    candidates = []
    for index, item in enumerate(ranked):
        candidate = dict(item)
        candidate["id"] = f"candidate-{index}"
        candidates.append(candidate)
    if planner:
        chosen_ids = planner(profile, candidates)
        candidate_by_id = {candidate["id"]: candidate for candidate in candidates}
        if chosen_ids and len(set(chosen_ids)) == len(chosen_ids) and all(item in candidate_by_id for item in chosen_ids):
            ranked = [candidate_by_id[item] for item in chosen_ids]
            ranked.extend(candidate for candidate in candidates if candidate["id"] not in chosen_ids)
        else:
            ranked = candidates
    else:
        ranked = candidates
    # Preserve the interview arc. A model can prioritize within a stage, but a
    # globally high-frequency project question must not displace the opening.
    stage_order = {"自我介绍": 0, "项目深挖": 1, "能力匹配": 2, "场景问题": 3}
    ranked = sorted(
        enumerate(ranked),
        key=lambda pair: (stage_order.get(pair[1].get("step", ""), 99), pair[0]),
    )
    ranked = [item for _, item in ranked]
    for index, original in enumerate(ranked):
        item = dict(original)
        if project and index == 0:
            item["anchor"] = project[:80]
            item["question"] = _opening_question(role, project, profile)
            item["opening_personalized"] = True
        elif project and item.get("step") == "项目深挖" and item.get("question"):
            item["anchor"] = project[:80]
            item["question"] = _project_deep_dive_question(project, item["question"])
            item["personalized_from_source"] = True
        item["resume_terms"] = [term for term in terms if term.lower() in item.get("question", "").lower()]
        item.pop("id", None)
        ranked[index] = item
    return ranked
