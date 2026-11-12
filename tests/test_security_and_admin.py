from fastapi.testclient import TestClient

from backend import main
from backend.auth import require_admin
from fastapi import HTTPException


def test_debug_endpoints_are_not_public():
    with TestClient(main.app) as client:
        assert client.get("/api/debug/db").status_code == 404
        assert client.post("/api/debug/selftest", json={}).status_code == 404


def test_ready_requires_database_connection(monkeypatch):
    monkeypatch.setattr(main.db, "fetchone", lambda *args, **kwargs: None)
    with TestClient(main.app) as client:
        assert client.get("/ready").status_code == 503

    monkeypatch.setattr(main.db, "fetchone", lambda *args, **kwargs: {"one": 1})
    with TestClient(main.app) as client:
        assert client.get("/ready").status_code == 200


def test_development_operator_is_allowed_without_production_email(monkeypatch):
    monkeypatch.setattr(main.auth, "load_settings", lambda: type("Settings", (), {"is_production": False, "admin_emails": frozenset()})())
    assert require_admin({"id": "anon", "email": "dev@local"})["id"] == "anon"


def test_non_operator_is_rejected(monkeypatch):
    monkeypatch.setattr(main.auth, "load_settings", lambda: type("Settings", (), {"is_production": True, "admin_emails": frozenset({"owner@example.com"})})())
    try:
        require_admin({"id": "u1", "email": "member@example.com"})
    except HTTPException as exc:
        assert exc.status_code == 403
    else:
        raise AssertionError("non-operator must be rejected")


def test_sensitive_account_and_admin_routes_have_a_rate_limit():
    from backend import security

    assert security._limit_for("/api/account/password") == ("/api/account/", 8)
    assert security._limit_for("/api/admin/metrics") == ("/api/admin/", 30)


def test_rate_limits_target_expensive_or_sensitive_writes_not_normal_reads():
    from backend import security

    assert security._limit_for("/api/auth/login") == ("/api/auth/credentials", 12)
    assert security._limit_for("/api/auth/register") == ("/api/auth/credentials", 12)
    assert security._limit_for("/api/sessions") == ("/api/sessions/create", 20)
    assert security._limit_for("/api/sessions/abc/transcribe") == ("/api/sessions/transcribe", 12)
    assert security._limit_for("/api/auth/me") is None
    assert security._limit_for("/api/sessions/mine") is None


def test_public_beta_adds_a_request_id_to_response_headers():
    with TestClient(main.app) as client:
        response = client.get("/api/health")

    assert response.status_code == 200
    assert len(response.headers["X-Request-ID"]) == 12


def test_rate_limit_uses_the_nginx_real_ip_header_only_in_production(monkeypatch):
    from backend import security

    monkeypatch.setattr(security, "_is_production", lambda: True)
    assert security._client_key(type("Request", (), {
        "client": type("Client", (), {"host": "172.18.0.3"})(),
        "headers": {"x-real-ip": "203.0.113.9"},
    })()) == "203.0.113.9"

    monkeypatch.setattr(security, "_is_production", lambda: False)
    assert security._client_key(type("Request", (), {
        "client": type("Client", (), {"host": "127.0.0.1"})(),
        "headers": {"x-real-ip": "203.0.113.9"},
    })()) == "127.0.0.1"


def test_development_mode_does_not_apply_public_rate_limits(monkeypatch):
    from backend import security

    security._requests.clear()
    monkeypatch.setattr(security, "_is_production", lambda: False)
    with TestClient(main.app) as client:
        responses = [client.get("/api/auth/me") for _ in range(13)]

    assert all(response.status_code == 200 for response in responses)


def test_production_does_not_throttle_normal_authenticated_read_endpoints(monkeypatch):
    from backend import security

    security._requests.clear()
    monkeypatch.setattr(security, "_is_production", lambda: True)
    with TestClient(main.app) as client:
        responses = [client.get("/api/auth/me") for _ in range(13)]

    assert all(response.status_code == 200 for response in responses)


def test_session_api_rejects_oversized_resume_and_answer(monkeypatch):
    main.SESSIONS.clear()
    monkeypatch.setattr(main.llm, "parse_resume", lambda _text: None)
    with TestClient(main.app) as client:
        oversized_resume = client.post("/api/sessions", json={
            "resume_text": "a" * 12001,
            "role": "AI 产品经理",
        })
        created = client.post("/api/sessions", json={
            "resume_text": "我做过校园招聘智能助手，负责需求调研和 RAG 问答流程。",
            "role": "AI 产品经理",
        }).json()
        oversized_answer = client.post(
            f"/api/sessions/{created['session_id']}/answer",
            json={"answer": "a" * 6001},
        )

    assert oversized_resume.status_code == 422
    assert oversized_answer.status_code == 422


def test_session_api_rejects_unknown_interview_mode(monkeypatch):
    main.SESSIONS.clear()
    monkeypatch.setattr(main.llm, "parse_resume", lambda _text: None)
    with TestClient(main.app) as client:
        response = client.post("/api/sessions", json={
            "resume_text": "我做过校园招聘智能助手，负责需求调研和 RAG 问答流程。",
            "role": "AI 产品经理",
            "mode": "unbounded",
        })

    assert response.status_code == 422


def test_resume_upload_rejects_content_type_that_does_not_match_the_extension():
    with TestClient(main.app) as client:
        response = client.post(
            "/api/parse_resume",
            files={"file": ("resume.pdf", b"not really a pdf", "text/plain")},
        )

    assert response.status_code == 400
    assert "格式" in response.json()["detail"]


def test_static_file_routes_reject_encoded_path_traversal():
    with TestClient(main.app) as client:
        web = client.get("/web/..%2F.env")
        asset = client.get("/assets/..%2Fmain.py")

    assert web.status_code == asset.status_code == 404
