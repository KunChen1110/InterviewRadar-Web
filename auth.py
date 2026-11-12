"""账号体系 + 免费无限次面试 + 面经投稿。

- 注册/登录：邮箱+密码（pbkdf2 哈希），Bearer token（30 天）
- 面经投稿：规则预检后进入运营审核；通过后贡献值+10，题目反哺 questions 表
- 无 DATABASE_URL 时降级为匿名开发模式，便于本地调试
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import uuid
import csv
import io
from datetime import date, datetime, timedelta, timezone

import db
import embeddings
import knowledge_plan
import profile
import rag_schema
from config import load_settings
from fastapi import APIRouter, Depends, File, Header, HTTPException, UploadFile
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field, field_validator

router = APIRouter(prefix="/api")

DEV_USER = {"id": "anon", "email": "dev@local", "nickname": "开发者", "contribution": 0}

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PBKDF2_ITERATIONS = 600_000


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ---------- 密码与 token ----------

def _hash_password(pw: str) -> str:
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), _PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${_PBKDF2_ITERATIONS}${salt}${dk.hex()}"


def _verify_password(pw: str, stored: str) -> bool:
    try:
        parts = stored.split("$")
        if len(parts) == 4 and parts[0] == "pbkdf2_sha256":
            _, raw_iterations, salt, expect = parts
            iterations = int(raw_iterations)
        elif len(parts) == 2:  # Compatibility with pre-hardening accounts.
            salt, expect = parts
            iterations = 100_000
        else:
            return False
        dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), iterations)
        return hmac.compare_digest(dk.hex(), expect)
    except Exception:
        return False


def _issue_token(user_id: str) -> str | None:
    token = secrets.token_hex(24)
    exp = datetime.now(timezone.utc) + timedelta(days=30)
    persisted = db._execute(
        "INSERT INTO auth_tokens (token, user_id, expires_at) VALUES (%s,%s,%s)",
        (_token_digest(token), user_id, exp),
    )
    return token if persisted or not db.DB_URL else None


def _persistence_unavailable() -> HTTPException:
    return HTTPException(status_code=503, detail="服务暂时不可用，请稍后重试")


def current_user(authorization: str = Header(default="")) -> dict:
    """FastAPI 依赖：解析 Bearer token 返回用户；无库模式返回匿名开发者。"""
    if not db.DB_URL:
        return dict(DEV_USER)
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="请先登录")
    token = authorization[7:].strip()
    token_digest = _token_digest(token)
    row = db.fetchone(
        "SELECT u.id, u.email, u.nickname, u.credits, u.contribution, u.trial_used "
        "FROM auth_tokens t JOIN users u ON u.id = t.user_id "
        "WHERE t.token IN (%s, %s) AND t.expires_at > now()",
        (token_digest, token),
    )
    if not row:
        raise HTTPException(status_code=401, detail="登录已过期，请重新登录")
    return row


def is_dev(user: dict) -> bool:
    return user["id"] == DEV_USER["id"]


def require_admin(user: dict = Depends(current_user)) -> dict:
    if is_dev(user) and not load_settings().is_production:
        return user
    if user.get("email", "").lower() not in load_settings().admin_emails:
        raise HTTPException(status_code=403, detail="需要运营管理员权限")
    return user


# ---------- 注册 / 登录 / 我 ----------

class AuthBody(BaseModel):
    email: str = Field(max_length=320)
    password: str = Field(max_length=256)
    nickname: str = Field(default="", max_length=80)


class AccountDeletionBody(BaseModel):
    confirmation: str = Field(max_length=32)


class PasswordChangeBody(BaseModel):
    current_password: str = Field(max_length=256)
    new_password: str = Field(max_length=256)


@router.post("/auth/register")
def register(body: AuthBody) -> dict:
    email = body.email.strip().lower()
    if not EMAIL_RE.match(email):
        raise HTTPException(status_code=400, detail="邮箱格式不正确")
    if len(body.password) < 8:
        raise HTTPException(status_code=400, detail="密码至少 8 位")
    if db.fetchone("SELECT id FROM users WHERE LOWER(email) = %s", (email,)):
        raise HTTPException(status_code=409, detail="该邮箱已注册，请直接登录")
    uid = uuid.uuid4().hex[:12]
    nickname = body.nickname.strip() or email.split("@")[0]
    token = secrets.token_hex(24)
    exp = datetime.now(timezone.utc) + timedelta(days=30)
    try:
        created = db.create_user_and_token(
            uid, email, _hash_password(body.password), nickname,
            _token_digest(token), exp,
        )
    except db.AccountExistsError as exc:
        raise HTTPException(status_code=409, detail="该邮箱已注册，请直接登录") from exc
    except db.DatabaseUnavailableError as exc:
        raise _persistence_unavailable() from exc
    if db.DB_URL and not created:
        raise _persistence_unavailable()
    # The transaction above already made the account and token durable. Do not risk
    # losing that token to a second, unnecessary database read during an outage.
    return {"token": token, "user": _public_user_from_row({
        "id": uid, "email": email, "nickname": nickname,
        "credits": 0, "contribution": 0, "trial_used": 0,
    }),
            "message": "注册成功，完整模拟面试免费不限次"}


@router.post("/auth/login")
def login(body: AuthBody) -> dict:
    email = body.email.strip().lower()
    row = db.fetchone(
        "SELECT id, email, nickname, credits, contribution, trial_used, password_hash "
        "FROM users WHERE LOWER(email) = %s",
        (email,),
    )
    if not row or not _verify_password(body.password, row["password_hash"]):
        raise HTTPException(status_code=401, detail="邮箱或密码错误")
    token = _issue_token(row["id"])
    if not token:
        raise _persistence_unavailable()
    return {"token": token, "user": _public_user_from_row(row)}


@router.post("/auth/logout")
def logout(authorization: str = Header(default="")) -> dict:
    if authorization.startswith("Bearer "):
        token = authorization[7:].strip()
        persisted = db._execute("DELETE FROM auth_tokens WHERE token IN (%s, %s)", (_token_digest(token), token))
        if db.DB_URL and not persisted:
            raise _persistence_unavailable()
    return {"ok": True}


def _public_user(uid: str) -> dict:
    u = db.fetchone(
        "SELECT id, email, nickname, credits, contribution, trial_used FROM users WHERE id = %s",
        (uid,),
    ) or {}
    return u


def _public_user_from_row(row: dict) -> dict:
    """Return the client-safe account projection from an already trusted DB row."""
    return {
        "id": row["id"],
        "email": row["email"],
        "nickname": row.get("nickname") or row["email"].split("@")[0],
        "credits": row.get("credits", 0),
        "contribution": row.get("contribution", 0),
        "trial_used": row.get("trial_used", 0),
    }


@router.get("/auth/me")
def me(user: dict = Depends(current_user)) -> dict:
    history = [] if is_dev(user) else db.user_history(user["id"])
    return {"user": user, "history": history, "unlimited_access": True}


@router.delete("/account/data")
def delete_account_data(body: AccountDeletionBody, user: dict = Depends(current_user)) -> dict:
    """Permanently erase the signed-in account and its interview data.

    The explicit phrase prevents accidental deletion from a stray browser request.
    """
    if body.confirmation != "DELETE_MY_ACCOUNT":
        raise HTTPException(status_code=400, detail="请填写确认短语 DELETE_MY_ACCOUNT")
    if is_dev(user):
        # Import lazily to avoid the module cycle during application startup.
        import main
        main.SESSIONS = {
            sid: session for sid, session in main.SESSIONS.items()
            if session.get("user_id") != user["id"]
        }
        main.DEV_FEEDBACK.clear()
        return {"deleted": True, "mode": "development"}
    try:
        deleted = db.delete_user_data(user["id"])
    except db.DatabaseUnavailableError as exc:
        raise _persistence_unavailable() from exc
    if db.DB_URL and not deleted:
        raise _persistence_unavailable()
    return {"deleted": True}


def change_password(body: PasswordChangeBody, user: dict) -> dict:
    if len(body.new_password) < 8:
        raise HTTPException(status_code=400, detail="新密码至少 8 位")
    if is_dev(user):
        return {"changed": True, "mode": "development"}
    row = db.fetchone("SELECT password_hash FROM users WHERE id = %s", (user["id"],))
    if not row or not _verify_password(body.current_password, row["password_hash"]):
        raise HTTPException(status_code=400, detail="当前密码不正确")
    try:
        changed = db.change_password_and_revoke_tokens(user["id"], _hash_password(body.new_password))
    except db.DatabaseUnavailableError as exc:
        raise _persistence_unavailable() from exc
    if db.DB_URL and not changed:
        raise _persistence_unavailable()
    return {"changed": True}


@router.post("/account/password")
def change_password_endpoint(body: PasswordChangeBody, user: dict = Depends(current_user)) -> dict:
    return change_password(body, user)


@router.get("/account/data/export")
def export_account_data(user: dict = Depends(current_user)) -> PlainTextResponse:
    """Download the caller's data only; raw resumes are never part of the export."""
    if is_dev(user):
        import main
        sessions = [
            {key: value for key, value in session.items() if key != "resume_text"}
            for session in main.SESSIONS.values() if session.get("user_id") == user["id"]
        ]
        payload = {
            "user": {key: user.get(key) for key in ("id", "email", "nickname", "contribution")},
            "sessions": sessions,
            "feedback": {sid: value for sid, value in main.DEV_FEEDBACK.items() if sid in {item["id"] for item in sessions}},
            "submissions": [],
        }
    else:
        payload = db.export_user_data(user["id"])
    return PlainTextResponse(
        json.dumps(payload, ensure_ascii=False, default=str, indent=2) + "\n",
        media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="interviewradar-my-data.json"'},
    )


