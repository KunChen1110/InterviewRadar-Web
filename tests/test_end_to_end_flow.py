from fastapi.testclient import TestClient

import main


def test_full_synthetic_text_interview_flow_survives_cache_recovery(monkeypatch):
    """Exercise the public candidate path without a live model, database, or licensed corpus."""
    main.SESSIONS.clear()
    main.DEV_FEEDBACK.clear()
    monkeypatch.setattr(main.llm, "parse_resume", lambda _text: None)
    monkeypatch.setattr(main.llm, "evaluate_turn", lambda *_args: None)
    monkeypatch.setattr(main.llm, "build_report", lambda _session: None)

    with TestClient(main.app) as client:
        created = client.post("/api/sessions", json={
            "resume_text": (
                "我做过校园招聘智能助手，负责需求调研、RAG 问答流程和 FastAPI 服务，"
                "用 SQL 看漏斗，用户完成率提升 18%。"
            ),
            "role": "AI 产品经理",
            "mode": "light",
        })
        assert created.status_code == 200
        session = created.json()
        sid = session["session_id"]
        assert session["source_backed"] is False
        assert session["profile"]["projects"][0]["name"] == "校园招聘智能助手"
        assert "校园招聘智能助手" in main.SESSIONS[sid]["plan"][0]["question"]
        assert all(
            "校园招聘智能助手" in item["question"]
            for item in main.SESSIONS[sid]["plan"]
            if item["step"] == "项目深挖"
        )

        persisted = dict(main.SESSIONS[sid])
        persisted.pop("question_source_mode", None)
        persisted.pop("source_backed", None)
        persisted.pop("question_source_notice", None)
        main.SESSIONS.pop(sid)
        monkeypatch.setattr(main.db, "load_session", lambda requested_sid: persisted if requested_sid == sid else None)

        resumed = client.get(f"/api/sessions/{sid}/current")
        assert resumed.status_code == 200
        assert resumed.json()["source_backed"] is False

        while True:
            answer = client.post(f"/api/sessions/{sid}/answer", json={
                "answer": "我先明确目标和约束，再用 SQL 建立漏斗，完成 A/B 验证；完成率从 62% 提升到 80%，并通过压测和复盘确认。"
            })
            assert answer.status_code == 200
            if answer.json()["done"]:
                break

        finished = client.post(f"/api/sessions/{sid}/finish")
        assert finished.status_code == 200
        assert finished.json()["already_finished"] is False
        assert finished.json()["coverage"]["is_early_exit"] is False

        repeated = client.post(f"/api/sessions/{sid}/finish")
        report = client.get(f"/api/sessions/{sid}/report")
        feedback = client.post(f"/api/sessions/{sid}/feedback", json={"rating": 5, "comment": "问题贴近项目"})
        exported_report = client.get(f"/api/sessions/{sid}/report/export")
        exported_account = client.get("/api/account/data/export")

    assert repeated.status_code == 200
    assert repeated.json()["already_finished"] is True
    assert report.status_code == 200
    assert report.json()["coverage"]["is_early_exit"] is False
    assert feedback.status_code == 200
    assert exported_report.status_code == 200
    assert "完成计划中的全部主问题" in exported_report.text
    assert exported_account.status_code == 200
    assert "校园招聘智能助手" in exported_account.text
    assert "完整简历原文" not in exported_account.text
