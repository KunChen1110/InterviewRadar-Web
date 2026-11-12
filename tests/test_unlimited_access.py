from fastapi.testclient import TestClient

from backend import main


FULL_SESSION = {
    "resume_text": "项目：增长分析平台，使用 Python、PostgreSQL 和 Redis。",
    "role": "运营策划",
    "mode": "normal",
}


def test_registered_user_can_create_two_complete_interviews(monkeypatch):
    main.SESSIONS.clear()
    monkeypatch.setattr(main.llm, "parse_resume", lambda text: None)
    with TestClient(main.app) as client:
        first = client.post("/api/sessions", json=FULL_SESSION)
        second = client.post("/api/sessions", json=FULL_SESSION)

    assert first.status_code == second.status_code == 200
    assert first.json()["kind"] == second.json()["kind"] == "full"
    assert first.json()["plan_summary"]["total_questions"] > 3


def test_payment_endpoints_are_removed():
    with TestClient(main.app) as client:
        assert client.get("/api/plans").status_code == 404
        assert client.post("/api/orders", json={"plan": "single"}).status_code == 404
