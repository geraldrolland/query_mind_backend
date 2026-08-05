"""Chat router: AI assistant backed by DeepSeek with a DSL query tool.

Streams newline-delimited JSON. Only final assistant messages are streamed; tool
calls and tool results are executed server-side and not exposed:
    {"id", "role", "type", "chart_type", "content", "created_at"}
        assistant message (text or record content)
    {"progress": "..."}
        status update emitted while the tool loop runs
    {"done": true}
    {"error": "..."}
"""

import json
import logging
from types import SimpleNamespace

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from fastapi.responses import StreamingResponse
from pydantic import ValidationError
from sqlmodel import Session, select

from app.core.settings import settings
from app.db import get_session
from app.mcp.tool_registry import TOOL_REGISTRY, get_tool_llm_specs
from app.models.dataset import Dataset
from app.models.message import Message, MessageChartTypeEnum
from app.schemas.dataset import ChatRequestSchema
from app.schemas.llm import RecordContentBlock, TextContentBlock
from middleware import invalidate_user_cache

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/chat", tags=["chat"])


def _yield_json(data) -> str:
    """Serialize an event as a newline-terminated NDJSON line."""
    return json.dumps(data, default=str) + "\n"

SYSTEM_PROMPT_TEMPLATE = """You are QueryMind, an AI data assistant. You answer questions about the
user's uploaded dataset by analysing it with the `get_profile` tool and validating your
query plan with the `validate_dsl_tool` tool.

Dataset ID: {DATASET_ID}

Dataset schema (field -> type):
{SCHEMA}

Rules:
1. First, call get_profile with a JSON object: "dataset_id" (the Dataset ID shown above)
   and "dsl_definition" (the DSL query plan) with these keys:
   - "select": list of field names to return — ALWAYS a list of plain field-name
     strings, never objects; time bucketing is expressed only in "group_by"
   - "filters": [{{"field", "operator", "value"}}] — operators: eq, neq, gt, gte, lt, lte,
     between, in, nin, contains, before, after (value is a list for between/in/nin)
   - "sorts": [{{"field", "direction"}}] — asc or desc
   - "group_by": list of field names, or {{"field", "granularity"}} objects for
     date fields (granularity: day|month|quarter|year) to bucket dates
   - "metrics": [{{"metric_type": sum|count|min|max|avg, "field", "alias"}}]
   - "limit": max rows to return (optional, 1–1000)
   The profile returns row_count, per-column statistics, and sample rows. Use it to
   reason about the data before designing your query.
 2. Design the DSL that answers the user's question, then call validate_dsl_tool with
    "dataset_id" and that "dsl_definition". The tool validates and executes the query
    internally. For "per year / per month / per quarter" questions, group the date
    field with the matching granularity, e.g. "group_by": [{{"field": "OrderDate",
    "granularity": "year"}}]. Call validate_dsl_tool with exactly ONE plan per turn.
    After ONE successful validation, call generate_final_response in your very next
    turn — never re-validate the same or a similar DSL. This rule applies only
    WITHIN a single turn's tool loop: every user question starts a fresh turn in
    which you must call get_profile and validate_dsl_tool again. Numbers from
    previous turns are NOT authoritative — past answers are truncated in your
    context for this reason, so always compute current values in this turn.
 3. If validate_dsl_tool returns an error, fix the DSL and call validate_dsl_tool again.
    Repeat until you get a success message. The success message includes the executed
    result rows with the actual computed values — they are authoritative. The profile's
    sample rows only show the first rows of the file and may be unrepresentative: never
    contradict the validation result based on them. The returned rows are only a slice
    (up to 20 rows): describe only rows you actually saw, and never generalize about
    the whole dataset (e.g. "all dates are in 2023") from that slice.
 4. Once your DSL is validated, answer by calling the generate_final_response tool — it
    must be the ONLY tool in your response content, in its own turn, never together with
    other tools. Its input is "content": a list of blocks, each
    {{"type": "text", "text": "..."}} for your explanation or
    {{"type": "record", "dsl_field": <the exact DSL validated above>, "chart_type":
    "barchart"|"linechart"|"tablechart"|"metricchart"|"piechart"}} to show a chart.
 5. For a record block, "dsl_field" must be exactly the DSL you validated with
    validate_dsl_tool, and "chart_type" must be one of the valid chart types (use
    list_chart_types if unsure).
 6. Never invent values; answer from get_profile and the validation result. If a tool
    errors, report the error. Never claim row counts, null counts, or "excluded rows"
    unless a query you actually ran returned them — do not infer them from sample rows.
    The validate_dsl_tool response includes a "message" with the real total and a
    "total_rows" field (its "data" may be truncated to the first 20 rows when
    "truncated" is true). Cite counts exactly from "total_rows"; if there are no
    nulls you observed, never mention nulls.
 7. Your text block must always state the actual computed values explicitly (e.g.
    "The average revenue per order in 2024 is 12,345.67"). Never write "shown above",
    "see above" or similar references, and never omit the numbers.
 8. For any quantitative question (averages, sums, counts, comparisons, trends), include
    a record block — with the exact DSL you validated and a chart type — in addition to
    the text block that states the values. If you used ANY tool for this question,
    a text-only generate_final_response is REJECTED and bounced back: your reply MUST
    contain a record block with the DSL validated in THIS turn and a chart type. A
    record block without a this-turn validate_dsl_tool is also rejected.
 9. Keep answers concise and cite what you queried (e.g. "across 4,230 rows").
10. Always end with generate_final_response, even for non-queryable questions
    (content = [{{"type": "text", "text": "..."}}]).
11. If the user's message is NOT a question about this dataset — small talk, feedback
    such as "thanks", "good job", "ok", "nice", or any off-topic question (weather,
    general knowledge, other data, etc.) — reply with a brief generic text-only
    response via generate_final_response. Do NOT call any tool and do NOT repeat or
    re-answer previous questions. For feedback say something like "You're welcome!
    Ask me anything about this dataset." For off-topic questions say something like
    "I can only help with questions about this dataset."
12. Never include raw tool output, JSON, or tab/pipe-separated data dumps in your
    answer. The validation result data is internal context for you, not content for
    the user. Present data only as clean markdown (e.g. a table) or prose, and do not
    duplicate rows that a record block already displays in a chart.
13. For time bucketing (per year/month/day), use exactly ONE mechanism per query:
    either a plain string group_by on a derived column if it exists in the schema
    (e.g. "group_by": ["OrderDate_year"]) or the granularity dict in "group_by" on the
    date field (e.g. "group_by": [{{"field": "OrderDate", "granularity": "year"}}]).
    Never mix them, and never reference a derived column (OrderDate_year/_month/_day)
    in "select" or "sorts" while grouping with the granularity dict. Objects with
    {{"field", "granularity"}} may ONLY appear in "group_by" — never in "select",
    "sorts", "filters", or "metrics". When using the granularity dict, put the plain
    date field name in "select" (e.g. "select": ["OrderDate"]); the bucketed label is
    returned automatically. The "dsl_field" of a record block must be identical to the
    last plan you validated with validate_dsl_tool.
"""


