"""知识库语料加载与组卷。

可选的策展语料放在 knowledge/demo_knowledge（公开仓库不包含生产语料）：
- questions.json：按岗位和环节标注、带来源信息的题目
- role_packs.json：每个 环节×主题 的评估维度、追问模板、好答案信号
- role_catalog.json：岗位元数据

设计取舍：语料为策展数据、规模小，启动时直接读 JSON 文件，
按 岗位→环节→频次 规则组卷（引入向量检索等题库规模上来后再做）。
用户投稿落 questions 表，由本模块定时拉取合并：投稿题按关键词分类到环节，
能匹配到知识库岗位的即并入该岗位组卷；未覆盖岗位攒够 5 题也可用投稿独立组卷。
"""
from __future__ import annotations

import json
import os
import re
import time

from .profile import project_names
from .retrieval import select_stage_questions
from . import embeddings
from .embeddings import embed_one

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KNOWLEDGE_DIR = os.path.join(BASE_DIR, "knowledge", "demo_knowledge")

_questions: list[dict] = []
_catalog: list[dict] = []
_strategies: dict[tuple[str, str], dict] = {}   # (role, stage, topic) -> strategy
_loaded = False
_curated_embeddings_ready = False
_curated_embeddings_last_attempt = 0.0
_EMBEDDING_RETRY_SECONDS = 300.0

# 用户投稿题（questions 表，60s 刷新一次）
_db_questions: list[dict] = []
_db_loaded_at = 0.0
_DB_TTL = 60.0
_DB_ROLE_MIN = 5      # 未覆盖岗位至少攒够这么多投稿题才独立组卷


def _load() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    try:
        with open(os.path.join(KNOWLEDGE_DIR, "questions.json"), encoding="utf-8") as f:
            _questions.extend(json.load(f))
        with open(os.path.join(KNOWLEDGE_DIR, "role_catalog.json"), encoding="utf-8") as f:
            _catalog.extend(json.load(f).get("roles", []))
        with open(os.path.join(KNOWLEDGE_DIR, "role_packs.json"), encoding="utf-8") as f:
            for pack in json.load(f).get("role_packs", []):
                for s in pack.get("strategies", []):
                    _strategies[(pack["role"], s.get("interview_stage", ""), s.get("topic", ""))] = s
        print(f"[knowledge] 加载 {len(_questions)} 道题 / {len(_catalog)} 个岗位 / {len(_strategies)} 条策略")
    except FileNotFoundError:
        print("[knowledge] 未找到 demo_knowledge，使用内置 CS 题库兜底")


def ensure_curated_embeddings() -> None:
    """Build a process-local semantic index for the small curated corpus once.

    Source files remain unchanged and no candidate data is stored here. If the
    embedding provider is unavailable, the ready flag stays false so a later
    successful request can retry; retrieval continues in keyword mode.
    """
    global _curated_embeddings_ready, _curated_embeddings_last_attempt
    if _curated_embeddings_ready or not _questions or not embeddings.is_configured():
        return
    now = time.time()
    if _curated_embeddings_last_attempt and now - _curated_embeddings_last_attempt < _EMBEDDING_RETRY_SECONDS:
        return
    _curated_embeddings_last_attempt = now
    texts = [
        " ".join(str(question.get(key, "")) for key in ("role", "stage", "topic", "question"))
        for question in _questions
    ]
    vectors = embeddings.embed_texts(texts)
    if not vectors or len(vectors) != len(_questions):
        return
    for question, vector in zip(_questions, vectors):
        question["embedding"] = vector
    _curated_embeddings_ready = True


