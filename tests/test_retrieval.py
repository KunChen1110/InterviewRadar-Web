import retrieval


def sourced(question, topic, text, frequency=1):
    return {"role": "产品运营", "stage": "项目深挖", "topic": topic,
            "question": text, "frequency": frequency,
            "_sources": [{"name": "真实面经", "url": "https://example.com"}]}


def test_retrieval_excludes_unsourced_and_prioritizes_profile_risk_terms():
    profile = {"target_role": "产品运营", "skills": ["SQL"], "risks": ["归因方法"],
               "projects": [{"name": "增长平台", "stack": ["SQL"], "evidence_gaps": ["归因方法"]}]}
    ranked = retrieval.rank_questions(profile, "产品运营", "项目深挖", [
        sourced("无关", "项目", "介绍一次活动执行"),
        sourced("相关", "实验", "如何验证 SQL 指标体系的归因方法？"),
        {"role": "产品运营", "stage": "项目深挖", "topic": "伪题", "question": "没有来源"},
    ])
    assert [item["question"] for item in ranked] == ["如何验证 SQL 指标体系的归因方法？", "介绍一次活动执行"]
    assert ranked[0]["retrieval"]["retrieval_mode"] == "keyword"
    assert "risks" in ranked[0]["retrieval"]["matched_profile_fields"]


def test_select_stage_questions_diversifies_topics():
    profile = {"target_role": "产品运营", "skills": ["SQL"], "projects": []}
    selected = retrieval.select_stage_questions(profile, "产品运营", "项目深挖", [
        sourced("a", "指标", "SQL 指标定义", 3), sourced("b", "指标", "SQL 指标口径", 2),
        sourced("c", "实验", "SQL A/B 实验", 1),
    ], 2)
    assert [item["topic"] for item in selected] == ["指标", "实验"]


def test_retrieval_weight_breaks_ties_for_preferred_sources():
    profile = {"skills": [], "risks": [], "projects": []}
    baseline = sourced("baseline", "topic", "source neutral question")
    preferred = sourced("preferred", "topic", "source preferred question")
    baseline.update(embedding=[1.0, 0.0], retrieval_weight=1.0)
    preferred.update(embedding=[1.0, 0.0], retrieval_weight=1.2)

    ranked = retrieval.rank_questions(profile, "产品运营", "项目深挖", [baseline, preferred], [1.0, 0.0])

    assert ranked[0]["question"] == "source preferred question"
