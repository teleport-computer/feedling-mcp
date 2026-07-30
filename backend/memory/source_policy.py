"""Closed provenance enums shared by memory writers and the public API."""

MEMORY_SOURCE_VALUES = frozenset({
    "bootstrap",
    "chat",
    "genesis_import",
    "genesis_resident_distill",
    "history_import",
    "hosted_runtime_state",
    "live_conversation",
    "memory_capture",
    "memory_dream",
    "memory_migrate",
    "model_api_capture",
    "model_api_correction",
    "model_api_repair",
    "ombre_brain_sync",
    "resident_absorb",
    "resident_patch",
})

MEMORY_CAPTURE_MODE_VALUES = frozenset({
    "agent_tool",
    "genesis_resident_distill",
    "memory_capture",
    "memory_dream",
    "repair",
    "state",
})

RESIDENT_ABSORB_SOURCE = "resident_absorb"
RESIDENT_PATCH_SOURCE = "resident_patch"

