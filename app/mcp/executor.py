"""Shared tool implementations used by the MCP server and the chat router."""

import json
import logging

from pydantic import ValidationError
from sqlmodel import Session, select

from app.core.settings import settings
from app.db import get_engine
from app.dsl.postgresjsonBcompiler import jsonBcompiler
from app.models.dataset import Dataset
from app.profiler.profiler import Profiler
from app.redis_conf import gen_cache_key, redis_client
from app.schemas.query import DslDefinitionSchema

logger = logging.getLogger(__name__)


def _format_plan_error(exc: ValidationError) -> str:
    """Turn a pydantic DSL validation error into an actionable message."""
    errors = exc.errors()
    for e in errors:
        loc = e.get("loc") or ()
        if loc and loc[0] == "select":
            return (
                "'select' must be a list of plain field-name strings, e.g. "
                '["OrderDate"]. Never put objects like {"field", "granularity"} in '
                '"select" — time bucketing goes only in "group_by" as '
                '[{"field": "OrderDate", "granularity": "month"}], and the plain '
                'date field name goes in "select".'
            )
    for e in errors:
        loc = e.get("loc") or ()
        if (loc and loc[0] == "group_by") or ("group_by" in (e.get("msg") or "")):
            return (
                "'group_by' entries must be plain field-name strings or "
                '[{"field": <date field>, "granularity": "day"|"month"|"quarter"|"year"}]. '
                'Every group_by field must appear in "select".'
            )
    return "The dsl_definition is invalid: " + "; ".join(
        ("." .join(str(part) for part in (e.get("loc") or ())) or "<plan>") + ": " + e.get("msg", "")
        for e in errors
    )


def _load_plan(dsl_definition: dict) -> DslDefinitionSchema:
    """Validate an LLM-produced DSL dict into a DslDefinitionSchema."""
    if not isinstance(dsl_definition, dict):
        raise ValueError("dsl_definition must be an object")
    try:
        return DslDefinitionSchema(**dsl_definition)
    except ValidationError as exc:
        raise ValueError(_format_plan_error(exc)) from exc


def execute_query_tool(dataset_id: str, dsl_definition: dict) -> dict:
    """Execute a DSL plan against a dataset with Redis caching.

    Raises:
        ValueError: for invalid plans or unknown datasets (handled by caller).
    """
    plan = _load_plan(dsl_definition)

    cache_key = gen_cache_key({"dataset_id": dataset_id, "plan": plan.model_dump()})
    cached = redis_client.get(cache_key)
    if cached:
        return {"data": json.loads(cached), "cached": True}

    with Session(get_engine()) as session:
        dataset = session.exec(select(Dataset).where(Dataset.id == dataset_id)).first()
        if not dataset:
            raise ValueError(f"Dataset {dataset_id} not found")

        results = jsonBcompiler.execute_dsl(plan, dataset.id)

    data = results[0] if results else []
    redis_client.set(cache_key, json.dumps(data), ex=settings.CACHE_EXPIRE_SECONDS)
    return {"data": data, "cached": False}


def get_profile_tool(dataset_id: str, dsl_definition: dict | None = None) -> dict:
    """Return the profiling report for a dataset.

    When ``dsl_definition`` is provided it is validated against
    DslDefinitionSchema and the profile is computed over the rows matching
    the plan's filters.
    """
    if dsl_definition is not None:
        dsl_definition = DslDefinitionSchema(**dsl_definition).model_dump(exclude_none=True)
    return Profiler(dataset_id=dataset_id, dsl_definition=dsl_definition).profile()


def validate_dsl_tool(dataset_id: str, dsl_definition: dict) -> dict:
    """Validate a DSL plan by executing it against the dataset.

    Runs ``jsonBcompiler.execute_dsl`` (validation + SQL compile + execution).
    Raises on invalid DSL or dataset errors; on success returns the validation
    message plus the executed result rows (up to 20) so the model can report
    the real computed values. The chat loop converts any raised error into a
    tool_result the model can use to fix and retry the DSL.
    """
    plan = _load_plan(dsl_definition)

    with Session(get_engine()) as session:
        dataset = session.exec(select(Dataset).where(Dataset.id == dataset_id)).first()
        if not dataset:
            raise ValueError(f"Dataset {dataset_id} not found")

        results = jsonBcompiler.execute_dsl(plan, dataset.id)

    rows = results[0] if results else []
    return {
        "status": "validated",
        "message": f"dsl validated successfully; {len(rows)} row(s) returned",
        "total_rows": len(rows),
        "truncated": len(rows) > 20,
        "data": rows[:20],
    }
