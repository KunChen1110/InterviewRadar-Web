from backend import interview_memory


def test_memory_extracts_missing_evidence_and_is_bounded():
    update = interview_memory.extract_update("项目提升了 12%，但是没有说明样本量和归因方法。")
    memory = interview_memory.merge_memory({}, update)
    memory = interview_memory.merge_memory(memory, {"gaps": [f"gap-{i}" for i in range(10)]})

    assert "样本量" in " ".join(memory["gaps"])
    assert len(memory["gaps"]) == 6
    assert len(memory["followup_targets"]) <= 6


def test_memory_query_terms_prioritize_unresolved_gaps():
    terms = interview_memory.query_terms({"gaps": ["归因方法未说明"], "followup_targets": ["样本量"]})
    assert terms == ["归因方法未说明", "样本量"]
