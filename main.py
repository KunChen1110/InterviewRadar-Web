"""InterviewRadar 后端。

- FastAPI + /health + 静态托管前端页面，单进程单端口。
- 启动时若平台注入了 DATABASE_URL，则连接 Postgres 并建表（见 db.py）。
- 账号/面经投稿在 auth.py；LLM 调用点在 llm.py（服务端 .env 密钥，
  不接受客户端传入的 key；无 key 时启发式兜底保证闭环可用）。
- 注册用户可免费不限次完成完整面试；面经投稿由运营审核后反哺题库。
- 无 DATABASE_URL 时降级匿名开发模式（不校验权限，便于本地调试）。
"""
from __future__ import annotations

import os
import tempfile
import copy
import threading
from contextlib import asynccontextmanager

# 本地/自托管时从 .env 加载密钥（平台预览/部署会自动注入环境变量，load 不覆盖已有值）
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except ImportError:
    pass

import re
import uuid
import json
from datetime import datetime, timezone
from typing import Literal

from fastapi import Depends, FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse
from pydantic import BaseModel, Field

from config import load_settings

SETTINGS = load_settings()

import auth
import asr
import db
import interview_plan
import interview_memory
import knowledge_plan
import llm
import profile
from security import protect_public_beta
from seed_questions import CS_TRACK, build_plan

