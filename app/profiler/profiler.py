"""Dataset profiler for QueryMind.

Analyzes a dataset's rows and produces a structured profile document with
schema metadata, per-column statistics, data-quality metrics, and sample rows.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import date, datetime
from statistics import mean

from sqlmodel import Session

from app.db import get_engine
from app.dsl.postgresjsonBcompiler import jsonBcompiler
from app.models.dataset import Dataset

logger = logging.getLogger(__name__)

_SAMPLE_LIMIT = 1000


class Profiler:
    """Analyse a dataset and produce a structured profile document.

    Rows are produced by the PostgreSQL DSL compiler from the dataset's active
    version. When a DSL plan is supplied, the profile is scoped to the rows
    matching the plan's filters, capped by the plan's limit (default
    ``_SAMPLE_LIMIT``).

    Args:
        dataset_id: Primary key of the dataset to profile.
        dsl_definition: Optional DSL query plan.
    """

    def __init__(self, dataset_id: str, dsl_definition: dict | None = None):
        self.dataset_id = dataset_id
        self.dsl_definition = dsl_definition or {}

    def _coerce(self, value):
        if value is None or isinstance(value, (int, float, bool)):
            return value
        if isinstance(value, (datetime, date)):
            return value
        text = str(value).strip()
        if text == "":
            return None
        try:
            return int(text)
        except ValueError:
            pass
        try:
            return float(text)
        except ValueError:
            pass
        try:
            return datetime.fromisoformat(text)
        except ValueError:
            return text

    def profile(self) -> dict[str, any]:
        """Generate a profile for the dataset (optionally scoped by a DSL plan)."""
        dsl = self.dsl_definition
        with Session(get_engine()) as session:
            dataset = session.get(Dataset, self.dataset_id)
            schema = dataset.dataset_schema if dataset else {}
            row_count = dataset.total_rows if dataset else 0

        rows: list[dict] = []
        if dataset and schema:
            plan = {
                "select": list(schema.keys()),
                "filters": dsl.get("filters") or [],
                "limit": dsl.get("limit") or _SAMPLE_LIMIT,
            }
            results = jsonBcompiler.execute_dsl(plan, self.dataset_id)
            rows = results[0] if results else []
            if plan["filters"]:
                row_count = len(rows)

        columns = []
        for name, rule in schema.items():
            col_rows = [r.get(name) for r in rows]
            values = [v for v in (self._coerce(v) for v in col_rows) if v is not None]
            stat: dict[str, any] = {"name": name, "type": rule.get("type", "string")}

            if stat["type"] == "number":
                if values:
                    stat["min"] = min(values)
                    stat["max"] = max(values)
                    try:
                        stat["avg"] = round(mean(values), 2)
                    except TypeError:
                        pass
            elif stat["type"] == "date":
                if values:
                    try:
                        stat["min"] = min(values)
                        stat["max"] = max(values)
                        if isinstance(stat["min"], datetime):
                            stat["min"] = stat["min"].isoformat()
                        if isinstance(stat["max"], datetime):
                            stat["max"] = stat["max"].isoformat()
                    except TypeError:
                        pass
            else:
                top = Counter(str(v) for v in values)
                stat["top_values"] = [v for v, _ in top.most_common(5)]

            stat["nulls"] = sum(1 for v in col_rows if v is None)
            columns.append(stat)

        return {
            "dataset_id": self.dataset_id,
            "row_count": row_count,
            "columns": columns,
            "quality": {"columns_with_nulls": sum(1 for c in columns if c["nulls"] > 0)},
            "sample_rows": rows[:10],
        }
    