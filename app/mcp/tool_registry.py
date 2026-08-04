"""Registry mapping MCP tool names to their callable implementations.

Each entry carries the callable plus an optional LLM-facing declaration
(``description`` and ``input_schema``) used to build the OpenAI ``tools``
parameter. Tools without a declaration are server-side only and not exposed
to the model.
"""

from app.mcp.executor import get_profile_tool, validate_dsl_tool
from app.models.message import MessageChartTypeEnum
from app.schemas.llm import LLMResponse
from app.schemas.query import DslDefinitionSchema


def generate_final_response_tool(content: list[dict]) -> LLMResponse:
    """Return the final LLMResponse from the model's answer content.

    Raises pydantic.ValidationError if the content doesn't conform, and
    validates every record block's ``dsl_field`` as a DslDefinitionSchema so
    an invalid DSL can never be persisted as a record.
    """
    response = LLMResponse(content=content)
    for block in response.content:
        if block.type == "record":
            DslDefinitionSchema(**block.dsl_field)
    return response


def list_chart_types_tool() -> list[str]:
    """List the chart types the model can pick for record content blocks."""
    return [
        member.value for member in MessageChartTypeEnum if member is not MessageChartTypeEnum.none
    ]


TOOL_REGISTRY = {
    "validate_dsl_tool": {
        "function": validate_dsl_tool,
        "description": (
            "Validate a DSL query plan by executing it against the dataset at the "
            "given Dataset ID. Input is a JSON object: dataset_id (the Dataset ID "
            "shown in the system prompt) and dsl_definition (the DSL query plan: "
            "select (list of field names), filters (list of {field, operator, "
            "value}), sorts (list of {field, direction}), group_by (list of "
            "fields, or {field, granularity} objects for date bucketing: "
            "day|month|quarter|year), metrics (list of {metric_type, field, "
            "alias}), limit "
            "(optional max rows to return, 1-1000)). Returns a success message "
            "with the executed result rows (the real computed values) when the "
            "plan is validated and executed; returns an error message if "
            "the plan is invalid or execution fails, so you can fix the DSL and "
            "retry this tool."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "dataset_id": {"type": "string"},
                "dsl_definition": {
                    "type": "object",
                    "properties": {
                        "select": {"type": "array", "items": {"type": "string"}},
                        "filters": {"type": "array", "items": {"type": "object"}},
                        "sorts": {"type": "array", "items": {"type": "object"}},
                                "group_by": {
                                    "type": "array",
                                    "items": {
                                        "oneOf": [
                                            {"type": "string"},
                                            {
                                                "type": "object",
                                                "properties": {
                                                    "field": {"type": "string"},
                                                    "granularity": {
                                                        "type": "string",
                                                        "enum": ["day", "month", "quarter", "year"],
                                                    },
                                                },
                                                "required": ["field", "granularity"],
                                                "additionalProperties": False,
                                            },
                                        ]
                                    },
                                },
                        "metrics": {"type": "array", "items": {"type": "object"}},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
                    },
                    "additionalProperties": False,
                },
            },
            "required": ["dataset_id", "dsl_definition"],
            "additionalProperties": False,
        },
    },
    "generate_final_response": {
        "function": generate_final_response_tool,
        "description": (
            "Return your complete structured final answer. Call this only as the "
            "sole tool in your turn — never together with other tools."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "content": {
                    "type": "array",
                    "items": {
                        "oneOf": [
                            {
                                "type": "object",
                                "properties": {
                                    "type": {"const": "text"},
                                    "text": {"type": "string"},
                                },
                                "required": ["type", "text"],
                                "additionalProperties": False,
                            },
                            {
                                "type": "object",
                                "properties": {
                                    "type": {"const": "record"},
                                    "dsl_field": {"type": "object"},
                                    "chart_type": {
                                        "enum": [
                                            "barchart",
                                            "linechart",
                                            "tablechart",
                                            "metricchart",
                                            "piechart",
                                        ]
                                    },
                                },
                                "required": ["type", "dsl_field", "chart_type"],
                                "additionalProperties": False,
                            },
                        ]
                    },
                }
            },
            "required": ["content"],
            "additionalProperties": False,
        },
    },
    "list_chart_types": {
        "function": list_chart_types_tool,
        "description": (
            "List the available chart types for record content blocks, as a list of "
            "strings. Use one of these as the chart_type when returning a record."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    "get_profile": {
        "function": get_profile_tool,
        "description": (
            "Return a profiling report for the dataset at the given Dataset ID, "
            "optionally scoped to the rows matching a DSL query plan. The report "
            "includes row_count, per-column statistics (type, min/max/avg for number "
            "fields, top_values for string fields, null counts), data-quality flags, "
            "and a few sample rows. Input is a JSON object: dataset_id (the Dataset ID "
            "shown in the system prompt) and dsl_definition (the DSL query plan: "
            "select, filters, sorts, group_by, metrics, limit; only filters are used "
            "for scoping). Use this first to understand field types, value ranges "
            "and missing-data patterns before designing the DSL you will validate "
            "with validate_dsl_tool."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "dataset_id": {"type": "string"},
                "dsl_definition": {
                    "type": "object",
                    "properties": {
                        "select": {"type": "array", "items": {"type": "string"}},
                        "filters": {"type": "array", "items": {"type": "object"}},
                        "sorts": {"type": "array", "items": {"type": "object"}},
                                "group_by": {
                                    "type": "array",
                                    "items": {
                                        "oneOf": [
                                            {"type": "string"},
                                            {
                                                "type": "object",
                                                "properties": {
                                                    "field": {"type": "string"},
                                                    "granularity": {
                                                        "type": "string",
                                                        "enum": ["day", "month", "quarter", "year"],
                                                    },
                                                },
                                                "required": ["field", "granularity"],
                                                "additionalProperties": False,
                                            },
                                        ]
                                    },
                                },
                        "metrics": {"type": "array", "items": {"type": "object"}},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
                    },
                    "additionalProperties": False,
                },
            },
            "required": ["dataset_id", "dsl_definition"],
            "additionalProperties": False,
        },
    },
}


def get_tool_llm_specs() -> list[dict]:
    """Return OpenAI-compatible tool declarations for LLM-exposed tools."""
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": spec["description"],
                "parameters": spec["input_schema"],
            },
        }
        for name, spec in TOOL_REGISTRY.items()
        if "description" in spec and "input_schema" in spec
    ]
