"""Runtime configuration for InterviewRadar."""
from __future__ import annotations

from dataclasses import dataclass
import os
from urllib.parse import urlparse


class ConfigurationError(RuntimeError):
    """Raised when deployment configuration is incomplete or invalid."""


@dataclass(frozen=True, repr=False)
class Settings:
    app_env: str
    database_url: str
    openai_api_key: str
    app_secret_key: str
    admin_emails: frozenset[str]
    aliyun_nls_token: str
    aliyun_nls_app_key: str

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"


def load_settings() -> Settings:
    app_env = os.environ.get("APP_ENV", "development").strip().lower()
    if app_env not in {"development", "production"}:
        raise ConfigurationError("APP_ENV must be development or production")

    values = {
        "DATABASE_URL": os.environ.get("DATABASE_URL", "").strip(),
        "OPENAI_API_KEY": os.environ.get("OPENAI_API_KEY", "").strip(),
        "APP_SECRET_KEY": os.environ.get("APP_SECRET_KEY", "").strip(),
        "APP_ADMIN_EMAILS": os.environ.get("APP_ADMIN_EMAILS", "").strip(),
        "ALIYUN_NLS_TOKEN": os.environ.get("ALIYUN_NLS_TOKEN", "").strip(),
        "ALIYUN_NLS_APP_KEY": os.environ.get("ALIYUN_NLS_APP_KEY", "").strip(),
    }
    if app_env == "production":
        required = (
            "DATABASE_URL", "OPENAI_API_KEY", "APP_SECRET_KEY", "APP_ADMIN_EMAILS",
        )
        missing = [name for name in required if not values[name]]
        if missing:
            raise ConfigurationError(
                "missing required production settings: " + ", ".join(missing)
            )
        if len(values["APP_SECRET_KEY"]) < 32:
            raise ConfigurationError("APP_SECRET_KEY must be at least 32 characters in production")
        db_host = (urlparse(values["DATABASE_URL"]).hostname or "").lower()
        if db_host in {"localhost", "127.0.0.1", "::1"}:
            raise ConfigurationError("DATABASE_URL must point to a production database host")

    return Settings(
        app_env=app_env,
        database_url=values["DATABASE_URL"],
        openai_api_key=values["OPENAI_API_KEY"],
        app_secret_key=values["APP_SECRET_KEY"],
        admin_emails=frozenset(
            email.strip().lower()
            for email in values["APP_ADMIN_EMAILS"].split(",")
            if email.strip()
        ),
        aliyun_nls_token=values["ALIYUN_NLS_TOKEN"],
        aliyun_nls_app_key=values["ALIYUN_NLS_APP_KEY"],
    )
