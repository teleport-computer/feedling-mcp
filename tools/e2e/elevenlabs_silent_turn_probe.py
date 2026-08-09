"""真打一通 ElevenLabs 电话,验证「这一轮不说话」不会杀掉整通。

这是 2026-08-08 那次线上事故唯一**验不到**的一环。我们的网关侧我验过了
(噪音轮返回的 SSE 从 0 正文变成有正文),但 ElevenLabs 收不收那个最小正文,
只有让它真的跑一遍才知道 —— 探针直接打我们的网关是绕过 ElevenLabs 的。

事故形态:噪音轮 / 生命周期已结束 / ASR 修订被取代这三条路径都返回过一个
**零 content** 的 SSE 流(只有 role 块 + finish 块)。ElevenLabs 的 Custom LLM
判协议错误 `1002 custom_llm_error: LLM Cascade Error`,**杀掉整通电话**,
用户侧显示「暂时无法通话」。

做法:用账号自己的 key 建一个**一次性** agent,Custom LLM 指向**测试环境**的
网关,用 WebSocket 真打一通,把会触发静音路径的词说进去,看连接是否存活。
agent 用完即删(测试账号同理)。

⚠️ **现状(2026-08-10):这个探针还没能跑通一通完整的电话。**

已经确认对的:
  - agent 建得出来,读回来 `custom_llm_extra_body: true`、`text_only` 允许覆盖、
    `custom_llm.url` 指向测试网关 —— 配置本身没问题;
  - WebSocket 协议走通了(错误信息从"连不上"变成了具体的字段校验错误);
  - `text_only` 必须在 **init 的 conversation_config_override** 里开,
    不是在 agent 配置里(第一版写错了,ElevenLabs 压根不把文字当用户输入);
  - `source_info.source` 是枚举,不能自己编值。

还没解决的:**连普通轮都拿不到回复**,ElevenLabs 报
`custom_llm_error: Failed to generate response from custom LLM`,而服务端那侧
只有 verify_ping、没有任何通话行 —— 说明请求没走到我们的网关(或在鉴权前就被拒)。
下一步该查的是 text_only 模式下 `custom_llm_extra_body` 到底有没有被转发给
Custom LLM(我们的网关从 `elevenlabs_extra_body` 里取 token,取不到就 401)。

**所以静音轮那一环仍然没有被验证。** 判定逻辑已经写成:普通轮不通就报
「什么都没验到」,绝不会拿它去下「最小正文不被接受」的结论 ——
第一版就是这么误报的,而且那个结论完全是错的。

用法:python3 -m tools.e2e.elevenlabs_silent_turn_probe [--keep]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import httpx  # noqa: E402

from tools.e2e.client import E2EClient  # noqa: E402
from tools.e2e.config import load_keys  # noqa: E402

_API = "https://api.elevenlabs.io"
_AGENT_NAME = "IO silent-turn probe (throwaway)"

# 这些词会命中网关的非语音过滤(voice.message_filter 的噪音标记),
# 走的正是事故那条「这一轮不说话」的路径。
_SILENT_TRIGGERS = ["噪音", "music"]
_NORMAL = "你好呀,今天过得怎么样"


def _client() -> httpx.Client:
    # 本机系统代理会污染回环与外网调用,固定关掉(见 mcp-suite-local-test-gotchas)
    return httpx.Client(timeout=60, verify=False, trust_env=False)


def create_agent(key: str, gateway_url: str) -> str:
    body = {
        "name": _AGENT_NAME,
        "tags": ["io", "throwaway", "probe"],
        "conversation_config": {
            "asr": {"quality": "high", "provider": "scribe_realtime",
                    "user_input_audio_format": "pcm_16000"},
            "turn": {"turn_eagerness": "normal", "turn_model": "turn_v3",
                     "silence_end_call_timeout": -1},
            "tts": {"model_id": "eleven_flash_v2_5"},
            "conversation": {
                "max_duration_seconds": 300,
                "client_events": [
                    "user_transcript", "agent_response", "interruption",
                ],
            },
            "agent": {
                "first_message": "",
                "language": "zh",
                "prompt": {
                    "prompt": "You are a bridge. Do nothing on your own.",
                    "llm": "custom-llm",
                    "custom_llm": {
                        "url": gateway_url,
                        "model_id": "io-current",
                        "api_type": "chat_completions",
                    },
                    "ignore_default_personality": True,
                    "temperature": 0.3,
                    "max_tokens": 1024,
                },
            },
        },
        "platform_settings": {
            "auth": {"enable_auth": False},
            "privacy": {"record_voice": False, "retention_days": 0,
                        "delete_audio": True},
            # 这两个 override 必须在 agent 上放行,init 里传的才会被接受。
            "overrides": {
                "custom_llm_extra_body": True,
                "conversation_config_override": {
                    "conversation": {"text_only": True},
                },
            },
        },
    }
    with _client() as http:
        r = http.post(f"{_API}/v1/convai/agents/create",
                      headers={"xi-api-key": key}, json=body)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"create agent failed {r.status_code}: {r.text[:400]}")
    return r.json()["agent_id"]


def delete_agent(key: str, agent_id: str) -> None:
    with _client() as http:
        r = http.delete(f"{_API}/v1/convai/agents/{agent_id}",
                        headers={"xi-api-key": key})
    print(f"[probe] 删除 agent {agent_id[:12]}… → {r.status_code}")


async def run_conversation(key: str, agent_id: str, session: dict,
                           messages: list[str]) -> dict:
    """跑一通文字模式的对话,返回观察到的事件与断连原因。"""
    import ssl

    import websockets

    # 本机根证书链不全(和 httpx 那边同一个毛病),这里显式给一个可用的 ctx。
    # ⚠️ 连不上是**本机传输问题**,绝不能算成「ElevenLabs 拒收」——
    # 下面的判定必须把这两种失败分开,否则会得出一个完全错误的结论。
    ctx = ssl.create_default_context()
    try:
        import certifi

        ctx = ssl.create_default_context(cafile=certifi.where())
    except Exception:  # noqa: BLE001
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

    url = (f"wss://api.elevenlabs.io/v1/convai/conversation"
           f"?agent_id={agent_id}")
    seen = {"agent_responses": [], "errors": [], "closed": None,
            "sent": 0, "transcripts": []}
    try:
        async with websockets.connect(
            url, max_size=8 * 1024 * 1024, ssl=ctx,
        ) as ws:
            # text_only 必须在 **init 的 conversation_config_override** 里开,
            # 不是在 agent 配置里 —— 照 Swift SDK 的 EventSerializer 的形状写。
            # 第一版写在 agent 配置里,ElevenLabs 压根没把文字当用户输入处理:
            # 两轮都发出去了、连 user_transcript 事件都没有,更别说调我们的网关。
            await ws.send(json.dumps({
                "type": "conversation_initiation_client_data",
                "conversation_config_override": {
                    "conversation": {"text_only": True},
                },
                "custom_llm_extra_body": {
                    "io_voice_token": session["token"],
                    "io_call_id": session["call_id"],
                },
            }))
            for text in messages:
                await ws.send(json.dumps({"type": "user_message", "text": text}))
                seen["sent"] += 1
                # 给这一轮留出时间:失败的话 ElevenLabs 会在几百毫秒内断开
                deadline = asyncio.get_event_loop().time() + 25
                while asyncio.get_event_loop().time() < deadline:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=5)
                    except asyncio.TimeoutError:
                        continue
                    event = json.loads(raw)
                    kind = event.get("type")
                    if kind == "agent_response":
                        body = (event.get("agent_response_event") or {})
                        seen["agent_responses"].append(
                            str(body.get("agent_response") or "")[:120])
                        break
                    if kind == "user_transcript":
                        body = (event.get("user_transcription_event") or {})
                        seen["transcripts"].append(
                            str(body.get("user_transcript") or "")[:80])
                    if kind in {"internal_tentative_agent_response"}:
                        continue
                    if "error" in json.dumps(event).lower():
                        seen["errors"].append(json.dumps(event)[:300])
                        break
    except Exception as exc:  # noqa: BLE001 — 断连本身就是结论
        seen["closed"] = f"{type(exc).__name__}: {str(exc)[:200]}"
        # 本机传输失败 ≠ 对方拒收。分开标注,否则会把「我连不上」误判成
        # 「ElevenLabs 不接受我们的回复」——那是一个完全错误的结论。
        seen["local_transport_failure"] = isinstance(
            exc, (OSError, ssl.SSLError)
        ) and not seen["sent"]
    return seen


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()

    keys = load_keys()
    el_key = keys.get("E2E_KEY_ELEVENLABS") or ""
    main_key = keys.get("E2E_KEY_OPENROUTER") or ""
    if not el_key:
        print("跳过:密钥池里没有 E2E_KEY_ELEVENLABS")
        return 0

    client = E2EClient.provision(route="model_api")
    print(f"[probe] 账号 {client.user_id}")
    agent_id = ""
    try:
        if main_key:
            client.post("/v1/model_api/setup", json={
                "provider": "openrouter", "model": "anthropic/claude-sonnet-4.6",
                "api_key": main_key})
        r = client.post("/v1/voice/sessions", json={})
        if r.status_code != 200:
            print(f"跳过:建通话会话失败 {r.status_code} {r.text[:200]}")
            return 0
        session = r.json()
        gateway = session["gateway_url"].rstrip("/") + "/chat/completions"
        print(f"[probe] 网关 {gateway}")

        agent_id = create_agent(el_key, gateway)
        print(f"[probe] 一次性 agent {agent_id[:12]}…")

        # 先一句正常的确认链路通,再送会触发静音路径的词
        result = asyncio.run(run_conversation(
            el_key, agent_id, session, [_NORMAL, *_SILENT_TRIGGERS]))

        print("\n=== 观察 ===")
        print("发出的轮次:", result["sent"])
        print("收到的回复:", result["agent_responses"])
        print("错误事件:", result["errors"] or "(无)")
        print("断连:", result["closed"] or "(未断)")

        print("\n" + "=" * 56)
        if result.get("local_transport_failure"):
            # 一次都没发出去 = 根本没验到那一环,不能给任何结论
            print("⚠️ 本机连不上 ElevenLabs(传输层),**这一轮什么都没验到**")
            print("   不是「对方拒收」——别把它当结论。修好本机 TLS 再跑。")
            print("=" * 56)
            return 2
        # ⚠️ 先确认**普通轮**通了,再谈静音轮。第一次跑就栽在这:两轮都发出去了、
        # 一条回复都没收到 —— 失败其实发生在普通轮,压根没走到静音路径,
        # 而判定却直接报了「最小正文不被接受」。没验到就说没验到。
        if not result["agent_responses"]:
            print("⚠️ 连**普通轮**都没拿到回复 —— 这一轮没有验到静音路径。")
            print("   可能是探针的 agent 配置/token 传递不对,不是产品问题。")
            print(f"   断连信息:{result['closed']}")
            print("=" * 56)
            return 2
        survived = (
            result["sent"] >= 2
            and not result["errors"]
            and not (result["closed"] and "1002" in str(result["closed"]))
        )
        if survived:
            print("✅ 静音轮之后通话仍然存活 —— ElevenLabs 接受最小正文")
        else:
            print("❌ 静音轮之后通话中断 —— 最小正文不被接受,需要改用 Skip Turn")
        print("=" * 56)
        return 0 if survived else 1
    finally:
        if agent_id and not args.keep:
            delete_agent(el_key, agent_id)
        if args.keep:
            print(f"[probe] --keep:账号 {client.user_id} 与 agent 保留")
        else:
            client.teardown()
            print("[probe] 账号已删除")


if __name__ == "__main__":
    raise SystemExit(main())
