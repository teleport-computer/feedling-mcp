"""Pure-unit tests for Garden「talk in chat」quoted-memory injection.

Covers the two ends of the backend path that carries a user-selected memory
into the agent's reply context (id persisted on the turn → enclave expands it
into a decrypted card → resident consumer injects it as a context block):

    - enclave_chat._attach_quoted_memories   (expand id → card on the message)
    - chat_resident_consumer._quoted_memory_context  (format card for the agent)

Both are pure (no DB / no network), so this module is in conftest's _PURE_UNIT
set and runs on a no-Postgres dev machine.

Run:
    pytest tests/test_quoted_memory_context.py -v
"""

import os
import sys
import types
from pathlib import Path


# Env + path bootstrap BEFORE importing the consumer (it reads env at module
# scope). Mirrors tests/test_chat_resident_consumer_image.py.
for k, v in {
    "FEEDLING_API_URL": "http://localhost:5001",
    "FEEDLING_API_KEY": "test_key_00000000",
    "AGENT_MODE": "http",
    "AGENT_HTTP_URL": "http://localhost:8080/chat",
    "CHECKPOINT_FILE": "/tmp/feedling_test_quoted_checkpoint.json",
}.items():
    os.environ.setdefault(k, v)

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

try:
    import content_encryption  # noqa: F401
except ModuleNotFoundError:
    _fake_enc = types.ModuleType("content_encryption")
    _fake_enc.build_envelope = lambda **kw: {"v": 1, "stub": True}
    sys.modules.setdefault("content_encryption", _fake_enc)

from enclave.routes import chat as enclave_chat  # noqa: E402
import tools.chat_resident_consumer as crc  # noqa: E402


# ---------------------------------------------------------------------------
# enclave_chat._attach_quoted_memories
# ---------------------------------------------------------------------------

def test_attach_expands_id_into_card():
    decrypted = [
        {"id": "u1", "role": "user", "content": "它最近老掉毛",
         "quoted_memory_ids": "mom_dog"},
        {"id": "a1", "role": "openclaw", "content": "..."},
    ]
    moments = [
        {"id": "mom_dog", "title": "养了一只叫蛋子的狗",
         "description": "柯基，两岁", "type": "fact"},
        {"id": "mom_other", "title": "别的", "description": "", "type": "moment"},
    ]

    enclave_chat._attach_quoted_memories(decrypted, moments)

    user = decrypted[0]
    # Raw id list is stripped so it never leaks in the response.
    assert "quoted_memory_ids" not in user
    assert user["quoted_memories"] == [{
        "id": "mom_dog",
        "type": "fact",
        "title": "养了一只叫蛋子的狗",
        "text": "养了一只叫蛋子的狗\n柯基，两岁",
    }]
    # Assistant message is untouched.
    assert "quoted_memories" not in decrypted[1]


def test_attach_unknown_id_is_skipped_not_errored():
    decrypted = [{"id": "u1", "role": "user", "quoted_memory_ids": "missing"}]
    enclave_chat._attach_quoted_memories(decrypted, [])
    assert "quoted_memory_ids" not in decrypted[0]
    assert "quoted_memories" not in decrypted[0]


def test_attach_title_only_memory_uses_title_as_text():
    decrypted = [{"id": "u1", "role": "user", "quoted_memory_ids": "m1"}]
    moments = [{"id": "m1", "title": "只有标题", "description": "", "type": ""}]
    enclave_chat._attach_quoted_memories(decrypted, moments)
    card = decrypted[0]["quoted_memories"][0]
    assert card["text"] == "只有标题"
    assert card["type"] == ""


def test_attach_v1_memory_falls_back_to_summary_content():
    # v1 memories keep their text in summary/content with title/description
    # empty — the enclave must still produce non-empty text (regression: the
    # card had empty text so the consumer skipped it and the agent saw nothing).
    decrypted = [{"id": "u1", "role": "user", "quoted_memory_ids": "m1"}]
    cards = [{
        "id": "m1", "title": "", "description": "",
        "summary": "用户有一只比熊犬，名叫崽崽。", "content": "", "type": "fact",
    }]
    enclave_chat._attach_quoted_memories(decrypted, cards)
    card = decrypted[0]["quoted_memories"][0]
    assert card["text"] == "用户有一只比熊犬，名叫崽崽。"
    assert card["title"] == "用户有一只比熊犬，名叫崽崽。"


def test_attach_noop_without_ids():
    decrypted = [{"id": "u1", "role": "user", "content": "hi"}]
    enclave_chat._attach_quoted_memories(decrypted, [{"id": "m", "title": "t"}])
    assert decrypted == [{"id": "u1", "role": "user", "content": "hi"}]


def test_attach_multiple_ids_preserve_order():
    decrypted = [{"id": "u1", "role": "user", "quoted_memory_ids": "a, b"}]
    moments = [
        {"id": "b", "title": "B", "description": "", "type": "fact"},
        {"id": "a", "title": "A", "description": "", "type": "fact"},
    ]
    enclave_chat._attach_quoted_memories(decrypted, moments)
    ids = [c["id"] for c in decrypted[0]["quoted_memories"]]
    assert ids == ["a", "b"]


# ---------------------------------------------------------------------------
# chat_resident_consumer._quoted_memory_context
# ---------------------------------------------------------------------------

def test_context_formats_block_with_type_prefix():
    msg = {"quoted_memories": [{
        "id": "mom_dog", "type": "fact", "title": "养了一只叫蛋子的狗",
        "text": "养了一只叫蛋子的狗\n柯基，两岁",
    }]}
    out = crc._quoted_memory_context(msg)
    assert out.startswith("The user is referring to this memory from their Garden:")
    assert "[fact] 养了一只叫蛋子的狗" in out
    assert "柯基，两岁" in out
    # id is surfaced so the agent can patch/delete the quoted card directly
    assert "(id=mom_dog)" in out
    assert "memory_patch" in out and "memory_delete" in out


def test_context_untyped_card_has_no_prefix():
    msg = {"quoted_memories": [{"id": "m", "type": "", "title": "t", "text": "hello"}]}
    out = crc._quoted_memory_context(msg)
    assert "- (id=m) hello" in out
    assert "[]" not in out


def test_context_card_without_id_omits_id_tag():
    msg = {"quoted_memories": [{"type": "fact", "text": "hello"}]}
    out = crc._quoted_memory_context(msg)
    assert "- [fact] hello" in out
    assert "(id=" not in out


def test_context_empty_returns_blank():
    assert crc._quoted_memory_context({}) == ""
    assert crc._quoted_memory_context({"quoted_memories": []}) == ""
    assert crc._quoted_memory_context({"quoted_memories": [{"text": ""}]}) == ""
    assert crc._quoted_memory_context({"quoted_memories": "not-a-list"}) == ""
