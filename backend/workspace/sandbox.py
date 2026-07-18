"""Lazy sandbox-provider abstraction and artifact execution boundary.

There is deliberately no Docker-socket implementation.  Production providers
must be registered by the deployment assembly (for example a broker in the
runner CVM or an E2B adapter) and must enforce their own egress, lifetime,
resource, and billing policy.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Protocol, runtime_checkable


class SandboxRequiredOperation(str, Enum):
    MATERIALIZE_ARTIFACT = "materialize_artifact"
    PARSE_UNTRUSTED_BINARY = "parse_untrusted_binary"
    SHELL = "shell"
    CODE = "code"


class SandboxUnavailable(RuntimeError):
    pass


class SandboxUsageUnavailable(SandboxUnavailable):
    """Provider acquired but the mandatory billing/audit write failed."""


@dataclass(frozen=True)
class SandboxAcquisitionEvent:
    """Content-free event suitable for billing/usage telemetry."""

    user_id: str
    provider: str
    purpose: str


@runtime_checkable
class SandboxSession(Protocol):
    def materialize(self, *, name: str, data: bytes, mime_type: str) -> str: ...
    def extract_text(self, *, path: str, mime_type: str) -> str: ...
    def run_shell(self, command: str) -> dict: ...
    def run_code(self, *, language: str, source: str) -> dict: ...
    def close(self) -> None: ...


@runtime_checkable
class SandboxProvider(Protocol):
    name: str
    def acquire(self, *, user_id: str, purpose: str) -> SandboxSession: ...


def requires_sandbox(operation: SandboxRequiredOperation) -> bool:
    """Authoritative trigger boundary for operations that touch physical bytes."""
    return operation in set(SandboxRequiredOperation)


class DisabledSandboxProvider:
    name = "disabled"

    def acquire(self, *, user_id: str, purpose: str) -> SandboxSession:
        raise SandboxUnavailable("sandbox provider is not configured")


class _MemoryTestSession:
    """Safe test seam: virtual bytes only; never executes host code or shell."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.closed = False

    def materialize(self, *, name: str, data: bytes, mime_type: str) -> str:
        if self.closed:
            raise SandboxUnavailable("sandbox session is closed")
        path = f"/sandbox/artifacts/{len(self.files):04d}-{str(name or 'artifact').replace('/', '_')}"
        self.files[path] = bytes(data)
        return path

    def extract_text(self, *, path: str, mime_type: str) -> str:
        data = self.files.get(path)
        if data is None:
            raise SandboxUnavailable("sandbox artifact not found")
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SandboxUnavailable("test sandbox has no binary parser") from exc

    def run_shell(self, command: str) -> dict:
        raise SandboxUnavailable("test sandbox never executes shell commands")

    def run_code(self, *, language: str, source: str) -> dict:
        raise SandboxUnavailable("test sandbox never executes code")

    def close(self) -> None:
        self.closed = True
        self.files.clear()


class MemoryTestSandboxProvider:
    """Explicit local unit-test provider; not selectable in production config."""

    name = "memory-test"

    def __init__(self) -> None:
        self.acquire_count = 0

    def acquire(self, *, user_id: str, purpose: str) -> SandboxSession:
        self.acquire_count += 1
        return _MemoryTestSession()


class LazySandbox:
    """Acquire exactly once, only at the first sandbox-required operation."""

    def __init__(
        self,
        provider: SandboxProvider,
        *,
        user_id: str,
        on_acquire: Callable[[SandboxAcquisitionEvent], None] | None = None,
    ) -> None:
        self.provider = provider
        self.user_id = str(user_id)
        self.on_acquire = on_acquire
        self._session: SandboxSession | None = None

    @property
    def acquired(self) -> bool:
        return self._session is not None

    def _get(self, operation: SandboxRequiredOperation) -> SandboxSession:
        if not requires_sandbox(operation):  # defensive if the enum expands
            raise SandboxUnavailable("operation is not sandbox-authorized")
        if self._session is None:
            session = self.provider.acquire(
                user_id=self.user_id, purpose=operation.value,
            )
            try:
                if self.on_acquire is not None:
                    self.on_acquire(SandboxAcquisitionEvent(
                        user_id=self.user_id,
                        provider=str(self.provider.name),
                        purpose=operation.value,
                    ))
            except Exception:
                # Usage persistence is part of acquisition, not best-effort
                # telemetry. Never expose an unbilled session to artifact
                # decryption, shell, or code execution.
                session.close()
                raise SandboxUsageUnavailable(
                    "sandbox usage ledger unavailable"
                )
            self._session = session
        return self._session

    def ensure(self, operation: SandboxRequiredOperation) -> None:
        """Acquire before a caller fetches/decrypts physical artifact bytes."""
        self._get(operation)

    def materialize_artifact(self, *, name: str, data: bytes,
                             mime_type: str = "application/octet-stream") -> str:
        return self._get(SandboxRequiredOperation.MATERIALIZE_ARTIFACT).materialize(
            name=name, data=data, mime_type=mime_type,
        )

    def extract_untrusted_text(self, *, path: str, mime_type: str) -> str:
        return self._get(SandboxRequiredOperation.PARSE_UNTRUSTED_BINARY).extract_text(
            path=path, mime_type=mime_type,
        )

    def run_shell(self, command: str) -> dict:
        return self._get(SandboxRequiredOperation.SHELL).run_shell(command)

    def run_code(self, *, language: str, source: str) -> dict:
        return self._get(SandboxRequiredOperation.CODE).run_code(
            language=language, source=source,
        )

    def close(self) -> None:
        if self._session is not None:
            self._session.close()
            self._session = None


_PROVIDER_FACTORIES: dict[str, Callable[[], SandboxProvider]] = {
    "disabled": DisabledSandboxProvider,
}


def _e2b_provider_factory() -> SandboxProvider:
    # Conditional import: E2B is optional and never loaded by text-only turns.
    from workspace.e2b_sandbox import E2BSandboxProvider

    return E2BSandboxProvider()


_PROVIDER_FACTORIES["e2b"] = _e2b_provider_factory


def register_sandbox_provider(name: str, factory: Callable[[], SandboxProvider]) -> None:
    """Deployment hook for ``cvm``/``e2b`` or another audited provider."""
    key = str(name or "").strip().lower()
    if not key or key in {"disabled", "memory-test"}:
        raise ValueError("invalid production sandbox provider name")
    _PROVIDER_FACTORIES[key] = factory


def register_cvm_sandbox_provider(factory: Callable[[], SandboxProvider]) -> None:
    """Assembly hook for a Feedling-controlled confidential-VM broker."""
    register_sandbox_provider("cvm", factory)


def register_e2b_sandbox_provider(factory: Callable[[], SandboxProvider]) -> None:
    """Assembly hook for E2B; deployment policy must authorize plaintext egress."""
    register_sandbox_provider("e2b", factory)


def configured_sandbox_provider() -> SandboxProvider:
    name = os.environ.get("FEEDLING_V2_SANDBOX_PROVIDER", "disabled").strip().lower()
    factory = _PROVIDER_FACTORIES.get(name)
    if factory is None:
        if name in {"cvm", "e2b"}:
            raise SandboxUnavailable(
                f"sandbox provider {name!r} needs a deployment adapter registration"
            )
        raise SandboxUnavailable("unknown sandbox provider")
    provider = factory()
    adapter_name = str(getattr(provider, "name", "") or "").strip().lower()
    if (
        adapter_name != name
        or not callable(getattr(provider, "acquire", None))
    ):
        raise SandboxUnavailable("sandbox provider adapter is invalid")
    return provider