def curated_posts(role_filter: str = "") -> list[dict]:
    """把有来源 URL 的策展题按来源帖分组供社区展示。

    没有外部策展语料时返回空列表；测试夹具没有真实来源 URL。
    """
    _load()
    groups: dict[str, dict] = {}
    for q in _questions:
        if role_filter and role_filter.strip().lower() not in (q.get("role") or "").lower():
            continue
        url = q.get("source_url") or ""
        if not url:
            continue
        g = groups.setdefault(url, {
            "company": "牛客面经",
            "role": q.get("role", ""),
            "interview_date": q.get("posted_at") or "",
            "rounds": "官方策展 · 高频真题",
            "questions": [],
            "created_at": q.get("posted_at") or "",
            "author": "官方策展",
            "curated": True,
            "source_url": url,
            "max_freq": 0,
        })
        # 优先用帖中原始行（evidence），没有再用清洗后的题干
        text = (q.get("evidence") or q.get("question") or "").strip()
        if text and text not in g["questions"]:
            g["questions"].append(text)
        g["max_freq"] = max(g["max_freq"], q.get("frequency", 1))
        # 取帖内最新的发布日期
        if (q.get("posted_at") or "") > g["created_at"]:
            g["created_at"] = q["posted_at"]
            g["interview_date"] = q["posted_at"]
    posts = list(groups.values())
    for p in posts:
        p["rounds"] = f"官方策展 · 最高出现 {p.pop('max_freq')} 次"
    posts.sort(key=lambda p: p["created_at"], reverse=True)
    return posts


def available_roles() -> list[dict]:
    _load()
    roles = [
        {"role": c["role"], "question_count": c.get("question_count", 0),
         "topics": c.get("topics", [])}
        for c in _catalog
    ]
    known = {c["role"] for c in _catalog}
    for bucket_role, qs in _db_role_buckets().items():
        if bucket_role not in known and len(qs) >= _DB_ROLE_MIN:
            roles.append({"role": bucket_role, "question_count": len(qs),
                          "topics": ["社区投稿真题"]})
    return roles


def _norm(text: str) -> str:
    text = text.lower()
    for w in ["职位", "岗位", "工程师", "实习", "校招", "社招", "方向", "岗", " ", "　"]:
        text = text.replace(w, "")
    return text


def _match_role_name(role_input: str, candidates: list[str]) -> str | None:
    """Prefer an exact role; accept only an unambiguous, substantive alias."""
    norm = _norm(role_input)
    if not norm:
        return None
    normalized = [(role, _norm(role)) for role in candidates]
    for role, role_norm in normalized:
        if norm == role_norm:
            return role
    if len(norm) < 3:
        return None
    matches = [role for role, role_norm in normalized
               if role_norm and (norm in role_norm or role_norm in norm)]
    return matches[0] if len(matches) == 1 else None


# ---------- 用户投稿题（questions 表）合并 ----------

def _classify_stage(question: str) -> str:
    """投稿题没有环节标注，按关键词粗分类。"""
    if re.search(r"自我介绍|介绍.{0,4}自己|自我评价", question):
        return "自我介绍"
    if re.search(r"项目|实习|经历|案例|做过|负责过|最有挑战", question):
        return "项目深挖"
    if re.search(r"如果|假设|怎么|如何|设计一个|遇到.*办", question):
        return "场景问题"
    return "能力匹配"


def _load_db_questions() -> None:
    """从 questions 表拉用户投稿题，60s 缓存；无库/失败时静默为空。"""
    global _db_questions, _db_loaded_at
    if time.time() - _db_loaded_at < _DB_TTL:
        return
    _db_loaded_at = time.time()
    from . import db
    rows = db.fetchall(
        "SELECT question, tags, source_name, source_url, published_at, source_type, metadata, embedding::text AS embedding FROM questions "
        "WHERE (source_name LIKE %s OR source_type = %s) AND is_active = true ORDER BY id DESC LIMIT 5000",
        ("用户面经%", "manual_import"))
    if not rows:
        _db_questions = []
        return
    out = []
    seen_texts = set()
    for r in rows:
        tags = r.get("tags") or []
        role_tag = next((t for t in tags if t != "用户投稿"), "")
        if not role_tag:
            continue
        key = re.sub(r"\s+", "", r["question"])
        if not key or key in seen_texts:
            continue  # 多人投了同一题，只留一条
        seen_texts.add(key)
        # 投稿岗位能匹配到知识库岗位就归并过去，否则保留原岗位名独立成桶
        role = match_role(role_tag) or role_tag
        metadata = r.get("metadata") or {}
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except json.JSONDecodeError:
                metadata = {}
        is_manual = r.get("source_type") == "manual_import"
        embedding = r.get("embedding")
        if isinstance(embedding, str):
            try:
                embedding = json.loads(embedding)
            except json.JSONDecodeError:
                embedding = None
        if not isinstance(embedding, list):
            embedding = metadata.get("embedding")
        out.append({
            "role": role,
            "stage": metadata.get("stage") or _classify_stage(r["question"]),
            "topic": metadata.get("topic") or (r.get("source_name") or "用户投稿").replace("用户面经 · ", ""),
            "question": r["question"],
            "frequency": 1,
            "supporting_sources": [],
            "answer_signals": metadata.get("answer_signals") or [],
            "followups": metadata.get("followup_templates") or [],
            "embedding": embedding,
            "retrieval_weight": metadata.get("source_metadata", {}).get("retrieval_weight", 1.0),
            "_sources": [{"name": r.get("source_name") or ("人工导入" if is_manual else "用户投稿"),
                          "url": r.get("source_url") or "", "note": "人工导入真题" if is_manual else "社区投稿真题"}],
        })
    _db_questions = out


