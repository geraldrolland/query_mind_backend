"""Application settings loaded from environment variables."""

from functools import lru_cache
from typing import List

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    APP_NAME: str = "QueryMind"
    APP_HOST: str = "http://localhost:3000"
    SECRET_KEY: str = "dev-secret-key-change-me"
    JWT_ACCESS_EXPIRE_MINUTES: int = 15
    JWT_REFRESH_EXPIRE_DAYS: int = 7
    COOKIE_SECURE: bool = False
    COOKIE_SAMESITE: str = "lax"
    COOKIE_PARTITIONED: bool = False
    COOKIE_HTTPONLY: bool = True
    CORS_ORIGINS: List[str] = ["http://localhost:3000", "http://localhost:3001"]

    DATABASE_URL: str = (
        "postgresql://postgres:querymind123@localhost:5433/querymind"
    )

    PROD_DATABASE_URL: str = ""

    REDIS_URL: str = "redis://localhost:6380/0"
    PROD_REDIS_URL: str = ""    
    REDIS_REFRESH_DB: int = 1
    REDIS_TOKEN: str = ""
    CACHE_EXPIRE_SECONDS: int = 300
    ENVIRONMENT: str = "development"

    GOOGLE_CLIENT_ID: str = ""
    GOOGLE_CLIENT_SECRET: str = ""
    GOOGLE_REDIRECT_URI: str = (
        "http://localhost:8000/api/v1/auth/google-auth"
    )
    GOOGLE_AUTH_URL: str = "https://accounts.google.com/o/oauth2/v2/auth"
    GOOGLE_TOKEN_URL: str = "https://oauth2.googleapis.com/token"
    GOOGLE_USERINFO_URL: str = "https://www.googleapis.com/oauth2/v2/userinfo"

    LLM_BASE_URL: str = "https://api.deepseek.com"
    LLM_API_KEY: str = ""
    LLM_MODEL: str = "deepseek-v4-flash"
    MAX_TOOL_ITERATIONS: int = 8

    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USERNAME: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM_EMAIL: str = "no-reply@querymind.app"
    SMTP_FROM_NAME: str = "QueryMind"
    SMTP_USE_STARTTLS: bool = True
    SMTP_USE_SSL: bool = False

    SENDGRID_API_KEY: str = ""

    MAX_UPLOAD_BYTES: int = 50 * 1024 * 1024


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
