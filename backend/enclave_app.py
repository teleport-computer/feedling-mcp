#!/usr/bin/env python3
"""Feedling enclave service — thin entrypoint.

实现在 backend/enclave/ 包（FastAPI/ASGI；迁移设计稿
2026-07-04-enclave-asgi-migration-design 已删，见 git 历史）。
本文件保持 `python -u backend/enclave_app.py` 启动方式不变
（compose 命令与 compose_hash 故事不变，CONTRIBUTING §7；
tools/e2e_encryption_test.py 与 tests/e2e_model_api_test.py 也直接拉起它）。"""

from __future__ import annotations

from enclave import config, serving, state

if __name__ == "__main__":
    state.bootstrap()
    tls = serving.materialize_tls_files()
    scheme = "https" if tls else "http"
    print(
        f"Feedling enclave service listening on {scheme}://0.0.0.0:{config.ENCLAVE_PORT}",
        flush=True,
    )
    serving.run_enclave_server(tls)
