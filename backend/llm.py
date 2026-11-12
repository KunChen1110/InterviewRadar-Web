"""LLM 调用层。

4 个调用点，职责分开：
1. parse_resume   —— 简历解析：抽项目、技能、指标、风险点
2. generate_plan  —— 面试计划：从已有候选题 ID 中建议顺序，不新增无来源题目
3. evaluate_turn  —— 逐轮评估：打分 + 是否追问 + 面试官下一句话（调用最频繁，需低延迟）
4. build_report   —— 复盘报告：总评、风险清单、改进话术、练习路线

约定：key 缺失、调用失败或 JSON 解析失败时返回 None，由 main.py 的启发式逻辑兜底，
保证无 key 时 demo 闭环可用。模型凭据只从服务端环境变量读取，绝不由前端传入。
"""
from __future__ import annotations

import json
import os
import re

DEFAULT_BASE = "https://api.openai.com/v1"
DEFAULT_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")


def _client():
    """Build a provider client from server-side environment only."""
    key = os.environ.get("OPENAI_API_KEY", "")
    if not key:
        return None
    from openai import OpenAI
    return OpenAI(api_key=key, base_url=os.environ.get("OPENAI_BASE_URL", DEFAULT_BASE),
                  timeout=30.0, max_retries=1)


def _chat_json(client, system: str, user: str, max_tokens: int = 800) -> dict | None:
    """调 chat completion 并解析 JSON 返回；任何失败返回 None。"""
    try:
        resp = client.chat.completions.create(
            model=DEFAULT_MODEL,
            messages=[
                {"role": "system", "content": system + "\n只输出 JSON，不要输出任何其他文字。"},
                {"role": "user", "content": user},
            ],
            temperature=0.3,
            max_tokens=max_tokens,
            response_format={"type": "json_object"},
        )
        text = resp.choices[0].message.content or ""
        return _parse_json(text)
    except Exception as exc:
        print(f"[llm] 调用失败（{type(exc).__name__}，回退启发式）")
        return None


def _parse_json(text: str) -> dict | None:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
    try:
        return json.loads(text)
    except Exception:
        m = re.search(r"\{[\s\S]*\}", text)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                return None
    return None


def _clamp(v, lo: int = -6, hi: int = 6) -> int:
    try:
        return max(lo, min(hi, int(v)))
    except Exception:
        return 0


def _text_list(value: object, *, limit: int = 5, item_limit: int = 500) -> list[str] | None:
    """Validate a small model-generated list before it reaches a page or DB."""
    if not isinstance(value, list):
        return None
    items: list[str] = []
    for raw in value:
        text = str(raw or "").strip()
        if text:
            items.append(text[:item_limit])
        if len(items) >= limit:
            break
    return items


def _resume_profile_payload(value: object) -> dict | None:
    """Accept only the bounded profile contract used by the rest of the app."""
    if not isinstance(value, dict) or not isinstance(value.get("skills"), list):
        return None
    projects = value.get("projects", [])
    if not isinstance(projects, list) or not all(isinstance(item, dict) for item in projects):
        return None
    def text_items(raw: object, limit: int) -> list[str]:
        if not isinstance(raw, list):
            return []
        return [str(item).strip()[:120] for item in raw if str(item).strip()][:limit]
    clean_projects = []
    for project in projects[:5]:
        clean = {key: project[key] for key in (
            "name", "domain", "stack", "responsibilities", "outcomes", "evidence_gaps"
        ) if key in project}
        if clean:
            clean_projects.append(clean)
    return {
        "skills": text_items(value.get("skills"), 12),
        "projects": clean_projects,
        "strengths": text_items(value.get("strengths"), 8),
        "risks": text_items(value.get("risks"), 8),
        "summary": str(value.get("summary") or "").strip()[:120],
        "parsed_by": "llm",
    }


# ---------- 1. 简历解析 ----------