def _tool_call_dict(tc) -> dict:
    """Serialize an OpenAI SDK tool call for the conversation history.

    Gemini thinking models require the model's `thought_signature` to be
    echoed back with the function call, or the next request is rejected.
    """
    call = {
        "id": tc.id,
        "type": "function",
        "function": {
            "name": tc.function.name,
            "arguments": tc.function.arguments,
        },
    }
    extra = getattr(tc, "extra_content", None)
    if extra:
        call["extra_content"] = extra
    return call


def _sanitize_history(history: list[dict], limit: int = 10) -> list[dict]:
    """Rewrite assistant history messages so the model cannot replay past answers.

    Full prior answers (big tables of numbers, chart DSLs) tempt the model to
    copy them instead of re-querying the dataset fresh. Text is truncated and
    record content is replaced with a placeholder that is explicitly not
    authoritative. User messages pass through unchanged.
    """
    clean: list[dict] = []
    for m in history[-limit:]:
        role = m.get("role")
        content = m.get("content")
        if role == "user":
            clean.append({"role": "user", "content": content})
            continue
        if role == "assistant":
            if isinstance(content, dict) or (
                isinstance(content, str)
                and content.lstrip().startswith(("{", "["))
                and m.get("type") == "record"
            ):
                clean.append(
                    {
                        "role": "assistant",
                        "content": "[chart from a previous turn — not authoritative]",
                    }
                )
            else:
                text = content if isinstance(content, str) else json.dumps(content, default=str)
                if len(text) > 300:
                    text = text[:300] + "…[truncated]"
                clean.append({"role": "assistant", "content": text})
    return clean