@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Initialize the database once per application process."""
    db.init_schema()
    yield


app = FastAPI(title="InterviewRadar", lifespan=lifespan)
app.include_router(auth.router)
app.middleware("http")(protect_public_beta)


@app.exception_handler(db.DatabaseUnavailableError)
async def database_unavailable_handler(_request: Request, _exc: db.DatabaseUnavailableError) -> JSONResponse:
    """Never leak database connection details or mistake an outage for bad credentials."""
    return JSONResponse(status_code=503, content={"detail": "服务暂时不可用，请稍后重试"})

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 会话运行时状态（进程内；持久化摘要落 sessions/turns/reports 表）
SESSIONS: dict[str, dict] = {}
DEV_FEEDBACK: dict[str, dict] = {}
_SESSION_GUARD = threading.Lock()
_SESSION_MUTATION_LOCKS: dict[str, threading.Lock] = {}

# 各强度模式的追问上限（每道主问题后面最多再追几轮）。
# 规则基线：自我介绍最多 1 次追问，其它环节每题最多 2 次；
# 轻量场更温和、高压场追问更凶。
# 全场总轮数上限 = 主问题数 + 各题追问上限之和（建会话时按试卷计算），
# 保证任何情况下面试都有确定的总轮数上限，绝不无限追问。
FOLLOW_LIMITS = {
    "light":  {"intro": 0, "other": 1},
    "normal": {"intro": 1, "other": 2},
    "hard":   {"intro": 2, "other": 3},
}


@app.get("/health")
@app.get("/api/health")
@app.get("/backend/health")
def health() -> dict:
    return {"status": "ok", "rev": os.environ.get("APP_REV", "?")}


@app.get("/ready")
def ready() -> Response:
    try:
        ready = db.is_ready()
    except db.DatabaseUnavailableError:
        ready = False
    if not ready:
        return Response(status_code=503, content='{"status":"not_ready"}', media_type="application/json")
    return Response(content='{"status":"ready"}', media_type="application/json")


_RESUME_MAX_BYTES = 5 * 1024 * 1024


@app.post("/api/parse_resume")
async def parse_resume_file(file: UploadFile = File(...),
                            user: dict = Depends(auth.current_user)) -> dict:
    """上传简历文件提取文本：PDF 用 pypdf 解析，txt/md 直接读取；图片暂不支持。"""
    name = (file.filename or "").lower()
    content_type = (file.content_type or "").lower()
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="文件为空")
    if len(data) > _RESUME_MAX_BYTES:
        raise HTTPException(status_code=400, detail="文件超过 5MB 上限")
    if name.endswith(".pdf") and content_type not in {"", "application/pdf", "application/x-pdf"}:
        raise HTTPException(status_code=400, detail="文件格式与扩展名不匹配，请上传有效 PDF")
    if name.endswith((".txt", ".md")):
        text = data.decode("utf-8", errors="ignore")
    elif name.endswith(".pdf"):
        if not data.startswith(b"%PDF-"):
            raise HTTPException(status_code=400, detail="文件格式与扩展名不匹配，请上传有效 PDF")
        try:
            import io
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(data))
            text = "\n".join((p.extract_text() or "") for p in reader.pages)
        except Exception as exc:
            # Do not return parser internals to the browser; users only need a
            # clear next action.
            raise HTTPException(status_code=400, detail="PDF 解析失败，请确认文件未损坏或粘贴简历文字") from exc
    else:
        raise HTTPException(status_code=400, detail="暂支持 PDF / TXT / MD，图片简历请先粘贴文字")
    text = text.strip()
    if len(text) < 20:
        raise HTTPException(status_code=400,
                            detail="未能提取到足够文本（扫描件图片型 PDF 暂不支持，请粘贴简历文字）")
    return {"text": text[:8000], "chars": len(text)}


@app.post("/api/sessions/{sid}/transcribe")
async def transcribe_answer(sid: str, file: UploadFile = File(...), user: dict = Depends(auth.current_user)) -> dict:
    _get(sid, user)
    suffix = os.path.splitext(file.filename or "")[1].lower()
    if suffix not in asr.ALLOWED_SUFFIXES:
        raise HTTPException(status_code=400, detail="仅支持 OGG/WebM/WAV/MP3/AAC/AMR 音频")
    data = await file.read()
    if not data or len(data) > asr.MAX_AUDIO_BYTES:
        raise HTTPException(status_code=400, detail="音频为空或超过 2 MB")
    with tempfile.NamedTemporaryFile(prefix="ir-asr-", suffix=suffix, delete=False) as temp:
        temp.write(data)
        path = temp.name
    try:
        try:
            text = asr.transcribe_once(path)
        except asr.AudioValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return {"text": text, "degraded": False}
    finally:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> Response:
    return Response(status_code=204)


@app.get("/assets/{filename}", include_in_schema=False)
def assets(filename: str) -> FileResponse:
    return _static_file("assets", filename)


@app.get("/web/{filename}", include_in_schema=False)
def web_files(filename: str) -> FileResponse:
    return _static_file("web", filename)


def _static_file(subdir: str, filename: str) -> FileResponse:
    if "/" in filename or ".." in filename:
        raise HTTPException(status_code=404, detail="not found")
    path = os.path.join(BASE_DIR, subdir, filename)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="页面不存在")
    return FileResponse(path)


@app.get("/")
def index() -> RedirectResponse:
    # 相对重定向：预览子路径下也能正确落到 /web/landing.html，
    # 页面里的相对资源（common.css/js）随之解析正确
    return RedirectResponse("web/landing.html", status_code=302)


# ---------- 请求模型 ----------

class SessionCreate(BaseModel):
    resume_text: str = Field(default="", max_length=12000)
    role: str = Field(default="计算机/软件方向", max_length=80)
    mode: Literal["normal", "hard", "light"] = "normal"
    kind: str = "full"            # 兼容旧客户端；所有会话均按完整场处理


class AnswerSubmit(BaseModel):
    answer: str = Field(min_length=1, max_length=6000)


class FeedbackBody(BaseModel):
    rating: int = Field(ge=1, le=5)
    comment: str = Field(default="", max_length=1000)


def extract_profile(resume_text: str) -> dict:
    """启发式简历要点抽取（LLM 调用失败时的兜底）。"""
    return profile.heuristic_profile(resume_text, target_role="")


# ---------- 面试 API ----------

@app.post("/api/sessions")
def create_session(body: SessionCreate, user: dict = Depends(auth.current_user)) -> dict:
    if not body.resume_text.strip():
        raise HTTPException(status_code=400, detail="resume_text 不能为空")
    kind = "full"

    # The model receives only a contact-redacted version. The raw input remains
    # request-scoped and is never placed in session persistence.
    sanitized_resume = profile.redact_contact_details(body.resume_text, limit=12000)
    parsed = llm.parse_resume(sanitized_resume)
    candidate_profile = profile.normalize_profile(parsed, body.role) if parsed else profile.heuristic_profile(sanitized_resume, body.role)
    plan = interview_plan.build_personalized_plan(body.role, body.mode, candidate_profile, planner=llm.generate_plan) \
        or knowledge_plan.build_plan(body.role, body.mode, candidate_profile) \
        or build_plan(track=CS_TRACK, role=body.role, mode=body.mode, profile=candidate_profile)
    # The generic practice set has no traceable source corpus, but it should
    # still begin as a real interview rather than a generic bank question.
    # Keep its source metadata untouched so the UI continues to disclose the
    # fallback honestly.
    if _question_source_metadata(plan)["source_backed"] is False:
        _personalize_fallback_opening(plan, body.role, candidate_profile)
    matched_role = knowledge_plan.resolve_role(body.role)
    source_meta = _question_source_metadata(plan)
    sid = uuid.uuid4().hex[:12]
    limits = FOLLOW_LIMITS.get(body.mode, FOLLOW_LIMITS["normal"])
    # 本场总轮数硬上限 = 主问题数 + 自我介绍追问上限 + 其余题追问上限之和
    max_turns = len(plan) + limits["intro"] + limits["other"] * max(0, len(plan) - 1)
    SESSIONS[sid] = {
        "id": sid,
        "user_id": user["id"],
        "kind": kind,
        "role": body.role,
        "mode": body.mode,
        # The raw resume is only used to construct the minimized profile and plan
        # above. It must not enter the durable session record.
        "resume_text": "",
        "profile": candidate_profile,
        "plan": plan,
        **source_meta,
        "max_turns": max_turns,
        "idx": 0,
        "follow_idx": -1,
        "turns": [],
        "memory": {},
        "scores": {"depth": 60, "clarity": 60, "logic": 60, "proof": 60},
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "in_progress",
    }
    try:
        persisted = db.save_session(SESSIONS[sid])
    except db.DatabaseUnavailableError as exc:
        SESSIONS.pop(sid, None)
        raise HTTPException(status_code=503, detail="服务暂时不可用，请稍后重试") from exc
    if db.DB_URL and not persisted:
        SESSIONS.pop(sid, None)
        raise HTTPException(status_code=503, detail="服务暂时不可用，请稍后重试")
    return {
        "session_id": sid,
        "kind": kind,
        "matched_role": matched_role,
        **source_meta,
        "profile": candidate_profile,
        "plan_summary": {
            "track": CS_TRACK,
            "total_questions": len(plan),
            "max_turns": max_turns,
            "steps": [q["step"] for q in plan],
        },
    }


@app.get("/api/roles")
def list_roles() -> dict:
    """知识库已覆盖的岗位（setup 页提示用）。"""
    return {"roles": knowledge_plan.available_roles()}


@app.get("/api/sessions/mine")
def my_sessions(user: dict = Depends(auth.current_user)) -> dict:
    if auth.is_dev(user):
        rows = [{"sid": s["id"], "role": s["role"], "kind": s["kind"], "status": s["status"],
                 "created_at": s["created_at"],
                 "overall": (s.get("report") or {}).get("overall")}
                for s in SESSIONS.values() if s.get("user_id") == user["id"]]
        return {"sessions": sorted(rows, key=lambda r: r["created_at"], reverse=True)}
    rows = db.user_history(user["id"])
    for r in rows:
        r["created_at"] = str(r["created_at"])
    return {"sessions": rows}


@app.get("/api/sessions/{sid}/plan")
def get_plan(sid: str, user: dict = Depends(auth.current_user)) -> dict:
    s = _get(sid, user)
    source_meta = _question_source_metadata(s.get("plan") or [])
    return {
        "session_id": sid,
        "kind": s["kind"],
        "plan": s["plan"],
        "current_idx": s["idx"],
        **source_meta,
    }


@app.get("/api/sessions/{sid}/current")
def get_current(sid: str, user: dict = Depends(auth.current_user)) -> dict:
    s = _get(sid, user)
    q = s["plan"][s["idx"]] if s["idx"] < len(s["plan"]) else None
    source_meta = _question_source_metadata(s.get("plan") or [])
    return {
        "session_id": sid,
        "kind": s["kind"],
        "role": s["role"],
        "status": s["status"],
        **source_meta,
        "question": q["question"] if q else None,
        "focus": _focus(q) if q else None,
        "scores": s["scores"],
        "progress": {"idx": s["idx"], "total": len(s["plan"])},
    }


@app.post("/api/sessions/{sid}/answer")
def submit_answer(sid: str, body: AnswerSubmit, user: dict = Depends(auth.current_user)) -> dict:
    s = _get(sid, user)
    if s["status"] != "in_progress":
        raise HTTPException(status_code=409, detail="面试已结束")
    if not body.answer.strip():
        raise HTTPException(status_code=400, detail="answer 不能为空")

    mutation_lock = _session_mutation_lock(sid)
    if not mutation_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="上一条回答仍在处理中，请勿重复提交")
    try:
        return _submit_answer_locked(sid, body, s)
    finally:
        mutation_lock.release()


def _submit_answer_locked(sid: str, body: AnswerSubmit, s: dict) -> dict:

    # Database-backed sessions must never acknowledge an answer that did not
    # survive the transaction. Keep a local rollback point before evaluation
    # mutates turns, scores, memory and question position.
    before = copy.deepcopy(s) if db.DB_URL else None

    q = s["plan"][s["idx"]]

    limits = FOLLOW_LIMITS.get(s["mode"], FOLLOW_LIMITS["normal"])
    step_cap = limits["intro"] if q["step"] == "自我介绍" else limits["other"]
    # 先把本题剩余追问预算写进 session，评估 prompt 据此决定是否值得追问
    s["_followups_left"] = step_cap - (s["follow_idx"] + 1)

    result = llm.evaluate_turn(s, q, body.answer) or _heuristic_evaluate(s, q, body.answer)
    s["memory"] = interview_memory.merge_memory(s.get("memory"), interview_memory.extract_update(body.answer))

    s["turns"].append({
        "step": q["step"],
        "question": q["question"],
        "answer": body.answer,
        "is_followup": result["is_followup_response"],
        "feedback": result["feedback"],
        "delta": result["delta"],
    })
    for k, d in result["delta"].items():
        s["scores"][k] = max(0, min(_score_ceiling(s), s["scores"][k] + d))

    followups = q.get("followups", [])
    used = s["follow_idx"] + 1  # 本题已追问次数
    custom = (result.get("custom_followup") or "").strip()
    # 三重硬上限：①本题追问次数达上限（自我介绍 1 次 / 其它按模式）；
    # ②全场总轮数达上限；③没有可用的追问内容（LLM 自定义追问或题库模板）。
    # 之前 custom 追问绕开了上限判断，会一直追问卡在原地，这里统一收口。
    has_material = bool(custom) or used < len(followups)
    can_follow = used < step_cap and len(s["turns"]) < s["max_turns"] and has_material
    want_follow = bool(result["need_followup"]) and can_follow

    if want_follow:
        s["follow_idx"] += 1
        interviewer_msg = custom or followups[s["follow_idx"]]
        next_focus = _focus(q)
        follow_progress = {"used": s["follow_idx"] + 1, "cap": step_cap}
    else:
        s["idx"] += 1
        s["follow_idx"] = -1
        follow_progress = None
        if s["idx"] < len(s["plan"]):
            _prioritize_remaining_questions(s)
            nq = s["plan"][s["idx"]]
            interviewer_msg = nq["question"]
            next_focus = _focus(nq)
        else:
            s["status"] = "finished"
            interviewer_msg = "本场面试到此结束，正在为你生成复盘报告。"
            next_focus = None

    try:
        persisted = db.save_turn_and_session(s, s["turns"][-1])
    except db.DatabaseUnavailableError as exc:
        if before is not None:
            s.clear()
            s.update(before)
        raise HTTPException(status_code=503, detail="面试进度暂未保存，请稍后重试") from exc
    if db.DB_URL and not persisted:
        if before is not None:
            s.clear()
            s.update(before)
        raise HTTPException(status_code=503, detail="面试进度暂未保存，请稍后重试")
    return {
        "feedback": result["feedback"],
        "degraded": result.get("generated_by") != "llm",
        "interviewer_message": interviewer_msg,
        "is_followup": want_follow,
        "follow_progress": follow_progress,
        "next_focus": next_focus,
        "scores": s["scores"],
        "progress": {"idx": s["idx"], "total": len(s["plan"]),
                     "turns": len(s["turns"]), "max_turns": s["max_turns"]},
        "done": s["status"] == "finished",
    }


@app.post("/api/sessions/{sid}/finish")
def finish(sid: str, user: dict = Depends(auth.current_user)) -> dict:
    s = _get(sid, user)
    mutation_lock = _session_mutation_lock(sid)
    if not mutation_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="上一条回答仍在处理中，请稍后再结束面试")
    try:
        return _finish_locked(sid, s)
    finally:
        mutation_lock.release()


def _finish_locked(sid: str, s: dict) -> dict:
    if s.get("report"):
        return {
            "session_id": sid,
            "report": s["report"],
            "degraded": s["report"].get("generated_by") != "llm",
            "coverage": _interview_coverage(s),
            "already_finished": True,
        }
    before = copy.deepcopy(s) if db.DB_URL else None
    s["status"] = "finished"
    coverage = _interview_coverage(s)
    # Models are useful for a full interview narrative, but should not infer a
    # comprehensive assessment from a thin early exit. The deterministic
    # report stays evidence-bound until at least two main questions exist.
    report = _heuristic_report(s) if coverage["answered_main_questions"] < 2 else (
        llm.build_report(s) or _heuristic_report(s)
    )
    if coverage["is_early_exit"] and coverage["answered_main_questions"] > 0:
        scope_risk = (
            f"本次只覆盖 {coverage['answered_main_questions']}/{coverage['planned_main_questions']} 道主问题，"
            "尚未覆盖的环节不能据此判断，建议完成一场全流程模拟后再作综合比较。"
        )
        report["risk_points"] = [scope_risk] + [
            item for item in report.get("risk_points") or [] if item != scope_risk
        ]
        report["risk_points"] = report["risk_points"][:3]
    # Scores shown throughout the interview are canonical. The model may write
    # explanations, but cannot silently replace recorded dimensions/overall.
    report["dimensions"] = dict(s["scores"])
    report["overall"] = round(sum(s["scores"].values()) / len(s["scores"]))
    report["stage_summary"] = _stage_summary(s)
    report["radar"] = dict(s["scores"])
    s["report"] = report
    try:
        persisted = db.save_report_and_session(sid, report, s)
    except db.DatabaseUnavailableError as exc:
        if before is not None:
            s.clear()
            s.update(before)
        raise HTTPException(status_code=503, detail="报告暂未保存，请稍后重试") from exc
    if db.DB_URL and not persisted:
        if before is not None:
            s.clear()
            s.update(before)
        raise HTTPException(status_code=503, detail="报告暂未保存，请稍后重试")
    return {
        "session_id": sid,
        "report": report,
        "degraded": report.get("generated_by") != "llm",
        "coverage": coverage,
        "already_finished": False,
    }


@app.get("/api/sessions/{sid}/report")
def get_report(sid: str, user: dict = Depends(auth.current_user)) -> dict:
    s = _get(sid, user)
    if "report" not in s:
        raise HTTPException(status_code=404, detail="报告尚未生成，请先 finish")
    return {"session_id": sid, "report": s["report"], "coverage": _interview_coverage(s)}


@app.post("/api/sessions/{sid}/feedback")
def submit_feedback(sid: str, body: FeedbackBody, user: dict = Depends(auth.current_user)) -> dict:
    s = _get(sid, user)
    if s.get("status") != "finished":
        raise HTTPException(status_code=409, detail="请在完成面试后再提交反馈")
    feedback = {"rating": body.rating, "comment": body.comment.strip()}
    if auth.is_dev(user):
        DEV_FEEDBACK[sid] = feedback
    else:
        try:
            persisted = db.save_feedback(sid, user["id"], **feedback)
        except db.DatabaseUnavailableError as exc:
            raise HTTPException(status_code=503, detail="反馈暂未保存，请稍后重试") from exc
        if db.DB_URL and not persisted:
            raise HTTPException(status_code=503, detail="反馈暂未保存，请稍后重试")
    return {"saved": True}


@app.get("/api/sessions/{sid}/report/export")
def export_report(sid: str, user: dict = Depends(auth.current_user)) -> PlainTextResponse:
    """Download a portable, plain-text review without exposing another user's report."""
    s = _get(sid, user)
    report = s.get("report")
    if not report:
        raise HTTPException(status_code=404, detail="报告尚未生成，请先 finish")
    coverage = _interview_coverage(s)
    lines = [
        "InterviewRadar 面试复盘",
        f"目标岗位：{s.get('role', '模拟面试')}",
        f"生成时间：{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        f"综合评分：{report.get('overall', '—')} / 100",
        f"答题覆盖：{coverage['notice']}",
        "",
        "维度评分：",
    ]
    dimension_names = {"clarity": "表达清晰度", "depth": "技术深度", "logic": "逻辑思维", "proof": "项目可信度"}
    for key, label in dimension_names.items():
        lines.append(f"- {label}：{(report.get('dimensions') or {}).get(key, '—')}")
    for title, values in (("暴露的风险点", report.get("risk_points") or []),
                          ("改进建议", report.get("suggestions") or []),
                          ("一周练习路线", report.get("next_plan") or [])):
        lines.extend(["", f"{title}："])
        lines.extend(f"- {value}" for value in values)
    return PlainTextResponse(
        "\n".join(lines) + "\n",
        headers={"Content-Disposition": f'attachment; filename="interviewradar-{sid[:12]}-report.txt"'},
    )


