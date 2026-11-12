"""数据库层：连平台注入的 DATABASE_URL（Postgres + pgvector）。

未注入 DATABASE_URL 时静默降级为"无库模式"（骨架阶段允许），
会话由 main.py 进程内字典保存，保证 demo 闭环可用。
"""
from __future__ import annotations

import json
import os
import time

DB_URL = os.environ.get("DATABASE_URL", "")
_conn = None
_schema_ready = not bool(DB_URL)
_schema_next_retry_at = 0.0
_vector_extension_available = False
_SCHEMA_RETRY_SECONDS = 5.0


class DatabaseUnavailableError(RuntimeError):
    """The configured persistence service could not complete an operation."""


class AccountExistsError(RuntimeError):
    """A concurrent registration won the database's case-insensitive unique slot."""


def _log_failure(operation: str, exc: Exception) -> None:
    """Keep operational diagnostics useful without writing DSNs or user data to logs."""
    print(f"[db] {operation}失败（{type(exc).__name__}）")

DDL = [
    """CREATE TABLE IF NOT EXISTS questions (
        id bigserial PRIMARY KEY,
        track text NOT NULL,
        question text NOT NULL,
        answer_hints text,
        tags text[],
        source_name text,
        source_url text,
        published_at date,
        embedding vector(1536),
        created_at timestamptz DEFAULT now()
    )""",
    """CREATE TABLE IF NOT EXISTS sessions (
        id text PRIMARY KEY,
        role text,
        mode text,
        resume_text text,
        profile jsonb,
        plan jsonb,
        status text DEFAULT 'in_progress',
        created_at timestamptz DEFAULT now()
    )""",
    """CREATE TABLE IF NOT EXISTS turns (
        id bigserial PRIMARY KEY,
        session_id text NOT NULL,
        question text,
        answer text,
        is_followup boolean DEFAULT false,
        feedback text,
        delta jsonb,
        created_at timestamptz DEFAULT now()
    )""",
    """CREATE TABLE IF NOT EXISTS reports (
        session_id text PRIMARY KEY,
        report jsonb,
        created_at timestamptz DEFAULT now()
    )""",
    """CREATE TABLE IF NOT EXISTS session_feedback (
        session_id text PRIMARY KEY,
        user_id text NOT NULL,
        rating integer NOT NULL CHECK (rating BETWEEN 1 AND 5),
        comment text NOT NULL DEFAULT '',
        created_at timestamptz DEFAULT now(),
        updated_at timestamptz DEFAULT now()
    )""",
    """CREATE TABLE IF NOT EXISTS users (
        id text PRIMARY KEY,
        email text UNIQUE NOT NULL,
        password_hash text NOT NULL,
        nickname text,
        credits int DEFAULT 0,
        contribution int DEFAULT 0,
        trial_used int DEFAULT 0,
        created_at timestamptz DEFAULT now()
    )""",
    """CREATE TABLE IF NOT EXISTS auth_tokens (
        token text PRIMARY KEY,
        user_id text NOT NULL,
        expires_at timestamptz NOT NULL,
        created_at timestamptz DEFAULT now()
    )""",
    """CREATE TABLE IF NOT EXISTS orders (
        id text PRIMARY KEY,
        user_id text NOT NULL,
        plan text NOT NULL,
        amount numeric NOT NULL,
        credits int NOT NULL,
        status text DEFAULT 'pending',
        created_at timestamptz DEFAULT now()
    )""",
    """CREATE TABLE IF NOT EXISTS submissions (
        id bigserial PRIMARY KEY,
        user_id text NOT NULL,
        company text,
        role text,
        interview_date date,
        rounds text,
        questions jsonb,
        status text DEFAULT 'pending',
        quality_note text,
        rights_confirmed boolean NOT NULL DEFAULT false,
        created_at timestamptz DEFAULT now()
    )""",
    # 兼容旧库的增量列
    "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS user_id text",
    "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS kind text DEFAULT 'full'",
    "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS current_idx integer NOT NULL DEFAULT 0",
    "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS follow_idx integer NOT NULL DEFAULT -1",
    "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS scores jsonb NOT NULL DEFAULT '{\"depth\":60,\"clarity\":60,\"logic\":60,\"proof\":60}'::jsonb",
    "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS max_turns integer NOT NULL DEFAULT 0",
    "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now()",
    "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS memory jsonb NOT NULL DEFAULT '{}'::jsonb",
    "ALTER TABLE turns ADD COLUMN IF NOT EXISTS step text",
    "ALTER TABLE questions ADD COLUMN IF NOT EXISTS content_hash text",
    "ALTER TABLE questions ADD COLUMN IF NOT EXISTS source_type text DEFAULT 'curated'",
    "ALTER TABLE questions ADD COLUMN IF NOT EXISTS import_batch_id text",
    "ALTER TABLE questions ADD COLUMN IF NOT EXISTS metadata jsonb NOT NULL DEFAULT '{}'::jsonb",
    "ALTER TABLE questions ADD COLUMN IF NOT EXISTS is_active boolean NOT NULL DEFAULT true",
    "ALTER TABLE submissions ADD COLUMN IF NOT EXISTS rights_confirmed boolean NOT NULL DEFAULT false",
    "UPDATE sessions SET resume_text = '' WHERE resume_text IS NOT NULL AND resume_text <> ''",
    "CREATE UNIQUE INDEX IF NOT EXISTS questions_content_hash_unique ON questions(content_hash) WHERE content_hash IS NOT NULL",
]


