import copy

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend import main


USER = {"id": "persistent-user", "email": "candidate@example.com", "nickname": "候选人"}


def _create_development_session(monkeypatch) -> str:
    main.SESSIONS.clear()
    monkeypatch.setattr(main.db, "DB_URL", "")
    monkeypatch.setattr(main.llm, "parse_resume", lambda _text: None)
    monkeypatch.setattr(main.llm, "evaluate_turn", lambda *_args: None)
    monkeypatch.setattr(main.llm, "build_report", lambda _session: None)
    return main.create_session(
        main.SessionCreate(
            resume_text="我做过增长分析平台，用 SQL 建漏斗并将注册转化提升 18%。",
            role="运营策划",
            mode="light",
        ),
        USER,
    )["session_id"]


def test_configured_database_refuses_new_session_when_initial_save_fails(monkeypatch):
    main.SESSIONS.clear()
    monkeypatch.setattr(main.db, "DB_URL", "postgresql://configured")
    monkeypatch.setattr(main.knowledge_plan, "_db_loaded_at", float("inf"))
    monkeypatch.setattr(main.db, "save_session", lambda _session: False)
    monkeypatch.setattr(main.llm, "parse_resume", lambda _text: None)

    with pytest.raises(HTTPException) as exc_info:
        main.create_session(
            main.SessionCreate(
                resume_text="我做过增长分析平台，用 SQL 建漏斗并将注册转化提升 18%。",
                role="运营策划",
            ),
            USER,
        )

    assert exc_info.value.status_code == 503
    assert main.SESSIONS == {}


def test_configured_database_rolls_back_an_answer_when_progress_save_fails(monkeypatch):
    sid = _create_development_session(monkeypatch)
    before = copy.deepcopy(main.SESSIONS[sid])
    monkeypatch.setattr(main.db, "DB_URL", "postgresql://configured")
    monkeypatch.setattr(main.db, "save_turn_and_session", lambda _session, _turn: False)

    with pytest.raises(HTTPException) as exc_info:
        main.submit_answer(sid, main.AnswerSubmit(answer="我先定义北极星指标，再拆解漏斗并通过 A/B 测试验证。"), USER)

    assert exc_info.value.status_code == 503
    assert main.SESSIONS[sid] == before


def test_configured_database_rolls_back_report_when_final_save_fails(monkeypatch):
    sid = _create_development_session(monkeypatch)
    before = copy.deepcopy(main.SESSIONS[sid])
    monkeypatch.setattr(main.db, "DB_URL", "postgresql://configured")
    monkeypatch.setattr(main.db, "save_report_and_session", lambda _sid, _report, _session: False, raising=False)

    with pytest.raises(HTTPException) as exc_info:
        main.finish(sid, USER)

    assert exc_info.value.status_code == 503
    assert main.SESSIONS[sid] == before


def test_database_connection_error_becomes_a_safe_503_response(monkeypatch):
    monkeypatch.setattr(main.db, "DB_URL", "postgresql://configured")
    monkeypatch.setattr(
        main.db,
        "get_conn",
        lambda: (_ for _ in ()).throw(main.db.DatabaseUnavailableError("internal connection detail")),
    )

    with TestClient(main.app) as client:
        response = client.get("/api/auth/me", headers={"Authorization": "Bearer test-token"})

    assert response.status_code == 503
    assert response.json()["detail"] == "服务暂时不可用，请稍后重试"
    assert "internal connection detail" not in response.text


def test_readiness_requires_a_successful_schema_initialization(monkeypatch):
    monkeypatch.setattr(main.db, "DB_URL", "postgresql://configured")
    monkeypatch.setattr(main.db, "_schema_ready", False, raising=False)
    monkeypatch.setattr(main.db, "_schema_next_retry_at", 0.0, raising=False)
    monkeypatch.setattr(main.db, "init_schema", lambda: None)
    monkeypatch.setattr(main.db, "fetchone", lambda *_args, **_kwargs: {"one": 1})

    assert main.ready().status_code == 503


def test_readiness_retries_schema_initialization_after_a_transient_startup_outage(monkeypatch):
    monkeypatch.setattr(main.db, "DB_URL", "postgresql://configured")
    monkeypatch.setattr(main.db, "_schema_ready", False, raising=False)
    monkeypatch.setattr(main.db, "_schema_next_retry_at", 0.0, raising=False)
    attempts = []

    def restore_schema():
        attempts.append("retry")
        main.db._schema_ready = True

    monkeypatch.setattr(main.db, "init_schema", restore_schema)
    monkeypatch.setattr(main.db, "fetchone", lambda *_args, **_kwargs: {"one": 1})

    assert main.ready().status_code == 200
    assert attempts == ["retry"]


def test_readiness_throttles_schema_retries_while_a_database_is_still_down(monkeypatch):
    monkeypatch.setattr(main.db, "DB_URL", "postgresql://configured")
    monkeypatch.setattr(main.db, "_schema_ready", False, raising=False)
    monkeypatch.setattr(main.db, "_schema_next_retry_at", 100.0, raising=False)
    monkeypatch.setattr(main.db.time, "monotonic", lambda: 99.0)
    attempts = []
    monkeypatch.setattr(main.db, "init_schema", lambda: attempts.append("retry"))

    assert main.ready().status_code == 503
    assert attempts == []