# ---------- 内部工具 ----------

def _question_source_metadata(plan: list[dict]) -> dict:
    """Describe the current plan honestly for the UI and audit exports.

    A generic seed plan is useful as a practice fallback, but must never be
    presented as if every question came from the traceable interview corpus.
    """
    # Seed questions retain illustrative source labels for UI compatibility, but
    # their empty URLs and explicit placeholder note are not evidence.
    def is_auditable_source(source: dict) -> bool:
        note = str(source.get("note") or "")
        if "占位" in note:
            return False
        # Curated sources have public URLs. A reviewed manual or community
        # record can deliberately omit a public URL for privacy/licensing, but
        # its explicit provenance label remains auditable in the operator DB.
        return bool(source.get("url")) or any(
            marker in note for marker in ("人工导入真题", "社区投稿真题")
        )

    sourced = bool(plan) and all(
        any(is_auditable_source(source) for source in item.get("sources", []))
        for item in plan
    )
    if not sourced:
        return {
            "question_source_mode": "general_fallback",
            "source_backed": False,
            "question_source_notice": (
                "该岗位暂未覆盖可追溯真实面经题库；本场使用通用练习题，"
                "仍会结合你的简历练习项目表达与基础能力。"
            ),
        }
    modes = {
        (item.get("retrieval") or {}).get("retrieval_mode")
        for item in plan
    }
    mode = "hybrid" if "hybrid" in modes else "keyword"
    return {
        "question_source_mode": mode,
        "source_backed": True,
        "question_source_notice": "本场题目来自已覆盖岗位的可追溯面经题库，并结合你的简历定制。",
    }