def get_conn():
    global _conn
    if not DB_URL:
        return None
    if _conn is None:
        try:
            import psycopg
            _conn = psycopg.connect(DB_URL, autocommit=True)
        except Exception as exc:
            _log_failure("连接数据库", exc)
            _reset()
            raise DatabaseUnavailableError("数据库暂时不可用") from exc
    return _conn


def init_schema() -> None:
    global _schema_ready, _schema_next_retry_at, _vector_extension_available
    _schema_ready = not bool(DB_URL)
    _schema_next_retry_at = time.monotonic() + _SCHEMA_RETRY_SECONDS if DB_URL else 0.0
    try:
        conn = get_conn()
    except DatabaseUnavailableError:
        print("[db] schema 初始化跳过：数据库暂时不可用")
        return
    if not conn:
        print("[db] DATABASE_URL 未注入，以无库模式运行（骨架阶段）")
        return
    try:
        with conn.cursor() as cur:
            # vector 扩展可能因权限装不上：单独尝试，失败则 embedding 列降级为 text，
            # 不影响其余建表（当前组卷走文件知识库，未用向量检索）
            have_vector = True
            try:
                cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
            except Exception as exc:
                have_vector = False
                print(f"[db] vector 扩展不可用，embedding 列降级为 text（{type(exc).__name__}）")
            _vector_extension_available = have_vector
            failed = []
            for stmt in DDL:
                if not have_vector:
                    stmt = stmt.replace("embedding vector(1536),", "embedding text,")
                try:
                    cur.execute(stmt)
                except Exception as exc:
                    failed.append(f"{stmt.split('(')[0].strip()}（{type(exc).__name__}）")
            if failed:
                print(f"[db] 部分建表失败: {' | '.join(failed)}")
            else:
                print(f"[db] schema 初始化完成（vector={'有' if have_vector else '无'}）")
                _schema_ready = True
    except Exception as exc:  # 建表失败不阻断启动，健康检查仍能过
        _log_failure("schema 初始化", exc)
        _reset()


def is_ready() -> bool:
    """Retry schema setup on readiness probes after a transient database startup failure."""
    if DB_URL and not _schema_ready and time.monotonic() >= _schema_next_retry_at:
        # Managed databases can come online a few seconds after the web process.
        # Retrying here lets the container recover without requiring an operator restart.
        init_schema()
    return _schema_ready and bool(fetchone("SELECT 1 AS one"))


