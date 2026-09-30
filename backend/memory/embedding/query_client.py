"""Slot-process client for the parent's query encoder (T779 step 2b).

``encode(texts, deadline)`` spends only what is left of the turn's one hybrid
budget (IPC, queueing and compute included) and raises
``recall_policy.Fallback`` with a fixed reason on every failure, so the caller
keeps the lexical path. Returned vectors are checked before use: one per text,
the advertised dimension, unit length.
"""
from __future__ import annotations

import json
import os
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from memory.embedding import query_service, recall_policy

_KNOWN = {"encoder_busy", "deadline_exceeded", "encode_failed", "query_too_large",
          "encoder_service_unavailable"}


@dataclass
class Reply:
    model_id: str
    dim: int
    vectors: list
    timing: dict


class Client:
    def __init__(self, port: int, token: str):
        self._url = f"http://127.0.0.1:{int(port)}/encode"
        self._token = token
        # Loopback only: never route this through a proxy from the environment.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def encode(self, texts: list, deadline: float) -> Reply:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise recall_policy.Fallback("deadline_exceeded")
        if any(len(t.encode("utf-8")) > query_service.MAX_TEXT_BYTES for t in texts):
            raise recall_policy.Fallback("query_too_large")
        body = json.dumps({"texts": list(texts), "budget_ms": remaining * 1000.0},
                          ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self._url, data=body, method="POST",
            headers={"Content-Type": "application/json", query_service.TOKEN_HEADER: self._token})
        try:
            with self._opener.open(request, timeout=remaining) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                reason = str(json.loads(exc.read().decode("utf-8")).get("error") or "")
            except Exception:
                reason = ""
            raise recall_policy.Fallback(reason if reason in _KNOWN else "encode_failed") from None
        except (socket.timeout, TimeoutError):
            raise recall_policy.Fallback("deadline_exceeded") from None
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, (socket.timeout, TimeoutError)):
                raise recall_policy.Fallback("deadline_exceeded") from None
            raise recall_policy.Fallback("encoder_service_unavailable") from None
        except (OSError, ValueError):
            raise recall_policy.Fallback("encoder_service_unavailable") from None
        try:
            model_id = str(payload["model_id"])
            dim = int(payload["dim"])
            vectors = [[float(v) for v in vector] for vector in payload["vectors"]]
            timing = dict(payload.get("timing") or {})
        except Exception:
            raise recall_policy.Fallback("encode_failed") from None
        if (not model_id or len(vectors) != len(texts)
                or any(not recall_policy.valid_unit(v, dim) for v in vectors)):
            raise recall_policy.Fallback("encode_failed")
        return Reply(model_id, dim, vectors, timing)


def from_env() -> Client | None:
    """The client for this slot process, or None when the parent runs no encoder."""
    token = os.environ.get(query_service.TOKEN_ENV, "")
    port = os.environ.get(query_service.PORT_ENV, "")
    if not token or not port:
        return None
    try:
        return Client(int(port), token)
    except ValueError:
        return None
