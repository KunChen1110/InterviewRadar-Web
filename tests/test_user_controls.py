from fastapi.testclient import TestClient

from backend import main


def test_report_export_is_authorized_and_contains_report(monkeypatch):
    main.SESSIONS.clear()
    monkeypatch.setattr(main.llm, "parse_resume", lambda _text: None)
    with TestClient(main.app) as client:
        session = client.post("/api/sessions", json={"resume_text": "项目：Python 数据平台，转化提升 10%。", "role": "运营策划"}).json()
        sid = session["session_id"]
        client.post(f"/api/sessions/{sid}/finish")
        response = client.get(f"/api/sessions/{sid}/report/export")

    assert response.status_code == 200
    assert "InterviewRadar 面试复盘" in response.text


def test_development_data_deletion_removes_cached_sessions(monkeypatch):
    main.SESSIONS["erase-me"] = {"id": "erase-me", "user_id": "anon"}
    main.DEV_FEEDBACK["erase-me"] = {"rating": 5, "comment": "test"}
    with TestClient(main.app) as client:
        response = client.request(
            "DELETE", "/api/account/data", json={"confirmation": "DELETE_MY_ACCOUNT"}
        )

    assert response.status_code == 200
    assert "erase-me" not in main.SESSIONS
    assert "erase-me" not in main.DEV_FEEDBACK


def test_finished_interview_accepts_a_single_feedback_rating(monkeypatch):
    main.SESSIONS.clear()
    main.DEV_FEEDBACK.clear()
    monkeypatch.setattr(main.llm, "parse_resume", lambda _text: None)
    with TestClient(main.app) as client:
        session = client.post("/api/sessions", json={"resume_text": "项目：指标建设", "role": "运营策划"}).json()
        sid = session["session_id"]
        client.post(f"/api/sessions/{sid}/finish")
        response = client.post(f"/api/sessions/{sid}/feedback", json={"rating": 5, "comment": "问题贴近岗位"})

    assert response.status_code == 200
    assert main.DEV_FEEDBACK[sid]["rating"] == 5


def test_admin_metrics_are_available_in_development_mode():
    with TestClient(main.app) as client:
        response = client.get("/api/admin/metrics")

    assert response.status_code == 200
    assert "sessions_total" in response.json()


def test_account_deletion_requires_the_explicit_confirmation_phrase():
    with TestClient(main.app) as client:
        response = client.request("DELETE", "/api/account/data", json={"confirmation": "no"})

    assert response.status_code == 400


def test_password_change_requires_current_password_and_invalidates_sessions(monkeypatch):
    from backend import auth

    monkeypatch.setattr(auth.db, "DB_URL", "postgresql://configured")
    monkeypatch.setattr(auth.db, "fetchone", lambda *_args: {"password_hash": auth._hash_password("old-password")})
    calls = []
    monkeypatch.setattr(
        auth.db,
        "change_password_and_revoke_tokens",
        lambda user_id, password_hash: calls.append((user_id, password_hash)) or True,
    )

    result = auth.change_password(
        auth.PasswordChangeBody(current_password="old-password", new_password="new-password"),
        {"id": "user-1"},
    )

    assert result == {"changed": True}
    assert calls[0][0] == "user-1"
    assert auth._verify_password("new-password", calls[0][1])


def test_development_account_export_contains_only_the_signed_in_users_sessions(monkeypatch):
    main.SESSIONS.clear()
    main.SESSIONS["mine"] = {"id": "mine", "user_id": "anon", "profile": {"skills": ["SQL"]}, "plan": [], "turns": [], "status": "finished"}
    main.SESSIONS["other"] = {"id": "other", "user_id": "someone-else", "profile": {"skills": ["secret"]}, "plan": [], "turns": [], "status": "finished"}
    with TestClient(main.app) as client:
        response = client.get("/api/account/data/export")

    assert response.status_code == 200
    assert "mine" in response.text
    assert "someone-else" not in response.text


def test_incomplete_session_can_be_loaded_for_its_owner_after_memory_cache_is_cleared(monkeypatch):
    main.SESSIONS.clear()
    monkeypatch.setattr(main.llm, "parse_resume", lambda _text: None)
    with TestClient(main.app) as client:
        created = client.post("/api/sessions", json={"resume_text": "项目：增长指标平台", "role": "运营策划"}).json()
        sid = created["session_id"]
        persisted = dict(main.SESSIONS[sid])
        main.SESSIONS.pop(sid)
        monkeypatch.setattr(main.db, "load_session", lambda requested_sid: persisted if requested_sid == sid else None)
        response = client.get(f"/api/sessions/{sid}/current")

    assert response.status_code == 200
    assert response.json()["status"] == "in_progress"


def test_heuristic_report_reflects_answer_evidence_instead_of_static_risks():
    session = {
        "scores": {"depth": 70, "clarity": 68, "logic": 72, "proof": 74},
        "turns": [{
            "question": "你如何优化接口延迟？",
            "answer": "我通过索引和缓存把 P95 从 600ms 降到 180ms，并完成压测验证。",
            "feedback": "回答较完整。",
            "delta": {"depth": 3, "clarity": 2, "logic": 3, "proof": 4},
        }],
        "memory": {"gaps": [], "strengths": ["回答包含量化或职责证据"]},
    }

    report = main._heuristic_report(session)

    assert all("缺少量化指标" not in item for item in report["risk_points"])
    assert any("600ms" in item or "180ms" in item for item in report["suggestions"])


