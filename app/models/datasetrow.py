from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import Column, ForeignKey
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel


class DatasetRow(SQLModel, table=True):
    """One row of dataset data stored as a JSONB document.

    Table name and column names match what the ported DSL compiler emits
    (``FROM datasetrow``, ``data->>'field'``, ``datasetrow.dataset_id``).
    """

    __tablename__ = "datasetrow"

    id: str = Field(default_factory=lambda: f"drow_{uuid4()}", primary_key=True)
    dataset_id: str = Field(
        sa_column=Column(
            "dataset_id",
            ForeignKey("dataset.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )
    )
    data: dict = Field(sa_column=Column(JSONB, nullable=False))
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc), nullable=False
    )
