import interview_plan


def test_personalized_first_question_uses_resume_project_and_real_source(monkeypatch):
    candidates = [
        {
            "step": "项目深挖",
            "topic": "项目经历",
            "question": "介绍一个你最有成就感的项目。",
            "anchor": "AI 产品经理 · 项目经历",
            "sources": [{"name": "牛客面经", "url": "https://example.com/q1"}],
            "frequency": 1,
        },
        {
            "step": "能力匹配",
            "topic": "数据分析",
            "question": "你如何设计指标体系？",
            "anchor": "AI 产品经理 · 数据分析",
            "sources": [{"name": "牛客面经", "url": "https://example.com/q2"}],
            "frequency": 2,
        },
    ]
    monkeypatch.setattr(interview_plan.knowledge_plan, "build_plan", lambda *args: candidates)

    plan = interview_plan.build_personalized_plan(
        "AI 产品经理", "normal", {"projects": ["负责 RAG 知识库问答平台"], "skills": ["RAG"]}
    )

    assert plan[0]["anchor"] == "负责 RAG 知识库问答平台"
    assert plan[0]["sources"]
    assert "RAG" in plan[0]["question"]


def test_plan_without_sources_is_rejected(monkeypatch):
    monkeypatch.setattr(
        interview_plan.knowledge_plan,
        "build_plan",
        lambda *args: [{"step": "自我介绍", "question": "请介绍自己", "sources": []}],
    )

    assert interview_plan.build_personalized_plan("后端", "normal", {"projects": []}) == []


def test_invalid_model_choice_falls_back_to_source_backed_candidates(monkeypatch):
    candidates = [{
        "step": "项目深挖", "topic": "项目", "question": "介绍你的项目",
        "anchor": "岗位", "sources": [{"url": "https://example.com"}],
    }]
    monkeypatch.setattr(interview_plan.knowledge_plan, "build_plan", lambda *args: candidates)

    plan = interview_plan.build_personalized_plan(
        "后端", "normal", {"projects": []}, planner=lambda *_: ["invented-id"]
    )

    assert [item["question"] for item in plan] == ["介绍你的项目"]


def test_opening_question_is_natural_and_grounded_in_candidate_project(monkeypatch):
    candidates = [
        {
            "step": "自我介绍", "topic": "求职动机", "question": "请做自我介绍。",
            "anchor": "产品运营", "sources": [{"name": "真实面经", "url": "https://example.com/intro"}],
        },
        {
            "step": "项目深挖", "topic": "增长", "question": "介绍一个增长项目。",
            "anchor": "产品运营", "sources": [{"name": "真实面经", "url": "https://example.com/project"}],
        },
    ]
    monkeypatch.setattr(interview_plan.knowledge_plan, "build_plan", lambda *args: candidates)

    plan = interview_plan.build_personalized_plan(
        "产品运营", "normal", {"projects": [{"name": "增长实验平台", "responsibilities": ["设计指标"]}], "skills": ["SQL"]}
    )

    assert plan[0]["step"] == "自我介绍"
    assert "增长实验平台" in plan[0]["question"]
    assert "请做自我介绍" not in plan[0]["question"]
    assert plan[0]["sources"]


def test_stage_flow_keeps_intro_first_even_when_project_question_is_more_frequent(monkeypatch):
    candidates = [
        {"step": "自我介绍", "topic": "动机", "question": "请做自我介绍。", "frequency": 1,
         "sources": [{"name": "真实面经", "url": "https://example.com/intro"}]},
        {"step": "项目深挖", "topic": "增长", "question": "介绍增长项目。", "frequency": 99,
         "sources": [{"name": "真实面经", "url": "https://example.com/project"}]},
    ]
    monkeypatch.setattr(interview_plan.knowledge_plan, "build_plan", lambda *args: candidates)

    plan = interview_plan.build_personalized_plan(
        "产品运营", "normal", {"projects": [{"name": "增长实验平台"}], "skills": []}
    )

    assert [item["step"] for item in plan] == ["自我介绍", "项目深挖"]


def test_project_deep_dive_is_rephrased_around_the_candidate_project_without_returning_raw_source_question(monkeypatch):
    candidates = [
        {"step": "自我介绍", "topic": "动机", "question": "请做自我介绍。",
         "sources": [{"name": "真实面经", "url": "https://example.com/intro"}]},
        {"step": "项目深挖", "topic": "项目经历", "question": "介绍 AI 创业项目及你在其中的职责。",
         "sources": [{"name": "真实面经", "url": "https://example.com/project"}]},
    ]
    monkeypatch.setattr(interview_plan.knowledge_plan, "build_plan", lambda *args: candidates)

    plan = interview_plan.build_personalized_plan(
        "AI 产品经理", "normal", {"projects": [{"name": "校园招聘智能助手"}], "skills": ["RAG", "SQL"]}
    )

    deep_dive = plan[1]
    assert "校园招聘智能助手" in deep_dive["question"]
    assert "AI 创业项目" not in deep_dive["question"]
    assert "原始题意" not in deep_dive["question"]
    assert "个人职责、协作边界和实际产出" in deep_dive["question"]
    assert "source_question" not in deep_dive
    assert deep_dive["sources"]