def _interview_coverage(session: dict) -> dict:
    """Make an early stop explicit instead of dressing it up as full coverage."""
    answered_main = sum(not turn.get("is_followup") for turn in session.get("turns") or [])
    planned = len(session.get("plan") or [])
    return {
        "answered_main_questions": answered_main,
        "planned_main_questions": planned,
        "is_early_exit": answered_main < planned,
        "notice": (
            f"本次只完成了 {answered_main}/{planned} 道主问题，报告更适合用于局部复盘。"
            if answered_main < planned else "本次已完成计划中的全部主问题。"
        ),
    }


def _score_ceiling(session: dict) -> int:
    """Prevent a thin sample from being displayed as a high-confidence top score."""
    answered_main = sum(not turn.get("is_followup") for turn in session.get("turns") or [])
    planned_main = max(1, len(session.get("plan") or []))
    if answered_main < 2:
        return 64
    if answered_main < planned_main:
        return 80
    return 95


def _personalize_fallback_opening(plan: list[dict], role: str, candidate_profile: dict) -> None:
    """Give a seed-plan opening candidate context without fabricating provenance."""
    if not plan or plan[0].get("step") != "自我介绍":
        return
    projects = profile.project_names(candidate_profile)
    skills = [str(item) for item in (candidate_profile.get("skills") or [])[:2]]
    project_clause = f"我看到你简历中提到「{projects[0][:50]}」。" if projects else ""
    skill_clause = f"其中涉及 {'、'.join(skills)}。" if skills else ""
    plan[0]["question"] = (
        f"我们先从你的求职方向聊起：你为什么想做{role}？"
        f"{project_clause}{skill_clause}请用 1–2 分钟讲清楚你在其中负责什么、做成了什么，以及它和这个岗位有什么关联。"
    )
    if projects:
        plan[0]["anchor"] = projects[0][:80]
    plan[0]["opening_personalized"] = bool(projects or skills)

