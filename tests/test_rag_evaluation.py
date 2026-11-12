from rag_evaluation import evaluate_cases
import interview_plan


def test_evaluation_reports_complete_source_backed_case():
    def builder(role, mode, profile):
        return [{
            "step": "自我介绍",
            "question": f"为什么想做{role}？",
            "sources": [{"name": "许可数据集", "url": "https://example.com/source"}],
            "retrieval": {"retrieval_mode": "keyword"},
            "opening_personalized": True,
        }]

    report = evaluate_cases([{
        "id": "ops",
        "role": "运营策划",
        "mode": "normal",
        "profile": {"projects": [{"name": "增长项目"}]},
    }], builder=builder)

    assert report["passed"] is True
    assert report["summary"] == {"total": 1, "passed": 1, "failed": 0}
    assert report["cases"][0]["issues"] == []


def test_evaluation_flags_missing_source_coverage():
    report = evaluate_cases([{
        "id": "backend",
        "role": "后端开发",
        "mode": "normal",
        "profile": {},
    }], builder=lambda *_args: [])

    assert report["passed"] is False
    assert "no_source_backed_plan" in report["cases"][0]["issues"]


def test_live_evaluation_distinguishes_covered_and_uncovered_roles():
    profile = {
        "summary": "有增长项目经验。",
        "skills": ["SQL", "Python"],
        "projects": [{"name": "增长项目", "stack": ["指标体系"]}],
        "risks": [],
    }

    report = evaluate_cases([
        {"id": "ops", "role": "运营策划", "mode": "normal", "profile": profile},
        {"id": "backend", "role": "后端开发", "mode": "normal", "profile": profile},
    ], builder=interview_plan.build_personalized_plan)

    assert report["summary"] == {"total": 2, "passed": 1, "failed": 1}
    assert report["cases"][0]["issues"] == []
    assert report["cases"][1]["issues"] == ["no_source_backed_plan"]
