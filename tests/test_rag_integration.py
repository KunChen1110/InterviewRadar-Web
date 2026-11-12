from fastapi.testclient import TestClient

from backend import main


def test_curated_plan_keeps_keyword_retrieval_evidence_for_each_question():
    profile = {
        "summary": "有增长运营实习经验，负责招聘活动转化分析。",
        "skills": ["SQL", "A/B 测试"],
        "projects": [{"name": "校招活动增长项目", "stack": ["Python", "指标体系"]}],
        "risks": ["指标表达不足"],
    }

    plan = main.knowledge_plan.build_plan("运营策划", "normal", profile)

    assert plan
    assert all(item["retrieval"]["retrieval_mode"] == "keyword" for item in plan)


def test_manual_reviewed_source_without_a_public_url_is_still_source_backed():
    metadata = main._question_source_metadata([{
        "sources": [{"name": "人工审核面经", "url": "", "note": "人工导入真题"}],
        "retrieval": {"retrieval_mode": "keyword"},
    }])

    assert metadata["source_backed"] is True
    assert metadata["question_source_mode"] == "keyword"


def test_session_creation_discloses_when_role_uses_generic_fallback(monkeypatch):
    main.SESSIONS.clear()
    monkeypatch.setattr(main.llm, "parse_resume", lambda _text: None)
    with TestClient(main.app) as client:
        response = client.post("/api/sessions", json={
            "resume_text": "我负责 FastAPI 服务和数据库优化，接口延迟降低 40%。",
            "role": "后端开发岗",
            "mode": "normal",
        })

    assert response.status_code == 200
    data = response.json()
    assert data["question_source_mode"] == "general_fallback"
    assert data["source_backed"] is False
    assert "未覆盖" in data["question_source_notice"]


def test_generic_fallback_opening_is_still_grounded_in_the_candidate_project(monkeypatch):
    main.SESSIONS.clear()
    monkeypatch.setattr(main.llm, "parse_resume", lambda _text: None)
    with TestClient(main.app) as client:
        created = client.post("/api/sessions", json={
            "resume_text": "我做过校园招聘智能助手，负责 FastAPI 服务和数据库优化，接口延迟降低 40%。",
            "role": "后端开发岗",
        }).json()
        plan = client.get(f"/api/sessions/{created['session_id']}/plan").json()["plan"]

    assert plan[0]["step"] == "自我介绍"
    assert "校园招聘智能助手" in plan[0]["question"]
    assert "后端开发岗" in plan[0]["question"]
    assert plan[0]["opening_personalized"] is True


def test_session_creation_does_not_label_synthetic_fixture_as_real_source(monkeypatch):
    main.SESSIONS.clear()
    monkeypatch.setattr(main.llm, "parse_resume", lambda _text: None)
    with TestClient(main.app) as client:
        response = client.post("/api/sessions", json={
            "resume_text": "我负责增长实验和 SQL 漏斗分析，注册转化提升 18%。",
            "role": "运营策划",
            "mode": "normal",
        })

    assert response.status_code == 200
    data = response.json()
    assert data["question_source_mode"] == "general_fallback"
    assert data["source_backed"] is False


def test_current_session_exposes_source_notice_for_recovered_page(monkeypatch):
    main.SESSIONS.clear()
    monkeypatch.setattr(main.llm, "parse_resume", lambda _text: None)
    with TestClient(main.app) as client:
        session = client.post("/api/sessions", json={
            "resume_text": "我负责 FastAPI 服务和数据库优化，接口延迟降低 40%。",
            "role": "后端开发岗",
        }).json()
        response = client.get(f"/api/sessions/{session['session_id']}/current")

    assert response.status_code == 200
    assert response.json()["question_source_mode"] == "general_fallback"


