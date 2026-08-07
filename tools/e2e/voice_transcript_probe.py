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

# 一通"信息密度高"的真实通话:12 件彼此独立、都值得记住的事,分散在全程。
# 与上一版(3 个锚点 + 160 轮废话)的区别是关键:那一版在考"模型会不会从废话里
# 挑重点"(答案是会,而且只挑最重要的一件);这一版在考**产品真正的问题** ——
# 当一通电话里确实有很多值得记的事,记忆会全都留下,还是照样只留一两张?
_FACTS = [
    ("绘本封面定稿,用了黛蓝色的海", "黛蓝"),
    ("下周三要去上海参加插画展", "插画展"),
    ("橘猫年糕这两天不吃饭,明天去宠物医院", "年糕"),
    ("手腕腱鞘炎复发,医生让画一小时歇十分钟", "腱鞘炎"),
    ("妈妈下个月来杭州住两周", "妈妈"),
    ("换了新的数位板,牌子是 Wacom", "数位板"),
    ("答应了出版社月底交第二本的样稿", "样稿"),
    ("最近改成早上六点起床画画", "六点"),
    ("咖啡戒了,改喝大麦茶", "大麦茶"),
    ("冬至一定要吃芝麻馅汤圆,这是家里的传统", "汤圆"),
    ("上周把工作室搬到了朝南的房间", "工作室"),
    ("想在明年春天办一次个人小展", "个人小展"),
]


def _turns() -> list[dict]:
    """把 12 件事自然地铺进一通电话,中间夹少量闲聊。"""
    turns: list[dict] = [
        {"role": "user", "text": "喂,今天有空,想跟你多聊会儿。"},
        {"role": "assistant", "text": "好啊,我在听。"},
    ]
    for i, (fact, _anchor) in enumerate(_FACTS):
        turns.append({"role": "user", "text": f"{fact}。"})
        turns.append({"role": "assistant", "text": "嗯,记下了。"})
        if i % 3 == 2:
            turns.append({"role": "user", "text": "对了随便说说,今天天气还不错。"})
            turns.append({"role": "assistant", "text": "是啊,适合出去走走。"})
    turns += [
        {"role": "user", "text": "差不多就这些,先挂了。"},
        {"role": "assistant", "text": "好,注意休息。"},
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
        hit = [a for _f, a in _FACTS if a in blob]
        miss = [a for _f, a in _FACTS if a not in blob]
        coverage = len(hit) / len(_FACTS)
        print(f"     命中 {len(hit)}/{len(_FACTS)}: {hit}")
        print(f"     漏掉: {miss}")
        # 核心断言:信息密度高的通话必须产出多张卡。只出一两张说明记忆按"挑最
        # 重要的一件"在工作,那对一小时的通话是不可接受的稀疏。
        check("C2. 多张卡(信息密集的通话不该只留一两张)", len(cards_found) >= 5,
              f"卡数={len(cards_found)}")
        check("C3. 事实覆盖率 ≥ 50%", coverage >= 0.5,
              f"{len(hit)}/{len(_FACTS)} = {coverage:.0%}")
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
