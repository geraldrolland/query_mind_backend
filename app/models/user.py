from datetime import datetime, timezone
from enum import Enum

from sqlmodel import Field, SQLModel


class AuthProviderEnum(str, Enum):
    email = "email"
    google = "google"


class User(SQLModel, table=True):
    __tablename__ = "qm_user"

    id: int = Field(primary_key=True)
    email: str = Field(index=True, unique=True)
    hashed_password: str | None = Field(default=None)
    auth_provider: str = Field(default="email")
    is_email_verified: bool = Field(default=False)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc), nullable=False
    )
