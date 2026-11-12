"""The public repository's documented package layout remains runnable."""

from pathlib import Path

from fastapi.testclient import TestClient

from backend import main


def test_backend_entrypoint_finds_root_assets_and_health_route(monkeypatch):
    assert Path(main.BASE_DIR) == Path(__file__).resolve().parents[1]
    monkeypatch.setattr(main.db, "is_ready", lambda: True)

    with TestClient(main.app) as client:
        assert client.get("/ready").json() == {"status": "ready"}
        assert client.get("/web/landing.html").status_code == 200
        assert client.get("/assets/interviewer.svg").status_code == 200
