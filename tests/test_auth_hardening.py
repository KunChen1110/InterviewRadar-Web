import hashlib
from datetime import date

import auth
from fastapi import HTTPException


def test_new_tokens_are_hashed_before_being_persisted(monkeypatch):
    writes = []
    monkeypatch.setattr(auth.db, "_execute", lambda sql, params: writes.append((sql, params)) or True)

    token = auth._issue_token("user-1")

    assert len(writes) == 1
    persisted_token = writes[0][1][0]
    assert persisted_token == hashlib.sha256(token.encode("utf-8")).hexdigest()
    assert persisted_token != token


def test_logout_revokes_hashed_and_legacy_token_records(monkeypatch):
    writes = []
    monkeypatch.setattr(auth.db, "_execute", lambda sql, params: writes.append((sql, params)) or True)

    auth.logout("Bearer token-for-test")

    assert len(writes) == 1
    assert "DELETE FROM auth_tokens" in writes[0][0]
    assert hashlib.sha256(b"token-for-test").hexdigest() in writes[0][1]
    assert "token-for-test" in writes[0][1]


def test_password_hash_uses_a_versioned_stronger_format_and_keeps_legacy_logins_working():
    hashed = auth._hash_password("correct horse battery staple")
    assert hashed.startswith("pbkdf2_sha256$")
    assert auth._verify_password("correct horse battery staple", hashed)
    assert not auth._verify_password("wrong password", hashed)

    salt = "00112233445566778899aabbccddeeff"
    legacy = hashlib.pbkdf2_hmac(
        "sha256", b"legacy-password", bytes.fromhex(salt), 100_000
    ).hex()
    assert auth._verify_password("legacy-password", f"{salt}${legacy}")


def test_current_user_queries_hashed_token_but_still_accepts_legacy_rows(monkeypatch):
    monkeypatch.setattr(auth.db, "DB_URL", "postgresql://configured")
    captured = {}
    monkeypatch.setattr(
        auth.db,
        "fetchone",
        lambda sql, params: captured.update(sql=sql, params=params) or {"id": "u1", "email": "u@example.com"},
    )

    user = auth.current_user("Bearer token-for-test")

    assert user["id"] == "u1"
    assert hashlib.sha256(b"token-for-test").hexdigest() in captured["params"]
    assert "token-for-test" in captured["params"]


def test_registration_requires_an_eight_character_password_and_canonicalizes_email(monkeypatch):
    monkeypatch.setattr(auth.db, "fetchone", lambda *_args: None)
    writes = []
    monkeypatch.setattr(auth.db, "create_user_and_token", lambda *args: writes.append(args) or True)
    monkeypatch.setattr(auth, "_public_user", lambda uid: {"id": uid})

    try:
        auth.register(auth.AuthBody(email="Person@Example.COM ", password="short1", nickname="候选人"))
    except HTTPException as exc:
        assert exc.status_code == 400
        assert "8 位" in exc.detail
    else:
        raise AssertionError("weak password must be rejected")

    result = auth.register(auth.AuthBody(email=" Person@Example.COM ", password="long-enough", nickname="候选人"))

    assert result["token"]
    assert writes[0][1] == "person@example.com"


def test_login_uses_case_insensitive_email_lookup(monkeypatch):
    captured = {}
    monkeypatch.setattr(auth.db, "fetchone", lambda sql, params: captured.update(sql=sql, params=params) or None)

    try:
        auth.login(auth.AuthBody(email="Person@Example.COM", password="long-enough"))
    except HTTPException as exc:
        assert exc.status_code == 401
    else:
        raise AssertionError("missing account must not authenticate")

    assert "LOWER(email)" in captured["sql"]
    assert captured["params"] == ("person@example.com",)


