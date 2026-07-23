"""Task 5: provider_usage tool-schema catalog entry + lane scoping.

Pure unit — only imports ``capabilities.tool_schema`` and
``model_api_runtime.v2.worker``, no DB. See
``.superpowers/sdd/task-5-brief.md`` for the binding design decisions:
static catalog (not extra_tool_specs), subagent auto-exclusion via
absence from ``_SUBAGENT_ALLOWED_TOOLS``, not in
``provenance.EXTERNAL_READS``, yes in ``_PRIVATE_READ_TOOLS``, and
unconditionally withheld from the wake/screen_watch/manual_wake lane.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from capabilities import tool_schema


def test_provider_usage_in_catalog_no_params():
    specs = {s.name: s for s in tool_schema.build_tool_specs()}
    assert tool_schema.PROVIDER_USAGE_TOOL in specs
    spec = specs[tool_schema.PROVIDER_USAGE_TOOL]
    assert spec.parameters.get("properties") == {}
    assert "余额" in spec.description or "usage" in spec.description.lower()


def test_provider_usage_args_validate_empty_only():
    assert tool_schema.validate_tool_args(tool_schema.PROVIDER_USAGE_TOOL, {}) is None
    err = tool_schema.validate_tool_args(tool_schema.PROVIDER_USAGE_TOOL, {"x": 1})
    assert err  # non-empty args rejected


def test_provider_usage_excluded_from_subagent_and_private_read():
    from model_api_runtime.v2 import worker

    assert tool_schema.PROVIDER_USAGE_TOOL not in worker._SUBAGENT_ALLOWED_TOOLS
    assert tool_schema.PROVIDER_USAGE_TOOL in worker._SUBAGENT_DISABLED_TOOLS
    assert tool_schema.PROVIDER_USAGE_TOOL in worker._PRIVATE_READ_TOOLS


def test_provider_usage_not_in_external_reads():
    # Design decision: results are our own normalized JSON (numbers/enums/
    # truncated slugs), not third-party free text — must not trip the
    # external-content fence.
    from model_api_runtime.v2 import provenance

    assert tool_schema.PROVIDER_USAGE_TOOL not in provenance.EXTERNAL_READS


def test_provider_usage_withheld_from_wake_lane_source():
    """The wake/screen_watch/manual_wake lane (``_run_wake``) must never be
    able to offer this tool — read the source of ``_run_wake`` and assert
    the disabled-tool-names expression it builds for its
    ``run_tool_loop`` call includes ``PROVIDER_USAGE_TOOL``.

    This is a source-level assertion (not a call-through unit test)
    because ``_run_wake`` requires full DB-backed TurnDeps to execute.
    """
    import inspect

    from model_api_runtime.v2 import worker

    source = inspect.getsource(worker._run_wake)
    assert "PROVIDER_USAGE_TOOL" in source, (
        "expected _run_wake's disabled_tool_names construction to "
        "reference cap_tool_schema.PROVIDER_USAGE_TOOL"
    )