# ---------- 面经投稿（换 token / 反哺题库） ----------

class SubmissionCreate(BaseModel):
    company: str = Field(max_length=120)
    role: str = Field(max_length=80)
    interview_date: date | None = None
    rounds: str = Field(default="", max_length=120)
    questions: list[str] = Field(min_length=3, max_length=20)
    content_rights_confirmed: bool = Field(default=False, validate_default=True)

    @field_validator("content_rights_confirmed")
    @classmethod
    def require_content_rights(cls, confirmed: bool) -> bool:
        if not confirmed:
            raise ValueError("请确认你有权提交这些内容，并同意匿名化题型复用")
        return confirmed

    @field_validator("questions")
    @classmethod
    def validate_questions(cls, questions: list[str]) -> list[str]:
        if any(len(str(question).strip()) > 1000 for question in questions):
            raise ValueError("单道题目不能超过 1000 字")
        return questions


def _submission_rows(row: dict, drafts: list[dict] | None = None) -> list[dict]:
    """Convert protected evidence into safe, abstract question-type import rows."""
    drafts = drafts if drafts is not None else submission_review_material(row)["drafts"]
    source_name = f"用户授权投稿 · {row['role']}"
    return [{
        "question": draft["question"],
        "role_tags": draft["role_tags"],
        "stage": draft["stage"],
        "topic": draft["topic"],
        "source_name": source_name,
        # The protected contribution record is the audit source. Do not create
        # a fake public source URL or expose the candidate's original wording.
        "source_url": "",
        "published_at": str(row.get("interview_date") or ""),
    } for draft in drafts]