def _reset() -> None:
    global _conn
    try:
        if _conn:
            _conn.close()
    except Exception:
        pass
    _conn = None


def _execute(sql: str, params: tuple) -> bool:
    conn = get_conn()
    if not conn:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
        return True
    except Exception as exc:
        _log_failure("写入", exc)
        _reset()
        raise DatabaseUnavailableError("数据库暂时不可用") from exc


def fetchone(sql: str, params: tuple = ()) -> dict | None:
    conn = get_conn()
    if not conn:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
            if row is None:
                return None
            cols = [d.name if hasattr(d, "name") else d[0] for d in cur.description]
            return dict(zip(cols, row))
    except Exception as exc:
        _log_failure("查询", exc)
        _reset()
        raise DatabaseUnavailableError("数据库暂时不可用") from exc


def fetchall(sql: str, params: tuple = ()) -> list[dict]:
    conn = get_conn()
    if not conn:
        return []
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
            cols = [d.name if hasattr(d, "name") else d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in rows]
    except Exception as exc:
        _log_failure("查询", exc)
        _reset()
        raise DatabaseUnavailableError("数据库暂时不可用") from exc


def _json_value(value, fallback):
    if value is None:
        return fallback
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return fallback
    return value


def _session_params(s: dict, resume_text: str | None = None) -> tuple:
    return (
        s["id"], s["role"], s["mode"], resume_text if resume_text is not None else s.get("resume_text", ""),
        json.dumps(s["profile"], ensure_ascii=False),
        json.dumps(s["plan"], ensure_ascii=False), s["status"],
        s.get("user_id"), s.get("kind", "full"), s.get("idx", 0), s.get("follow_idx", -1),
        json.dumps(s.get("scores", {})), s.get("max_turns", 0), json.dumps(s.get("memory", {})),
    )


def _save_session_sql() -> str:
    return (
        "INSERT INTO sessions (id, role, mode, resume_text, profile, plan, status, user_id, kind, "
        "current_idx, follow_idx, scores, max_turns, memory) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
        "ON CONFLICT (id) DO UPDATE SET "
        "role=EXCLUDED.role, mode=EXCLUDED.mode, resume_text=EXCLUDED.resume_text, "
        "profile=EXCLUDED.profile, plan=EXCLUDED.plan, status=EXCLUDED.status, "
        "user_id=EXCLUDED.user_id, kind=EXCLUDED.kind, current_idx=EXCLUDED.current_idx, "
        "follow_idx=EXCLUDED.follow_idx, scores=EXCLUDED.scores, max_turns=EXCLUDED.max_turns, "
        "memory=EXCLUDED.memory, updated_at=now()"
    )


def save_session(s: dict, resume_text: str | None = None) -> bool:
    return _execute(_save_session_sql(), _session_params(s, resume_text))


def _turn_params(session_id: str, turn: dict) -> tuple:
    return (
        session_id, turn.get("step", "其他"), turn["question"], turn["answer"], turn["is_followup"],
        turn["feedback"], json.dumps(turn["delta"]),
    )


def _save_turn_sql() -> str:
    return (
        "INSERT INTO turns (session_id, step, question, answer, is_followup, feedback, delta) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s)"
    )


def save_turn(session_id: str, turn: dict) -> bool:
    return _execute(_save_turn_sql(), _turn_params(session_id, turn))


def _report_params(session_id: str, report: dict) -> tuple:
    return (session_id, json.dumps(report, ensure_ascii=False))


def _save_report_sql() -> str:
    return (
        "INSERT INTO reports (session_id, report) VALUES (%s,%s) "
        "ON CONFLICT (session_id) DO UPDATE SET report = EXCLUDED.report"
    )


def save_report(session_id: str, report: dict) -> bool:
    return _execute(_save_report_sql(), _report_params(session_id, report))