def _tools_with_dataset_id() -> list[str]:
    """Tool names whose functions take a dataset_id input."""
    return ["get_profile", "validate_dsl_tool"]


def _save_message(
    session: Session,
    *,
    dataset_id: str,
    role: str,
    content: str | dict,
    type: str = "text",
    chart_type: str = MessageChartTypeEnum.none,
    is_error: bool = False,
) -> Message:
    message = Message(
        dataset_id=dataset_id,
        role=role,
        content=content,
        type=type,
        chart_type=chart_type,
        is_error=is_error,
    )
    session.add(message)
    session.commit()
    session.refresh(message)
    return message


def _friendly_llm_error(exc: Exception) -> str:
    """Map LLM API errors to user-friendly messages."""
    message = str(exc)
    lowered = message.lower()
    if "invalid api key" in lowered or "authentication" in lowered or "unauthorized" in lowered or "api key" in lowered and "invalid" in lowered:
        return (
            "The AI assistant is unavailable: the API key is invalid or missing. "
            "Set LLM_API_KEY in the backend .env and restart the backend."
        )
    if "rate limit" in lowered or "429" in lowered or "quota" in lowered or "resource exhausted" in lowered:
        return (
            "The AI assistant is busy (rate limited). Wait a moment and try again."
        )
    if "overload" in lowered or "529" in lowered or "busy" in lowered or "try again later" in lowered:
        return (
            "The AI assistant is busy right now (the model provider is "
            "overloaded). Wait a moment and try again."
        )
    if "insufficient balance" in lowered or "payment required" in lowered or "402" in lowered:
        return (
            "The AI assistant is unavailable: the DeepSeek account balance is "
            "exhausted. Top up at https://platform.deepseek.com/top_up and retry."
        )
    if "model" in lowered and ("not found" in lowered or "not supported" in lowered or "404" in lowered):
        return (
            "The AI assistant is unavailable: the model is not found. Check "
            "LLM_MODEL in the backend .env."
        )
    if "connect" in lowered or "connection" in lowered or "refused" in lowered:
        return (
            "The AI assistant is unreachable. Check your internet connection and "
            "that LLM_BASE_URL is correct."
        )
    if "timed out" in lowered or "timeout" in lowered or "timedout" in lowered:
        return (
            "The AI assistant took too long to respond. Try again in a moment."
        )
    return (
        "The AI assistant hit an unexpected error. Please try again in a moment."
    )


def _build_system_prompt(dataset: Dataset) -> str:
    schema = dataset.dataset_schema or {}
    if isinstance(schema, dict):
        schema_text = "\n".join(
            f"- {field}: {rule.get('type', 'unknown')}"
            for field, rule in schema.items()
        )
    else:
        schema_text = json.dumps(schema, default=str)
    return SYSTEM_PROMPT_TEMPLATE.format(
        DATASET_ID=dataset.id, SCHEMA=schema_text
    )


def _streamed_llm_message(client, messages, *, tools: list[dict], max_tokens: int) -> SimpleNamespace:
    """Run a streamed chat completion, accumulating the full assistant message."""
    stream = client.chat.completions.create(
        model=settings.LLM_MODEL,
        max_tokens=max_tokens,
        messages=messages,
        tools=tools,
        stream=True,
        timeout=60,
    )
    content_parts: list[str] = []
    tool_entries: dict[int, dict] = {}
    for chunk in stream:
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta
        if delta.content:
            content_parts.append(delta.content)
        for tc in delta.tool_calls or []:
            entry = tool_entries.setdefault(
                tc.index,
                {
                    "_idx": tc.index,
                    "id": "",
                    "function": {"name": "", "arguments": ""},
                },
            )
            if tc.id:
                entry["id"] = tc.id
            if tc.function:
                if tc.function.name:
                    entry["function"]["name"] = tc.function.name
                if tc.function.arguments:
                    entry["function"]["arguments"] += tc.function.arguments
    tool_calls = [
        SimpleNamespace(
            id=entry["id"],
            type="function",
            function=SimpleNamespace(
                name=entry["function"]["name"],
                arguments=entry["function"]["arguments"],
            ),
        )
        for entry in sorted(tool_entries.values(), key=lambda e: e["_idx"])
    ]
    return SimpleNamespace(
        content="".join(content_parts) or None,
        tool_calls=tool_calls or None,
    )