def _submission_questions(row: dict) -> list[str]:
    raw = row.get("questions") or []
    values = raw if isinstance(raw, list) else json.loads(raw)
    return [str(question).strip() for question in values if str(question).strip()]


def _abstract_question(question: str) -> str:
    """Keep a reusable interview intent, never an identifying submission preface."""
    text = profile.redact_contact_details(question, limit=1000)
    text = re.sub(r"(?:联系(?:方式)?|电话|邮箱|微信|个人主页|内推码)[^，,；;。]{0,120}[，,；;。]", "", text)
    match = re.search(r"(?:请|如何|当|如果|怎样|什么|为什么|介绍|设计|讲讲)", text)
    if match:
        text = text[match.start():]
    return re.sub(r"\s+", " ", text).strip(" ，,;；：:")[:1000]


def submission_review_material(row: dict) -> dict:
    """Derive private redacted evidence and publication-safe draft question types."""
    role = str(row.get("role") or "").strip()
    redacted_questions = [profile.redact_contact_details(question, limit=1000) for question in _submission_questions(row)]
    drafts = []
    for question in redacted_questions:
        abstract = _abstract_question(question)
        if not abstract:
            continue
        drafts.append({
            "question": abstract,
            "role_tags": [role] if role else [],
            "stage": knowledge_plan._classify_stage(abstract),
            "topic": "社区共建题型",
            "content_hash": rag_schema.content_hash(abstract),
        })
    return {"redacted_questions": redacted_questions, "drafts": drafts}


