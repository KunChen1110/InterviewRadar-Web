import threading

import main
import auth
from fastapi import HTTPException


USER = {"id": "candidate-1", "email": "candidate@example.com", "nickname": "候选人"}


def _session(monkeypatch) -> str:
    main.SESSIONS.clear()
    monkeypatch.setattr(main.db, "DB_URL", "")
    monkeypatch.setattr(main.llm, "parse_resume", lambda _text: None)
    monkeypatch.setattr(main.llm, "evaluate_turn", lambda *_args: None)
    return main.create_session(main.SessionCreate(
        resume_text="我负责增长分析平台，用 SQL 建漏斗，转化率提升 18%。",
        role="运营策划", mode="light",
    ), USER)["session_id"]


def test_concurrent_answers_for_one_session_are_serialized(monkeypatch):
    sid = _session(monkeypatch)
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def slow_evaluation(*_args):
        calls.append("evaluated")
        entered.set()
        release.wait(timeout=2)
        return None

    monkeypatch.setattr(main.llm, "evaluate_turn", slow_evaluation)
    first = threading.Thread(
        target=lambda: main.submit_answer(sid, main.AnswerSubmit(answer="我先定义指标，再验证结果，提升 18%。"), USER),
    )
    first.start()
    assert entered.wait(timeout=1)

    second_error = []

    def submit_second_answer():
        try:
            main.submit_answer(sid, main.AnswerSubmit(answer="这不应被当作下一题答案。"), USER)
        except HTTPException as exc:
            second_error.append(exc)

    second = threading.Thread(target=submit_second_answer)
    second.start()
    second.join(timeout=1)
    release.set()
    first.join(timeout=2)

    assert not second.is_alive()
    assert len(calls) == 1
    assert len(main.SESSIONS[sid]["turns"]) == 1
    assert len(second_error) == 1
    assert second_error[0].status_code == 409


def test_finish_cannot_race_an_inflight_answer_for_the_same_session(monkeypatch):
    sid = _session(monkeypatch)
    entered = threading.Event()
    release = threading.Event()

    def slow_evaluation(*_args):
        entered.set()
        release.wait(timeout=2)
        return None

    monkeypatch.setattr(main.llm, "evaluate_turn", slow_evaluation)
    answer = threading.Thread(
        target=lambda: main.submit_answer(sid, main.AnswerSubmit(answer="我先定义指标，再验证结果，提升 18%。"), USER),
    )
    answer.start()
    assert entered.wait(timeout=1)

    try:
        main.finish(sid, USER)
    except HTTPException as exc:
        assert exc.status_code == 409
    else:
        raise AssertionError("finish must not generate a report while an answer is mutating the session")
    finally:
        release.set()
        answer.join(timeout=2)


def test_memory_reprioritization_does_not_cross_the_next_interview_stage():
    session = {
        "idx": 1,
        "memory": {"gaps": ["样本量"], "followup_targets": []},
        "plan": [
            {"step": "自我介绍", "question": "Q0", "topic": "介绍", "desc": ""},
            {"step": "项目深挖", "question": "Q1", "topic": "项目", "desc": ""},
            {"step": "能力匹配", "question": "Q2", "topic": "样本量", "desc": ""},
            {"step": "场景问题", "question": "Q3", "topic": "样本量", "desc": ""},
        ],
    }

    main._prioritize_remaining_questions(session)

    assert [item["question"] for item in session["plan"]] == ["Q0", "Q1", "Q2", "Q3"]


def test_heuristic_scoring_cannot_reach_a_high_score_from_repeated_keyword_padding():
    session = {"follow_idx": -1}
    question = {"step": "项目深挖"}
    padded = "首先 项目 系统 接口 数据库 指标 负责 实现 优化 排查 上线 测试 方案 原因 结果 999"

    result = main._heuristic_evaluate(session, question, padded)

    assert max(result["delta"].values()) <= 2


def test_score_ceiling_only_expands_with_completed_interview_coverage(monkeypatch):
    sid = _session(monkeypatch)
    monkeypatch.setattr(main.llm, "evaluate_turn", lambda *_args: {
        "delta": {"depth": 6, "clarity": 6, "logic": 6, "proof": 6},
        "need_followup": False, "custom_followup": "", "feedback": "很好",
        "is_followup_response": False, "generated_by": "llm",
    })

    result = main.submit_answer(sid, main.AnswerSubmit(answer="完整且具体的回答，包含 18% 提升和验证方式。"), USER)

    assert result["scores"] == {"depth": 64, "clarity": 64, "logic": 64, "proof": 64}


def test_rejection_refuses_to_overwrite_a_submission_already_processed(monkeypatch):
    monkeypatch.setattr(auth.db, "fetchone", lambda *_args: {
        "id": 9, "user_id": "candidate-1", "company": "测试公司", "role": "运营策划",
        "interview_date": None, "questions": [], "status": "pending",
    })
    monkeypatch.setattr(auth.db, "reject_pending_submission", lambda *_args: False, raising=False)

    try:
        auth._moderate_submission(9, "rejected", "来源无法核验")
    except HTTPException as exc:
        assert exc.status_code == 409
    else:
        raise AssertionError("a stale rejection must not overwrite an approved submission")