def _get(sid: str, user: dict) -> dict:
    s = SESSIONS.get(sid)
    if not s:
        s = db.load_session(sid)
        if s:
            SESSIONS[sid] = s
    if not s:
        raise HTTPException(status_code=404, detail="session 不存在")
    if not auth.is_dev(user) and s.get("user_id") != user["id"]:
        raise HTTPException(status_code=404, detail="session 不存在")
    return s


def _session_mutation_lock(sid: str) -> threading.Lock:
    """Return one process-local non-blocking lock per active interview session."""
    with _SESSION_GUARD:
        return _SESSION_MUTATION_LOCKS.setdefault(sid, threading.Lock())


def _focus(question: dict) -> dict:
    retrieval = question.get("retrieval") or {}
    return {"step": question["step"], "topic": question["topic"], "anchor": question["anchor"],
            "desc": question["desc"], "retrieval_mode": retrieval.get("retrieval_mode"),
            "matched_profile_fields": retrieval.get("matched_profile_fields", [])}


def _stage_summary(s: dict) -> list[dict]:
    """分环节点评：逐题 delta 聚合（规则实现，与 LLM 报告互补）。"""
    by_step: dict[str, list[dict]] = {}
    order: list[str] = []
    for t in s["turns"]:
        step = t.get("step", "其他")
        if step not in by_step:
            by_step[step] = []
            order.append(step)
        by_step[step].append(t)
    out = []
    for step in order:
        turns = by_step[step]
        nets = [sum(t["delta"].values()) / 4 for t in turns]
        avg = sum(nets) / len(nets)
        if avg >= 2:
            comment = "表现扎实，回答有细节和依据。"
        elif avg >= 0:
            comment = "基本达标，但深度和量化仍可提高。"
        else:
            comment = "明显短板，建议针对性补强后再实战。"
        out.append({"step": step, "questions": len(turns), "avg_delta": round(avg, 1),
                    "comment": comment})
    return out


