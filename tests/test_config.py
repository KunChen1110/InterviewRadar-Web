import importlib

import pytest


def load_config(monkeypatch, **values):
    for key in (
        "APP_ENV",
        "DATABASE_URL",
        "OPENAI_API_KEY",
        "APP_SECRET_KEY",
        "APP_ADMIN_EMAILS",
        "ALIYUN_NLS_TOKEN",
        "ALIYUN_NLS_APP_KEY",
    ):
        monkeypatch.delenv(key, raising=False)
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    from backend import config

    return importlib.reload(config)


def test_production_requires_database_model_secret_and_admins(monkeypatch):
    config = load_config(monkeypatch, APP_ENV="production")

    with pytest.raises(config.ConfigurationError, match="DATABASE_URL"):
        config.load_settings()


def test_complete_production_settings_have_no_secret_in_repr(monkeypatch):
    config = load_config(
        monkeypatch,
        APP_ENV="production",
        DATABASE_URL="postgresql://example",
        OPENAI_API_KEY="secret-key",
        APP_SECRET_KEY="0123456789abcdef0123456789abcdef",
        APP_ADMIN_EMAILS="owner@example.com,ops@example.com",
        ALIYUN_NLS_TOKEN="nls-token",
        ALIYUN_NLS_APP_KEY="nls-app-key",
    )

    settings = config.load_settings()

    assert settings.is_production is True
    assert settings.admin_emails == frozenset({"owner@example.com", "ops@example.com"})
    assert "secret-key" not in repr(settings)


def test_production_can_start_without_optional_voice_credentials(monkeypatch):
    config = load_config(
        monkeypatch,
        APP_ENV="production",
        DATABASE_URL="postgresql://app:password@db:5432/interviewradar",
        OPENAI_API_KEY="secret-key",
        APP_SECRET_KEY="0123456789abcdef0123456789abcdef",
        APP_ADMIN_EMAILS="owner@example.com",
    )

    settings = config.load_settings()

    assert settings.is_production is True
    assert settings.aliyun_nls_token == ""
    assert settings.aliyun_nls_app_key == ""


def test_production_requires_a_strong_session_secret(monkeypatch):
    config = load_config(
        monkeypatch,
        APP_ENV="production",
        DATABASE_URL="postgresql://example",
        OPENAI_API_KEY="secret-key",
        APP_SECRET_KEY="too-short",
        APP_ADMIN_EMAILS="owner@example.com",
        ALIYUN_NLS_TOKEN="nls-token",
        ALIYUN_NLS_APP_KEY="nls-app-key",
    )

    with pytest.raises(config.ConfigurationError, match="APP_SECRET_KEY"):
        config.load_settings()


def test_production_rejects_a_development_database_host(monkeypatch):
    config = load_config(
        monkeypatch,
        APP_ENV="production",
        DATABASE_URL="postgresql://app:password@localhost:5432/interviewradar",
        OPENAI_API_KEY="secret-key",
        APP_SECRET_KEY="0123456789abcdef0123456789abcdef",
        APP_ADMIN_EMAILS="owner@example.com",
        ALIYUN_NLS_TOKEN="nls-token",
        ALIYUN_NLS_APP_KEY="nls-app-key",
    )

    with pytest.raises(config.ConfigurationError, match="DATABASE_URL"):
        config.load_settings()


def test_schema_includes_resume_minimization_migration():
    from backend import db

    assert "UPDATE sessions SET resume_text = ''" in "\n".join(db.DDL)
