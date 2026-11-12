import profile


def test_normalize_profile_keeps_only_bounded_structured_fields():
    result = profile.normalize_profile(
        {
            "skills": [f"skill-{index}" for index in range(12)],
            "projects": [
                {
                    "name": "增长分析平台",
                    "stack": ["Python", "SQL"],
                    "responsibilities": ["搭建指标体系"],
                    "outcomes": ["转化率提升 12%"],
                    "evidence_gaps": ["没有样本量"],
                }
            ],
            "strengths": ["数据分析"],
            "risks": ["归因不清"],
            "summary": "x" * 200,
            "raw_resume": "must not survive",
        },
        target_role="产品运营",
    )

    assert result["target_role"] == "产品运营"
    assert result["projects"][0]["name"] == "增长分析平台"
    assert result["summary"] == "x" * 120
    assert "raw_resume" not in result
    assert len(result["skills"]) == 8


def test_heuristic_profile_extracts_project_skill_outcome_and_gap():
    result = profile.heuristic_profile(
        "增长分析平台项目：用 Python 和 SQL 搭建指标体系，负责 A/B 测试看板，转化率提升 12%。",
        target_role="产品运营",
    )

    project = result["projects"][0]
    assert "Python" in result["skills"]
    assert "12%" in project["outcomes"][0]
    assert result["target_role"] == "产品运营"


def test_heuristic_profile_extracts_a_short_project_name_from_plain_resume_sentence():
    result = profile.heuristic_profile(
        "我做过校园招聘智能助手，负责需求调研、RAG 问答流程和 FastAPI 服务，用 SQL 看漏斗，用户完成率提升 18%。",
        target_role="AI 产品经理",
    )

    project = result["projects"][0]
    assert project["name"] == "校园招聘智能助手"
    assert "负责需求调研" in project["responsibilities"][0]
    assert len(project["name"]) < 20


def test_profile_strips_contact_details_from_heuristic_project_fields():
    result = profile.normalize_profile(
        {"projects": [{"name": "增长分析平台 13800000000 zhangsan@example.com", "responsibilities": ["联系人 13800000000"]}],
         "summary": "邮箱 zhangsan@example.com"},
        target_role="产品运营",
    )

    serialized = str(result)
    assert "13800000000" not in serialized
    assert "zhangsan@example.com" not in serialized
    assert "增长分析平台" in serialized


def test_profile_strips_contact_details_from_llm_shaped_lists_and_summary():
    result = profile.normalize_profile(
        {
            "skills": ["SQL", "联系 13800000000", "https://resume.example.com/private"],
            "strengths": ["邮箱 alice@example.com", "可做指标分析"],
            "risks": ["拨打 13900000000 核验", "归因方法未说明"],
            "projects": [{
                "name": "增长项目",
                "stack": ["Python", "mail@example.com secret-stack-token"],
                "responsibilities": ["联系 13800000000 负责人 secret-responsibility-token", "搭建漏斗"],
                "outcomes": ["转化提升 12%"],
            }],
            "summary": "候选人联系方式 13800000000，邮箱 alice@example.com secret-summary-token，擅长 SQL。",
        },
        target_role="产品运营",
    )

    serialized = str(result)
    assert "13800000000" not in serialized
    assert "13900000000" not in serialized
    assert "alice@example.com" not in serialized
    assert "mail@example.com" not in serialized
    assert "secret-stack-token" not in serialized
    assert "secret-responsibility-token" not in serialized
    assert "secret-summary-token" not in serialized
    assert "SQL" in result["skills"]
    assert "归因方法未说明" in result["risks"]


def test_contact_redaction_keeps_resume_content_needed_for_model_parsing():
    text = profile.redact_contact_details(
        "校园招聘智能助手，联系人 13800000000，邮箱 alice@example.com，作品 https://example.com/private"
    )

    assert "校园招聘智能助手" in text
    assert "13800000000" not in text
    assert "alice@example.com" not in text
    assert "example.com" not in text


def test_contact_redaction_removes_common_non_http_profile_urls():
    text = profile.redact_contact_details(
        "作品集 www.example.com/u/alice，GitHub github.com/alice/project，个人站 example.cn/resume，负责增长平台项目。"
    )

    assert "example.com" not in text
    assert "github.com" not in text
    assert "example.cn" not in text
    assert "增长平台项目" in text