def save_feedback(session_id: str, user_id: str, rating: int, comment: str) -> bool:
    return _execute(
        "INSERT INTO session_feedback (session_id, user_id, rating, comment) VALUES (%s,%s,%s,%s) "
        "ON CONFLICT (session_id) DO UPDATE SET rating=EXCLUDED.rating, comment=EXCLUDED.comment, updated_at=now()",
        (session_id, user_id, rating, comment),
    )


def create_user_and_token(
    user_id: str, email: str, password_hash: str, nickname: str, token_digest: str, expires_at,
) -> bool:
    """Create a usable account only when its first bearer token is durable too."""
    conn = get_conn()
    if not conn:
        return False
    try:
        with conn.transaction(), conn.cursor() as cur:
            cur.execute(
                "INSERT INTO users (id, email, password_hash, nickname) VALUES (%s,%s,%s,%s)",
                (user_id, email, password_hash, nickname),
            )
            cur.execute(
                "INSERT INTO auth_tokens (token, user_id, expires_at) VALUES (%s,%s,%s)",
                (token_digest, user_id, expires_at),
            )
        return True
    except Exception as exc:
        # The registration handler pre-checks email for a friendly fast path,
        # but the unique index remains authoritative under concurrent requests.
        if getattr(exc, "sqlstate", None) == "23505":
            raise AccountExistsError("账号已存在") from exc
        _log_failure("创建账号", exc)
        _reset()
        raise DatabaseUnavailableError("数据库暂时不可用") from exc


def create_submission(
    user_id: str, company: str, role: str, interview_date, rounds: str, questions_json: str, quality_note: str | None,
    rights_confirmed: bool,
) -> int | None:
    """Create a reviewable community submission and return its durable ID."""
    conn = get_conn()
    if not conn:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO submissions (user_id, company, role, interview_date, rounds, "
                "questions, status, quality_note, rights_confirmed) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                (user_id, company, role, interview_date or None, rounds, questions_json, "pending", quality_note, rights_confirmed),
            )
            row = cur.fetchone()
        return int(row[0]) if row else None
    except Exception as exc:
        _log_failure("创建投稿", exc)
        _reset()
        raise DatabaseUnavailableError("数据库暂时不可用") from exc


def load_turns(session_id: str) -> list[dict]:
    rows = fetchall(
        "SELECT step, question, answer, is_followup, feedback, delta FROM turns "
        "WHERE session_id = %s ORDER BY id ASC",
        (session_id,),
    )
    return [
        {
            "step": row.get("step") or "其他",
            "question": row["question"],
            "answer": row["answer"],
            "is_followup": row["is_followup"],
            "feedback": row["feedback"],
            "delta": _json_value(row.get("delta"), {}),
        }
        for row in rows
    ]


def load_report(session_id: str) -> dict | None:
    row = fetchone("SELECT report FROM reports WHERE session_id = %s", (session_id,))
    return _json_value(row.get("report"), None) if row else None


def load_session(session_id: str) -> dict | None:
    row = fetchone(
        "SELECT id, role, mode, resume_text, profile, plan, status, user_id, kind, "
        "current_idx, follow_idx, scores, max_turns, memory, created_at "
        "FROM sessions WHERE id = %s",
        (session_id,),
    )
    if not row:
        return None
    session = {
        "id": row["id"],
        "user_id": row.get("user_id"),
        "kind": row.get("kind") or "full",
        "role": row.get("role") or "",
        "mode": row.get("mode") or "normal",
        "resume_text": row.get("resume_text") or "",
        "profile": _json_value(row.get("profile"), {}),
        "plan": _json_value(row.get("plan"), []),
        "max_turns": row.get("max_turns") or 0,
        "idx": row.get("current_idx") or 0,
        "follow_idx": row.get("follow_idx") if row.get("follow_idx") is not None else -1,
        "turns": load_turns(session_id),
        "scores": _json_value(row.get("scores"), {"depth": 60, "clarity": 60, "logic": 60, "proof": 60}),
        "memory": _json_value(row.get("memory"), {}),
        "created_at": str(row.get("created_at") or ""),
        "status": row.get("status") or "in_progress",
    }
    report = load_report(session_id)
    if report is not None:
        session["report"] = report
    return session