def _approve_submission_questions(row: dict, note: str, drafts: list[dict] | None = None) -> int:
    """Validate then atomically import, approve and award a community post."""
    rows = _submission_rows(row, drafts)
    payload = json.dumps(rows, ensure_ascii=False).encode("utf-8")
    owner_id = f"moderation-{row['id']}"
    preview = rag_schema.create_preview(
        owner_id, f"approved-submission-{row['id']}.json", payload,
        existing_hashes=db.question_hashes([rag_schema.content_hash(item["question"]) for item in rows]),
    )
    if not preview["valid_count"]:
        return 0
    preview_data = rag_schema.get_preview(preview["preview_id"], owner_id, consume=True)
    valid_rows = preview_data["rows"]
    vectors = embeddings.embed_texts([
        " ".join([item["question"], item.get("topic", ""), " ".join(item.get("role_tags") or [])]).strip()
        for item in valid_rows
    ])
    if vectors:
        for item, vector in zip(valid_rows, vectors):
            item["embedding"] = vector
    return db.approve_submission_with_manual_questions(
        row["id"], row["user_id"], valid_rows, uuid.uuid4().hex[:12], note or None,
    )


def _quality_check(body: SubmissionCreate) -> tuple[bool, str]:
    qs = [q.strip() for q in body.questions if q and q.strip()]
    if not body.company.strip() or not body.role.strip():
        return False, "请填写公司和岗位"
    if len(qs) < 3:
        return False, "请至少填写 3 道真实面试题"
    if sum(len(q) for q in qs) < 60:
        return False, "题目内容过短，请补充更完整的描述"
    return True, ""


@router.post("/submissions")
def create_submission(body: SubmissionCreate, user: dict = Depends(current_user)) -> dict:
    if is_dev(user):
        return {"status": "approved", "note": "开发模式：直接通过", "contribution": user["contribution"]}
    ok, note = _quality_check(body)
    qs = [q.strip() for q in body.questions if q and q.strip()]
    try:
        sub_id = db.create_submission(
            user["id"], body.company.strip(), body.role.strip(), body.interview_date,
            body.rounds.strip(), json.dumps(qs, ensure_ascii=False), note or None, body.content_rights_confirmed,
        )
    except db.DatabaseUnavailableError as exc:
        raise _persistence_unavailable() from exc
    if db.DB_URL and sub_id is None:
        raise _persistence_unavailable()
    precheck = "内容预检通过，" if ok else f"内容预检提示：{note}；"
    return {"status": "pending", "submission_id": sub_id,
            "note": precheck + "已进入人工复核队列，通过后将获得 10 点贡献值。"}


@router.get("/submissions/mine")
def my_submissions(user: dict = Depends(current_user)) -> dict:
    rows = [] if is_dev(user) else db.fetchall(
        "SELECT id, company, role, status, quality_note, created_at "
        "FROM submissions WHERE user_id = %s ORDER BY created_at DESC LIMIT 50",
        (user["id"],),
    )
    for r in rows:
        r["created_at"] = str(r["created_at"])
    return {"submissions": rows}


class QuestionDraft(BaseModel):
    question: str = Field(min_length=8, max_length=1000)
    role_tags: list[str] = Field(min_length=1, max_length=8)
    stage: str
    topic: str = Field(default="社区共建题型", min_length=1, max_length=120)

    @field_validator("role_tags")
    @classmethod
    def validate_draft_roles(cls, tags: list[str]) -> list[str]:
        normalized = [str(tag).strip()[:80] for tag in tags if str(tag).strip()]
        if not normalized:
            raise ValueError("role_tags 不能为空")
        return list(dict.fromkeys(normalized))

    @field_validator("stage")
    @classmethod
    def validate_draft_stage(cls, stage: str) -> str:
        if stage not in rag_schema.ALLOWED_STAGES:
            raise ValueError("stage 不合法")
        return stage


class ModerationBody(BaseModel):
    note: str = Field(default="", max_length=1000)
    drafts: list[QuestionDraft] = Field(default_factory=list, max_length=20)


class ImportConfirmBody(BaseModel):
    preview_id: str = Field(min_length=1, max_length=128)


class QuestionStatusBody(BaseModel):
    active: bool


@router.get("/admin/submissions")
def admin_submissions(status: str = "pending", user: dict = Depends(require_admin)) -> dict:
    status = status if status in {"pending", "approved", "rejected"} else "pending"
    rows = db.fetchall(
        "SELECT s.id, s.company, s.role, s.interview_date, s.rounds, s.questions, s.rights_confirmed, "
        "s.status, s.quality_note, s.created_at, u.email AS submitter "
        "FROM submissions s JOIN users u ON u.id = s.user_id WHERE s.status = %s "
        "ORDER BY s.created_at ASC LIMIT 100",
        (status,),
    )
    for row in rows:
        row["created_at"] = str(row["created_at"])
        review = submission_review_material(row)
        row.pop("questions", None)
        row["redacted_questions"] = review["redacted_questions"]
        row["drafts"] = review["drafts"]
    return {"submissions": rows}


