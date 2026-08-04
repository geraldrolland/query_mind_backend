"""DSL query plan schemas (ported from insightly_dataset_service)."""

from pydantic import BaseModel, Field, model_validator
from typing import Literal, Union, Optional


class FilterSchema(BaseModel):
    field: str = Field(..., min_length=1)
    operator: Literal[
        "eq", "neq", "gt", "gte", "lt", "lte",
        "between", "in", "nin", "contains", "before", "after",
    ]
    value: Union[str, int, float, bool, list]


class SortSchema(BaseModel):
    field: str = Field(..., min_length=1)
    direction: Literal["asc", "desc"]


class GroupByFieldSchema(BaseModel):
    """Date-bucketed grouping for date fields (e.g. group by year)."""

    field: str = Field(..., min_length=1)
    granularity: Literal["day", "month", "quarter", "year"]


GroupByEntry = Union[str, GroupByFieldSchema]

    
class MeasurableSchema(BaseModel):
    metric_type: Literal["sum", "count", "min", "max", "avg"]
    field: Optional[str] = Field(default=None, min_length=1)
    alias: str = Field(..., min_length=1)

    @model_validator(mode="after")
    def validate_metric(self):
        if self.metric_type in ["sum", "min", "max", "avg"] and not self.field:
            raise ValueError(f"Field must be provided for metric type '{self.metric_type}'")
        if self.metric_type == "count" and self.field:
            raise ValueError("Field should not be provided for 'count' metric type")
        if self.field == self.alias:
            raise ValueError("Alias cannot be the same as the field name")
        return self


class DslDefinitionSchema(BaseModel):
    model_config = {"extra": "forbid"}

    select: list[str] = Field(default=[])
    filters: list[FilterSchema] = Field(default=[])
    sorts: list[SortSchema] = Field(default=[])
    group_by: list[GroupByEntry] = Field(default=[])
    metrics: list[MeasurableSchema] = Field(default=[])
    limit: Optional[int] = Field(default=None, ge=1, le=1000)

    @model_validator(mode="after")
    def validate_dsl(self):
        if self.select == [] and not self.metrics:
            raise ValueError("At least one of 'select' or 'metrics' must be provided.")
        if self.select and self.metrics and any(m.field in self.select for m in self.metrics if m.field):
            raise ValueError("Fields used in 'metrics' cannot be included in 'select'.")
        group_fields = [g.field if isinstance(g, GroupByFieldSchema) else g for g in self.group_by]
        if self.select and group_fields and any(col not in self.select for col in group_fields):
            raise ValueError("All fields in 'group_by' must be included in 'select'.")
        return self
