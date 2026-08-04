from enum import Enum
from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field


class ChartTypeEnum(str, Enum):
    barchart = "barchart"
    linechart = "linechart"
    tablechart = "tablechart"
    metricchart = "metricchart"
    piechart = "piechart"


class TextContentBlock(BaseModel):
    model_config = {"extra": "forbid"}

    type: Literal["text"]
    text: str


class RecordContentBlock(BaseModel):
    model_config = {"extra": "forbid"}

    type: Literal["record"]
    dsl_field: dict
    chart_type: ChartTypeEnum


ContentBlock = Annotated[
    Union[TextContentBlock, RecordContentBlock], Field(discriminator="type")
]


class LLMResponse(BaseModel):
    model_config = {"extra": "forbid"}

    content: list[ContentBlock]