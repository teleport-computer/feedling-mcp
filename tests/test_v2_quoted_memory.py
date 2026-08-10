"""V2 的「在 CHAT 里聊聊」——把用户引用的记忆卡展开进对话上下文。

现场(2026-08-10):用户在花园点「在 CHAT 里聊聊」引用一张卡问「你怎么看待这个」,
agent 回答说「这是 app 附在消息里的时间上下文」—— 它**真的只看到了时间戳块**。

客户端一直在发 `quoted_memory_ids`,send 层两条路都存了(model_api_chat_send_core
和 _send_resident),V1 由 enclave 的 _attach_quoted_memories 展开成解密卡片,
**唯独 V2 组装 turn 时从不查这个字段**,引用内容直接蒸发。

展开发生在读完 tail 之后、组装 turn 之前:解密聊天行归解密,展开引用归展开。
没有引用的轮次完全不查记忆库(绝大多数轮次都没有引用)。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from model_api_runtime.v2 import serve_worker  # noqa: E402


def _tail(user_id: str, rows: list[dict]) -> list[dict]:
    """生产路径的两步:解密聊天行 → 展开引用记忆。"""
    return serve_worker._expand_quoted_memories(
        user_id, serve_worker._decrypt_chat_rows(user_id, rows, user_only=False)
    )


def _row(mid: str, *, quoted: str = "") -> dict:
    row = {
        "id": mid,
        "ts": 1.0,
        "seq": 1,
        "role": "user",
        "body_ct": "ct",
        "K_enclave": "key",
    }
    if quoted:
        row["quoted_memory_ids"] = quoted
    return row


def _stub_decrypt(monkeypatch, plaintext: bytes = "你怎么看待这个".encode()):
    monkeypatch.setattr(serve_worker, "_mint_runtime_token", lambda _uid: "rt")
    monkeypatch.setattr(
        serve_worker.core_enclave,
        "_decrypt_envelope_via_enclave",
        lambda *_a, **_k: plaintext,
    )


def _stub_cards(monkeypatch, items: list[dict], calls: list | None = None):
    def _fetch(_store, _api_key, payload, **_kwargs):
        if calls is not None:
            calls.append(payload)
        return {"items": items}, 200

    monkeypatch.setattr(serve_worker.memory_core, "fetch", _fetch)
    monkeypatch.setattr(serve_worker.core_store, "get_store", lambda _uid: object())


def test_quoted_card_is_expanded_into_the_message(monkeypatch):
    """引用的卡要变成模型看得见的文字,前置在用户那句话之前。"""
    _stub_decrypt(monkeypatch)
    _stub_cards(monkeypatch, [{
        "id": "mem_1",
        "type": "fact",
        "summary": "hx 是 router-teamwork 全栈",
        "content": "hx 负责 router-teamwork 的全栈开发,含 CLI 和 MCP 集成。",
    }])

    rows = _tail("u1", [_row("m1", quoted="mem_1")])

    content = rows[0]["content"]
    assert "hx 是 router-teamwork 全栈" in content, "卡的内容必须进上下文"
    assert "mem_1" in content, "id 要带上,用户说改这张卡时 agent 才知道改哪张"
    assert content.rstrip().endswith("你怎么看待这个"), "用户原话必须在最后"
    # 原始 id 字段不该继续留在给模型的行上
    assert "quoted_memory_ids" not in rows[0]


def test_no_quote_means_no_memory_lookup(monkeypatch):
    """没有引用的轮次一次记忆库都不查 —— 绝大多数轮次都属于这种。"""
    _stub_decrypt(monkeypatch)
    calls: list = []
    _stub_cards(monkeypatch, [], calls)

    rows = _tail("u1", [_row("m1")])

    assert rows[0]["content"] == "你怎么看待这个", "没引用就原样"
    assert calls == [], "不该查记忆库"


def test_unresolvable_id_does_not_break_the_turn(monkeypatch):
    """卡被删了 / 取不回来时,这一轮照常进行,不能崩也不能吞掉用户的话。"""
    _stub_decrypt(monkeypatch)
    _stub_cards(monkeypatch, [])

    rows = _tail("u1", [_row("m1", quoted="mem_gone")])

    assert rows[0]["content"] == "你怎么看待这个"


def test_quote_block_does_not_teach_v2_an_op_its_schema_rejects(monkeypatch):
    """⚠️ V1 那段文案教的是 memory_patch / memory_delete —— V2 的 schema 只认
    memory_write(op=add/update/delete)。照抄会教模型调用自己 schema 拒绝的工具,
    比不给指引更糟(同 1c8293cd 钉死的那个陷阱)。"""
    _stub_decrypt(monkeypatch)
    _stub_cards(monkeypatch, [{
        "id": "mem_1", "type": "fact",
        "summary": "s", "content": "c",
    }])

    content = _tail("u1", [_row("m1", quoted="mem_1")])[0]["content"]

    assert "memory_patch" not in content
    assert "memory_delete" not in content