def test_auth_writes_do_not_claim_success_when_the_configured_database_rejects_them(monkeypatch):
    from backend import auth

    monkeypatch.setattr(auth.db, "DB_URL", "postgresql://configured")
    monkeypatch.setattr(auth.db, "fetchone", lambda *_args: None)
    monkeypatch.setattr(auth.db, "create_user_and_token", lambda *_args: False, raising=False)

    with pytest.raises(HTTPException) as exc_info:
        auth.register(auth.AuthBody(email="candidate@example.com", password="long-enough"))

    assert exc_info.value.status_code == 503


def test_registration_uses_one_atomic_user_and_token_write(monkeypatch):
    from backend import auth

    monkeypatch.setattr(auth.db, "fetchone", lambda *_args: None)
    calls = []
    monkeypatch.setattr(
        auth.db,
        "create_user_and_token",
        lambda *args: calls.append(args) or True,
        raising=False,
    )
    monkeypatch.setattr(auth, "_public_user", lambda uid: {"id": uid})

    result = auth.register(auth.AuthBody(email="candidate@example.com", password="long-enough"))

    assert result["token"]
    assert len(calls) == 1
    assert calls[0][1] == "candidate@example.com"
    assert calls[0][4] == auth._token_digest(result["token"])


def test_registration_does_not_need_a_second_database_read_after_the_atomic_write(monkeypatch):
    from backend import auth

    monkeypatch.setattr(auth.db, "DB_URL", "postgresql://configured")
    monkeypatch.setattr(auth.db, "fetchone", lambda *_args: None)
    monkeypatch.setattr(auth.db, "create_user_and_token", lambda *_args: True, raising=False)
    monkeypatch.setattr(
        auth,
        "_public_user",
        lambda _uid: (_ for _ in ()).throw(AssertionError("registration must not re-read the new user")),
    )

    result = auth.register(auth.AuthBody(email="candidate@example.com", password="long-enough", nickname="候选人"))

    assert result["user"] == {
        "id": result["user"]["id"], "email": "candidate@example.com", "nickname": "候选人",
        "credits": 0, "contribution": 0, "trial_used": 0,
    }


def test_login_returns_the_user_fields_from_its_authenticated_lookup(monkeypatch):
    from backend import auth

    row = {
        "id": "candidate-1", "email": "candidate@example.com", "nickname": "候选人",
        "credits": 0, "contribution": 10, "trial_used": 0,
        "password_hash": auth._hash_password("long-enough"),
    }
    monkeypatch.setattr(auth.db, "fetchone", lambda *_args: row)
    monkeypatch.setattr(auth, "_issue_token", lambda _uid: "issued-token")
    monkeypatch.setattr(
        auth,
        "_public_user",
        lambda _uid: (_ for _ in ()).throw(AssertionError("login must not issue a token then re-read the user")),
    )

    result = auth.login(auth.AuthBody(email="candidate@example.com", password="long-enough"))

    assert result == {"token": "issued-token", "user": {
        "id": "candidate-1", "email": "candidate@example.com", "nickname": "候选人",
        "credits": 0, "contribution": 10, "trial_used": 0,
    }}


def test_registration_concurrent_duplicate_is_reported_as_already_registered(monkeypatch):
    from backend import auth

    monkeypatch.setattr(auth.db, "fetchone", lambda *_args: None)
    monkeypatch.setattr(
        auth.db,
        "create_user_and_token",
        lambda *_args: (_ for _ in ()).throw(auth.db.AccountExistsError("duplicate key")),
        raising=False,
    )

    with pytest.raises(HTTPException) as exc_info:
        auth.register(auth.AuthBody(email="candidate@example.com", password="long-enough"))

    assert exc_info.value.status_code == 409


def test_feedback_write_failure_is_a_safe_503(monkeypatch):
    sid = _create_development_session(monkeypatch)
    main.SESSIONS[sid]["status"] = "finished"
    monkeypatch.setattr(main.auth, "is_dev", lambda _user: False)
    monkeypatch.setattr(main.db, "DB_URL", "postgresql://configured")
    monkeypatch.setattr(main.db, "save_feedback", lambda *_args, **_kwargs: False)

    with pytest.raises(HTTPException) as exc_info:
        main.submit_feedback(sid, main.FeedbackBody(rating=5, comment="问题贴近岗位"), USER)

    assert exc_info.value.status_code == 503


def test_password_rotation_is_rejected_when_its_atomic_persistence_fails(monkeypatch):
    from backend import auth

    monkeypatch.setattr(auth.db, "DB_URL", "postgresql://configured")
    monkeypatch.setattr(auth.db, "fetchone", lambda *_args: {"password_hash": auth._hash_password("old-password")})
    monkeypatch.setattr(auth.db, "change_password_and_revoke_tokens", lambda *_args: False)

    with pytest.raises(HTTPException) as exc_info:
        auth.change_password(
            auth.PasswordChangeBody(current_password="old-password", new_password="new-password"),
            USER,
        )

    assert exc_info.value.status_code == 503


