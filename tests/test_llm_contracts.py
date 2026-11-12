from backend import llm


def test_turn_evaluation_falls_back_when_model_delta_is_not_an_object(monkeypatch):
    monkeypatch.setattr(llm, "_client", lambda: object())
    monkeypatch.setattr(llm, "_chat_json", lambda *_args, **_kwargs: {"delta": []})

    result = llm.evaluate_turn(
        {"role": "AI 产品经理", "profile": {}, "mode": "normal", "follow_idx": -1},
        {"topic": "项目经历", "desc": "考察项目", "question": "介绍你的项目"},
        "我负责了一个项目，并完成上线。",
    )

    assert result is None


def test_turn_evaluation_normalizes_non_string_model_feedback(monkeypatch):
    monkeypatch.setattr(llm, "_client", lambda: object())
    monkeypatch.setattr(
        llm,
        "_chat_json",
        lambda *_args, **_kwargs: {
            "delta": {"depth": 2, "clarity": 1, "logic": 1, "proof": 2},
            "need_followup": False,
            "followup_question": None,
            "feedback": 123,
        },
    )

    result = llm.evaluate_turn(
        {"role": "AI 产品经理", "profile": {}, "mode": "normal", "follow_idx": -1},
        {"topic": "项目经历", "desc": "考察项目", "question": "介绍你的项目"},
        "我负责了一个项目，并完成上线。",
    )

    assert result is not None
    assert result["feedback"] == "123"
    assert result["custom_followup"] == ""


def test_report_builder_rejects_malformed_or_empty_list_fields(monkeypatch):
    monkeypatch.setattr(llm, "_client", lambda: object())
    monkeypatch.setattr(
        llm,
        "_chat_json",
        lambda *_args, **_kwargs: {
            "overall": 80,
            "risk_points": "这不是列表",
            "suggestions": [],
            "next_plan": ["再练一次"],
        },
    )

    assert llm.build_report({"role": "AI 产品经理", "scores": {}, "turns": []}) is None


def test_report_builder_keeps_only_bounded_text_list_items(monkeypatch):
    monkeypatch.setattr(llm, "_client", lambda: object())
    monkeypatch.setattr(
        llm,
        "_chat_json",
        lambda *_args, **_kwargs: {
            "overall": 80,
            "risk_points": ["  缺少指标口径  ", 7, "x" * 700],
            "suggestions": ["补充样本和指标定义"],
            "next_plan": ["第 1 天整理项目", "第 2 天录音练习"],
        },
    )

    report = llm.build_report({"role": "AI 产品经理", "scores": {}, "turns": []})

    assert report is not None
    assert report["risk_points"] == ["缺少指标口径", "7", "x" * 500]
    assert report["suggestions"] == ["补充样本和指标定义"]
    assert report["next_plan"] == ["第 1 天整理项目", "第 2 天录音练习"]


def test_report_builder_rejects_non_numeric_overall_and_normalizes_dimensions(monkeypatch):
    monkeypatch.setattr(llm, "_client", lambda: object())
    monkeypatch.setattr(
        llm,
        "_chat_json",
        lambda *_args, **_kwargs: {
            "overall": "not-a-score",
            "risk_points": ["风险"],
            "suggestions": ["建议"],
            "next_plan": ["练习"],
        },
    )
    assert llm.build_report({"role": "AI 产品经理", "scores": {}, "turns": []}) is None

    monkeypatch.setattr(
        llm,
        "_chat_json",
        lambda *_args, **_kwargs: {
            "overall": "101",
            "dimensions": {"depth": "80", "clarity": 200, "logic": "bad"},
            "risk_points": ["风险"],
            "suggestions": ["建议"],
            "next_plan": ["练习"],
        },
    )
    report = llm.build_report({"role": "AI 产品经理", "scores": {}, "turns": []})
    assert report["overall"] == 100
    assert report["dimensions"] == {"depth": 80, "clarity": 100, "logic": 0, "proof": 0}


def test_resume_parser_rejects_malformed_project_shapes(monkeypatch):
    monkeypatch.setattr(llm, "_client", lambda: object())
    monkeypatch.setattr(
        llm,
        "_chat_json",
        lambda *_args, **_kwargs: {"skills": ["SQL"], "projects": "not-a-list"},
    )

    assert llm.parse_resume("我做过增长分析平台项目") is None


def test_resume_parser_drops_unexpected_extra_fields_and_bounds_lists(monkeypatch):
    monkeypatch.setattr(llm, "_client", lambda: object())
    monkeypatch.setattr(
        llm,
        "_chat_json",
        lambda *_args, **_kwargs: {
            "skills": ["SQL"] * 20,
            "projects": [{"name": "增长分析平台", "stack": ["Python", "SQL"]}],
            "strengths": ["数据分析"],
            "risks": ["指标口径"],
            "summary": "候选人画像",
            "raw_resume": "must not survive",
        },
    )

    result = llm.parse_resume("我做过增长分析平台项目")

    assert result is not None
    assert result["skills"] == ["SQL"] * 12
    assert result["projects"] == [{"name": "增长分析平台", "stack": ["Python", "SQL"]}]
    assert "raw_resume" not in result


def test_model_client_does_not_accept_per_request_credentials(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "server-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://server.example/v1")
    captured = {}

    class FakeClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    import openai
    monkeypatch.setattr(openai, "OpenAI", FakeClient)

    client = llm._client()

    assert isinstance(client, FakeClient)
    assert captured["api_key"] == "server-key"
    assert captured["base_url"] == "https://server.example/v1"