def test_recovered_synthetic_session_keeps_honest_source_metadata(monkeypatch):
    main.SESSIONS.clear()
    monkeypatch.setattr(main.llm, "parse_resume", lambda _text: None)
    with TestClient(main.app) as client:
        created = client.post("/api/sessions", json={
            "resume_text": "我负责增长实验和 SQL 漏斗分析，注册转化提升 18%。",
            "role": "运营策划",
        }).json()
        sid = created["session_id"]
        persisted = dict(main.SESSIONS[sid])
        for key in ("question_source_mode", "source_backed", "question_source_notice"):
            persisted.pop(key)
        main.SESSIONS.pop(sid)
        monkeypatch.setattr(main.db, "load_session", lambda requested_sid: persisted if requested_sid == sid else None)
        response = client.get(f"/api/sessions/{sid}/current")

    assert response.status_code == 200
    assert response.json()["source_backed"] is False
    assert response.json()["question_source_mode"] == "general_fallback"


def test_session_plan_exposes_retrieval_evidence_without_raw_resume(monkeypatch):
    main.SESSIONS.clear()
    monkeypatch.setattr(main.llm, "parse_resume", lambda _text: None)
    with TestClient(main.app) as client:
        response = client.post("/api/sessions", json={
            "resume_text": "增长分析平台项目：我用 SQL 和 Python 建指标体系，转化率提升 12%。",
            "role": "运营策划", "mode": "normal",
        })
        sid = response.json()["session_id"]
        plan = client.get(f"/api/sessions/{sid}/plan").json()["plan"]

    assert response.status_code == 200
    sourced = [item for item in plan if item.get("retrieval")]
    assert sourced
    assert all(item["retrieval"]["retrieval_mode"] == "keyword" for item in sourced)
    assert all("增长分析平台项目：我用" not in str(item["retrieval"]) for item in sourced)


def test_new_session_discards_raw_resume_after_profile_and_plan_are_created(monkeypatch):
    main.SESSIONS.clear()
    monkeypatch.setattr(main.llm, "parse_resume", lambda _text: None)
    raw_resume = "这是不应被持久保存的完整简历原文：联系方式 13800000000"
    with TestClient(main.app) as client:
        session = client.post("/api/sessions", json={"resume_text": raw_resume, "role": "运营策划"}).json()

    assert main.SESSIONS[session["session_id"]]["resume_text"] == ""


def test_session_redacts_contact_details_before_sending_resume_to_the_model(monkeypatch):
    main.SESSIONS.clear()
    seen = []
    monkeypatch.setattr(main.llm, "parse_resume", lambda text: seen.append(text) or None)
    raw_resume = (
        "我做过校园招聘智能助手，联系人 13800000000，"
        "邮箱 alice@example.com，作品 https://example.com/private。"
    )
    with TestClient(main.app) as client:
        response = client.post("/api/sessions", json={"resume_text": raw_resume, "role": "AI 产品经理"})

    assert response.status_code == 200
    assert seen
    assert "校园招聘智能助手" in seen[0]
    assert "13800000000" not in seen[0]
    assert "alice@example.com" not in seen[0]
    assert "example.com" not in seen[0]


def test_model_profile_can_only_reorder_candidates_and_never_change_the_interview_arc(monkeypatch):
    main.SESSIONS.clear()
    monkeypatch.setattr(main.llm, "parse_resume", lambda _text: None)
    # Deliberately reverse all candidate IDs: a model's preference must not
    # move a scene question ahead of the personalized introduction.
    monkeypatch.setattr(main.llm, "generate_plan", lambda _profile, candidates: [item["id"] for item in reversed(candidates)])
    with TestClient(main.app) as client:
        created = client.post("/api/sessions", json={
            "resume_text": "我做过校园招聘智能助手，负责需求调研、RAG 问答流程和 FastAPI 服务。",
            "role": "AI 产品经理",
            "mode": "normal",
        }).json()

    plan = main.SESSIONS[created["session_id"]]["plan"]
    stages = [item["step"] for item in plan]
    assert stages == sorted(stages, key={"自我介绍": 0, "项目深挖": 1, "能力匹配": 2, "场景问题": 3}.get)
    assert plan[0]["opening_personalized"] is True
    assert "校园招聘智能助手" in plan[0]["question"]
