"""Shared transport policy for setup and runtime visual-model calls."""

from __future__ import annotations

import math

import provider_client


VISUAL_OUTPUT_MAX_TOKENS = 2000
VISUAL_REQUEST_INACTIVITY_TIMEOUT_SEC = 45.0
VISUAL_MAX_ATTEMPTS = 2
VISUAL_RETRY_BASE_DELAY_SEC = 0.5
VISUAL_INCLUDE_REASONING = False
# Fixed batch-local allowance for the work performed after the monotonic timer
# starts but outside the provider retry envelope: runtime-token minting, pinned
# route lookup/decrypt, image block serialization, and content-free traces. It
# is deliberately not multiplied by image count and is not a fallback bonus;
# image reads, dedicated calls, and any exact-main fallback all consume the same
# absolute deadline. The batch-budget event exposes this component separately;
# it is the operator evidence for revisiting this value.
VISUAL_BATCH_FIXED_OVERHEAD_SEC = 5.0


class VisualOutputTruncated(RuntimeError):
    """The provider explicitly exhausted the visual reply token budget."""

    reason = "output_truncated"

    def __init__(self):
        super().__init__(self.reason)


def visual_batch_budget_sec(image_count: int) -> float:
    """Derive one batch deadline from the live visual transport policy."""
    count = max(0, int(image_count))
    if count == 0:
        return 0.0
    overhead = float(VISUAL_BATCH_FIXED_OVERHEAD_SEC)
    if not math.isfinite(overhead) or overhead <= 0:
        raise ValueError(
            "VISUAL_BATCH_FIXED_OVERHEAD_SEC must be finite and positive"
        )
    per_image = provider_client.reliable_chat_nominal_envelope_sec(
        request_inactivity_timeout_sec=VISUAL_REQUEST_INACTIVITY_TIMEOUT_SEC,
        max_attempts=VISUAL_MAX_ATTEMPTS,
        base_delay_sec=VISUAL_RETRY_BASE_DELAY_SEC,
    )
    return count * per_image + overhead


def _visual_messages(*, prompt: str, image_mime: str, image_b64: str) -> list[dict]:
    return [{
        "role": "user",
        "content": [
            {"type": "text", "text": prompt},
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:{image_mime};base64,{image_b64}",
                },
            },
        ],
    }]


def _accept_visual_result(result: dict) -> dict:
    if provider_client.is_token_limit_stop_reason(result.get("stop_reason")):
        raise VisualOutputTruncated()
    return result


def request_visual_completion(
    config: provider_client.ProviderConfig,
    *,
    prompt: str,
    image_mime: str,
    image_b64: str,
    absolute_deadline: float | None = None,
) -> dict:
    """Call a visual route with the shared bounded transport configuration."""
    result = provider_client.reliable_chat_completion_isolated(
        config,
        _visual_messages(
            prompt=prompt, image_mime=image_mime, image_b64=image_b64
        ),
        max_tokens=VISUAL_OUTPUT_MAX_TOKENS,
        temperature=None,
        timeout=VISUAL_REQUEST_INACTIVITY_TIMEOUT_SEC,
        include_reasoning=VISUAL_INCLUDE_REASONING,
        max_attempts=VISUAL_MAX_ATTEMPTS,
        base_delay_sec=VISUAL_RETRY_BASE_DELAY_SEC,
        absolute_deadline=absolute_deadline,
    )
    return _accept_visual_result(result)
