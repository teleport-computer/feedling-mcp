"""Content-free vocabulary for the enclave trace aggregate and alert consumer.

This is a projection allowlist, not a list of permitted enclave operations.
New/unrecognized trace purpose strings count under ``other`` until reviewed;
matching a slug regex would also admit user identifiers and secret strings.
Stdlib only: the Actions reporter imports this without backend dependencies.
"""
PURPOSE_LABELS = frozenset({
    "genesis_checkpoint", "genesis_staged_payload", "genesis_chunk",
    "genesis_persona", "genesis_voice", "identity_get", "identity_update_merge",
    "mcp_server_config", "memory_action", "model_api_provider_key",
    "model_api_recap_history", "runtime_v2_trajectory_review",
    "runtime_v2_trajectory_break_glass", "v2_caption_read", "v2_chat_read",
    "v2_effect_apply", "v2_summary_read", "v2_summary_segment_read",
    "v2_wake_discarded_draft", "v2_workspace_read", "screen_frame_decrypt",
    "screen_frame_image", "plaintext_shadow_frame", "voice_transcript_capture",
    "voice_transcript_read", "perception:location_signal", "perception:motion_state",
    "perception:calendar_next_event", "perception:playback", "perception:audio_route",
    "perception:weather", "perception:reminders", "perception:health_sleep",
    "perception:health_workout", "perception:health_vitals", "perception:health_activity",
    "perception:health_body", "perception:health_metabolic", "perception:health_cycle",
    "perception:health_mood", "perception:health_deleted", "other",
})
COUNT_KEYS = ("done", "timeout", "transport_error", "http_401", "http_403",
              "http_other", "calls", "unavailable", "users_affected")
WINDOW_KEYS = frozenset((*COUNT_KEYS, "start_at", "end_at", "unavailable_rate",
                         "top_purposes"))
PAYLOAD_KEYS = frozenset({"window_minutes", "calculated_at", "current", "previous"})