@router.post("/{dataset_id}/query")
def chat_query(
    request: Request,
    body: ChatRequestSchema,
    dataset_id: str = Path(...),
    session: Session = Depends(get_session),
):
    """Stream an AI answer for the dataset, using DeepSeek + the DSL query tool."""
    if not settings.LLM_API_KEY:
        raise HTTPException(status_code=503, detail="AI assistant is not configured")

    logger.info(
        "Chat request received: user=%s dataset=%s message=%r",
        request.state.auth_user.get("id"),
        dataset_id,
        (body.message or "")[:120],
    )

    dataset = session.exec(
        select(Dataset).where(Dataset.id == dataset_id)
    ).first()
    if not dataset or dataset.user_id != request.state.auth_user["id"]:
        raise HTTPException(status_code=404, detail="Dataset not found")

    system_prompt = _build_system_prompt(dataset)

    def event_stream():
        try:
            user_id = str(request.state.auth_user["id"])
            _save_message(
                session,
                dataset_id=dataset_id,
                role="user",
                content=body.message,
            )
            invalidate_user_cache(int(user_id))

            from openai import OpenAI

            client = OpenAI(
                base_url=settings.LLM_BASE_URL,
                api_key=settings.LLM_API_KEY,
            )
            tools: list[dict] = get_tool_llm_specs()

            messages = [{"role": "system", "content": system_prompt}]
            messages.extend(_sanitize_history(body.history))
            messages.append({"role": "user", "content": body.message})

            queried = False
            tools_used = False
            for _ in range(settings.MAX_TOOL_ITERATIONS):
                yield _yield_json(
                    {
                        "progress": (
                            "Analyzing your data…" if not tools_used else "Running your query…"
                        )
                    }
                )
                msg = _streamed_llm_message(client, messages, tools=tools, max_tokens=1024)
                tool_calls = list(msg.tool_calls or [])

                final_tool = (
                    tool_calls[0]
                    if len(tool_calls) == 1
                    and getattr(tool_calls[0].function, "name", None)
                    == "generate_final_response"
                    else None
                )

                if final_tool:
                    try:
                        llm_response = TOOL_REGISTRY["generate_final_response"]["function"](
                            **json.loads(final_tool.function.arguments or "{}")
                        )
                    except (json.JSONDecodeError, ValidationError) as exc:
                        logger.warning("Invalid generate_final_response; retrying: %s", exc)
                        messages.append(
                            {
                                "role": "assistant",
                                "content": msg.content or "",
                                "tool_calls": [_tool_call_dict(final_tool)],
                            }
                        )
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": final_tool.id,
                                "content": (
                                    "Your generate_final_response failed validation. "
                                    "Return a valid structured response."
                                ),
                            }
                        )
                        continue

                    has_record = any(
                        getattr(b, "chart_type", None) for b in llm_response.content
                    )
                    if has_record and not queried:
                        logger.warning(
                            "generate_final_response record block without a "
                            "validate_dsl_tool this turn; bouncing"
                        )
                        messages.append(
                            {
                                "role": "assistant",
                                "content": msg.content or "",
                                "tool_calls": [_tool_call_dict(final_tool)],
                            }
                        )
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": final_tool.id,
                                "content": (
                                    "A record block must reference a DSL you "
                                    "validated with validate_dsl_tool in THIS turn. "
                                    "Call validate_dsl_tool with your plan, then "
                                    "re-issue generate_final_response with a record "
                                    "block containing that exact DSL and a chart type."
                                ),
                            }
                        )
                        continue

                    if tools_used and not has_record:
                        logger.warning(
                            "generate_final_response text-only after tool use; bouncing"
                        )
                        messages.append(
                            {
                                "role": "assistant",
                                "content": msg.content or "",
                                "tool_calls": [_tool_call_dict(final_tool)],
                            }
                        )
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": final_tool.id,
                                "content": (
                                    "You used tools for this dataset question, so "
                                    "your reply MUST include a record block "
                                    "containing the exact validated DSL and a chart "
                                    "type. Text-only replies are rejected. "
                                    "Re-issue generate_final_response with a record "
                                    "block."
                                ),
                            }
                        )
                        continue

                    for block in llm_response.content:
                        if isinstance(block, TextContentBlock):
                            message = _save_message(
                                session,
                                dataset_id=dataset_id,
                                role="assistant",
                                content=block.text,
                            )
                        else:
                            message = _save_message(
                                session,
                                dataset_id=dataset_id,
                                role="assistant",
                                content=block.dsl_field,
                                type="record",
                                chart_type=block.chart_type.value,
                            )
                        yield _yield_json(message.model_dump())
                    break

                if tool_calls:
                    messages.append(
                        {
                            "role": "assistant",
                            "content": msg.content or "",
                            "tool_calls": [_tool_call_dict(tc) for tc in tool_calls],
                        }
                    )

                    tool_results = []
                    for tc in tool_calls:
                        if getattr(tc.function, "name", None) == "generate_final_response":
                            content = json.dumps(
                                {
                                    "error": (
                                        "generate_final_response must be called alone, "
                                        "not together with other tools. Retry it in its "
                                        "own turn."
                                    )
                                }
                            )
                        else:
                            fn = TOOL_REGISTRY.get(tc.function.name, {}).get("function")
                            try:
                                args = json.loads(tc.function.arguments or "{}")
                            except json.JSONDecodeError:
                                args = {}
                            if fn is None:
                                content = json.dumps(
                                    {"error": f"Unknown tool: {tc.function.name}"}
                                )
                            else:
                                if tc.function.name in _tools_with_dataset_id():
                                    incoming = args.get("dataset_id")
                                    if incoming != dataset_id:
                                        logger.warning(
                                            "Overriding dataset_id %r -> %r for %s",
                                            incoming,
                                            dataset_id,
                                            tc.function.name,
                                        )
                                        args["dataset_id"] = dataset_id
                                try:
                                    result = fn(**args)
                                    tools_used = True
                                    if (
                                        tc.function.name == "validate_dsl_tool"
                                        and isinstance(result, dict)
                                        and result.get("status") == "validated"
                                    ):
                                        queried = True
                                    content = json.dumps(result, default=str)
                                except Exception as exc:
                                    logger.warning("Tool exec failed: %s", exc)
                                    content = json.dumps({"error": str(exc)})

                        tool_results.append(
                            {
                                "role": "tool",
                                "tool_call_id": tc.id,
                                "content": content,
                            }
                        )

                    messages.extend(tool_results)
                    continue

                if msg.content:
                    message = _save_message(
                        session,
                        dataset_id=dataset_id,
                        role="assistant",
                        content=msg.content,
                    )
                    yield _yield_json(message.model_dump())
                break
            else:
                fallback = "I hit the maximum number of query attempts. Try a simpler question."
                message = _save_message(
                    session,
                    dataset_id=dataset_id,
                    role="assistant",
                    content=fallback,
                )
                yield _yield_json(message.model_dump())

            yield _yield_json({"done": True})
        except Exception as exc:
            from openai import APIError

            if isinstance(exc, APIError):
                logger.warning("LLM API error in chat stream: %s", exc)
            else:
                logger.error("Chat stream failed: %s", exc, exc_info=True)

            friendly = _friendly_llm_error(exc)
            try:
                message = _save_message(
                    session,
                    dataset_id=dataset_id,
                    role="assistant",
                    content=friendly,
                    is_error=True,
                )
                yield _yield_json(message.model_dump())
            except Exception as save_exc:
                logger.error("Failed to persist error message: %s", save_exc)
                yield _yield_json({"error": friendly})
            yield _yield_json({"done": True})

    return StreamingResponse(
        event_stream(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
