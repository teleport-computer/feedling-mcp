"""Offline regression tests for the checked-in public OpenAPI contract.

These tests intentionally build the document from the FastAPI application via
the same exporter used by the documentation workflow.  They do not start a
server or make network requests.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / "tools"
sys.path.insert(0, str(TOOLS))

from export_public_openapi import _build_public_schema, _load_schema  # noqa: E402


HTTP_METHODS = {"get", "post", "put", "patch", "delete", "options", "head", "trace"}
MUTATION_METHODS = {"post", "put", "patch", "delete"}

EXPECTED_BODYLESS_POSTS = {
    ("post", "/v1/bootstrap"),
    ("post", "/v1/capture/force"),
    ("post", "/v1/genesis/persona_backfill"),
    ("post", "/v1/mcp/servers/{name}/test"),
    ("post", "/v1/model_api/driver"),
    ("post", "/v1/model_api/test"),
    ("post", "/v1/model_api/routes/{route_id}/activate"),
    ("post", "/v1/model_api/routes/{route_id}/test"),
    ("post", "/v1/proactive/scheduled/fire"),
}

EXPECTED_PUBLIC_OPERATIONS = {
    ("get", "/healthz"),
    ("get", "/v1/copytext"),
    ("post", "/v1/access/claim-token"),
    ("post", "/v1/account/recover/challenge"),
    ("post", "/v1/account/recover/verify"),
    ("post", "/v1/users/register"),
}

EXPECTED_API_KEY_ONLY_OPERATIONS = {
    ("post", "/v1/access/link-token"),
    ("post", "/v1/account/reset"),
    ("get", "/v1/mcp/servers"),
    ("post", "/v1/mcp/servers"),
    ("patch", "/v1/mcp/servers/{name}"),
    ("delete", "/v1/mcp/servers/{name}"),
    ("post", "/v1/mcp/servers/{name}/test"),
    ("post", "/v1/perception/report"),
}

EXPECTED_CORE_BODY_REFS = {
    ("post", "/v1/model_api/chat/send"): "HostedChatSendRequest",
    ("post", "/v1/chat/message"): "ChatTransportRequest",
    ("post", "/v1/chat/response"): "ChatResponseRequest",
    ("delete", "/v1/chat/history"): "ChatHistoryClearRequest",
    ("post", "/v1/memory/index"): "MemoryIndexRequest",
    ("post", "/v1/memory/fetch"): "MemoryFetchRequest",
    ("post", "/v1/memory/actions"): "MemoryActionsRequest",
    ("post", "/v1/memory/add"): "MemoryAddRequest",
    ("post", "/v1/memory/retype"): "MemoryRetypeRequest",
    ("post", "/v1/perception/report"): "PerceptionReportRequest",
    ("post", "/v1/perception/photo/evaluate"): "PerceptionPhotoEvaluateRequest",
}

EXPECTED_HEADER_OPERATIONS = {
    ("get", "/v1/chat/poll"): {
        "x-feedling-consumer",
        "x-feedling-consumer-id",
        "x-feedling-consumer-version",
        "x-feedling-consumer-commit",
    },
    ("post", "/v1/chat/response"): {
        "x-feedling-consumer",
        "x-feedling-consumer-id",
        "x-feedling-consumer-version",
        "x-feedling-consumer-commit",
    },
    ("put", "/v1/genesis/imports/{job_id}/chunks/{seq}"): {
        "x-envelope-meta",
        "x-byte-start",
        "x-byte-end",
        "x-content-sha256",
        "x-ciphertext-sha256",
    },
    ("get", "/v1/screen/frames/{frame_id}/image"): {"range"},
    ("get", "/v1/copytext"): {"if-none-match"},
}


@pytest.fixture(scope="module")
def public_schema() -> dict[str, Any]:
    return _build_public_schema(_load_schema())


@pytest.fixture(scope="module")
def operations(public_schema: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    return {
        (method.lower(), path): operation
        for path, path_item in public_schema["paths"].items()
        for method, operation in path_item.items()
        if method.lower() in HTTP_METHODS and isinstance(operation, dict)
    }


def _parameters(
    operation: dict[str, Any],
    where: str,
) -> dict[str, dict[str, Any]]:
    return {
        str(parameter["name"]).lower(): parameter
        for parameter in operation.get("parameters", [])
        if isinstance(parameter, dict) and parameter.get("in") == where
    }


def _json_body_ref(operation: dict[str, Any]) -> str | None:
    return (
        operation.get("requestBody", {})
        .get("content", {})
        .get("application/json", {})
        .get("schema", {})
        .get("$ref")
    )


def _walk_refs(value: Any) -> Iterator[str]:
    if isinstance(value, dict):
        ref = value.get("$ref")
        if isinstance(ref, str):
            yield ref
        for child in value.values():
            yield from _walk_refs(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_refs(child)


def _resolve_local_ref(document: dict[str, Any], ref: str) -> Any:
    assert ref.startswith("#/components/"), f"public contract must not require an external ref: {ref}"
    current: Any = document
    for encoded_token in ref[2:].split("/"):
        token = encoded_token.replace("~1", "/").replace("~0", "~")
        assert isinstance(current, dict) and token in current, f"unresolved OpenAPI ref: {ref}"
        current = current[token]
    return current


def test_public_operation_and_parameter_inventory(
    operations: dict[tuple[str, str], dict[str, Any]],
) -> None:
    assert len(operations) == 143
    assert sum("requestBody" in operation for operation in operations.values()) == 65

    query_operations = {
        key for key, operation in operations.items() if _parameters(operation, "query")
    }
    header_operations = {
        key for key, operation in operations.items() if _parameters(operation, "header")
    }
    assert len(query_operations) == 31
    assert header_operations == set(EXPECTED_HEADER_OPERATIONS)

    for key, expected_names in EXPECTED_HEADER_OPERATIONS.items():
        assert set(_parameters(operations[key], "header")) == expected_names


def test_operator_copytext_writer_is_not_public(
    operations: dict[tuple[str, str], dict[str, Any]],
) -> None:
    assert ("post", "/v1/copytext") not in operations
    assert operations[("get", "/v1/copytext")]["security"] == []
    assert ("get", "/v1/proactive/debug") not in operations


def test_all_mutations_have_an_explicit_body_contract_or_bodyless_classification(
    operations: dict[tuple[str, str], dict[str, Any]],
) -> None:
    actual_bodyless_posts = {
        key
        for key, operation in operations.items()
        if key[0] == "post" and "requestBody" not in operation
    }
    assert actual_bodyless_posts == EXPECTED_BODYLESS_POSTS

    for key, operation in operations.items():
        method, _path = key
        if method not in MUTATION_METHODS:
            continue

        assert operation.get("x-feedling-contract-level") in {"documented", "compatibility"}, key
        if method in {"post", "put", "patch"} and key not in EXPECTED_BODYLESS_POSTS:
            assert isinstance(operation.get("requestBody"), dict), key

        request_body = operation.get("requestBody")
        if request_body is not None:
            content = request_body.get("content") if isinstance(request_body, dict) else None
            assert isinstance(content, dict) and content, key
            assert any(
                isinstance(media, dict)
                and isinstance(media.get("schema"), dict)
                and media["schema"]
                for media in content.values()
            ), key

    delete_bodies = {
        key
        for key, operation in operations.items()
        if key[0] == "delete" and "requestBody" in operation
    }
    assert delete_bodies == {("delete", "/v1/chat/history")}


def test_every_success_response_has_a_nonempty_media_schema(
    operations: dict[tuple[str, str], dict[str, Any]],
) -> None:
    for key, operation in operations.items():
        assert "422" not in operation.get("responses", {}), key
        success_responses = [
            (status, response)
            for status, response in operation.get("responses", {}).items()
            if str(status).startswith("2")
        ]
        assert success_responses, key

        for status, response in success_responses:
            content = response.get("content") if isinstance(response, dict) else None
            assert isinstance(content, dict) and any(
                isinstance(media, dict)
                and isinstance(media.get("schema"), dict)
                and media["schema"]
                for media in content.values()
            ), (*key, status)


def test_runtime_success_statuses_and_non_json_media_are_explicit(
    operations: dict[tuple[str, str], dict[str, Any]],
) -> None:
    expected_success_statuses = {
        ("post", "/v1/identity/init"): {"201"},
        ("post", "/v1/genesis/imports"): {"200", "201"},
        ("post", "/v1/genesis/imports/plaintext"): {"200", "202"},
        ("post", "/v1/genesis/imports/{job_id}/finalize"): {"200", "202"},
        ("post", "/v1/genesis/persona_backfill"): {"200", "202"},
        ("post", "/v1/history_import/upload"): {"200", "202"},
        ("post", "/v1/model_api/memory/repair"): {"200", "202"},
    }
    for key, expected in expected_success_statuses.items():
        actual = {
            str(status)
            for status in operations[key]["responses"]
            if str(status).startswith("2")
        }
        assert actual == expected, key

    image_responses = operations[
        ("get", "/v1/screen/frames/{frame_id}/image")
    ]["responses"]
    for status in ("200", "206"):
        assert image_responses[status]["content"] == {
            "image/jpeg": {"schema": {"type": "string", "format": "binary"}}
        }
    assert "Content-Range" in image_responses["206"]["headers"]
    assert "416" in image_responses

    copytext_responses = operations[("get", "/v1/copytext")]["responses"]
    assert "304" in copytext_responses
    assert "content" not in copytext_responses["304"]
    assert "ETag" in copytext_responses["304"]["headers"]


def test_chat_memory_and_perception_contracts_are_concrete(
    public_schema: dict[str, Any],
    operations: dict[tuple[str, str], dict[str, Any]],
) -> None:
    for key, schema_name in EXPECTED_CORE_BODY_REFS.items():
        assert _json_body_ref(operations[key]) == f"#/components/schemas/{schema_name}"
        assert operations[key]["x-feedling-contract-level"] == "documented"

    schemas = public_schema["components"]["schemas"]
    for schema_name in ("HostedChatSendRequest", "ChatTransportRequest"):
        client_msg_id = schemas[schema_name]["properties"]["client_msg_id"]
        assert client_msg_id["type"] == "string"
        assert client_msg_id["format"] == "uuid"
        assert "600 seconds" in client_msg_id["description"]

    poll_query = _parameters(operations[("get", "/v1/chat/poll")], "query")
    assert set(poll_query) == {"since", "timeout", "consumer_id", "claim"}
    assert poll_query["timeout"]["schema"]["maximum"] == 60

    history_query = _parameters(operations[("get", "/v1/chat/history")], "query")
    assert set(history_query) == {
        "limit",
        "since",
        "before",
        "include_image_body",
        "include_image_bodies",
    }
    assert history_query["limit"]["schema"]["maximum"] == 200
    assert history_query["include_image_bodies"]["deprecated"] is True

    memory_id = _parameters(operations[("get", "/v1/memory/get")], "query")["id"]
    assert memory_id["required"] is True
    memory_delete_id = _parameters(
        operations[("delete", "/v1/memory/delete")], "query"
    )["id"]
    assert memory_delete_id["required"] is True

    perception_query = _parameters(
        operations[("get", "/v1/perception/app_open")], "query"
    )
    assert set(perception_query) == {"app", "bundle_id", "category", "ts", "client_ts"}
    assert perception_query["app"]["required"] is True
    assert perception_query["app"]["schema"]["minLength"] == 1
    assert perception_query["bundle_id"]["deprecated"] is True
    assert perception_query["client_ts"]["deprecated"] is True

    pending_consumer = _parameters(
        operations[("get", "/v1/genesis/resident/pending")], "query"
    )["consumer_id"]
    assert pending_consumer["required"] is True
    assert pending_consumer["schema"]["minLength"] == 1

    range_header = _parameters(
        operations[("get", "/v1/screen/frames/{frame_id}/image")], "header"
    )["range"]
    assert range_header["schema"]["pattern"] == r"^bytes=(?:\d+-\d*|-\d+)$"

    # Authentication belongs in securitySchemes, never in query parameters.
    for key, operation in operations.items():
        assert not ({"key", "api_key", "x-api-key"} & set(_parameters(operation, "query"))), key


def test_conditional_payloads_and_review_contract_match_runtime(
    public_schema: dict[str, Any],
    operations: dict[tuple[str, str], dict[str, Any]],
) -> None:
    schemas = public_schema["components"]["schemas"]

    for schema_name in ("EncryptedEnvelope", "MemoryEnvelope"):
        envelope = schemas[schema_name]
        assert envelope["if"]["properties"]["visibility"] == {"const": "shared"}
        assert envelope["then"]["required"] == ["K_enclave"]
        assert envelope["example"]["visibility"] == "shared"
        assert envelope["example"]["K_enclave"]

    perception_signal = schemas["PerceptionSignal"]
    assert perception_signal["if"] == {"required": ["envelope"]}
    assert perception_signal["then"] == {"required": ["changed"]}
    perception_items = schemas["PerceptionReportRequest"]["properties"]["items"]
    rows = perception_items["additionalProperties"]
    assert rows["type"] == "array"
    assert rows["items"]["type"] == "object"

    review_schema = schemas["ProactiveDecisionReviewRequest"]
    assert review_schema["required"] == ["label"]
    assert "good_presence" in review_schema["properties"]["label"]["enum"]
    review_content = operations[
        ("post", "/v1/proactive/decisions/{decision_id}/review")
    ]["requestBody"]["content"]
    for media_type in ("application/json", "application/x-www-form-urlencoded"):
        assert review_content[media_type]["schema"] == {
            "$ref": "#/components/schemas/ProactiveDecisionReviewRequest"
        }


def test_error_response_supports_unified_and_mcp_shapes(
    public_schema: dict[str, Any],
) -> None:
    error = public_schema["components"]["schemas"]["ErrorResponse"]
    variants = error["properties"]["error"]["oneOf"]
    assert {"type": "string"} in variants
    structured = next(variant for variant in variants if variant.get("type") == "object")
    assert structured["required"] == ["kind"]
    assert structured["properties"]["detail"]["type"] == "string"
    assert error["properties"]["blame"]["enum"] == [
        "user_provider",
        "provider_transient",
        "system",
    ]


def test_mcp_probe_and_approval_contract_matches_runtime_limits(
    public_schema: dict[str, Any],
    operations: dict[tuple[str, str], dict[str, Any]],
) -> None:
    from hosted import mcp_approvals, mcp_core

    schemas = public_schema["components"]["schemas"]
    upsert = schemas["McpServerUpsertRequest"]
    patch = schemas["McpServerPatchRequest"]
    probe = schemas["McpServerTestResponse"]

    assert upsert["additionalProperties"] is False
    assert upsert["properties"]["enabled"]["type"] == "boolean"
    assert upsert["properties"]["headers"]["maxProperties"] == mcp_core.MAX_HEADERS
    assert patch["additionalProperties"] is False
    assert patch["properties"]["enabled"]["type"] == "boolean"

    approval_schemas = [
        upsert["properties"]["read_only_tool_fingerprints"],
        patch["properties"]["read_only_tool_fingerprints"],
        probe["properties"]["read_only_tool_fingerprints"],
    ]
    for approval in approval_schemas:
        assert approval["maxProperties"] == (
            mcp_approvals.MAX_READ_ONLY_TOOL_APPROVALS)
        names = approval["propertyNames"]
        assert names["minLength"] == 1
        assert names["maxLength"] == 256
        for sample in ("search", "", "bad\x00name", "x" * 257, "搜索"):
            assert bool(re.fullmatch(names["pattern"], sample)) is (
                mcp_approvals.valid_tool_name(sample))
        fingerprint = approval["additionalProperties"]["pattern"]
        for sample in ("a" * 64, "A" * 64, "a" * 63):
            assert bool(re.fullmatch(fingerprint, sample)) is (
                mcp_approvals.valid_fingerprint(sample))

    post_description = operations[("post", "/v1/mcp/servers")]["description"]
    for kind in (
        "invalid_request",
        "invalid_enabled",
        "invalid_read_only_tool_fingerprints",
        "too_many_read_only_tool_fingerprints",
    ):
        assert kind in post_description

    patch_operation = operations[("patch", "/v1/mcp/servers/{name}")]
    assert "invalid_enabled" in patch_operation["description"]
    assert "409" in patch_operation["responses"]
    assert "cannot_encrypt" in patch_operation["responses"]["409"]["description"]

    probe_description = operations[
        ("post", "/v1/mcp/servers/{name}/test")
    ]["description"]
    for term in (
        "http://",
        "https://",
        "dns_busy",
        "transport",
        "response_too_large",
    ):
        assert term in probe_description

    url_description = upsert["properties"]["url"]["description"]
    assert "tools/list" in url_description
    assert "never dials the URL itself" not in url_description


def test_public_and_api_key_only_security_are_exact(
    public_schema: dict[str, Any],
    operations: dict[tuple[str, str], dict[str, Any]],
) -> None:
    assert public_schema["security"] == [
        {"ApiKeyAuth": []},
        {"RuntimeTokenAuth": []},
    ]
    schemes = public_schema["components"]["securitySchemes"]
    assert schemes["ApiKeyAuth"] == {
        "type": "apiKey",
        "in": "header",
        "name": "X-API-Key",
        "description": "Long-lived Feedling user API key.",
    }
    assert schemes["RuntimeTokenAuth"]["name"] == "X-Feedling-Runtime-Token"

    for key in EXPECTED_PUBLIC_OPERATIONS:
        assert operations[key]["security"] == [], key
    for key in EXPECTED_API_KEY_ONLY_OPERATIONS:
        assert operations[key]["security"] == [{"ApiKeyAuth": []}], key

    explicitly_public = {
        key for key, operation in operations.items() if operation.get("security") == []
    }
    explicitly_api_key_only = {
        key
        for key, operation in operations.items()
        if operation.get("security") == [{"ApiKeyAuth": []}]
    }
    assert explicitly_public == EXPECTED_PUBLIC_OPERATIONS
    assert explicitly_api_key_only == EXPECTED_API_KEY_ONLY_OPERATIONS


def test_sensitive_control_planes_enforce_api_key_in_backend(
    public_schema: dict[str, Any],
) -> None:
    # An OpenAPI security declaration is not an authorization boundary. Keep
    # the actual route dependency pinned so runtime tokens cannot mint a
    # long-lived key, delete an account, or submit a report the enclave cannot
    # authenticate correctly.
    del public_schema  # fixture ensures asgi_app has been imported
    from asgi.deps import require_api_key
    from asgi_app import app

    protected = {
        ("POST", "/v1/access/link-token"),
        ("POST", "/v1/account/reset"),
        ("POST", "/v1/perception/report"),
    }
    matched: set[tuple[str, str]] = set()
    routes = [
        route
        for included in app.routes
        for route in getattr(
            getattr(included, "original_router", None), "routes", [included]
        )
    ]
    for route in routes:
        path = getattr(route, "path", "")
        methods = getattr(route, "methods", set()) or set()
        for method, expected_path in protected:
            if path == expected_path and method in methods:
                dependencies = {
                    dependency.call
                    for dependency in getattr(route, "dependant").dependencies
                }
                assert require_api_key in dependencies, (method, path)
                matched.add((method, path))

    assert matched == protected


def test_path_templates_match_required_path_parameters(
    public_schema: dict[str, Any],
    operations: dict[tuple[str, str], dict[str, Any]],
) -> None:
    for (method, path), operation in operations.items():
        path_item = public_schema["paths"][path]
        parameters = [
            *path_item.get("parameters", []),
            *operation.get("parameters", []),
        ]
        path_parameters = [
            parameter
            for parameter in parameters
            if isinstance(parameter, dict) and parameter.get("in") == "path"
        ]
        names = [parameter.get("name") for parameter in path_parameters]
        template_tokens = set(re.findall(r"{([^{}]+)}", path))

        assert len(names) == len(set(names)), (method, path, names)
        assert set(names) == template_tokens, (method, path, names)
        assert all(parameter.get("required") is True for parameter in path_parameters), (
            method,
            path,
            path_parameters,
        )


def test_every_reference_resolves_to_a_component(public_schema: dict[str, Any]) -> None:
    refs = list(_walk_refs(public_schema))
    assert refs
    for ref in refs:
        resolved = _resolve_local_ref(public_schema, ref)
        assert isinstance(resolved, dict) and resolved, ref
