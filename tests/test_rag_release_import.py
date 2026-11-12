from backend.rag_release_import import prepare_import_rows
import json


def test_import_rows_embed_only_reviewed_documents_and_keep_source_metadata(monkeypatch):
    monkeypatch.setenv("EMBEDDING_DIMENSIONS", "2")
    documents = [
        {
            "document_id": "doc-mit-1",
            "text": "如果支付接口出现重复扣款，你会如何定位并保证幂等？",
            "source_type": "github_mit_question",
            "source_url": "https://example.com/mit-question",
            "role_tags": ["后端开发"],
            "review_state": "reviewed",
            "retrieval_weight": 1.0,
            "metadata": {"license": "MIT", "topic": "支付场景"},
        },
        {
            "document_id": "doc-pending",
            "text": "待审核题目不应该进入生产库。",
            "source_type": "github_mit_question",
            "source_url": "https://example.com/pending",
            "role_tags": ["后端开发"],
            "review_state": "pending",
            "retrieval_weight": 1.0,
            "metadata": {"license": "MIT", "topic": "待审核"},
        },
    ]
    embedded_inputs = []

    rows = prepare_import_rows(
        documents,
        embed=lambda texts: embedded_inputs.extend(texts) or [[0.25, 0.75]],
    )

    assert len(rows) == 1
    assert rows[0]["stage"] == "场景问题"
    assert rows[0]["embedding"] == [0.25, 0.75]
    assert rows[0]["source_metadata"]["document_id"] == "doc-mit-1"
    assert rows[0]["source_metadata"]["license"] == "MIT"
    assert len(embedded_inputs) == 1


def test_import_rejects_vectors_with_a_dimension_mismatch(monkeypatch):
    monkeypatch.setenv("EMBEDDING_DIMENSIONS", "1536")
    document = {
        "document_id": "doc-1",
        "text": "如何定位服务中的慢请求？",
        "source_type": "github_mit_question",
        "source_url": "https://example.com/question",
        "role_tags": ["后端开发"],
        "review_state": "reviewed",
        "retrieval_weight": 1.0,
        "metadata": {"license": "MIT", "topic": "性能优化"},
    }

    try:
        prepare_import_rows([document], embed=lambda _texts: [[0.25, 0.75]])
    except ValueError as exc:
        assert "维度" in str(exc)
    else:
        raise AssertionError("vectors with a dimension mismatch must be rejected")


def test_import_rejects_xiaohongshu_rows_without_declared_authorization():
    document = {
        "document_id": "xhs-1",
        "text": "请描述你如何完成一次招聘面试。",
        "source_type": "xiaohongshu_authorized_question",
        "source_url": "https://www.xiaohongshu.com/explore/example",
        "role_tags": ["人力资源"],
        "review_state": "reviewed",
        "retrieval_weight": 1.2,
        "metadata": {"topic": "招聘", "license": "user-authorized"},
    }

    try:
        prepare_import_rows([document], embed=lambda _texts: [[0.1, 0.9]])
    except ValueError as exc:
        assert "授权" in str(exc)
    else:
        raise AssertionError("unauthorized XHS content must not be imported")


def test_database_import_params_store_pgvector_and_preserve_provenance():
    from backend.db import _manual_question_insert_params

    params = _manual_question_insert_params({
        "question": "如何处理接口重复请求并保证幂等？",
        "role_tags": ["后端开发"],
        "source_name": "GitHub · MIT",
        "source_url": "https://example.com/question",
        "content_hash": "hash",
        "stage": "场景问题",
        "topic": "支付场景",
        "embedding": [0.25, 0.75],
        "source_metadata": {"document_id": "doc-1", "license": "MIT"},
    }, "release-v1")

    assert params[-2] == "[0.25,0.75]"
    metadata = json.loads(params[-1])
    assert metadata["source_metadata"] == {"document_id": "doc-1", "license": "MIT"}
