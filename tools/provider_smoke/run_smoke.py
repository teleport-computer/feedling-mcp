"""Provider 适配冒烟测试编排器。

用法（默认打线上 test CVM）：
    python -m tools.provider_smoke.run_smoke                 # 跑 .env key 齐全的全部 provider
    python -m tools.provider_smoke.run_smoke deepseek gemini # 指定子集
    python -m tools.provider_smoke.run_smoke --reuse         # 单账号切配置, 顺带测 respawn
      --base-url   默认 https://test-api.feedling.app
      --timeout    首条回复轮询超时秒(默认 120)
      --turns      默认 2(口令回声 + 上下文记忆)
"""
import argparse
import os
import sys

from tools.provider_smoke import assertions, matrix
from tools.provider_smoke.client import SmokeClient, SmokeError

DEFAULT_BASE_URL = "https://test-api.feedling.app"
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def load_dotenv(path: str) -> dict:
    env: dict = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return env


def build_env(dotenv_path: str) -> dict:
    env = dict(load_dotenv(dotenv_path))
    env.update({k: v for k, v in os.environ.items() if v})  # 真实环境变量优先
    return env


def parse_args(argv: list) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="provider_smoke")
    p.add_argument("providers", nargs="*", help="要测的 provider 子集；默认全部(有 key 的)")
    p.add_argument("--reuse", action="store_true", help="复用单账号, 连续 setup 切 provider")
    p.add_argument("--base-url", default=os.environ.get("FEEDLING_SMOKE_BASE_URL", DEFAULT_BASE_URL))
    p.add_argument("--timeout", type=float, default=120.0)
    p.add_argument("--turns", type=int, default=2)
    return p.parse_args(argv)


def _res(provider: str, result: str, stage: str, detail: str) -> dict:
    return {"provider": provider, "result": result, "stage": stage, "detail": detail}


def format_summary(results: list) -> str:
    lines = ["provider            result   stage         detail", "-" * 72]
    for r in results:
        lines.append(f"{r['provider']:<19} {r['result']:<8} {r['stage']:<13} {r['detail']}")
    return "\n".join(lines)


def run_provider(client: SmokeClient, provider: str, cfg: dict, *, turns: int, timeout: float, sess=None) -> dict:
    try:
        if sess is None:
            sess = client.register(f"provider-smoke-{provider}")

        # setup：候选 model 逐个试，直到一个通过同步自检
        used_model, last_detail = None, ""
        for model in cfg["models"]:
            try:
                client.setup(sess, provider, model, cfg["base_url"], cfg["api_key"])
                used_model = model
                break
            except SmokeError as e:
                last_detail = e.detail
        if used_model is None:
            return _res(provider, "FAIL", "setup", last_detail)

        # Write the bootstrap identity card (idempotent) so the account leaves
        # stage `needs_identity`; without it every chat reply is gated 409.
        client.init_identity(sess)

        client.enable_hosting(sess)

        # Open the needs_live_connection gate via verify_loop BEFORE any real
        # send — a reply to a message sent while the gate is closed is 409'd and
        # never retried, so the message would silently never get answered.
        client.open_chat_gate(sess)

        # 第 1 轮：口令回声
        token = assertions.make_token()
        r1 = client.send(sess, f"请只回复这一个词,不要任何其他内容: {token}")
        out1 = client.poll_turn(sess, str(r1["user_message"]["id"]), timeout)
        if out1["failed"]:
            stage = "no-reply" if not out1["settled"] else "turn-failure"
            return _res(provider, "FAIL", stage, f"turn1: {out1['failure']}")
        reply1 = out1["reply"]
        if assertions.is_fallback(reply1):
            return _res(provider, "FAIL", "fallback", reply1[:120])
        if not assertions.token_echoed(reply1, token):
            return _res(provider, "FAIL", "wrong-token", reply1[:120])
        if turns < 2:
            return _res(provider, "PASS", "-", f"1 turn OK (model={used_model})")

        # 第 2 轮：上下文记忆（同一 token 须被记住）
        r2 = client.send(sess, "我上一条让你回复的那个词是什么?请原样再说一遍,只回复那个词。")
        out2 = client.poll_turn(sess, str(r2["user_message"]["id"]), timeout)
        if out2["failed"]:
            stage = "no-reply" if not out2["settled"] else "turn-failure"
            return _res(provider, "FAIL", stage, f"turn2: {out2['failure']}")
        reply2 = out2["reply"]
        if assertions.is_fallback(reply2):
            return _res(provider, "FAIL", "fallback", reply2[:120])
        if not assertions.context_recalled(reply2, token):
            return _res(provider, "FAIL", "context-miss", f"turn2 未复述 token; got: {reply2[:120]}")
        return _res(provider, "PASS", "-", f"2 turns OK (model={used_model})")
    except SmokeError as e:
        return _res(provider, "FAIL", e.stage, e.detail)
    except Exception as e:  # noqa: BLE001 — 报告任何意外失败而非崩溃
        return _res(provider, "FAIL", "error", str(e)[:160])


def main(argv=None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    env = build_env(os.path.join(_REPO_ROOT, ".env"))
    loaded = matrix.load_matrix(env)
    requested = args.providers or matrix.all_providers()
    client = SmokeClient(args.base_url)

    results: list = []
    shared_sess = None
    print(f"base_url={args.base_url} reuse={args.reuse} timeout={args.timeout:.0f}s turns={args.turns}\n")
    for provider in requested:
        if provider not in matrix.PROVIDER_MATRIX:
            results.append(_res(provider, "SKIP", "-", "unknown provider"))
            continue
        if provider not in loaded:
            results.append(_res(provider, "SKIP", "-", f".env 无 {matrix.PROVIDER_MATRIX[provider]['env_var']}"))
            continue
        print(f"── {provider} ──")
        if args.reuse:
            if shared_sess is None:
                shared_sess = client.register("provider-smoke-reuse")
            res = run_provider(client, provider, loaded[provider], turns=args.turns, timeout=args.timeout, sess=shared_sess)
        else:
            res = run_provider(client, provider, loaded[provider], turns=args.turns, timeout=args.timeout)
        print(f"   {res['result']} ({res['stage']}) {res['detail']}\n")
        results.append(res)

    print(format_summary(results))
    return 0 if all(r["result"] in ("PASS", "SKIP") for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
