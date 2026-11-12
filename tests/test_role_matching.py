"""Role resolution must not mix questions from different jobs."""

import time

from backend import knowledge_plan


def _seed_imported_roles(monkeypatch, roles):
    monkeypatch.setattr(
        knowledge_plan,
        "_db_questions",
        [{"role": role} for role in roles for _ in range(5)],
    )
    monkeypatch.setattr(knowledge_plan, "_db_loaded_at", time.time())


def test_exact_imported_role_wins_over_earlier_overlapping_role(monkeypatch):
    _seed_imported_roles(monkeypatch, ["前端开发", "后端开发"])

    assert knowledge_plan.resolve_role("后端开发") == "后端开发"


def test_role_suffixes_normalize_to_the_same_imported_role(monkeypatch):
    _seed_imported_roles(monkeypatch, ["前端开发", "后端开发"])

    assert knowledge_plan.resolve_role("后端开发岗位") == "后端开发"


def test_overlapping_but_different_imported_role_does_not_match(monkeypatch):
    _seed_imported_roles(monkeypatch, ["前端开发"])

    assert knowledge_plan.resolve_role("后端开发") is None


def test_ambiguous_short_role_does_not_choose_arbitrary_imported_role(monkeypatch):
    _seed_imported_roles(monkeypatch, ["前端开发", "后端开发"])

    assert knowledge_plan.resolve_role("开发") is None


def test_curated_role_alias_still_matches(monkeypatch):
    _seed_imported_roles(monkeypatch, ["前端开发", "后端开发"])

    assert knowledge_plan.resolve_role("AI产品经理") == "AI 产品经理"


def test_unrelated_curated_role_is_not_chosen_by_two_shared_characters(monkeypatch):
    _seed_imported_roles(monkeypatch, [])

    assert knowledge_plan.resolve_role("招聘策划") is None
