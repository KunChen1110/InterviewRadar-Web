from datetime import datetime, timezone

import pytest

from backend import main


def restored_session() -> dict:
    return {
        "id": "restored-session",
        "user_id": "user-1",
        "kind": "full",
        "role": "AI 产品经理",
        "mode": "normal",
        "profile": {"skills": [], "projects": []},
        "plan": [],
        "max_turns": 7,
        "idx": 0,
        "follow_idx": -1,
        "turns": [],
        "scores": {"depth": 60, "clarity": 60, "logic": 60, "proof": 60},
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "in_progress",
    }


def test_session_is_restored_after_cache_is_cleared(monkeypatch):
    main.SESSIONS.clear()
    expected = restored_session()
    monkeypatch.setattr(main.db, "load_session", lambda sid: expected if sid == expected["id"] else None, raising=False)

    session = main._get(expected["id"], {"id": "user-1"})

    assert session == expected
    assert main.SESSIONS[expected["id"]] == expected


def test_other_user_cannot_restore_session(monkeypatch):
    main.SESSIONS.clear()
    expected = restored_session()
    monkeypatch.setattr(main.db, "load_session", lambda sid: expected if sid == expected["id"] else None, raising=False)

    with pytest.raises(main.HTTPException) as exc_info:
        main._get(expected["id"], {"id": "user-2"})

    assert exc_info.value.status_code == 404