@router.get("/admin/metrics")
def admin_metrics(user: dict = Depends(require_admin)) -> dict:
    if is_dev(user):
        import main
        sessions = list(main.SESSIONS.values())
        feedback = list(main.DEV_FEEDBACK.values())
        return {
            "users_total": 1, "users_7d": 1,
            "sessions_total": len(sessions),
            "sessions_finished": sum(item.get("status") == "finished" for item in sessions),
            "sessions_7d": len(sessions),
            "imported_questions": 0, "pending_submissions": 0,
            "feedback_average": round(sum(item["rating"] for item in feedback) / len(feedback), 2) if feedback else None,
            "feedback_count": len(feedback),
        }
    return db.operator_metrics()


@router.get("/admin/questions")
def admin_questions(query: str = "", user: dict = Depends(require_admin)) -> dict:
    if is_dev(user):
        return {"questions": []}
    rows = db.list_manual_questions(query)
    for row in rows:
        row["created_at"] = str(row.get("created_at") or "")
        row["published_at"] = str(row.get("published_at") or "")
    return {"questions": rows}


def set_question_status(question_id: int, active: bool, user: dict) -> dict:
    if is_dev(user):
        return {"id": question_id, "active": active}
    try:
        updated = db.set_question_active(question_id, active)
    except db.DatabaseUnavailableError as exc:
        raise _persistence_unavailable() from exc
    if updated is None and db.DB_URL:
        raise _persistence_unavailable()
    if not updated:
        raise HTTPException(status_code=404, detail="未找到可管理的人工导入题目")
    knowledge_plan.invalidate_db_cache()
    return {"id": question_id, "active": active}


@router.patch("/admin/questions/{question_id}")
def set_question_status_endpoint(question_id: int, body: QuestionStatusBody,
                                 user: dict = Depends(require_admin)) -> dict:
    return set_question_status(question_id, body.active, user)


def question_export_csv(rows: list[dict]) -> str:
    """Create a reviewable CSV that can be edited offline before the next import."""
    output = io.StringIO(newline="")
    fields = ["question", "role_tags", "stage", "topic", "source_name", "source_url", "published_at", "is_active"]
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    for row in rows:
        metadata = row.get("metadata") or {}
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except json.JSONDecodeError:
                metadata = {}
        writer.writerow({
            "question": row.get("question", ""),
            "role_tags": "|".join(row.get("tags") or []),
            "stage": metadata.get("stage", ""),
            "topic": metadata.get("topic", ""),
            "source_name": row.get("source_name", ""),
            "source_url": row.get("source_url", ""),
            "published_at": str(row.get("published_at") or ""),
            "is_active": "true" if row.get("is_active", True) else "false",
        })
    return "\ufeff" + output.getvalue()


@router.get("/admin/questions/export")
def export_admin_questions(user: dict = Depends(require_admin)) -> PlainTextResponse:
    rows = [] if is_dev(user) else db.list_manual_questions()
    return PlainTextResponse(
        question_export_csv(rows),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="interviewradar-manual-questions.csv"'},
    )


def _moderate_submission(submission_id: int, status: str, note: str, drafts: list[dict] | None = None) -> dict:
    row = db.fetchone(
        "SELECT id, user_id, company, role, interview_date, questions, status, rights_confirmed "
        "FROM submissions WHERE id = %s",
        (submission_id,),
    )
    if not row:
        raise HTTPException(status_code=404, detail="投稿不存在")
    if row["status"] != "pending":
        raise HTTPException(status_code=409, detail="该投稿已处理")
    if status == "rejected" and not note.strip():
        raise HTTPException(status_code=400, detail="驳回时请填写原因")
    if status == "approved":
        if not row.get("rights_confirmed"):
            raise HTTPException(status_code=409, detail="该投稿未记录内容复用授权，不能发布到题库")
        try:
            inserted = (
                _approve_submission_questions(row, note.strip(), drafts)
                if drafts else _approve_submission_questions(row, note.strip())
            )
        except db.DatabaseUnavailableError as exc:
            raise _persistence_unavailable() from exc
        if inserted is None and db.DB_URL:
            raise _persistence_unavailable()
        if inserted <= 0:
            raise HTTPException(status_code=400, detail="投稿没有可写入题库的有效题目，请驳回或补充后再审核")
        knowledge_plan.invalidate_db_cache()
    else:
        try:
            persisted = db.reject_pending_submission(submission_id, note.strip() or None)
        except db.DatabaseUnavailableError as exc:
            raise _persistence_unavailable() from exc
        if persisted is None and db.DB_URL:
            raise _persistence_unavailable()
        if not persisted:
            raise HTTPException(status_code=409, detail="该投稿已处理")
    return {"id": submission_id, "status": status, "note": note.strip()}


