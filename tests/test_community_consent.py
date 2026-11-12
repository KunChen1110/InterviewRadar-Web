import pytest
from pydantic import ValidationError

from backend import auth


QUESTIONS = [
    "请介绍一次你通过实验优化注册转化的经历。",
    "当核心指标连续下降时，你会怎样定位原因？",
    "如何验证一次 A/B 测试的结论可信？",
]


def test_submission_requires_explicit_rights_confirmation():
    with pytest.raises(ValidationError, match="确认"):
        auth.SubmissionCreate(company="测试公司", role="运营策划", questions=QUESTIONS)


def test_submission_accepts_explicit_rights_confirmation():
    body = auth.SubmissionCreate(
        company="测试公司", role="运营策划", questions=QUESTIONS,
        content_rights_confirmed=True,
    )

    assert body.content_rights_confirmed is True


def test_review_material_redacts_contacts_and_derives_abstract_drafts():
    review = auth.submission_review_material({
        "company": "测试公司", "role": "运营策划",
        "questions": ["联系 13800000000 或 alice@example.com，如何设计增长实验？"],
    })

    assert "13800000000" not in review["redacted_questions"][0]
    assert "alice@example.com" not in review["redacted_questions"][0]
    assert review["drafts"][0]["question"] == "如何设计增长实验？"
    assert review["drafts"][0]["role_tags"] == ["运营策划"]


def test_review_material_marks_duplicate_drafts_without_exposing_original_question():
    review = auth.submission_review_material({
        "company": "测试公司", "role": "运营策划", "questions": QUESTIONS,
    })

    assert all(item["content_hash"] for item in review["drafts"])
    assert "original_questions" not in review


def test_admin_submission_list_returns_redacted_evidence_and_drafts_not_raw_questions(monkeypatch):
    raw = "联系 13800000000，如何设计增长实验？"
    monkeypatch.setattr(auth.db, "fetchall", lambda *_args: [{
        "id": 7, "company": "测试公司", "role": "运营策划", "questions": [raw],
        "status": "pending", "rights_confirmed": True, "created_at": "2026-08-13",
        "interview_date": None, "rounds": "", "quality_note": None, "submitter": "candidate@example.com",
    }])

    result = auth.admin_submissions(user={"id": "admin"})
    submission = result["submissions"][0]

    assert "questions" not in submission
    assert "13800000000" not in submission["redacted_questions"][0]
    assert submission["drafts"][0]["question"] == "如何设计增长实验？"


def test_approval_uses_an_admin_edited_abstract_draft_not_the_raw_submission(monkeypatch):
    row = {
        "id": 12, "user_id": "candidate-1", "company": "测试公司", "role": "运营策划",
        "interview_date": None, "questions": ["原始投稿：如何设计增长实验？"], "status": "pending", "rights_confirmed": True,
    }
    edited = {"question": "如何设计增长实验的指标与验证方案？", "role_tags": ["运营策划"],
              "stage": "场景问题", "topic": "增长实验"}
    monkeypatch.setattr(auth.db, "fetchone", lambda *_args: row)
    preview = {}
    monkeypatch.setattr(
        auth.rag_schema, "create_preview",
        lambda _owner, _name, payload, **_kwargs: preview.update(payload=payload) or {"preview_id": "preview-1", "valid_count": 1},
    )
    monkeypatch.setattr(auth.rag_schema, "get_preview", lambda *_args, **_kwargs: {"rows": [
        {**edited, "source_name": "用户授权投稿 · 运营策划", "source_url": "", "content_hash": "draft-hash"},
    ]})
    monkeypatch.setattr(auth.db, "question_hashes", lambda _hashes: set())
    monkeypatch.setattr(auth.embeddings, "embed_texts", lambda _texts: None)
    captured = []
    monkeypatch.setattr(auth.db, "approve_submission_with_manual_questions", lambda *_args: captured.append(_args) or 1)
    monkeypatch.setattr(auth.knowledge_plan, "invalidate_db_cache", lambda: None)

    auth._moderate_submission(12, "approved", "已核验", drafts=[edited])

    assert edited["question"].encode() in preview["payload"]
    assert "原始投稿" not in preview["payload"].decode()
    assert captured[0][2][0]["question"] == edited["question"]


def test_moderation_payload_bounds_and_validates_admin_drafts_before_publishing():
    with pytest.raises(Exception, match="stage"):
        auth.ModerationBody(drafts=[{
            "question": "如何设计增长实验？", "role_tags": ["运营策划"],
            "stage": "任意环节", "topic": "增长实验",
        }])

    with pytest.raises(Exception, match="question"):
        auth.ModerationBody(drafts=[{
            "question": "x" * 1001, "role_tags": ["运营策划"],
            "stage": "场景问题", "topic": "增长实验",
        }])


def test_approval_refuses_historical_submission_without_recorded_content_rights(monkeypatch):
    monkeypatch.setattr(auth.db, "fetchone", lambda *_args: {
        "id": 22, "user_id": "candidate-1", "company": "测试公司", "role": "运营策划",
        "interview_date": None, "questions": QUESTIONS, "status": "pending", "rights_confirmed": False,
    })

    with pytest.raises(Exception, match="授权"):
        auth._moderate_submission(22, "approved", "已核验")


def test_community_feed_reads_published_question_types_not_approved_submission_bodies(monkeypatch):
    queries = []
    monkeypatch.setattr(
        auth.db,
        "fetchall",
        lambda sql, params=(): queries.append(sql) or [{
            "question": "如何设计增长实验的指标与验证方案？", "tags": ["运营策划"],
            "source_name": "用户授权投稿 · 运营策划", "published_at": "2026-08-13", "created_at": "2026-08-13",
        }],
    )
    monkeypatch.setattr(auth.knowledge_plan, "curated_posts", lambda _role: [])

    result = auth.community_posts()

    assert "FROM questions" in queries[0]
    assert result["posts"][0]["questions"] == ["如何设计增长实验的指标与验证方案？"]
    assert result["posts"][0]["company"] == "用户共建题型"