def _prioritize_remaining_questions(s: dict) -> None:
    """Re-rank only questions in the immediate next interview stage."""
    terms = interview_memory.query_terms(s.get("memory"))
    if not terms or s["idx"] >= len(s["plan"]):
        return
    remaining = s["plan"][s["idx"]:]
    next_stage = remaining[0].get("step")
    stage_count = 0
    for question in remaining:
        if question.get("step") != next_stage:
            break
        stage_count += 1
    stage_questions = remaining[:stage_count]
    def score(question):
        text = " ".join(str(question.get(key, "")) for key in ("question", "topic", "desc"))
        return sum(term.lower() in text.lower() for term in terms)
    best_index = max(range(len(stage_questions)), key=lambda index: score(stage_questions[index]))
    if score(stage_questions[best_index]) > score(stage_questions[0]):
        stage_questions.insert(0, stage_questions.pop(best_index))
        s["plan"][s["idx"]:s["idx"] + stage_count] = stage_questions


# ---------- 启发式兜底（无 LLM key 时保证闭环可用） ----------

def _heuristic_evaluate(s: dict, q: dict, answer: str) -> dict:
    length = len(answer)
    has_number = bool(re.search(r"\d+(?:\.\d+)?(?:%|ms|秒|人|次|万|k)?", answer))
    has_tradeoff = any(w in answer for w in ["权衡", "但是", "不过", "取舍", "缺点", "对比", "trade"])
    has_detail = any(w in answer for w in [
        "项目", "系统", "接口", "数据库", "缓存", "索引", "模型", "用户", "指标",
        "负责", "实现", "优化", "排查", "上线", "测试", "方案", "原因", "结果",
    ])
    has_logic = any(w in answer for w in [
        "首先", "其次", "然后", "因为", "所以", "第一", "第二", "一方面", "另一方面",
        "如果", "因此", "最后", "比如", "例如",
    ])
    # 语音转写常缺标点、漏逻辑连接词，也会把专有名词识别错。
    # 兜底评分只惩罚明显过短或离题，不因口语化文本直接重扣表达。
    # The fallback has no semantic model. Reward only a compact combination of
    # structure, concrete detail and a meaningful metric, never keyword volume.
    strong_evidence = has_number and has_detail
    delta = {
        "depth": 2 if length > 120 and strong_evidence else (1 if length > 60 and has_detail else (-2 if length <= 35 else 0)),
        "clarity": 2 if length > 50 and has_logic else (-1 if length < 20 else 0),
        "logic": 2 if has_logic and (has_detail or length > 80) else (1 if has_detail and length > 60 else 0),
        "proof": 2 if strong_evidence else (1 if has_tradeoff or has_detail else -1),
    }
    need_followup = length < 60 or not (has_number or has_detail)
    if length < 60:
        feedback = "我会按语义理解你的回答；目前内容偏简略，建议补充具体场景、动作或结果。"
    elif not has_number:
        feedback = "回答方向可以，若有量化结果或具体例子会更有说服力。"
    else:
        feedback = "回答较完整，继续深入。"
    return {
        "delta": delta,
        "need_followup": need_followup,
        "feedback": feedback,
        "is_followup_response": s["follow_idx"] >= 0,
        "generated_by": "heuristic",
    }


