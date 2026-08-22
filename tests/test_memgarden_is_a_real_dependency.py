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


def test_lock_pins_an_immutable_release_url():
    """lock 里必须钉死一个具体版本的 wheel，且带哈希。

    钉分支（@main）会让构建不可复现 —— compose 哈希上链的整条证明链就失效了。
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