def parse_resume(resume_text: str) -> dict | None:
    client = _client()
    if client is None:
        return None
    data = _chat_json(
        client,
        "你是一名资深技术面试官，擅长从简历中提炼面试考察点。",
        f"""请解析下面这份简历，输出 JSON：
{{
  "skills": ["技能关键词，最多12个"],
  "projects": [{{"name":"项目名/一句话概括","domain":"业务领域","stack":["技术"],"responsibilities":["职责"],"outcomes":["量化结果"],"evidence_gaps":["缺少的证据或易被追问点"]}}],
  "strengths": ["可验证的岗位匹配优势，最多8个"],
  "risks": ["面试官最可能追问或质疑的风险点（写得虚、缺指标、技术栈与岗位不匹配等），3-5条"],
  "summary": "100字以内的候选人画像摘要"
}}
简历原文：
{resume_text[:4000]}""",
    )
    return _resume_profile_payload(data)


# ---------- 2. 简历驱动面试计划 ----------

def generate_plan(profile: dict, candidates: list[dict]) -> list[str] | None:
    """Ask the model to rank only source-backed candidate IDs.

    Returning IDs rather than free-form questions prevents the model from inventing
    an unsupported question or a fabricated source.
    """
    client = _client()
    if client is None or not candidates:
        return None
    compact = [
        {
            "id": candidate["id"],
            "stage": candidate.get("step"),
            "topic": candidate.get("topic"),
            "question": candidate.get("question"),
            "source_count": len(candidate.get("sources") or []),
        }
        for candidate in candidates
    ]
    data = _chat_json(
        client,
        "你是面试规划师。只能选择输入候选题的 id，绝不能创建新问题或虚构来源。"
        "首题优先选择最值得从候选人简历项目深入追问的题。",
        "候选人画像：" + json.dumps(profile, ensure_ascii=False) + "\n"
        "真实面经候选题：" + json.dumps(compact, ensure_ascii=False) + "\n"
        "输出 JSON：{\"candidate_ids\":[\"按面试顺序排列的候选题 id\"]}。",
        max_tokens=500,
    )
    ids = data.get("candidate_ids") if data else None
    if not isinstance(ids, list) or not all(isinstance(item, str) for item in ids):
        return None
    return ids


# ---------- 3. 逐轮评估 ----------

def evaluate_turn(session: dict, question: dict, answer: str) -> dict | None:
    client = _client()
    if client is None:
        return None
    profile = session.get("profile") or {}
    summary = profile.get("summary") or json.dumps(
        {"skills": profile.get("skills", []), "projects": profile.get("projects", [])},
        ensure_ascii=False)
    mode = session.get("mode", "normal")
    mode_note = {
        "hard": "本场为高压模式：只要回答不够具体、缺少量化或逻辑跳跃，一律追问，不要放过。",
        "normal": "回答空泛、缺细节或有疑点时追问；回答已经充分则进入下一题。",
        "light": "本场为轻量练习：尽量不追问，除非回答严重离题。",
    }.get(mode, "")
    signals = question.get("answer_signals") or []
    signals_text = ("\n好的回答应包含这些信号：" + "；".join(signals)) if signals else ""
    left = session.get("_followups_left")
    if left is not None and left <= 0:
        budget_note = ("\n本题已达追问次数上限：无论回答如何，need_followup 必须为 false，"
                       "followup_question 为空字符串。")
    elif left is not None:
        budget_note = (f"\n本题最多还能再追问 {left} 次：追问机会有限，只在回答确有重要疑点、"
                       f"关键细节缺失或疑似转写错误影响判断时才追问；回答基本合格就不要追问，"
                       f"把机会留给真正值得深挖的点。")
    else:
        budget_note = ""
    data = _chat_json(
        client,
        f"你是一名资深技术面试官，正在对候选人进行{session.get('role', '技术岗')}面试。"
        f"候选人画像：{summary}。本系统输入来自实时语音转写，可能出现错别字、术语误识别、"
        f"漏标点、断句混乱和少量漏词。评分客观、鼓励为主，并要把 ASR 噪声与候选人的真实表达能力区分开："
        f"不要因为口语化、标点缺失或疑似识别错误直接重扣表达分；优先评估语义结构、技术内容、"
        f"项目证据和因果链。只有在答案本身语义混乱、前后矛盾、缺少关键细节时才扣分。"
        f"如果某个关键信息可能是转写错误，应通过追问澄清，而不是直接判错。"
        f"分数基线是 60 分（合格水平），delta 是每轮的加减分："
        f"合格但无亮点 0~+1；良好 +2~+3；优秀（有细节、有数据、有因果）+4~+6；"
        f"偏弱 -1~-2；明显跑题或空洞 -3~-6。大多数回答应落在 -2~+3，"
        f"一场正常发挥的面试结束后总分应在 70~85 区间，不要把合格回答打出负分。{mode_note}{budget_note}",
        f"""当前考察点：{question.get('topic', '')}（{question.get('desc', '')}）
面试问题：{question.get('question', '')}{signals_text}
候选人回答：{answer}

请评估该回答，输出 JSON：
{{
  "delta": {{"depth": -6到6的整数, "clarity": 整数, "logic": 整数, "proof": 整数}},
  "asr_risk": "low/medium/high（三选一；判断回答是否疑似受语音转写错误影响）",
  "need_followup": true或false（回答空泛/缺细节/有疑点时为true；疑似 ASR 错误影响关键判断时也为true；回答已经充分则为false）,
  "followup_question": "若需要追问，给出一句针对回答内容的具体追问；如果是 ASR 疑似错误，先澄清关键术语/数字/因果，不要泛泛追问；不需要则为空字符串",
  "feedback": "一句话中文点评。若存在疑似转写误差，请说明'我会先按语义理解，不因转写噪声直接扣分'，再指出需要补充什么"
}}""",
        max_tokens=500,
    )
    if not data or not isinstance(data.get("delta"), dict):
        return None
    delta = data["delta"]
    return {
        "delta": {k: _clamp(delta.get(k, 0)) for k in ("depth", "clarity", "logic", "proof")},
        "need_followup": bool(data.get("need_followup")),
        "custom_followup": str(data.get("followup_question") or "").strip()[:500],
        "feedback": str(data.get("feedback") or "").strip()[:500] or "已记录你的回答。",
        "is_followup_response": session["follow_idx"] >= 0,
        "generated_by": "llm",
    }


