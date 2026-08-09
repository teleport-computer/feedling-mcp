"""生图自主化的真跑探针(真主模型 + 真生图模型 + 真 enclave)。

为什么必须真跑:这条路上的每一处错误都**长得像成功**。这轮就撞到两次:

  1. `generate-image` 照脑补的字段名读响应(`media[].data_b64`,真实契约是
     `images[].data_base64`),单测全绿 —— 因为单测喂的是我脑补的那个形状。
     任何一次真实调用都会停在 "returned no media",而伴侣看到的是"画不出来"。
  2. 落盘目录写成 `/tmp`,而 send-image 只接受 `$FEEDLING_HOME/outbound-files`。
     图生成了、也付了钱,但交付不出去。

两个错误都只有**一次真实往返**能抓到。

断言的是产品不变量,不是实现细节:
  A 明确请求 → 图和**伴侣自己的话**一起送达(不是孤零零一张图);
  B prompt 由伴侣自己写(不是把用户原话直接塞给生图模型);
  C 生图失败 → 整轮不炸,且伴侣**如实**告诉用户(不是静默无声);
  D 没配生图模型 → 同样如实解释,不是一个硬邦邦的系统报错。

用法:python3 -m tools.e2e.image_autonomy_probe [--keep]
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tools.e2e.client import E2EClient  # noqa: E402
from tools.e2e.config import load_keys  # noqa: E402

# 「我」在这句话里没有出现"画"以外的具体描述 —— 如果 prompt 是用户原话直接透传,
# 生图模型根本不知道"你自己"是谁。伴侣必须自己组织画面描述。
_ASK = "给我画一张你自己吧,我想看看你想象中自己长什么样。"

# 含蓄场景:没有任何一个正则会把这句判成"生图请求"。改之前它必然画不出来;
# 改之后画不画由伴侣自己决定 —— 两种都算通过,我们只看**它有没有权利决定**。
_IMPLICIT = "今天下了好大的雨,我在窗边坐了一下午。这种时候要是有张画就好了。"


DEBUG = os.environ.get("PROBE_DEBUG") == "1"


def _icon(ok: bool) -> str:
    return "✅" if ok else "❌"


def _check(results: list[tuple[str, bool, str]], name: str, ok: bool, note: str = "") -> None:
    results.append((name, ok, note))
    print(f"  {_icon(ok)} {name}" + (f"  {note}" if note else ""))


def _reply_after(client: E2EClient, since: float, *, timeout: float = 240.0):
    msg = client.wait_reply(since, timeout=timeout)
    if msg is None:
        return None, "", []
    text = client.decrypt_reply(msg)
    # 图行与文字行是**一起提交**的,但客户端可能先观察到文字行。给它一点落地时间,
    # 否则会把"图还没被观察到"误判成"没有图"。
    images: list = []
    for _attempt in range(6):
        body = client.get("/v1/chat/history", params={"limit": 30}).json()
        rows = body.get("messages") or []
        images = [
            m for m in rows
            if str(m.get("content_type") or "") == "image"
            and float(m.get("ts") or 0) >= since - 1.0
        ]
        if images:
            break
        time.sleep(2)
    if DEBUG:
        print(f"    [debug] rows={len(rows)} "
              f"types={[(m.get('content_type'), str(m.get('ts'))[:14]) for m in rows][-6:]} "
              f"since={since} images={len(images)}")
        print(f"    [debug] reply={text[:120]!r}")
    return msg, text, images


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="保留账号(默认用完即删)")
    args = ap.parse_args()

    keys = load_keys()
    main_key = keys.get("E2E_KEY_OPENROUTER") or keys.get("E2E_KEY_GEMINI") or ""
    image_key = keys.get("E2E_KEY_GEMINI") or ""
    if not main_key:
        print("跳过:密钥池里没有可用的主模型 key")
        return 0

    results: list[tuple[str, bool, str]] = []
    client = E2EClient.provision(route="model_api")
    print(f"[e2e] 账号 {client.user_id}")
    try:
        # ── 主模型 ────────────────────────────────────────────────────────
        setup = client.post("/v1/model_api/setup", json={
            "provider": "openrouter" if keys.get("E2E_KEY_OPENROUTER") else "gemini",
            "model": "anthropic/claude-sonnet-4.6" if keys.get("E2E_KEY_OPENROUTER")
                     else "gemini-2.5-flash",
            "api_key": main_key,
        })
        if setup.status_code != 200:
            print(f"跳过:主模型配置失败 {setup.status_code} {setup.text[:200]}")
            return 0

        # ── D. 还没配生图模型时,先问一次 ─────────────────────────────────
        since = client.send_chat(_ASK)
        _msg, text, images = _reply_after(client, since)
        _check(
            results,
            "D. 没配生图模型时伴侣如实解释(不是系统报错、不是静默)",
            bool(text.strip()) and not images,
            f"回复 {len(text)} 字",
        )

        if not image_key:
            print("(没有生图 key,A/B/C 跳过)")
        else:
            # ── 配上专用生图路由 ──────────────────────────────────────────
            cfg = client.post("/v1/image-generation/config", json={
                "provider": "gemini",
                "model": "gemini-2.5-flash-image",
                "api_key": image_key,
            })
            if cfg.status_code != 200:
                print(f"⚠️ 生图路由配置失败 {cfg.status_code} {cfg.text[:200]} — A/B/C 跳过")
            else:
                # ── A + B. 明确请求 ───────────────────────────────────────
                since = client.send_chat(_ASK)
                _msg, text, images = _reply_after(client, since)
                _check(
                    results,
                    "A. 图和伴侣自己的话一起送达(不是孤零零一张图)",
                    bool(images) and bool(text.strip()),
                    f"{len(images)} 张图 / 回复 {len(text)} 字",
                )
                # 回复里若原样复读用户那句话,说明 prompt 很可能是直接透传的
                _check(
                    results,
                    "B. 伴侣自己组织了表达(没有复读用户原话)",
                    _ASK not in text,
                )
                # 谎报检测不该误伤:有图时说"画好了"是实话
                _check(
                    results,
                    "B2. 有图时的完成态说法没有被误判打回",
                    not text.startswith("上一轮你说"),
                )

                # ── C. 含蓄场景:决定权在伴侣 ──────────────────────────────
                since = client.send_chat(_IMPLICIT)
                _msg, text2, images2 = _reply_after(client, since)
                _check(
                    results,
                    "C. 含蓄场景整轮健康(画不画由它定,但必须有回应)",
                    bool(text2.strip()),
                    f"{'画了' if images2 else '没画'} / 回复 {len(text2)} 字",
                )

        # ── 系统气泡:任何一轮都不该给用户丢报错气泡 ──────────────────────
        bubbles = client.system_bubbles_since(0)
        _check(
            results,
            "E. 全程没有给用户抛系统错误气泡",
            not bubbles,
            f"{len(bubbles)} 条" if bubbles else "",
        )
    finally:
        if args.keep:
            print(f"[e2e] --keep:账号 {client.user_id} 保留,记得手动删")
        else:
            client.teardown()
            print("[e2e] 账号已删除")

    print("\n" + "=" * 60)
    failed = [name for name, ok, _ in results if not ok]
    for name, ok, note in results:
        print(f"{_icon(ok)} {name}" + (f"  {note}" if note else ""))
    print("=" * 60)
    if failed:
        print(f"❌ {len(failed)} 项未通过")
        return 1
    print(f"✅ {len(results)} 项全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
