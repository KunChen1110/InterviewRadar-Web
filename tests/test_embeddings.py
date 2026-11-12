import embeddings


def test_embedding_settings_are_disabled_without_explicit_embedding_model(monkeypatch):
    monkeypatch.delenv("EMBEDDING_MODEL", raising=False)
    monkeypatch.delenv("EMBEDDING_API_KEY", raising=False)
    assert embeddings.is_configured() is False


def test_embedding_requests_are_batched_and_use_configured_vector_dimensions(monkeypatch):
    import sys
    from types import SimpleNamespace

    calls = []

    class FakeEmbeddings:
        def create(self, *, model, input, dimensions):
            calls.append((model, input, dimensions))
            return SimpleNamespace(data=[SimpleNamespace(embedding=[1.0, 0.0]) for _ in input])

    monkeypatch.setenv("EMBEDDING_API_KEY", "embedding-secret")
    monkeypatch.setenv("EMBEDDING_MODEL", "text-embedding-v4")
    monkeypatch.setenv("EMBEDDING_DIMENSIONS", "1536")
    monkeypatch.setitem(
        sys.modules,
        "openai",
        SimpleNamespace(OpenAI=lambda **_kwargs: SimpleNamespace(embeddings=FakeEmbeddings())),
    )

    vectors = embeddings.embed_texts([f"question {index}" for index in range(12)])

    assert len(vectors) == 12
    assert [len(call[1]) for call in calls] == [10, 2]
    assert all(call[0] == "text-embedding-v4" and call[2] == 1536 for call in calls)


def test_cosine_similarity_handles_missing_or_mismatched_vectors():
    assert embeddings.cosine_similarity([1, 0], [1, 0]) == 1.0
    assert embeddings.cosine_similarity([1], [1, 0]) is None
    assert embeddings.cosine_similarity([], [1]) is None


def test_semantic_similarity_is_available_to_retrieval_evidence():
    import retrieval
    question = {"role": "产品运营", "stage": "项目深挖", "topic": "实验", "question": "如何设计实验？", "frequency": 1,
                "embedding": [1.0, 0.0], "_sources": [{"name": "来源"}]}
    ranked = retrieval.rank_questions({"projects": [], "skills": [], "risks": []}, "产品运营", "项目深挖", [question], query_embedding=[1.0, 0.0])
    assert ranked[0]["retrieval"]["retrieval_mode"] == "hybrid"
    assert ranked[0]["retrieval"]["semantic_score"] == 1.0


def test_retrieval_query_contains_only_minimized_profile_fields():
    import knowledge_plan

    query = knowledge_plan.retrieval_query(
        "产品运营", {"resume_text": "绝不能出现在检索文本的完整简历原文", "summary": "有实验经历", "skills": ["SQL"], "risks": ["指标口径"],
                   "projects": [{"name": "增长项目", "stack": ["Python"]}]},
    )

    assert "产品运营" in query
    assert "SQL" in query
    assert "增长项目" in query
    assert "完整简历原文" not in query


def test_curated_questions_receive_embeddings_once_when_provider_is_available(monkeypatch):
    import knowledge_plan

    monkeypatch.setattr(knowledge_plan, "_questions", [{"question": "如何设计增长实验？", "role": "产品运营", "topic": "实验"}])
    monkeypatch.setattr(knowledge_plan, "_curated_embeddings_ready", False)
    calls = []
    monkeypatch.setattr(knowledge_plan.embeddings, "is_configured", lambda: True)
    monkeypatch.setattr(knowledge_plan.embeddings, "embed_texts", lambda texts: calls.append(texts) or [[0.5, 0.5]])

    knowledge_plan.ensure_curated_embeddings()
    knowledge_plan.ensure_curated_embeddings()

    assert knowledge_plan._questions[0]["embedding"] == [0.5, 0.5]
    assert len(calls) == 1


def test_curated_embedding_failure_is_cooled_down_before_retry(monkeypatch):
    import knowledge_plan

    monkeypatch.setattr(knowledge_plan, "_questions", [{"question": "如何设计增长实验？"}])
    monkeypatch.setattr(knowledge_plan, "_curated_embeddings_ready", False)
    monkeypatch.setattr(knowledge_plan, "_curated_embeddings_last_attempt", 0.0)
    monkeypatch.setattr(knowledge_plan.embeddings, "is_configured", lambda: True)
    monkeypatch.setattr(knowledge_plan.time, "time", lambda: 100.0)
    calls = []
    monkeypatch.setattr(knowledge_plan.embeddings, "embed_texts", lambda texts: calls.append(texts) or None)

    knowledge_plan.ensure_curated_embeddings()
    knowledge_plan.ensure_curated_embeddings()

    assert len(calls) == 1


def test_query_embedding_uses_the_same_provider_vector_dimension_as_the_curated_index(monkeypatch):
    import knowledge_plan

    monkeypatch.setattr(knowledge_plan, "_loaded", True)
    monkeypatch.setattr(knowledge_plan, "_catalog", [{"role": "产品运营"}])
    monkeypatch.setattr(knowledge_plan, "_questions", [{
        "role": "产品运营", "stage": "自我介绍", "topic": "动机", "question": "为什么想做产品运营？",
        "frequency": 1, "supporting_sources": [{"source_url": "https://example.com/source"}],
        "embedding": [0.6, 0.8],
    }])
    monkeypatch.setattr(knowledge_plan, "_strategies", {})
    monkeypatch.setattr(knowledge_plan, "ensure_curated_embeddings", lambda: None)
    monkeypatch.setattr(knowledge_plan, "_db_role_buckets", lambda: {})
    monkeypatch.setattr(knowledge_plan, "embed_one", lambda _query: [0.6, 0.8])

    plan = knowledge_plan.build_plan("产品运营", "light", {"skills": [], "projects": [], "risks": []})

    assert plan[0]["retrieval"]["retrieval_mode"] == "hybrid"
    assert plan[0]["retrieval"]["semantic_score"] == 1.0