# ---------- 4. 复盘报告 ----------

def build_report(session: dict) -> dict | None:
    client = _client()
    if client is None:
        return None
    turns_text = "\n".join(
        f"Q: {t['question']}\nA: {t['answer'][:300]}\n评: {t['feedback']}"
        for t in session.get("turns", [])
    )[:5000]
    data = _chat_json(
        client,
        f"你是一名资深技术面试官，刚结束一场{session.get('role', '技术岗')}模拟面试，"
        "需要给候选人一份诚实、具体、可执行的复盘报告。",
        f"""候选人画像：{(session.get('profile') or {}).get('summary', '见问答记录')}
当前四维度得分（0-100）：{json.dumps(session.get('scores', {}), ensure_ascii=False)}
完整问答记录：
{turns_text}

输出 JSON：
{{
  "overall": 0-100的综合分（与维度分一致，不要虚高）,
  "dimensions": {{"depth": 整数, "clarity": 整数, "logic": 整数, "proof": 整数}},
  "risk_points": ["面试中暴露的具体风险点，引用回答中的实际内容，3-5条"],
  "suggestions": ["针对每个风险点的改进建议/推荐话术，3-5条"],
  "next_plan": ["接下来一周的练习安排，按天给出，5-7条"]
}}""",
        max_tokens=1500,
    )
    if not data or "overall" not in data:
        return None
    risk_points = _text_list(data.get("risk_points"))
    suggestions = _text_list(data.get("suggestions"))
    next_plan = _text_list(data.get("next_plan"), limit=7)
    if not risk_points or not suggestions or not next_plan:
        return None
    try:
        overall = max(0, min(100, int(data.get("overall"))))
    except (TypeError, ValueError):
        return None
    raw_dimensions = data.get("dimensions") if isinstance(data.get("dimensions"), dict) else {}
    dimensions = {}
    for key in ("depth", "clarity", "logic", "proof"):
        try:
            dimensions[key] = max(0, min(100, int(raw_dimensions.get(key, 0))))
        except (TypeError, ValueError):
            dimensions[key] = 0
    return {
        "overall": overall,
        "dimensions": dimensions,
        "risk_points": risk_points,
        "suggestions": suggestions,
        "next_plan": next_plan,
        "generated_by": "llm",
    }