def invalidate_db_cache() -> None:
    """投稿入库后立即失效缓存（auth.py 反哺题库时调用），下场面试即可用上新题。"""
    global _db_loaded_at
    _db_loaded_at = 0.0


def _db_role_buckets() -> dict[str, list[dict]]:
    _load_db_questions()
    buckets: dict[str, list[dict]] = {}
    for q in _db_questions:
        buckets.setdefault(q["role"], []).append(q)
    return buckets


def match_role(role_input: str) -> str | None:
    """把用户输入的岗位模糊匹配到知识库岗位，匹配不上返回 None。"""
    _load()
    return _match_role_name(role_input, [c["role"] for c in _catalog])


# 各强度下每个环节的题量配额
_QUOTAS = {
    "light": {"自我介绍": 1, "项目深挖": 1, "能力匹配": 2, "场景问题": 1},
    "normal": {"自我介绍": 1, "项目深挖": 2, "能力匹配": 2, "场景问题": 2},
    "hard": {"自我介绍": 1, "项目深挖": 2, "能力匹配": 3, "场景问题": 2},
}


def _pick_stage_questions(qs: list[dict], stage: str, n: int) -> list[dict]:
    """按频次降序选题，优先覆盖不同 topic。"""
    pool = sorted((q for q in qs if q.get("stage") == stage),
                  key=lambda q: q.get("frequency", 1), reverse=True)
    picked, seen_topics = [], set()
    for q in pool:  # 第一轮：每个 topic 取最高频的一道
        if q.get("topic") not in seen_topics:
            picked.append(q)
            seen_topics.add(q.get("topic"))
        if len(picked) >= n:
            return picked
    for q in pool:  # 第二轮：补足数量
        if q not in picked:
            picked.append(q)
        if len(picked) >= n:
            break
    return picked


def _to_plan_item(q: dict, role: str, profile: dict) -> dict:
    stage = q.get("stage", "")
    topic = q.get("topic", "")
    strategy = _strategies.get((role, stage, topic), {})
    projects = project_names(profile)
    if stage == "自我介绍":
        anchor = "整体简历"
    elif stage == "项目深挖" and projects:
        anchor = projects[0][:40]
    else:
        anchor = f"{role} · {topic}"
    dims = strategy.get("evaluation_dimensions") or []
    sources = q.get("_sources") or []
    for s in (q.get("supporting_sources") or [])[:2]:
        posted = s.get("posted_at") or ""
        sources.append({
            "name": f"牛客面经 · {posted}" if posted else "牛客面经",
            "url": s.get("source_url", ""),
            "note": f"共出现 {q.get('frequency', 1)} 次" if q.get("frequency_label") else "",
        })
    item = {
        "step": stage,
        "topic": topic,
        "anchor": anchor,
        "desc": ("考察" + "、".join(dims) + "。") if dims else f"考察 {topic} 相关的岗位能力。",
        "question": q.get("question") or q.get("canonical_question", ""),
        "followups": strategy.get("follow_up_templates") or [],
        "answer_signals": strategy.get("answer_signals") or [],
        "tags": [role, topic],
        "frequency": q.get("frequency", 1),
        "sources": sources,
    }
    if q.get("retrieval"):
        item["retrieval"] = {
            "retrieval_mode": q["retrieval"].get("retrieval_mode", "keyword"),
            "query_terms": q["retrieval"].get("query_terms", []),
            "matched_profile_fields": q["retrieval"].get("matched_profile_fields", []),
            "rank_score": q["retrieval"].get("rank_score"),
            "semantic_score": q["retrieval"].get("semantic_score"),
        }
    return item