@router.post("/admin/submissions/{submission_id}/approve")
def approve_submission(submission_id: int, body: ModerationBody, user: dict = Depends(require_admin)) -> dict:
    return _moderate_submission(submission_id, "approved", body.note, [draft.model_dump() for draft in body.drafts])


@router.post("/admin/submissions/{submission_id}/reject")
def reject_submission(submission_id: int, body: ModerationBody, user: dict = Depends(require_admin)) -> dict:
    return _moderate_submission(submission_id, "rejected", body.note)


def confirm_question_import(user: dict, preview_id: str) -> dict:
    # Keep the reviewed preview available until the transaction has committed.
    # An RDS outage should be safely retryable without making an operator upload
    # and review the same batch again.
    preview = rag_schema.get_preview(preview_id, user["id"])
    rows = preview["rows"]
    if not rows:
        raise HTTPException(status_code=400, detail="预览中没有可导入题目")
    existing = db.question_hashes([row["content_hash"] for row in rows])
    rows = [row for row in rows if row["content_hash"] not in existing]
    if not rows:
        return {"inserted_count": 0, "skipped_duplicates": len(preview["rows"])}
    vectors = embeddings.embed_texts([
        " ".join([row["question"], row.get("topic", ""), " ".join(row.get("role_tags") or [])]).strip()
        for row in rows
    ])
    if vectors:
        for row, vector in zip(rows, vectors):
            row["embedding"] = vector
    batch_id = uuid.uuid4().hex[:12]
    try:
        inserted = db.insert_manual_questions(rows, batch_id)
    except db.DatabaseUnavailableError as exc:
        raise _persistence_unavailable() from exc
    if inserted is None and db.DB_URL:
        raise _persistence_unavailable()
    rag_schema.get_preview(preview_id, user["id"], consume=True)
    knowledge_plan.invalidate_db_cache()
    return {"inserted_count": inserted, "skipped_duplicates": len(preview["rows"]) - len(rows), "batch_id": batch_id}


@router.post("/admin/question-imports/preview")
async def preview_question_import(file: UploadFile = File(...), user: dict = Depends(require_admin)) -> dict:
    payload = await file.read()
    try:
        # Existing DB hashes are checked once the preview knows candidate hashes;
        # query all candidate hashes after an inexpensive preliminary parse.
        preview = rag_schema.create_preview(user["id"], file.filename or "", payload, existing_hashes=set())
        existing = db.question_hashes([row["content_hash"] for row in preview["rows"]])
        if existing:
            preview = rag_schema.create_preview(user["id"], file.filename or "", payload, existing_hashes=existing)
        return preview
    except rag_schema.ImportValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/admin/question-imports/confirm")
def confirm_question_import_endpoint(body: ImportConfirmBody, user: dict = Depends(require_admin)) -> dict:
    try:
        return confirm_question_import(user, body.preview_id)
    except rag_schema.PreviewAccessError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/community/posts")
def community_posts(page: int = 1, role: str = "") -> dict:
    """Community shows published question types, never protected submission bodies."""
    page = max(1, min(page, 100))
    role = role.strip()
    where, params = "WHERE source_type = 'manual_import' AND source_name LIKE %s AND is_active = true", ["用户授权投稿 ·%"]
    if role:
        where += " AND tags::text ILIKE %s"
        params.append(f"%{role}%")
    rows = db.fetchall(
        f"SELECT question, tags, source_name, published_at, created_at FROM questions "
        f"{where} ORDER BY created_at DESC LIMIT 500",
        tuple(params),
    )
    posts = []
    for r in rows:
        posts.append({
            "company": "用户共建题型", "role": (r.get("tags") or [""])[0],
            "interview_date": str(r.get("published_at") or ""), "rounds": "人工审核后发布",
            "questions": [r["question"]], "created_at": str(r.get("created_at") or ""),
            "author": "匿名投稿者", "curated": False,
        })
    posts.extend(knowledge_plan.curated_posts(role))
    posts.sort(key=lambda p: p.get("created_at") or "", reverse=True)
    total = len(posts)
    start = (page - 1) * 10
    return {"posts": posts[start:start + 10], "page": page, "total": total}