def _heuristic_report(s: dict) -> dict:
    sc = s["scores"]
    overall = round(sum(sc.values()) / len(sc))
    turns = s.get("turns") or []
    if not turns:
        return {
            "overall": overall,
            "dimensions": dict(sc),
            "risk_points": ["本场尚未回答主问题，当前评分仅是初始基线，不能据此判断具体能力短板。"],
            "suggestions": ["下次至少完成 1 道主问题，并说明你的场景、动作、结果和验证方式，再获取有依据的复盘。"],
            "next_plan": [
                "先准备 1 个最能代表你的项目：背景、职责、关键动作、结果与验证方式。",
                "完成一场轻量模拟，优先获得第一轮真实回答反馈。",
            ],
            "generated_by": "heuristic",
        }
    answers = "\n".join(str(turn.get("answer") or "") for turn in turns)
    has_number = bool(re.search(r"\d+(?:\.\d+)?(?:%|ms|秒|人|次|万|k)?", answers))
    has_tradeoff = any(word in answers for word in ("权衡", "取舍", "对比", "但是", "不过", "成本"))
    has_diagnosis = any(word in answers for word in ("排查", "定位", "监控", "压测", "验证", "复盘"))
    evidence = _answer_evidence_excerpt(answers)
    risk_points = []
    suggestions = []
    if not has_number:
        risk_points.append("部分回答缺少量化指标（数字、对比数据），容易被追问“具体提升了多少”。")
        suggestions.append("为每个核心项目补一组可核验数字：基线、采取的动作、结果和验证方式。")
    else:
        suggestions.append(f"保留这组量化成果（如“{evidence}”），下次补充指标口径和验证样本，让结果更可追溯。")
    if not has_tradeoff:
        risk_points.append("技术选型缺少备选方案对比，建议准备“为什么不用 XX”的应答。")
        suggestions.append("为每个技术决策准备一个备选方案和取舍：成本、风险、性能及最终选择理由。")
    else:
        suggestions.append("继续把方案取舍讲成“约束—备选—决策—风险控制”的因果链，避免只报结论。")
    if not has_diagnosis:
        risk_points.append("项目难点的排查过程描述偏简略，可补充定位思路和时间线。")
        suggestions.append("补充一次问题定位过程：先看什么信号、如何缩小范围、怎样验证修复有效。")
    else:
        suggestions.append("把排查过程按时间线讲清楚：异常信号、定位假设、验证动作与最终复盘。")
    if not risk_points:
        risk_points.append("本场回答已有量化、取舍和验证证据；下一次可选择更复杂的边界条件来检验稳定性。")
    return {
        "overall": overall,
        "dimensions": dict(sc),
        "risk_points": risk_points[:3],
        "suggestions": suggestions[:3],
        "next_plan": [
            "第 1-2 天：整理 2 个核心项目的 STAR 话术并录音自评",
            "第 3-4 天：按本次暴露的短板刷对应基础知识题",
            "第 5 天：再做一场高压模式模拟面试检验改进",
            "第 6-7 天：复盘真实面经社区中同岗位题目，查漏补缺",
        ],
        "generated_by": "heuristic",
    }


def _answer_evidence_excerpt(answers: str) -> str:
    """Return a short non-sensitive evidence phrase for a local fallback report."""
    match = re.search(r"[^。；，]{0,24}\d+(?:\.\d+)?(?:%|ms|秒|人|次|万|k)?[^。；，]{0,36}", answers)
    return (match.group(0).strip() if match else "已有项目结果")[:70]


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8000")),
    )
