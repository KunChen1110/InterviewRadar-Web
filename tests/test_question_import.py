import json

import pytest

import rag_schema


def test_preview_validates_deduplicates_and_does_not_persist():
    payload = json.dumps([
        {"question": "如何设计 A/B 测试的分流？", "role_tags": ["产品运营"], "stage": "场景问题", "topic": "实验", "source_name": "手工整理", "source_url": "https://example.com/a"},
        {"question": "如何设计 A/B 测试的分流？", "role_tags": ["产品运营"], "stage": "场景问题", "topic": "实验", "source_name": "手工整理", "source_url": "https://example.com/a"},
        {"question": "", "role_tags": [], "stage": "错误环节", "source_name": "x"},
    ], ensure_ascii=False).encode()

    preview = rag_schema.create_preview("admin-1", "questions.json", payload, existing_hashes=set())

    assert preview["valid_count"] == 1
    assert preview["duplicate_count"] == 1
    assert len(preview["errors"]) == 1
    assert rag_schema.get_preview(preview["preview_id"], "admin-1")["rows"][0]["source_type"] == "manual_import"


def test_preview_is_bound_to_owner_and_expires(monkeypatch):
    csv_data = "question,role_tags,stage,topic,source_name\nSQL 指标,产品运营,能力匹配,指标,手工\n".encode()
    preview = rag_schema.create_preview("admin-1", "questions.csv", csv_data, existing_hashes=set())

    with pytest.raises(rag_schema.PreviewAccessError):
        rag_schema.get_preview(preview["preview_id"], "admin-2")
    monkeypatch.setattr(rag_schema.time, "time", lambda: preview["expires_at"] + 1)
    with pytest.raises(rag_schema.PreviewAccessError):
        rag_schema.get_preview(preview["preview_id"], "admin-1")


def test_admin_confirm_writes_only_previewed_rows(monkeypatch):
    import auth

    monkeypatch.setattr(auth.db, "question_hashes", lambda hashes: set())
    captured = {}
    monkeypatch.setattr(auth.db, "insert_manual_questions", lambda rows, batch_id: captured.update(rows=rows, batch_id=batch_id) or len(rows))
    preview = rag_schema.create_preview(
        "admin-1", "questions.json", json.dumps([{
            "question": "如何判断实验显著性？", "role_tags": ["产品运营"],
            "stage": "场景问题", "topic": "实验", "source_name": "手工整理",
        }], ensure_ascii=False).encode(), existing_hashes=set(),
    )

    result = auth.confirm_question_import({"id": "admin-1"}, preview["preview_id"])

    assert result["inserted_count"] == 1
    assert captured["rows"][0]["question"] == "如何判断实验显著性？"
    with pytest.raises(rag_schema.PreviewAccessError):
        rag_schema.get_preview(preview["preview_id"], "admin-1")


def test_admin_confirm_attaches_optional_embedding_to_approved_question(monkeypatch):
    import auth

    monkeypatch.setattr(auth.db, "question_hashes", lambda hashes: set())
    monkeypatch.setattr(auth.embeddings, "embed_texts", lambda texts: [[0.25, 0.75]])
    captured = {}
    monkeypatch.setattr(auth.db, "insert_manual_questions", lambda rows, _batch_id: captured.update(rows=rows) or len(rows))
    preview = rag_schema.create_preview(
        "admin-1", "questions.json", json.dumps([{
            "question": "如何设计实验的核心指标？", "role_tags": ["产品运营"],
            "stage": "场景问题", "topic": "实验", "source_name": "手工整理",
        }], ensure_ascii=False).encode(), existing_hashes=set(),
    )

    auth.confirm_question_import({"id": "admin-1"}, preview["preview_id"])

    assert captured["rows"][0]["embedding"] == [0.25, 0.75]


def test_admin_question_status_change_invalidates_the_retrieval_cache(monkeypatch):
    import auth

    captured = {}
    monkeypatch.setattr(auth.db, "set_question_active", lambda question_id, active: captured.update(id=question_id, active=active) or True)
    monkeypatch.setattr(auth.knowledge_plan, "invalidate_db_cache", lambda: captured.update(cache_invalidated=True))

    result = auth.set_question_status(42, False, {"id": "admin-1"})

    assert result == {"id": 42, "active": False}
    assert captured == {"id": 42, "active": False, "cache_invalidated": True}


def test_question_export_csv_keeps_review_fields_and_uses_utf8_bom():
    import auth

    output = auth.question_export_csv([{
        "question": "如何设计核心指标？", "tags": ["产品运营"], "source_name": "手工整理",
        "source_url": "https://example.com/source", "published_at": "2026-08-01",
        "metadata": {"stage": "能力匹配", "topic": "指标"}, "is_active": True,
    }])

    assert output.startswith("\ufeffquestion,role_tags,stage,topic,source_name,source_url,published_at,is_active")
    assert "如何设计核心指标" in output
    assert "产品运营" in output
