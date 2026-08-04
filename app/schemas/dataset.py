from pydantic import BaseModel, Field
from typing import Optional


class CleaningReportSchema(BaseModel):
    raw_rows: int
    rows_ingested: int
    duplicates_removed: int
    null_counts: dict[str, int]
    columns: dict[str, str]


class DatasetOut(BaseModel):
    id: str
    name: str
    description: Optional[str] = None
    populated: bool
    total_rows: int
    total_size_bytes: int
    created_at: str
    updated_at: str


class UploadResponseSchema(BaseModel):
    dataset: DatasetOut
    cleaning_report: CleaningReportSchema


class DatasetRowOut(BaseModel):
    id: str
    data: dict
    created_at: str


class RecordsResponseSchema(BaseModel):
    records: list[DatasetRowOut]
    total: int
    page: int
    page_size: int


class ChatRequestSchema(BaseModel):
    message: str = Field(..., min_length=1, max_length=4000)
    history: Optional[list[dict]] = Field(default=[])
