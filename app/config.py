"""
Central configuration. All values are overridable via environment variables,
which is how this gets configured in production (Render/Railway/Fly env vars,
or a real secrets manager in an enterprise deployment).
"""
import os
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    APP_NAME: str = "Agent Identity Management System"
    ENV: str = os.getenv("ENV", "development")

    # Database: defaults to local SQLite file for zero-config dev, but honours
    # DATABASE_URL for a real Postgres instance in production (e.g. Render/Neon/Supabase).
    DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite:///./agent_identity.db")

    # JWT signing secret for issuing scoped, time-bounded agent credentials.
    # MUST be overridden in production via env var.
    JWT_SECRET: str = os.getenv("JWT_SECRET", "dev-only-change-me-in-production")
    JWT_ALGORITHM: str = "HS256"

    # Default credential lifetime (time-bounded, like human access reviews).
    DEFAULT_CREDENTIAL_TTL_DAYS: int = int(os.getenv("DEFAULT_CREDENTIAL_TTL_DAYS", "90"))

    # Stale = no API call in N days (quarterly review rule from the PS).
    STALE_THRESHOLD_DAYS: int = int(os.getenv("STALE_THRESHOLD_DAYS", "30"))

    # Admin API key required to call privileged endpoints (register/suspend/etc.)
    # In a real enterprise deployment this would be OIDC/SSO — see bonus section in README.
    ADMIN_API_KEY: str = os.getenv("ADMIN_API_KEY", "dev-admin-key")

    # Groq (free, fast inference) powers the autonomous review agent.
    GROQ_API_KEY: str = os.getenv("GROQ_API_KEY", "")
    GROQ_MODEL: str = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")

    # How often the autonomous review agent runs itself, with no human trigger.
    # Set to 0 to disable the scheduler entirely (manual trigger via API still works).
    AI_REVIEW_INTERVAL_HOURS: float = float(os.getenv("AI_REVIEW_INTERVAL_HOURS", "6"))


settings = Settings()
