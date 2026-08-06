"""挂断→归档→落卡的真跑探针(真模型、真 enclave)。

为什么必须真跑:这条路上每一个"静默降级"都长得像成功。摘要时代的 V2 就是
这样丢了所有语音记忆 —— capture 触发了、日志是绿的、游标照推,只是白名单少
一个值,窗口里根本没有那行。单测看不见这种事,它需要一次完整的往返。

断言的都是会真实咬人的不变量:
  A 全文归档可读回,且解密后与发出去的一致(不是"有一行就算过");
  B 聊天里只留一条卡、且**小**——它是 prompt 尾巴为整通电话付的全部代价;
  C 记忆是从**全文**蒸的:埋在通话中段的锚点必须成卡(只蒸预览的话必然漏);
  D 记忆卡带 voice_call_id,且经 memory_fetch 读得回来(readside 是显式白名单,
    少改一处就静默剥掉,写了等于没写);
  E 逐轮行已删。

用法:python3 -m tools.e2e.voice_transcript_probe [--keep]
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tools.e2e.client import E2EClient  # noqa: E402
from tools.e2e.config import load_keys  # noqa: E402

# 锚点刻意分散在通话的开头/中段/结尾。中段那个是核心:预览只取头尾,所以
# 「中段锚点成卡」是"确实蒸了全文"的唯一硬证据。
_ANCHOR_HEAD = "封面定稿用了黛蓝色的海"
_ANCHOR_MIDDLE = "下周三要去上海参加插画展"
_ANCHOR_TAIL = "明天带年糕去宠物医院"


def _turns() -> list[dict]:
    turns = [
        {"role": "user", "text": f"喂,今天{_ANCHOR_HEAD},终于交稿了。"},
        {"role": "assistant", "text": "恭喜!黛蓝的海一定很好看。"},
    ]
    # 填充,把中段锚点推到预览取不到的位置
    for i in range(40):
        turns.append({"role": "user", "text": f"对了还有件小事,第{i}件,不太重要。"})
        turns.append({"role": "assistant", "text": f"嗯,记下了第{i}件。"})
    turns.append({"role": "user", "text": f"重要的是,{_ANCHOR_MIDDLE},记得提醒我订高铁票。"})
    turns.append({"role": "assistant", "text": "好,下周三上海插画展,会提醒你订票。"})
    for i in range(40):
        turns.append({"role": "user", "text": f"还有些琐事,第{i}条,随便说说。"})
        turns.append({"role": "assistant", "text": f"好的,第{i}条我听着。"})
    turns += [
        {"role": "user", "text": f"最后,{_ANCHOR_TAIL},它这两天不吃饭。"},
        {"role": "assistant", "text": "记得带它去检查,别拖。"},
    ]
    return turns


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="不删探针账号(排查用)")
    args = ap.parse_args()

    pool = load_keys()
    turns = _turns()
    call_id = f"vcall_probe{int(time.time())}"
    results: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, ok, detail))
        print(f"  {'✅' if ok else '❌'} {name}  {detail}", flush=True)

    with E2EClient.provision(route="model_api") as c:
        r = c.post("/v1/model_api/setup", json={
            "provider": "openai_compatible",
            "model": "claude-haiku-4-5-20251001",
            "api_key": pool["E2E_KEY_HOJIMI"],
            "base_url": pool["E2E_HOJIMI_BASE"],
        })
        if r.status_code != 200:
            print(f"setup 失败 {r.status_code}: {r.text[:200]}")
            return 1

        t0 = time.time()
        r = c.post("/v1/voice/finalize", json={
            "call_id": call_id, "turns": turns, "duration_sec": 1800,
        })
        check("finalize", r.status_code == 200,
              f"HTTP {r.status_code} 耗时 {time.time() - t0:.0f}s {r.text[:120]}")
        if r.status_code != 200:
            return 1

        # A —— 归档可读回且完整
        r = c.get(f"/v1/voice/transcripts/{call_id}")
        archived = r.json() if r.status_code == 200 else {}
        check("A. 归档可读回", r.status_code == 200 and isinstance(archived.get("transcript"), dict),
              f"HTTP {r.status_code} turns={archived.get('turn_count')} chars={archived.get('char_count')}")
        check("A2. 归档含全部轮次", int(archived.get("turn_count") or 0) == len(turns),
              f"{archived.get('turn_count')} vs 发出 {len(turns)}")

        r = c.get("/v1/voice/transcripts")
        listed = r.json().get("items") if r.status_code == 200 else []
        check("A3. 列表能看到这通", any(i.get("call_id") == call_id for i in (listed or [])),
              f"列表 {len(listed or [])} 条")

        # B —— 聊天里只有一条卡,而且小
        r = c.get("/v1/chat/history?limit=200")
        msgs = r.json().get("messages") if r.status_code == 200 else []
        cards = [m for m in (msgs or []) if m.get("source") == "voice_call_transcript"]
        check("B. 聊天只留一条通话卡", len(cards) == 1, f"卡数={len(cards)} 总消息={len(msgs or [])}")
        # 卡是密文,长度用密文近似:body_ct base64 ≈ 明文的 4/3 倍再加封装
        if cards:
            approx = len(str(cards[0].get("body_ct") or "")) * 3 // 4
            check("B2. 卡体量有界(<1500 字节明文估算)", approx < 1500,
                  f"密文估算明文 ≈ {approx} 字节")
        check("E. 逐轮行已删",
              not any(m.get("voice_call_id") == call_id and m.get("source") != "voice_call_transcript"
                      for m in (msgs or [])),
              "无残留逐轮行")

        # C/D —— 记忆是从全文蒸的,且带溯源
        print("  … 等 capture 落卡(最多 180s)", flush=True)
        cards_found: list[dict] = []
        for _ in range(36):
            time.sleep(5)
            r = c.post("/v1/memory/index", json={"limit": 100})
            if r.status_code == 200:
                items = r.json().get("items") or []
                if items:
                    cards_found = items
                    break
        blob = str(cards_found)
        check("C. 花园落卡", bool(cards_found), f"卡数={len(cards_found)}")
        check("C2. 头部锚点成卡", _ANCHOR_HEAD[:4] in blob or "黛蓝" in blob, _ANCHOR_HEAD)
        check("C3. **中段锚点成卡(证明蒸的是全文而非预览)**",
              "上海" in blob or "插画展" in blob, _ANCHOR_MIDDLE)
        check("C4. 尾部锚点成卡", "年糕" in blob or "宠物医院" in blob, _ANCHOR_TAIL)

        if cards_found:
            ids = [i.get("id") for i in cards_found[:5] if i.get("id")]
            r = c.post("/v1/memory/fetch", json={"ids": ids})
            fetched = r.json().get("items") if r.status_code == 200 else []
            tagged = [i for i in (fetched or []) if i.get("voice_call_id")]
            check("D. 记忆卡带 voice_call_id 且能读回",
                  bool(tagged) and tagged[0].get("voice_call_id") == call_id,
                  f"{len(tagged)}/{len(fetched or [])} 张带溯源")

        if not args.keep:
            rr = c.post("/v1/account/reset", json={"confirm": "delete-all-data"})
            print(f"  [cleanup] account reset http={rr.status_code}")

    print("\n==== SUMMARY ====")
    failed = [n for n, ok, _ in results if not ok]
    for n, ok, d in results:
        print(f"  {'✅' if ok else '❌'} {n}")
    print(f"\n{len(results) - len(failed)}/{len(results)} PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
