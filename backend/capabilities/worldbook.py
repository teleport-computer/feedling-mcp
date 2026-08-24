"""World Book read capability — thin facade over worldbook_core.match."""
from __future__ import annotations

from worldbook import worldbook_core

from capabilities import errors
from capabilities.types import CapabilityResult, err, ok


def match(
    store, *, api_key=None, runtime_token=None, params=None, trace_context=None,
) -> CapabilityResult:
    params = params or {}
    query = str(params.get("query") or "").strip()
    if not query:
        return err(
            errors.INVALID,
            "query is required for worldbook_match",
            retryable=False,
        )
    body, status = worldbook_core.match(
        store,
        {"message": query},
        api_key=api_key,
        runtime_token=runtime_token,
        **(
            {
                "trace_id": str(trace_context.get("trace_id") or ""),
                "job_id": str(trace_context.get("job_id") or ""),
                "lane": str(trace_context.get("lane") or ""),
                "actor": "host_agent_runtime",
            }
            if isinstance(trace_context, dict)
            else {}
        ),
    )
    if status == 200:
        data = body if isinstance(body, dict) else {"result": body}
        return ok(data=errors.cap_data(data))
    return err(
        errors.code_for_status(status),
        errors.message_for_body(body, "world book match unavailable"),
        retryable=errors.retryable_for_status(status),
    )
