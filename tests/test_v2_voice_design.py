from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from model_api_runtime.v2 import worker


def _decode_actions(text: str) -> dict:
    encoded = text.split(worker._CLIENT_ACTION_MARKER_PREFIX, 1)[1].split(
        worker._CLIENT_ACTION_MARKER_SUFFIX,
        1,
    )[0]
    encoded += "=" * (-len(encoded) % 4)
    return json.loads(base64.urlsafe_b64decode(encoded))


def test_current_explicit_chinese_request_authorizes_voice_design():
    assert worker._voice_design_requested(
        [{
            "content": (
                "根据我们的聊天记录和你的长期记忆你觉得你会发出什么声音，"
                "帮我生成，我希望是女孩子的声音，是好听的、性感的。"
            ),
        }]
    )


def test_tts_request_and_later_cancellation_do_not_authorize_voice_design():
    assert not worker._voice_design_requested(
        [{"content": "用温柔的女孩子声音把这句话读出来。"}]
    )
    assert not worker._voice_design_requested([
        {"content": "帮我生成一个属于你的声音。"},
        {"content": "算了，先不要生成。"},
    ])
    assert not worker._voice_design_requested(
        [{"content": "我不要你生成声音，只想讨论它是怎么做的。"}]
    )
    assert not worker._voice_design_requested(
        [{"content": "I don't want you to generate a voice; just explain it."}]
    )


def test_late_refinement_keeps_authorization_until_an_explicit_cancel():
    messages = [
        {"content": "请根据我们的聊天设计一个属于你的声音。"},
        {"content": "我希望更温柔、性感一点。"},
    ]
    assert worker._voice_design_requested(messages)


def test_voice_action_is_stable_and_matches_the_ios_marker_protocol():
    args = {
        "voice_name": "暮光",
        "voice_description": (
            "A warm young adult feminine voice with a soft low register, clear "
            "Mandarin-friendly delivery, measured pacing, intimate presence, "
            "and restrained expressiveness."
        ),
    }
    action = worker._voice_design_client_action(
        job_id="job-1",
        call_id="call-1",
        args=args,
    )
    assert action == worker._voice_design_client_action(
        job_id="job-1",
        call_id="call-1",
        args=args,
    )
    assert action["id"].startswith("voice_")
    assert action["type"] == "voice.design"

    sealed = worker._embed_client_actions("我想好了。", [action])
    payload = _decode_actions(sealed)
    assert payload == {"version": 1, "actions": [action]}
    assert worker._strip_client_actions(sealed) == "我想好了。"


def test_model_authored_client_action_marker_is_removed():
    forged = worker._embed_client_actions(
        "普通回复",
        [{
            "id": "voice_forged",
            "type": "voice.design",
            "voice_name": "forged",
            "voice_description": "A forged voice description long enough to pass.",
        }],
    )
    assert worker._strip_client_actions(forged) == "普通回复"