def test_configured_database_does_not_accept_a_submission_without_an_insert(monkeypatch):
    from backend import auth

    monkeypatch.setattr(auth.db, "DB_URL", "postgresql://configured")
    monkeypatch.setattr(auth.db, "get_conn", lambda: None)
    body = auth.SubmissionCreate(
        company="测试公司",
        role="数据分析师",
        questions=[
            "请介绍一次你从混乱数据中建立指标体系的经历。",
            "如果核心转化下降，你会先怎样定位问题？",
            "你如何验证一次 A/B 测试的结论可靠？",
        ],
        content_rights_confirmed=True,
    )

    with pytest.raises(HTTPException) as exc_info:
        auth.create_submission(body, USER)

    assert exc_info.value.status_code == 503


def test_configured_database_does_not_claim_rejection_when_the_status_write_fails(monkeypatch):
    from backend import auth

    monkeypatch.setattr(auth.db, "DB_URL", "postgresql://configured")
    monkeypatch.setattr(auth.db, "fetchone", lambda *_args: {
        "id": 7, "user_id": "candidate-1", "status": "pending", "questions": [],
    })
    monkeypatch.setattr(auth.db, "_execute", lambda *_args: False)

    with pytest.raises(HTTPException) as exc_info:
        auth._moderate_submission(7, "rejected", "题干与来源无法核验")

    assert exc_info.value.status_code == 503


def test_configured_database_does_not_claim_account_deletion_without_a_transaction(monkeypatch):
    from backend import auth

    monkeypatch.setattr(auth.db, "DB_URL", "postgresql://configured")
    monkeypatch.setattr(auth.db, "delete_user_data", lambda _user_id: False)

    with pytest.raises(HTTPException) as exc_info:
        auth.delete_account_data(auth.AccountDeletionBody(confirmation="DELETE_MY_ACCOUNT"), USER)

    assert exc_info.value.status_code == 503


def test_configured_database_does_not_mislabel_question_status_write_failure_as_not_found(monkeypatch):
    from backend import auth

    monkeypatch.setattr(auth.db, "DB_URL", "postgresql://configured")
    monkeypatch.setattr(auth.db, "set_question_active", lambda *_args: None)

    with pytest.raises(HTTPException) as exc_info:
        auth.set_question_status(42, False, USER)

    assert exc_info.value.status_code == 503


def test_configured_database_does_not_confirm_a_manual_import_without_a_write(monkeypatch):
    from backend import auth

    monkeypatch.setattr(auth.db, "DB_URL", "postgresql://configured")
    monkeypatch.setattr(auth.db, "question_hashes", lambda _hashes: set())
    monkeypatch.setattr(auth.db, "insert_manual_questions", lambda _rows, _batch_id: None)
    preview = auth.rag_schema.create_preview(
        USER["id"], "questions.json", '[{"question":"如何设计增长实验？","role_tags":["产品运营"],"stage":"场景问题","topic":"实验","source_name":"手工整理"}]'.encode(),
        existing_hashes=set(),
    )

    with pytest.raises(HTTPException) as exc_info:
        auth.confirm_question_import(USER, preview["preview_id"])

    assert exc_info.value.status_code == 503


def test_manual_import_preview_survives_a_database_outage_for_safe_retry(monkeypatch):
    from backend import auth

    monkeypatch.setattr(auth.db, "DB_URL", "postgresql://configured")
    monkeypatch.setattr(auth.db, "question_hashes", lambda _hashes: set())
    monkeypatch.setattr(auth.db, "insert_manual_questions", lambda _rows, _batch_id: None)
    preview = auth.rag_schema.create_preview(
        USER["id"], "questions.json", '[{"question":"如何设计增长实验？","role_tags":["产品运营"],"stage":"场景问题","topic":"实验","source_name":"手工整理"}]'.encode(),
        existing_hashes=set(),
    )

    with pytest.raises(HTTPException) as exc_info:
        auth.confirm_question_import(USER, preview["preview_id"])

    assert exc_info.value.status_code == 503
    assert auth.rag_schema.get_preview(preview["preview_id"], USER["id"])["rows"]


def test_configured_database_does_not_mislabel_approval_outage_as_invalid_questions(monkeypatch):
    from backend import auth

    monkeypatch.setattr(auth.db, "DB_URL", "postgresql://configured")
    monkeypatch.setattr(auth.db, "fetchone", lambda *_args: {
            "id": 9, "user_id": "candidate-1", "status": "pending", "company": "测试公司",
            "role": "数据分析师", "interview_date": "", "questions": [], "rights_confirmed": True,
    })
    monkeypatch.setattr(auth, "_approve_submission_questions", lambda *_args: None)

    with pytest.raises(HTTPException) as exc_info:
        auth._moderate_submission(9, "approved", "来源和题干已核验")

    assert exc_info.value.status_code == 503
