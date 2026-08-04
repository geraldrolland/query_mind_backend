from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import Column, ForeignKey, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel


class Dataset(SQLModel, table=True):
    """Top-level dataset entity.

    ``dataset_schema`` is required by the DSL compiler which fetches it via
    ``SELECT dataset_schema FROM dataset WHERE id = :dataset_id``.
    """

    __tablename__ = "dataset"

    id: str = Field(default_factory=lambda: f"dst_{uuid4()}", primary_key=True)
    user_id: int = Field(
        sa_column=Column(
            "user_id",
            ForeignKey("qm_user.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )
    )
    name: str = Field(index=True)
    description: str | None = Field(default=None, max_length=500)
    dataset_schema: dict = Field(default_factory=dict, sa_column=Column(JSONB, nullable=False))
    populated: bool = Field(default=False)
    total_rows: int = Field(default=0)
    total_size_bytes: int = Field(default=0)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc), nullable=False
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        nullable=False,
        sa_column_kwargs={
            "server_default": func.now(),
            "onupdate": func.now(),
            "nullable": False,
        },
    )