def test_finish_is_idempotent_and_discloses_early_exit_coverage(monkeypatch):
    main.SESSIONS.clear()
    monkeypatch.setattr(main.llm, "parse_resume", lambda _text: None)
    report_calls = []
    monkeypatch.setattr(main.llm, "build_report", lambda session: report_calls.append(session["id"]) or None)
    with TestClient(main.app) as client:
        created = client.post("/api/sessions", json={
            "resume_text": "我做过增长看板，用 SQL 分析漏斗，注册转化提升 18%。",
            "role": "运营策划",
        }).json()
        sid = created["session_id"]
        client.post(f"/api/sessions/{sid}/answer", json={"answer": "我负责 SQL 漏斗分析，注册转化提升 18%。"})
        first = client.post(f"/api/sessions/{sid}/finish")
        second = client.post(f"/api/sessions/{sid}/finish")

    assert first.status_code == second.status_code == 200
    # With only one submitted answer, an evidence-based local report is more
    # honest than asking a model to infer a full interview from thin context.
    assert report_calls == []
    assert first.json()["coverage"]["is_early_exit"] is True
    assert first.json()["coverage"]["answered_main_questions"] == 1
    assert second.json()["report"] == first.json()["report"]


def test_report_and_export_preserve_early_exit_coverage(monkeypatch):
    main.SESSIONS.clear()
    monkeypatch.setattr(main.llm, "parse_resume", lambda _text: None)
    with TestClient(main.app) as client:
        created = client.post("/api/sessions", json={
            "resume_text": "我做过增长看板，用 SQL 分析漏斗，注册转化提升 18%。",
            "role": "运营策划",
        }).json()
        sid = created["session_id"]
        client.post(f"/api/sessions/{sid}/answer", json={"answer": "我负责 SQL 漏斗分析，注册转化提升 18%。"})
        client.post(f"/api/sessions/{sid}/finish")
        report = client.get(f"/api/sessions/{sid}/report")
        exported = client.get(f"/api/sessions/{sid}/report/export")

    assert report.status_code == exported.status_code == 200
    assert report.json()["coverage"]["is_early_exit"] is True
    assert "只完成了" in exported.text


def test_empty_early_exit_report_does_not_claim_answer_derived_risks(monkeypatch):
    main.SESSIONS.clear()
    monkeypatch.setattr(main.llm, "parse_resume", lambda _text: None)
    with TestClient(main.app) as client:
        created = client.post("/api/sessions", json={
            "resume_text": "我做过增长看板，用 SQL 分析漏斗，注册转化提升 18%。",
            "role": "运营策划",
        }).json()
        finished = client.post(f"/api/sessions/{created['session_id']}/finish").json()

    assert finished["coverage"]["answered_main_questions"] == 0
    assert "尚未回答主问题" in finished["report"]["risk_points"][0]
    assert all("部分回答缺少" not in item for item in finished["report"]["risk_points"])


def test_partial_interview_report_keeps_heuristic_output_when_the_model_hallucinates_empty_evidence(monkeypatch):
    main.SESSIONS.clear()
    monkeypatch.setattr(main.llm, "parse_resume", lambda _text: None)
    monkeypatch.setattr(main.llm, "build_report", lambda _session: {
        "overall": 96,
        "dimensions": {"depth": 99, "clarity": 99, "logic": 99, "proof": 99},
        "risk_points": ["已经完成所有环节，表现完美。"],
        "suggestions": ["无需改进。"],
        "next_plan": ["无。"],
        "generated_by": "llm",
    })
    with TestClient(main.app) as client:
        created = client.post("/api/sessions", json={
            "resume_text": "我做过增长看板，用 SQL 分析漏斗，注册转化提升 18%。",
            "role": "运营策划",
        }).json()
        sid = created["session_id"]
        client.post(f"/api/sessions/{sid}/answer", json={"answer": "我负责 SQL 漏斗分析，注册转化提升 18%。"})
        finished = client.post(f"/api/sessions/{sid}/finish").json()

    assert finished["coverage"]["is_early_exit"] is True
    assert finished["report"]["generated_by"] == "heuristic"
    assert "已经完成所有环节" not in " ".join(finished["report"]["risk_points"])
    assert "无需改进" not in " ".join(finished["report"]["suggestions"])
    assert "只覆盖 1/7 道主问题" in finished["report"]["risk_points"][0]


def test_model_report_cannot_override_recorded_dimension_scores(monkeypatch):
    main.SESSIONS.clear()
    main.SESSIONS["canonical-report"] = {
        "id": "canonical-report",
        "user_id": "anon",
        "kind": "full",
        "role": "运营策划",
        "mode": "normal",
        "profile": {},
        "plan": [{"step": "项目深挖"}, {"step": "能力匹配"}],
        "idx": 2,
        "follow_idx": -1,
        "max_turns": 4,
        "turns": [
            {"step": "项目深挖", "question": "Q1", "answer": "我用 SQL 分析漏斗，提升 18%，并做了验证。", "is_followup": False, "feedback": "ok", "delta": {}},
            {"step": "能力匹配", "question": "Q2", "answer": "我比较了两个方案并说明成本取舍。", "is_followup": False, "feedback": "ok", "delta": {}},
        ],
        "scores": {"depth": 71, "clarity": 68, "logic": 74, "proof": 70},
        "memory": {},
        "status": "finished",
        "created_at": "2026-08-12T00:00:00+00:00",
    }
    monkeypatch.setattr(main.llm, "build_report", lambda _session: {
        "overall": 99,
        "dimensions": {"depth": 99, "clarity": 99, "logic": 99, "proof": 99},
        "risk_points": ["有具体证据。"],
        "suggestions": ["继续保持。"],
        "next_plan": ["再练一次。"],
        "generated_by": "llm",
    })
    with TestClient(main.app) as client:
        result = client.post("/api/sessions/canonical-report/finish")

    assert result.status_code == 200
    report = result.json()["report"]
    assert report["dimensions"] == {"depth": 71, "clarity": 68, "logic": 74, "proof": 70}
    assert report["overall"] == 71