def resolve_role(role_input: str) -> str | None:
    """决定组卷用的岗位：优先知识库岗位，其次投稿攒够的社区岗位，都没有返回 None。"""
    _load()
    role = match_role(role_input)
    if role:
        return role
    catalog_roles = {c["role"] for c in _catalog}
    candidates = [bucket_role for bucket_role, bucket_qs in _db_role_buckets().items()
                  if bucket_role not in catalog_roles and len(bucket_qs) >= _DB_ROLE_MIN]
    return _match_role_name(role_input, candidates)


def retrieval_query(role: str, candidate_profile: dict) -> str:
    """Build semantic-query input from the privacy-minimized profile only."""
    parts = [role, str(candidate_profile.get("summary") or "")]
    parts.extend(str(item) for item in candidate_profile.get("skills") or [])
    parts.extend(str(item) for item in candidate_profile.get("risks") or [])
    for project in candidate_profile.get("projects") or []:
        if isinstance(project, dict):
            parts.append(str(project.get("name") or ""))
            parts.extend(str(item) for item in project.get("stack") or [])
    return " ".join(part for part in parts if part).strip()


def build_plan(role_input: str, mode: str, profile: dict) -> list[dict] | None:
    """知识库/社区组卷；岗位匹配不上时返回 None（调用方回退内置题库）。"""
    _load()
    ensure_curated_embeddings()
    role = resolve_role(role_input)
    if not role:
        return None
    if role in {c["role"] for c in _catalog}:
        qs = [q for q in _questions if q.get("role") == role]
        # 合并该岗位的社区投稿真题（去重：题干归一后相同的以文件题库为准）
        seen = {re.sub(r"\s+", "", q.get("question", "")) for q in qs}
        for q in _db_role_buckets().get(role, []):
            key = re.sub(r"\s+", "", q.get("question", ""))
            if key and key not in seen:
                qs.append(q)
                seen.add(key)
        if not qs:
            return None
    else:
        # 知识库未覆盖的岗位：投稿题独立成卷（resolve_role 已保证数量达标）
        qs = _db_role_buckets().get(role, [])
        if not qs:
            return None
    quotas = _QUOTAS.get(mode, _QUOTAS["normal"])
    query_embedding = embed_one(retrieval_query(role, profile))
    plan = []
    for stage in ["自我介绍", "项目深挖", "能力匹配", "场景问题"]:
        selected = select_stage_questions(profile, role, stage, qs, quotas.get(stage, 1), query_embedding)
        # Curated legacy data is normalized with supporting sources later in
        # _to_plan_item; retrieval needs its source signal before then.
        if not selected:
            # Legacy curated questions carry source_url instead of _sources, so
            # preserve the same auditable keyword evidence on the fallback path.
            selected = _pick_stage_questions(qs, stage, quotas.get(stage, 1))
            for item in selected:
                item = item.setdefault("retrieval", {
                    "retrieval_mode": "keyword",
                    "query_terms": [],
                    "matched_profile_fields": [],
                    "rank_score": item.get("frequency", 1),
                    "semantic_score": None,
                })
        for q in selected:
            plan.append(_to_plan_item(q, role, profile))
    # 社区投稿保底：匹配到知识库岗位时，若该岗位有未入选的投稿真题，
    # 替换同环节中频次最低的一题，保证每场至少出现 1 道社区新鲜真题
    db_fresh = [q for q in qs if q.get("_sources")]
    if db_fresh and role in {c["role"] for c in _catalog}:
        plan_texts = {p["question"] for p in plan}
        fresh = next((q for q in db_fresh if q["question"] not in plan_texts), None)
        if fresh:
            idxs = [i for i, p in enumerate(plan) if p["step"] == fresh["stage"]]
            if idxs:
                i = min(idxs, key=lambda j: plan[j]["frequency"])
                plan[i] = _to_plan_item(fresh, role, profile)
    print(f"[knowledge] 岗位「{role_input}」→ 匹配「{role}」，组卷 {len(plan)} 题")
    return plan
