from __future__ import annotations
import logging
import os
import httpx

log = logging.getLogger("feedling.observability")


def scrape_enclave_resource(url: str | None = None, timeout: float = 3.0) -> dict | None:
    base = (url or os.environ.get("FEEDLING_ENCLAVE_URL") or "https://enclave:5003").rstrip("/")
    try:
        # enclave presents a dstack-KMS-derived self-signed TLS cert; the backend
        # calls it with verify=False throughout (see core/enclave.py). The privacy
        # boundary is the envelope, not transport.
        with httpx.Client(timeout=timeout, verify=False) as client:
            resp = client.get(f"{base}/internal/resource")
        if resp.status_code != 200:
            return None
        j = resp.json()
        return {"mem_bytes": j.get("mem_bytes"), "cpu_usage_usec": j.get("cpu_usage_usec")}
    except Exception as e:
        log.warning("[obs] enclave scrape failed: %s", e)
        return None