def save_turn_and_session(s: dict, turn: dict) -> bool:
    conn = get_conn()
    if not conn:
        return False
    try:
        with conn.transaction(), conn.cursor() as cur:
            cur.execute(_save_turn_sql(), _turn_params(s["id"], turn))
            cur.execute(_save_session_sql(), _session_params(s))
        return True
    except Exception as exc:
        _log_failure("保存面试进度", exc)
        _reset()
        raise DatabaseUnavailableError("数据库暂时不可用") from exc


def save_report_and_session(session_id: str, report: dict, s: dict) -> bool:
    """Persist the final report and final session status as one transaction."""
    conn = get_conn()
    if not conn:
        return False
    try:
        with conn.transaction(), conn.cursor() as cur:
            cur.execute(_save_report_sql(), _report_params(session_id, report))
            cur.execute(_save_session_sql(), _session_params(s))
        return True
    except Exception as exc:
        _log_failure("保存面试报告", exc)
        _reset()
        raise DatabaseUnavailableError("数据库暂时不可用") from exc


def change_password_and_revoke_tokens(user_id: str, password_hash: str) -> bool:
    """Rotate the password and revoke all tokens without an unsafe half-success."""
    conn = get_conn()
    if not conn:
        return False
    try:
        with conn.transaction(), conn.cursor() as cur:
            cur.execute("UPDATE users SET password_hash = %s WHERE id = %s", (password_hash, user_id))
            if cur.rowcount != 1:
                return False
            cur.execute("DELETE FROM auth_tokens WHERE user_id = %s", (user_id,))
        return True
    except Exception as exc:
        _log_failure("更新密码", exc)
        _reset()
        raise DatabaseUnavailableError("数据库暂时不可用") from exc


def user_history(user_id: str) -> list[dict]:
    """控制台用：某用户的面试历史（含报告总分）。"""
    return fetchall(
        "SELECT s.id AS sid, s.role, s.kind, s.status, s.created_at, "
        "       (r.report->>'overall')::int AS overall "
        "FROM sessions s LEFT JOIN reports r ON r.session_id = s.id "
        "WHERE s.user_id = %s ORDER BY s.created_at DESC LIMIT 50",
        (user_id,),
    )


def delete_user_data(user_id: str) -> bool:
    """Erase account-scoped records in one transaction, then invalidate its tokens."""
    conn = get_conn()
    if not conn:
        return False
    try:
        with conn.transaction(), conn.cursor() as cur:
            cur.execute("DELETE FROM session_feedback WHERE user_id = %s", (user_id,))
            cur.execute("DELETE FROM reports WHERE session_id IN (SELECT id FROM sessions WHERE user_id = %s)", (user_id,))
            cur.execute("DELETE FROM turns WHERE session_id IN (SELECT id FROM sessions WHERE user_id = %s)", (user_id,))
            cur.execute("DELETE FROM sessions WHERE user_id = %s", (user_id,))
            cur.execute("DELETE FROM submissions WHERE user_id = %s", (user_id,))
            cur.execute("DELETE FROM auth_tokens WHERE user_id = %s", (user_id,))
            cur.execute("DELETE FROM users WHERE id = %s", (user_id,))
        return True
    except Exception as exc:
        _log_failure("删除账号数据", exc)
        _reset()
        raise DatabaseUnavailableError("数据库暂时不可用") from exc


