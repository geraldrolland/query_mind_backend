from datetime import datetime, timezone
from enum import Enum
from uuid import uuid4

from sqlalchemy import Column, ForeignKey
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel


class MessageChartTypeEnum(str, Enum):
    none = "none"
    barchart = "barchart"
    linechart = "linechart"
    tablechart = "tablechart"
    metricchart = "metricchart"
    piechart = "piechart"


class MessageRoleEnum(str, Enum):
    user = "user"
    assistant = "assistant"


class Message(SQLModel, table=True):
    """Chat message between a user and the assistant."""

    __tablename__ = "message"

    id: str = Field(default_factory=lambda: f"msg_{uuid4()}", primary_key=True)
    dataset_id: str = Field(
        sa_column=Column(
            "dataset_id",
            ForeignKey("dataset.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )
    )
    role: str = Field(default=MessageRoleEnum.user, index=True, nullable=False)
    type: str = Field(default="text", nullable=False)
    chart_type: str = Field(default=MessageChartTypeEnum.none, nullable=False)
    is_error: bool = Field(default=False, nullable=False)
    content: str | dict = Field(
        sa_column=Column(JSONB, nullable=False)
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc), nullable=False
    )