def test_approved_submission_uses_the_same_validator_and_atomic_database_approval(monkeypatch):
    row = {
        "id": 12,
        "user_id": "candidate-1",
        "company": "测试公司",
        "role": "数据分析师",
        "interview_date": "2026-08-12",
        "questions": [
            "请介绍一次你从混乱数据中建立指标体系的经历。",
            "如果核心转化下降，你会先怎样定位问题？",
            "你如何验证一次 A/B 测试的结论可靠？",
        ],
            "status": "pending",
            "rights_confirmed": True,
    }
    monkeypatch.setattr(auth.db, "fetchone", lambda *_args: row)
    preview = {}
    normalized_rows = [{
        "question": "请介绍一次你从混乱数据中建立指标体系的经历。",
        "role_tags": ["数据分析师"], "stage": "项目深挖", "topic": "社区投稿真题",
        "source_name": "用户面经 · 测试公司 数据分析师", "source_url": "",
        "content_hash": "hash-1", "source_type": "manual_import",
    }]
    monkeypatch.setattr(auth.rag_schema, "create_preview", lambda owner, name, payload, existing_hashes: preview.update(owner=owner, name=name, payload=payload) or {"preview_id": "preview-1", "valid_count": 1})
    monkeypatch.setattr(auth.rag_schema, "get_preview", lambda preview_id, owner, consume: preview.update(preview_id=preview_id, consumed=consume) or {"rows": normalized_rows})
    monkeypatch.setattr(auth.embeddings, "embed_texts", lambda _texts: None)
    approvals = []
    monkeypatch.setattr(
        auth.db,
        "approve_submission_with_manual_questions",
        lambda submission_id, user_id, rows, batch_id, note: approvals.append({
            "submission_id": submission_id, "user_id": user_id, "rows": rows,
            "batch_id": batch_id, "note": note,
        }) or 1,
    )
    monkeypatch.setattr(auth.db, "_execute", lambda *_args: (_ for _ in ()).throw(AssertionError("approval must not split question/status/reward writes")))
    monkeypatch.setattr(auth.knowledge_plan, "invalidate_db_cache", lambda: None)

    result = auth._moderate_submission(12, "approved", "来源和题干已核验")

    assert result["status"] == "approved"
    assert preview["owner"] == "moderation-12"
    assert preview["preview_id"] == "preview-1"
    assert preview["consumed"] is True
    assert b"manual_import" not in preview["payload"]  # canonical path adds this server-side
    assert approvals[0]["submission_id"] == 12
    assert approvals[0]["user_id"] == "candidate-1"
    assert approvals[0]["rows"] == normalized_rows
    assert approvals[0]["note"] == "来源和题干已核验"


def test_approval_does_not_mark_or_reward_a_submission_when_no_question_survives_validation(monkeypatch):
    row = {
        "id": 13, "user_id": "candidate-1", "company": "测试公司", "role": "数据分析师",
            "interview_date": "", "questions": ["一道没有足够长度的题"], "status": "pending", "rights_confirmed": True,
    }
    monkeypatch.setattr(auth.db, "fetchone", lambda *_args: row)
    writes = []
    monkeypatch.setattr(auth.db, "_execute", lambda sql, params: writes.append((sql, params)) or True)
    monkeypatch.setattr(auth, "_approve_submission_questions", lambda _row, _note: 0)

    try:
        auth._moderate_submission(13, "approved", "来源核验")
    except HTTPException as exc:
        assert exc.status_code == 400
        assert "没有可写入" in exc.detail
    else:
        raise AssertionError("approval must stop when the canonical import yields no question")

    assert not writes


def test_submission_model_rejects_invalid_date_and_unbounded_questions():
    try:
        auth.SubmissionCreate(
            company="测试公司", role="数据分析师", interview_date="not-a-date",
            questions=["第一道足够长度的真实面试题", "第二道足够长度的真实面试题", "第三道足够长度的真实面试题"],
        )
    except Exception as exc:
        assert "interview_date" in str(exc)
    else:
        raise AssertionError("invalid interview dates must be blocked before reaching PostgreSQL")

    try:
        auth.SubmissionCreate(
            company="测试公司", role="数据分析师", interview_date=date(2026, 8, 12),
            questions=["x" * 1001, "第二道足够长度的真实面试题", "第三道足够长度的真实面试题"],
        )
    except Exception as exc:
        assert "questions" in str(exc)
    else:
        raise AssertionError("oversized question text must be rejected")


def test_auth_payload_has_bounded_email_password_and_nickname_fields():
    try:
        auth.AuthBody(email="a" * 321 + "@example.com", password="long-enough", nickname="候选人")
    except Exception as exc:
        assert "email" in str(exc)
    else:
        raise AssertionError("oversized email must be rejected")

    try:
        auth.AuthBody(email="person@example.com", password="x" * 257, nickname="候选人")
    except Exception as exc:
        assert "password" in str(exc)
    else:
        raise AssertionError("oversized password must be rejected before expensive hashing")


def test_account_and_moderation_payloads_are_bounded_before_handlers_run():
    for factory, field in (
        (lambda: auth.AccountDeletionBody(confirmation="x" * 33), "confirmation"),
        (lambda: auth.PasswordChangeBody(current_password="x" * 257, new_password="safe-password"), "current_password"),
        (lambda: auth.PasswordChangeBody(current_password="safe-password", new_password="x" * 257), "new_password"),
        (lambda: auth.ModerationBody(note="x" * 1001), "note"),
        (lambda: auth.ImportConfirmBody(preview_id="x" * 129), "preview_id"),
    ):
        try:
            factory()
        except Exception as exc:
            assert field in str(exc)
        else:
            raise AssertionError(f"oversized {field} must be rejected")