def export_user_data(user_id: str) -> dict:
    """Return portable account data, deliberately excluding raw resume text."""
    user = fetchone("SELECT id, email, nickname, contribution, created_at FROM users WHERE id = %s", (user_id,)) or {}
    sessions = fetchall(
        "SELECT id, role, mode, profile, plan, status, created_at FROM sessions WHERE user_id = %s ORDER BY created_at ASC",
        (user_id,),
    )
    for session in sessions:
        session["turns"] = load_turns(session["id"])
        session["report"] = load_report(session["id"])
        feedback = fetchone("SELECT rating, comment, created_at FROM session_feedback WHERE session_id = %s", (session["id"],))
        session["feedback"] = feedback or None
    submissions = fetchall(
        "SELECT company, role, interview_date, rounds, questions, status, quality_note, created_at "
        "FROM submissions WHERE user_id = %s ORDER BY created_at ASC",
        (user_id,),
    )
    return {"user": user, "sessions": sessions, "submissions": submissions}


def operator_metrics() -> dict:
    """Aggregate operational counters only; no resume or answer text is queried."""
    row = fetchone(
        "SELECT "
        "(SELECT count(*) FROM users) AS users_total, "
        "(SELECT count(*) FROM users WHERE created_at >= now() - interval '7 days') AS users_7d, "
        "(SELECT count(*) FROM sessions) AS sessions_total, "
        "(SELECT count(*) FROM sessions WHERE status = 'finished') AS sessions_finished, "
        "(SELECT count(*) FROM sessions WHERE created_at >= now() - interval '7 days') AS sessions_7d, "
        "(SELECT count(*) FROM questions WHERE source_type = 'manual_import') AS imported_questions, "
        "(SELECT count(*) FROM submissions WHERE status = 'pending') AS pending_submissions, "
        "(SELECT round(avg(rating)::numeric, 2) FROM session_feedback) AS feedback_average, "
        "(SELECT count(*) FROM session_feedback) AS feedback_count"
    ) or {}
    return {
        "users_total": int(row.get("users_total") or 0),
        "users_7d": int(row.get("users_7d") or 0),
        "sessions_total": int(row.get("sessions_total") or 0),
        "sessions_finished": int(row.get("sessions_finished") or 0),
        "sessions_7d": int(row.get("sessions_7d") or 0),
        "imported_questions": int(row.get("imported_questions") or 0),
        "pending_submissions": int(row.get("pending_submissions") or 0),
        "feedback_average": float(row["feedback_average"]) if row.get("feedback_average") is not None else None,
        "feedback_count": int(row.get("feedback_count") or 0),
    }


def insert_corpus_questions(rows: list[dict]) -> int:
    """面经投稿反哺题库：批量写入 questions 表（无 embedding，检索时走标签/文本兜底）。"""
    conn = get_conn()
    if not conn:
        return 0
    n = 0
    try:
        with conn.cursor() as cur:
            for r in rows:
                cur.execute(
                    "INSERT INTO questions (track, question, answer_hints, tags, "
                    "source_name, source_url, published_at) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    (r.get("track", "cs"), r["question"], r.get("answer_hints", ""),
                     r.get("tags", []), r.get("source_name", ""), r.get("source_url", ""),
                     r.get("published_at")),
                )
                n += 1
    except Exception as exc:
        _log_failure("题库写入", exc)
        _reset()
    return n


def question_hashes(hashes: list[str]) -> set[str]:
    if not hashes:
        return set()
    rows = fetchall("SELECT content_hash FROM questions WHERE content_hash = ANY(%s)", (hashes,))
    return {row["content_hash"] for row in rows if row.get("content_hash")}


def _manual_question_insert_params(row: dict, batch_id: str) -> tuple:
    """Keep standalone imports and approved community posts in one row shape."""
    vector = row.get("embedding")
    vector_literal = (
        "[" + ",".join(format(float(value), ".9g") for value in vector) + "]"
        if vector else None
    )
    return (
        "general", row["question"], "", row["role_tags"], row["source_name"], row["source_url"],
        row.get("published_at"), row["content_hash"], "manual_import", batch_id,
        vector_literal,
        json.dumps({
            "stage": row["stage"], "topic": row["topic"],
            "answer_signals": row.get("answer_signals", []),
            "followup_templates": row.get("followup_templates", []),
            "source_metadata": row.get("source_metadata", {}),
        }, ensure_ascii=False),
    )


