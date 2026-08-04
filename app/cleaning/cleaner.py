"""CSV cleaning: parse, drop duplicates, count nulls, infer types."""

import io
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)

TYPE_MAP = {
    "int64": "number",
    "float64": "number",
    "object": "string",
    "bool": "boolean",
    "datetime64[ns]": "date",
    "datetime64[us]": "date",
}

OPERATORS_BY_TYPE = {
    "number": ["eq", "neq", "gt", "gte", "lt", "lte", "between", "in", "nin"],
    "string": ["eq", "neq", "contains", "in", "nin", "before", "after"],
    "boolean": ["eq", "neq"],
    "date": ["eq", "neq", "gt", "gte", "lt", "lte", "before", "after", "between", "in", "nin"],
}


@dataclass
class CleaningResult:
    raw_rows: int
    rows_ingested: int
    duplicates_removed: int
    null_counts: dict[str, int] = field(default_factory=dict)
    columns: dict[str, str] = field(default_factory=dict)
    schema: dict = field(default_factory=dict)
    rows: list[dict] = field(default_factory=list)


def _infer_type(series: pd.Series) -> str:
    if series.dtype == "bool":
        return "boolean"
    if pd.api.types.is_numeric_dtype(series):
        return "number"
    if pd.api.types.is_datetime64_any_dtype(series):
        return "date"
    # Object columns: try numeric coercion on non-null values.
    sample = series.dropna()
    if len(sample) >= 3:
        coerced = pd.to_numeric(sample, errors="coerce")
        if coerced.notna().mean() >= 0.8:
            return "number"
        parsed = pd.to_datetime(sample.astype(str), errors="coerce", format="mixed")
        if parsed.notna().mean() >= 0.8:
            return "date"
    return "string"


def _normalize_float(value):
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _parse_date_part(value, part: str) -> Optional[int]:
    """Extract a year/month/day integer from a date value (ISO string or None)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value)[:10])
        except ValueError:
            return None
    if part == "year":
        return dt.year
    if part == "month":
        return dt.month
    return dt.day


def enrich_dataset(rows: list[dict], schema: dict) -> None:
    """Add <date_column>_day/_month/_year number columns for every date column.

    The derived columns are typed as numbers (so they support the numeric
    operators and plain grouping) but are marked non-aggregatable, so metrics
    such as sum/avg can never be applied to them.
    """
    date_columns = [col for col, rule in schema.items() if rule.get("type") == "date"]
    for col in date_columns:
        for part in ("year", "month", "day"):
            new_col = f"{col}_{part}"
            if new_col in schema:
                continue
            schema[new_col] = {
                "type": "number",
                "allowed_operators": OPERATORS_BY_TYPE["number"],
                "aggregatable": False,
            }
            for row in rows:
                row[new_col] = _parse_date_part(row.get(col), part)


def clean_csv(file_bytes: bytes, filename: str = "upload.csv") -> CleaningResult:
    """Parse a CSV file, drop exact-duplicate rows, and infer the schema.

    Returns cleaned rows (as plain JSON-safe dicts) plus statistics used to
    build the cleaning report.
    """
    try:
        df = pd.read_csv(io.BytesIO(file_bytes))
    except Exception as exc:
        raise ValueError(f"Failed to parse CSV: {exc}") from exc

    if df.empty:
        raise ValueError("CSV file contains no rows")

    raw_rows = len(df)

    # Normalize column names (strip whitespace) and reject empties.
    df.columns = [str(c).strip() for c in df.columns]
    if any(not c for c in df.columns):
        raise ValueError("CSV contains a column with an empty name")

    # Treat empty/whitespace strings as missing values so null counts and
    # type inference behave consistently.
    df = df.replace(r"^\s*$", pd.NA, regex=True)

    # Drop exact-duplicate rows (all columns identical).
    df = df.drop_duplicates()
    duplicates_removed = raw_rows - len(df)

    # Null counts per column.
    null_counts = {col: int(df[col].isna().sum()) for col in df.columns}

    # Replace NaN with None for JSON serialization (convert to object dtype first
    # so pandas does not coerce None back to NaN).
    df = df.astype(object).where(pd.notnull(df), None)

    # Infer schema + types.
    columns: dict[str, str] = {}
    schema: dict = {}
    for col in df.columns:
        dtype = _infer_type(df[col])
        columns[col] = dtype
        schema[col] = {
            "type": dtype,
            "allowed_operators": OPERATORS_BY_TYPE[dtype],
            "aggregatable": dtype in ("number", "date"),
        }

    rows: list[dict] = []
    for record in df.to_dict(orient="records"):
        clean = {}
        for key, value in record.items():
            if isinstance(value, (pd.Timestamp,)):
                value = value.isoformat()
            elif value is None:
                pass
            else:
                value = _normalize_float(value)
            clean[key] = value
        rows.append(clean)

    enrich_dataset(rows, schema)

    return CleaningResult(
        raw_rows=raw_rows,
        rows_ingested=len(rows),
        duplicates_removed=duplicates_removed,
        null_counts=null_counts,
        columns=columns,
        schema=schema,
        rows=rows,
    )
