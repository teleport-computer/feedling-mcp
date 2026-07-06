"""Sync ASGI test client for the assembled backend (``asgi_app.app``).

The test suite drives the backend through :func:`make_client`, which returns a
**sync** client backed by ``httpx.ASGITransport`` over the real assembled
FastAPI app. Its request/response surface descends from the old Flask
test-client API (the suite was written against it pre-migration); no Flask is
involved anywhere here.

Supported surface (measured against the suite):
  - methods: ``get`` / ``post`` / ``put`` / ``delete`` / ``open``
  - request kwargs: ``headers=`` , ``json=`` , ``data=`` (raw bytes/str, form dict,
    or Flask-style multipart ``{"file": (fileobj|bytes, filename[, ctype]), ...}``),
    ``content_type=`` , and ``environ_overrides=`` (accepted + ignored — the one
    chunked-upload test asserts only the 413 status, which the up-front
    Content-Length guard already returns).
  - response: ``.status_code`` , ``.get_json(silent=)`` , ``.json`` , ``.data`` ,
    ``.text`` , ``.headers`` , ``.get_data(as_text=)``.

Each request runs on its own ``asyncio.run`` loop (mirroring the existing
``test_asgi_*`` helpers). Cross-thread poll wakes still work: a parked waiter
captures its own loop at registration and ``registry.wake`` reaches it via
``loop.call_soon_threadsafe`` — so a background-thread poll is woken by a
main-thread write exactly as under gunicorn. ``make_client()`` wires the async
wake hook and the envelope pubkey lookup (the lifespan side effects the tests
depend on; the lifespan itself is not run by ASGITransport, and no route reads
the lifespan httpx clients).
"""

from __future__ import annotations

import asyncio

import httpx


class _ShimResponse:
    """Flask-``Response``-shaped view over an ``httpx.Response``."""

    def __init__(self, resp: httpx.Response):
        self._resp = resp
        self.status_code = resp.status_code
        self.headers = resp.headers
        self.data = resp.content

    @property
    def text(self) -> str:
        return self._resp.text

    @property
    def json(self):
        # Flask exposes ``.json`` as a property (None on non-JSON); some tests
        # read it without parentheses.
        try:
            return self._resp.json()
        except Exception:
            return None

    def get_json(self, silent: bool = False):
        try:
            return self._resp.json()
        except Exception:
            if silent:
                return None
            raise

    def get_data(self, as_text: bool = False):
        return self._resp.text if as_text else self._resp.content


def _translate_multipart(data: dict):
    """Flask ``data={"file": (fileobj|bytes, name[, ctype]), "k": "v"}`` →
    httpx ``(files, fields)``."""
    files: dict = {}
    fields: dict = {}
    for key, val in (data or {}).items():
        if isinstance(val, tuple):
            fileobj = val[0]
            filename = val[1] if len(val) > 1 else key
            ctype = val[2] if len(val) > 2 else "application/octet-stream"
            raw = fileobj.read() if hasattr(fileobj, "read") else fileobj
            files[key] = (filename, raw, ctype)
        else:
            fields[key] = "" if val is None else str(val)
    return files, fields


class _AsgiTestClient:
    def __init__(self, app):
        self._app = app

    def _request(
        self,
        method: str,
        path: str,
        *,
        headers=None,
        json=None,
        data=None,
        content_type=None,
        environ_overrides=None,  # accepted for API-compat; see module docstring
        **_ignored,
    ) -> _ShimResponse:
        req_kwargs: dict = {"headers": dict(headers or {})}
        if json is not None:
            req_kwargs["json"] = json
        elif content_type == "multipart/form-data" and isinstance(data, dict):
            files, fields = _translate_multipart(data)
            req_kwargs["files"] = files  # httpx sets its own multipart content-type
            if fields:
                req_kwargs["data"] = fields
        elif isinstance(data, (bytes, str)):
            req_kwargs["content"] = data
            if content_type:
                req_kwargs["headers"].setdefault("content-type", content_type)
        elif isinstance(data, dict):
            req_kwargs["data"] = data
        if (
            content_type
            and content_type != "multipart/form-data"
            and "json" not in req_kwargs
            and "content" not in req_kwargs
        ):
            req_kwargs["headers"].setdefault("content-type", content_type)

        async def go():
            transport = httpx.ASGITransport(app=self._app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                return await client.request(method, path, **req_kwargs)

        return _ShimResponse(asyncio.run(go()))

    def get(self, path, **kw):
        return self._request("GET", path, **kw)

    def post(self, path, **kw):
        return self._request("POST", path, **kw)

    def put(self, path, **kw):
        return self._request("PUT", path, **kw)

    def delete(self, path, **kw):
        return self._request("DELETE", path, **kw)

    def open(self, path, method="GET", **kw):
        return self._request(method, path, **kw)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def make_client() -> _AsgiTestClient:
    """Sync test client over the real assembled FastAPI app (``asgi_app.app``).

    The canonical way for tests to drive the backend in-process (the ``client``
    fixture in tests/conftest.py wraps this).
    """
    import asgi_app  # lazy: only tests need the fully assembled app

    # The lifespan side effects the tests rely on — ASGITransport does not run
    # lifespan, so mirror them here (both idempotent):
    #   - async poll-waiter wakes (lifespan step 3);
    #   - core.envelope user-public-key lookup (lifespan step 4), without which
    #     server-side shared-envelope builds 500 with "not wired by assembly".
    from accounts import registry as accounts_registry
    from core import envelope as core_envelope
    from core import store as core_store
    from runtime.waiters import registry

    core_store.set_async_wake_hook(registry.wake)
    core_envelope.get_user_public_key = accounts_registry._get_user_public_key
    return _AsgiTestClient(asgi_app.app)