def _manual_question_insert_sql() -> str:
    vector_placeholder = "%s::vector" if _vector_extension_available else "%s"
    return (
        "INSERT INTO questions (track, question, answer_hints, tags, source_name, source_url, published_at, "
        "content_hash, source_type, import_batch_id, embedding, metadata) "
        f"VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,{vector_placeholder},%s) "
        "ON CONFLICT (content_hash) WHERE content_hash IS NOT NULL DO NOTHING"
    )


def insert_manual_questions(rows: list[dict], batch_id: str) -> int | None:
    conn = get_conn()
    if not conn:
        return None
    inserted = 0
    try:
        with conn.transaction(), conn.cursor() as cur:
            for row in rows:
                cur.execute(
                    _manual_question_insert_sql(),
                    _manual_question_insert_params(row, batch_id),
                )
                inserted += cur.rowcount
    except Exception as exc:
        _log_failure("手工题库导入", exc)
        _reset()
        raise DatabaseUnavailableError("数据库暂时不可用") from exc
    return inserted


def approve_submission_with_manual_questions(
    submission_id: int, user_id: str, rows: list[dict], batch_id: str, note: str | None,
) -> int | None:
    """Atomically import reviewed questions, approve the post and award contribution.

    A community post must never become approved (or receive points) unless its
    validated questions are written in the same database transaction.
    """
    conn = get_conn()
    if not conn:
        return None
    inserted = 0
    try:
        with conn.transaction(), conn.cursor() as cur:
            for row in rows:
                cur.execute(
                    _manual_question_insert_sql(),
                    _manual_question_insert_params(row, batch_id),
                )
                inserted += cur.rowcount
            if inserted <= 0:
                # Returning without changing submission/user rows lets callers
                # tell an all-duplicate review from a successful approval.
                return 0
            cur.execute(
                "UPDATE submissions SET status = 'approved', quality_note = %s WHERE id = %s AND status = 'pending'",
                (note, submission_id),
            )
            if cur.rowcount != 1:
                raise RuntimeError("投稿状态已变化，无法完成审核")
            cur.execute("UPDATE users SET contribution = contribution + 10 WHERE id = %s", (user_id,))
    except Exception as exc:
        _log_failure("审核投稿写入", exc)
        _reset()
        raise DatabaseUnavailableError("数据库暂时不可用") from exc
    return inserted


def list_manual_questions(query: str = "", active_only: bool = False) -> list[dict]:
    where = "WHERE source_type = 'manual_import'"
    params: list = []
    if active_only:
        where += " AND is_active = true"
    if query.strip():
        where += " AND (question ILIKE %s OR source_name ILIKE %s OR tags::text ILIKE %s)"
        params.extend([f"%{query.strip()}%"] * 3)
    return fetchall(
        "SELECT id, question, tags, source_name, source_url, published_at, metadata, is_active, created_at "
        f"FROM questions {where} ORDER BY id DESC LIMIT 200",
        tuple(params),
    )


def set_question_active(question_id: int, active: bool) -> bool | None:
    conn = get_conn()
    if not conn:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE questions SET is_active = %s WHERE id = %s AND source_type = 'manual_import'",
                (active, question_id),
            )
            return cur.rowcount == 1
    except Exception as exc:
        _log_failure("更新题目状态", exc)
        _reset()
        raise DatabaseUnavailableError("数据库暂时不可用") from exc


def reject_pending_submission(submission_id: int, note: str | None) -> bool | None:
    """Atomically move one still-pending community submission to rejected."""
    conn = get_conn()
    if not conn:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE submissions SET status = 'rejected', quality_note = %s "
                "WHERE id = %s AND status = 'pending'",
                (note, submission_id),
            )
            return cur.rowcount == 1
    except Exception as exc:
        _log_failure("驳回投稿", exc)
        _reset()
        raise DatabaseUnavailableError("数据库暂时不可用") from exc
