"""memgarden 必须是**装进来的外部包**，不是仓库里的一份副本。

## 为什么需要这条

2026-08-23 之前，`backend/memgarden/` 是 io 自己的源码，包只是手工拷出去的副本。
两边靠人同步 —— 实测一个 session 内就漂了 6 个文件，而且**没有任何测试会报警**，
同步还两次把已经清掉的真实用户 id 又带了回去。

改成真依赖之后，「漂移」这个问题从根上没有了：只有一份源码，io 装它。这条测试
守的就是别有人图省事把副本加回来 —— 一旦 `backend/memgarden/` 重新出现，
io 会优先 import 本地那份（PYTHONPATH 在前），依赖形同虚设，而且不会有任何报错。

## 原来的纯度守卫去哪了

搬进包自己的仓库了（`tests/test_purity.py`）。内核是不是纯的，应该由内核自己证明；
io 读不到它的源文件，也不该越俎代庖。
"""
from __future__ import annotations

import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]


def test_no_local_copy_of_the_kernel():
    """仓库里不许再有内核的源码副本。"""
    for stale in ("backend/memgarden", "backend/memory_garden", "backend/agent_protocol_core"):
        assert not (REPO / stale).exists(), (
            f"{stale} 又出现了 —— io 会优先 import 它，装进来的包被无声架空"
        )


def test_kernel_is_not_imported_from_backend():
    """内核不能来自 ``backend/`` —— 那就是本地副本，装进来的包被架空了。

    不断言「必须在 site-packages」：本地开发常把依赖装到仓库里的临时目录
    （`.deps/`，已 gitignore）方便跑测试，那是合法的。真正致命的是 `backend/`
    下面出现一份 —— PYTHONPATH 里它在最前面，会静默盖掉装进来的版本。
    """
    import memgarden

    where = pathlib.Path(memgarden.__file__).resolve()
    assert (REPO / "backend") not in where.parents, f"memgarden 来自 backend/：{where}"


def test_lock_pins_a_hash_locked_release_asset():
    """lock 里必须钉一个具体版本的 wheel URL，且带哈希。

    ⚠️ **哈希锁住的是字节，不是出处。** 这条测试之前叫 "immutable"，那是过度声称
    （codex code_review 2026-08-23 指出，实测 GitHub Release 的 `immutable` 字段
    确实是 false）。准确的说法是：

        能保证   同一个 URL 被换成不同字节时，构建会失败而不是静默换包
        不保证   这些字节由公开 tag 的源码构建 —— tag 可移动、asset 可删可重传，
                 而且 tag 未签名、没有 build attestation

    要补上「出处」这一环，得由 tag 绑定的 CI 构建 Release 并生成 artifact
    attestation，升级依赖时验证 tag commit / digest / provenance 三者。那是独立
    一批活，见 HANDOFF 里的待拍板项。

    这里守住的是底线：钉分支（@main）会让构建完全不可复现，compose 哈希上链的
    整条证明链直接失效。
    """
    lock = (REPO / "backend" / "requirements.lock").read_text(encoding="utf-8")
    lines = lock.splitlines()
    for pkg in ("memgarden @", "agent-protocol-core @"):
        idx = next((i for i, l in enumerate(lines) if l.startswith(pkg)), None)
        assert idx is not None, f"lock 里没有 {pkg}"
        url = lines[idx]
        assert "/releases/download/v" in url, f"{pkg} 不是 Release 的固定 wheel：{url}"
        assert url.endswith(".whl \\") or url.endswith(".whl"), f"{pkg} 不是 wheel：{url}"
        assert any("--hash=sha256:" in l for l in lines[idx:idx + 3]), f"{pkg} 缺哈希"


@pytest.mark.parametrize("mod", ["memgarden", "agent_protocol_core"])
def test_declared_in_requirements_not_just_the_lock(mod):
    """requirements.txt 也要有 —— 只写进 lock 的话，下次 compile 就被抹掉了。"""
    req = (REPO / "backend" / "requirements.txt").read_text(encoding="utf-8")
    assert mod.replace("_", "-") in req, f"{mod} 没写进 requirements.txt"


def test_every_prompt_builder_call_passes_a_locale():
    """全仓每个 build_*_prompt 调用都必须显式给 locale。

    **这条是栽了三次才加的。** locale 是必填参数，漏传会 TypeError —— 听上去
    很安全，问题在于**本地跑不到就发现不了**：

      第一次  test_card_user_referent.py    文件名不匹配我挑测试用的模式
      第二次  test_v2_extraction_lanes.py   本地无 Postgres，它 error 而非执行断言
      第三次  test_v2_extraction*.py ×4     同样是文件名不匹配，CI 才炸出来

    三次的共同点是「我按文件名猜哪些测试相关」。这条不猜，扫全仓语法树。
    在 CI 之前跑，比让 CI 用 15 分钟告诉你便宜得多。
    """
    import ast

    BUILDERS = {"build_capture_prompt", "build_dream_prompt", "build_migrate_prompt"}
    SKIP_DIRS = (".deps/", ".venv/", "docs-site/", "node_modules/")
    repo = pathlib.Path(__file__).resolve().parents[1]

    offenders = []
    for path in repo.rglob("*.py"):
        rel = str(path.relative_to(repo))
        if any(rel.startswith(d) or f"/{d}" in rel for d in SKIP_DIRS):
            continue
        try:
            src = path.read_text(encoding="utf-8")
            tree = ast.parse(src)
        except (SyntaxError, UnicodeDecodeError):
            continue
        # 在 import 层用 partial 绑过 locale 的，调用点不必再传
        bound = {b for b in BUILDERS if f"{b} = _partial(" in src}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = (fn.attr if isinstance(fn, ast.Attribute)
                    else fn.id if isinstance(fn, ast.Name) else None)
            if name not in BUILDERS or name in bound:
                continue
            call = ast.unparse(node)
            # **kwargs 展开的：参数在别处（fixture / dict），这里看不出来
            if "locale" in call or "**" in call:
                continue
            offenders.append(f"{rel}:{node.lineno} {name}")

    assert not offenders, (
        "这些调用没传 locale —— 运行到就 TypeError：\n  " + "\n  ".join(offenders)
    )
