#!/usr/bin/env python3
"""Write isolated Codex configs for eight independent qualification workers.

Codex 0.144.3 applies a named ``$CODEX_HOME/<name>.config.toml`` profile only
when that profile is selected on a top-level invocation with ``-p``.  It does
not reliably apply a custom child-agent permission profile.  This module
therefore writes one top-level profile per locked API-key profile and no
``[agents]`` configuration at all.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

try:
    from qa.orchestration_contract import (
        MEMORY_CONTRACT_PROFILE_ID,
        PROFILE_AGENT_TYPES,
    )
except ModuleNotFoundError:  # Direct ``python qa/...py`` execution.
    from orchestration_contract import MEMORY_CONTRACT_PROFILE_ID, PROFILE_AGENT_TYPES


SUPERVISOR_PERMISSION_PROFILE = "io-e2e-agent-driven-test-supervisor"
PROFILE_NAME = SUPERVISOR_PERMISSION_PROFILE  # Stable public constant.
PERSONA_MEMORY_JUDGE_PROFILE_NAME = "persona_memory_judge"
PERSONA_MEMORY_JUDGE_PERMISSION_PROFILE = "io-e2e-agent-driven-test-persona-memory-judge"
_DNS_NAME = re.compile(
    r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z"
)
_MODEL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_AMBIENT_READ_ROOTS = tuple(
    dict.fromkeys(Path(value).resolve() for value in ("/tmp", "/var/tmp", "/dev/shm"))
)
_COMMON_ENV = (
    "HOME",
    "PATH",
    "LANG",
    "TMPDIR",
    "PYTHONDONTWRITEBYTECODE",
    "PYTHONPATH",
    "QA_SOURCE_ROOT",
    "QA_RUN_ID",
    "QA_EXPECTED_DEPLOYMENT_SHA",
    "QA_EXPECTED_RUNTIME",
    "IO_E2E_BASE_URL",
)


class CodexConfigError(RuntimeError):
    """A fixed error for an unsafe permission-profile input."""


@dataclass(frozen=True)
class CodexConfigBundle:
    main: str
    profiles: Mapping[Path, str]


def worker_permission_profile(profile_id: str) -> str:
    return f"io-e2e-agent-driven-test-{profile_id}"


def _directory(path: Path, name: str) -> Path:
    if not path.is_absolute() or path.is_symlink():
        raise CodexConfigError(f"{name} must be an absolute non-symlink directory")
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        raise CodexConfigError(f"{name} is missing") from None
    if not stat.S_ISDIR(metadata.st_mode) or resolved == Path(resolved.anchor):
        raise CodexConfigError(f"{name} must be a non-root directory")
    if metadata.st_uid != os.geteuid():
        raise CodexConfigError(f"{name} must be owned by the current user")
    return resolved


def _runtime_directory(path: Path) -> Path:
    """Return one narrow, owner-controlled interpreter runtime root."""

    resolved = _directory(path, "runtime read root")
    metadata = resolved.stat()
    home = Path.home().resolve()
    codex_private = (home / ".codex").resolve()
    if stat.S_IMODE(metadata.st_mode) & 0o022:
        raise CodexConfigError("runtime read root must not be group/world writable")
    if _contains(resolved, home) or _contains(codex_private, resolved):
        raise CodexConfigError("runtime read root is too broad or private")
    return resolved


def _worker_executable(path: Path, runtimes: Sequence[Path]) -> Path:
    """Bind the worker interpreter to a validated runtime ``bin`` directory."""

    if not path.is_absolute():
        raise CodexConfigError("worker Python must be an absolute path")
    try:
        executable = path.resolve(strict=True)
        metadata = executable.stat()
    except (OSError, RuntimeError):
        raise CodexConfigError("worker Python is unavailable") from None
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) & 0o022
        or not os.access(executable, os.X_OK)
    ):
        raise CodexConfigError("worker Python must be an owner-controlled executable")
    matching_runtime = next(
        (runtime for runtime in runtimes if executable.parent == runtime / "bin"),
        None,
    )
    if matching_runtime is None:
        raise CodexConfigError("worker Python must be directly beneath a runtime bin")
    try:
        bin_metadata = executable.parent.stat()
    except OSError:
        raise CodexConfigError("worker Python runtime bin is unavailable") from None
    if (
        not stat.S_ISDIR(bin_metadata.st_mode)
        or bin_metadata.st_uid != os.geteuid()
        or stat.S_IMODE(bin_metadata.st_mode) & 0o022
    ):
        raise CodexConfigError("worker Python runtime bin must be owner-controlled")
    return executable


def _private_directory(path: Path, name: str, *, empty: bool = False) -> Path:
    resolved = _directory(path, name)
    metadata = path.lstat()
    if stat.S_IMODE(metadata.st_mode) != 0o700:
        raise CodexConfigError(f"{name} must be owner-only")
    if empty:
        try:
            if any(path.iterdir()):
                raise CodexConfigError(f"{name} must be empty")
        except CodexConfigError:
            raise
        except OSError:
            raise CodexConfigError(f"{name} is unreadable") from None
    return resolved


def _future_file(path: Path, name: str, *, private_parent: bool = False) -> Path:
    if not path.is_absolute() or path.is_symlink() or not path.name:
        raise CodexConfigError(f"{name} must be an absolute non-symlink path")
    parent = (
        _private_directory(path.parent, f"{name} parent")
        if private_parent
        else _directory(path.parent, f"{name} parent")
    )
    candidate = parent / path.name
    if candidate.exists():
        try:
            metadata = candidate.lstat()
        except OSError:
            raise CodexConfigError(f"{name} is unsafe") from None
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise CodexConfigError(f"{name} is unsafe")
    return candidate


def _contains(parent: Path, child: Path) -> bool:
    return child == parent or parent in child.parents


def _reject_ambient_readable(paths: Sequence[Path]) -> None:
    # Codex 0.144.3's ``:minimal`` permission grants system temporary roots.
    # A more-specific ``deny`` does not subtract that ambient grant, so private
    # and explicitly denied data must never be placed beneath those roots.
    if any(
        candidate == ambient or ambient in candidate.parents
        for candidate in paths
        for ambient in _AMBIENT_READ_ROOTS
    ):
        raise CodexConfigError("denied run data is beneath an ambient readable root")


def _quoted(value: str) -> str:
    # JSON strings are valid TOML basic strings and safely quote paths.
    return json.dumps(value, ensure_ascii=True)


def _permission_header(profile: str, description: str) -> list[str]:
    key = _quoted(profile)
    return [
        f"[permissions.{key}]",
        f"description = {_quoted(description)}",
        "",
        f"[permissions.{key}.filesystem]",
    ]


def _network_lines(
    profile: str, host: str, *, allow_local_binding: bool = False
) -> list[str]:
    key = _quoted(profile)
    return [
        "",
        f"[permissions.{key}.network]",
        "enabled = true",
        'mode = "full"',
        "enable_socks5 = false",
        "enable_socks5_udp = false",
        "allow_upstream_proxy = false",
        "dangerously_allow_non_loopback_proxy = false",
        "dangerously_allow_all_unix_sockets = false",
        f"allow_local_binding = {'true' if allow_local_binding else 'false'}",
        "",
        f"[permissions.{key}.network.domains]",
        f'{_quoted(host)} = "allow"',
        "",
    ]


def _disabled_network_lines(profile: str) -> list[str]:
    """Explicitly deny shell/tool network for an offline reasoning profile.

    Codex's own authenticated model transport is outside the tool sandbox.  The
    profile therefore does not need deployment or arbitrary egress in order to
    grade already-collected evidence received through its private inputs.
    """

    key = _quoted(profile)
    return [
        "",
        f"[permissions.{key}.network]",
        "enabled = false",
        'mode = "full"',
        "enable_socks5 = false",
        "enable_socks5_udp = false",
        "allow_upstream_proxy = false",
        "dangerously_allow_non_loopback_proxy = false",
        "dangerously_allow_all_unix_sockets = false",
        "allow_local_binding = false",
        "",
    ]


def _shell_policy(
    home: Path,
    temporary: Path,
    *,
    work: Path,
    manifest: Path | None,
    profile_id: str | None,
    agent_type: str | None,
    artifact_root: Path | None,
    aggregation_input_root: Path | None,
    orchestration_receipt: Path | None,
    worker_python: Path | None,
    qualification_mode: str | None,
) -> list[str]:
    include = list(_COMMON_ENV)
    fixed = {
        "HOME": str(home),
        "TMPDIR": str(temporary),
        "QA_WORK_ROOT": str(work),
    }
    include.append("QA_WORK_ROOT")
    if manifest is not None:
        if worker_python is None or qualification_mode not in {"release", "diagnostic"}:
            raise CodexConfigError("worker shell runtime is missing")
        include.extend(
            (
                "QA_PRIVATE_MANIFEST",
                "QA_PROFILE_ID",
                "QA_AGENT_TYPE",
                "QA_ARTIFACT_DIR",
                "QA_PYTHON_BIN",
                "QA_QUALIFICATION_MODE",
            )
        )
        fixed.update(
            {
                "QA_PRIVATE_MANIFEST": str(manifest),
                "QA_PROFILE_ID": str(profile_id),
                "QA_AGENT_TYPE": str(agent_type),
                "QA_ARTIFACT_DIR": str(artifact_root),
                "QA_PYTHON_BIN": str(worker_python),
                "QA_QUALIFICATION_MODE": qualification_mode,
            }
        )
    if aggregation_input_root is not None:
        include.extend(("QA_AGGREGATION_INPUT_ROOT", "QA_ORCHESTRATION_RECEIPT"))
        fixed["QA_AGGREGATION_INPUT_ROOT"] = str(aggregation_input_root)
        fixed["QA_ORCHESTRATION_RECEIPT"] = str(orchestration_receipt)
    lines = [
        "[shell_environment_policy]",
        'inherit = "all"',
        "ignore_default_excludes = false",
        "experimental_use_profile = false",
        "include_only = [",
    ]
    lines.extend(f"  {_quoted(name)}," for name in include)
    lines.extend(("]", "", "[shell_environment_policy.set]"))
    lines.extend(f"{name} = {_quoted(value)}" for name, value in fixed.items())
    lines.append("")
    return lines


def _judge_shell_policy(home: Path, temporary: Path, work: Path) -> list[str]:
    """Return a deliberately tiny environment for the semantic judge.

    In particular, this does not inherit the Feedling base URL, deployment
    metadata, provisioning-manifest paths, provider keys, or admin token names
    used by provisioning and live profile workers.
    """

    include = (
        "HOME",
        "PATH",
        "LANG",
        "TMPDIR",
        "PYTHONDONTWRITEBYTECODE",
        "QA_RUN_ID",
        "QA_WORK_ROOT",
    )
    fixed = {
        "HOME": str(home),
        "TMPDIR": str(temporary),
        "QA_WORK_ROOT": str(work),
    }
    lines = [
        "[shell_environment_policy]",
        'inherit = "all"',
        "ignore_default_excludes = false",
        "experimental_use_profile = false",
        "include_only = [",
    ]
    lines.extend(f"  {_quoted(name)}," for name in include)
    lines.extend(("]", "", "[shell_environment_policy.set]"))
    lines.extend(f"{name} = {_quoted(value)}" for name, value in fixed.items())
    lines.append("")
    return lines


def _base_settings(profile: str, codex_model: str) -> list[str]:
    return [
        f"default_permissions = {_quoted(profile)}",
        f"model = {_quoted(codex_model)}",
        # The qualification journey is highly constrained and its semantic
        # decisions are bounded.  Pin the lowest practical supervisor effort
        # and verbosity so the final schema-bound response does not spend
        # minutes reasoning silently and trip the authenticated stream's idle
        # timeout after every deterministic probe has already completed.
        'model_reasoning_effort = "low"',
        'model_reasoning_summary = "none"',
        'model_verbosity = "low"',
        'approval_policy = "never"',
        'web_search = "disabled"',
        'cli_auth_credentials_store = "file"',
        "check_for_update_on_startup = false",
        "allow_login_shell = false",
        "",
    ]


def _feature_lines() -> list[str]:
    return [
        "[features]",
        "apps = false",
        "auth_elicitation = false",
        "browser_use = false",
        "browser_use_external = false",
        "browser_use_full_cdp_access = false",
        "code_mode = false",
        "code_mode_host = false",
        "computer_use = false",
        "hooks = false",
        "image_generation = false",
        "in_app_browser = false",
        "plugins = false",
        "remote_plugin = false",
        "shell_snapshot = false",
        "skill_mcp_dependency_install = false",
        "standalone_web_search = false",
        "tool_suggest = false",
        "workspace_dependencies = false",
        "multi_agent = false",
        "network_proxy = true",
        "",
    ]


def _profile_config(
    *,
    profile_id: str,
    agent_type: str,
    manifest: Path,
    home: Path,
    temporary: Path,
    work: Path,
    artifact_root: Path,
    codex_model: str,
    worker_python: Path,
    qualification_mode: str,
) -> str:
    lines = _base_settings(worker_permission_profile(profile_id), codex_model)
    lines.insert(
        2,
        "developer_instructions = "
        + _quoted(
            "Run only the assigned Feedling qualification profile. Read the "
            "one-row QA_PRIVATE_MANIFEST and never seek another profile, the "
            "full provisioning manifest, public artifacts, or provider/admin secrets."
        ),
    )
    lines.extend(
        _shell_policy(
            home,
            temporary,
            work=work,
            manifest=manifest,
            profile_id=profile_id,
            agent_type=agent_type,
            artifact_root=artifact_root,
            aggregation_input_root=None,
            orchestration_receipt=None,
            worker_python=worker_python,
            qualification_mode=qualification_mode,
        )
    )
    return "\n".join(lines)


def _persona_memory_judge_config(
    *,
    home: Path,
    temporary: Path,
    work: Path,
    codex_model: str,
) -> str:
    lines = _base_settings(PERSONA_MEMORY_JUDGE_PERMISSION_PROFILE, codex_model)
    lines.insert(
        2,
        "developer_instructions = "
        + _quoted(
            "Grade only the blinded Feedling persona-memory trajectory supplied "
            "on stdin. Return only the requested structured result. Never seek "
            "provisioning data, account identities, provider/admin credentials, "
            "deployment endpoints, source files, prior run evidence, or tools."
        ),
    )
    lines.extend(_judge_shell_policy(home, temporary, work))
    return "\n".join(lines)


def build_config_bundle(
    *,
    output: Path,
    source_root: Path,
    artifact_root: Path,
    full_manifest: Path,
    profile_manifest_dir: Path,
    supervisor_home: Path,
    supervisor_tmp: Path,
    supervisor_work: Path,
    worker_root: Path,
    worker_output_root: Path,
    aggregation_input_root: Path,
    orchestration_receipt: Path,
    codex_model: str,
    allowed_host: str,
    worker_python: Path,
    qualification_mode: str = "release",
    runtime_read_roots: Sequence[Path] = (),
    allow_local_binding: bool = False,
    persona_judge_root: Path | None = None,
    persona_judge_scratch_root: Path | None = None,
) -> CodexConfigBundle:
    codex_home = _private_directory(output.parent, "CODEX_HOME")
    destination = _future_file(output, "Codex config", private_parent=True)
    source = _directory(source_root, "source root")
    artifacts = _directory(artifact_root, "artifact root")
    manifests = _private_directory(
        profile_manifest_dir, "profile manifest directory", empty=True
    )
    provisioning = _future_file(
        full_manifest, "full provisioning manifest", private_parent=True
    )
    memory_manifest = _future_file(
        manifests / f"{MEMORY_CONTRACT_PROFILE_ID}.json",
        "memory contract manifest",
        private_parent=True,
    )
    root_home = _private_directory(supervisor_home, "supervisor home")
    root_tmp = _private_directory(supervisor_tmp, "supervisor temp")
    root_work = _private_directory(supervisor_work, "supervisor work")
    workers = _private_directory(worker_root, "worker root")
    worker_outputs = _private_directory(
        worker_output_root, "worker output root", empty=True
    )
    aggregation_inputs = _private_directory(
        aggregation_input_root, "aggregation input root", empty=True
    )
    receipt = _future_file(
        orchestration_receipt, "orchestration receipt", private_parent=True
    )
    runtimes = tuple(
        dict.fromkeys(_runtime_directory(path) for path in runtime_read_roots)
    )
    judge_paths: tuple[Path, Path, Path, Path, Path] | None = None
    if (persona_judge_root is None) != (persona_judge_scratch_root is None):
        raise CodexConfigError(
            "persona judge profile and scratch roots must be configured together"
        )
    if persona_judge_root is not None and persona_judge_scratch_root is not None:
        judge_root = _private_directory(persona_judge_root, "persona judge root")
        judge_scratch = _private_directory(
            persona_judge_scratch_root, "persona judge scratch root", empty=True
        )
        judge_home = _private_directory(
            judge_root / "home", "persona judge home", empty=True
        )
        judge_tmp = _private_directory(
            judge_root / "tmp", "persona judge temp", empty=True
        )
        judge_work = _private_directory(
            judge_root / "work", "persona judge work", empty=True
        )
        try:
            entries = {entry.name for entry in judge_root.iterdir()}
        except OSError:
            raise CodexConfigError("persona judge root is unreadable") from None
        if entries != {"home", "tmp", "work"}:
            raise CodexConfigError("persona judge root must contain only clean roots")
        judge_paths = (judge_root, judge_home, judge_tmp, judge_work, judge_scratch)

    if qualification_mode not in {"release", "diagnostic"}:
        raise CodexConfigError("qualification mode is invalid")
    if type(allow_local_binding) is not bool:
        raise CodexConfigError("allow-local-binding must be a boolean")
    if destination.parent != codex_home:
        raise CodexConfigError("Codex config must be directly beneath CODEX_HOME")
    if qualification_mode == "release" and not _contains(source, artifacts):
        raise CodexConfigError("artifact root must be inside the source checkout")
    top_level_private = (
        codex_home,
        manifests,
        root_home,
        root_tmp,
        root_work,
        workers,
        worker_outputs,
        aggregation_inputs,
        *((judge_paths[0], judge_paths[4]) if judge_paths is not None else ()),
    )
    if any(_contains(source, private) for private in top_level_private):
        raise CodexConfigError("private run data must be outside the source checkout")
    if any(
        _contains(left, right) or _contains(right, left)
        for index, left in enumerate(top_level_private)
        for right in top_level_private[index + 1 :]
    ):
        raise CodexConfigError("private Codex roots must be disjoint")
    if any(
        _contains(artifacts, private) or _contains(private, artifacts)
        for private in top_level_private
    ):
        raise CodexConfigError("artifact root must be isolated from private run data")
    if _contains(source, provisioning) or any(
        _contains(private, provisioning) for private in top_level_private
    ):
        raise CodexConfigError("full provisioning manifest path is not isolated")
    if (
        receipt == provisioning
        or _contains(source, receipt)
        or any(_contains(private, receipt) for private in top_level_private)
    ):
        raise CodexConfigError("orchestration receipt path is not isolated")
    _reject_ambient_readable(
        (*top_level_private, artifacts, provisioning, memory_manifest, receipt)
    )
    if any(
        _contains(private, runtime) or _contains(runtime, private)
        for runtime in runtimes
        for private in top_level_private
    ):
        raise CodexConfigError("runtime read roots must not expose private run data")
    if any(
        _contains(runtime, protected) or _contains(protected, runtime)
        for runtime in runtimes
        for protected in (source, artifacts, provisioning, receipt)
    ):
        raise CodexConfigError("runtime read roots must be isolated from run data")
    worker_executable = _worker_executable(worker_python, runtimes)

    host = allowed_host.strip().lower().rstrip(".")
    if host != allowed_host or not _DNS_NAME.fullmatch(host):
        raise CodexConfigError("allowed host must be one normalized DNS name")
    if not isinstance(codex_model, str) or not _MODEL_NAME.fullmatch(codex_model):
        raise CodexConfigError("Codex model must be one normalized model ID")
    worker_paths: dict[str, tuple[Path, Path, Path]] = {}
    manifest_paths: dict[str, Path] = {}
    profile_configs: dict[Path, str] = {}
    for profile_id, agent_type in PROFILE_AGENT_TYPES:
        manifest = _future_file(
            manifests / f"{profile_id}.json",
            f"future {profile_id} manifest",
            private_parent=True,
        )
        profile_root = _private_directory(
            workers / agent_type, f"{profile_id} worker directory"
        )
        home = _private_directory(profile_root / "home", f"{profile_id} home")
        temporary = _private_directory(profile_root / "tmp", f"{profile_id} temp")
        work = _private_directory(profile_root / "work", f"{profile_id} work")
        if any(
            _contains(left, right) or _contains(right, left)
            for index, left in enumerate((home, temporary, work))
            for right in (home, temporary, work)[index + 1 :]
        ):
            raise CodexConfigError(f"{profile_id} worker roots must be disjoint")
        worker_paths[agent_type] = (home, temporary, work)
        manifest_paths[profile_id] = manifest
        profile_path = codex_home / f"{agent_type}.config.toml"
        profile_configs[profile_path] = _profile_config(
            profile_id=profile_id,
            agent_type=agent_type,
            manifest=manifest,
            home=home,
            temporary=temporary,
            work=work,
            artifact_root=artifacts,
            codex_model=codex_model,
            worker_python=worker_executable,
            qualification_mode=qualification_mode,
        )

    if judge_paths is not None:
        _, judge_home, judge_tmp, judge_work, _ = judge_paths
        judge_profile_path = (
            codex_home / f"{PERSONA_MEMORY_JUDGE_PROFILE_NAME}.config.toml"
        )
        profile_configs[judge_profile_path] = _persona_memory_judge_config(
            home=judge_home,
            temporary=judge_tmp,
            work=judge_work,
            codex_model=codex_model,
        )

    lines = _base_settings(SUPERVISOR_PERMISSION_PROFILE, codex_model)
    lines.extend(
        _shell_policy(
            root_home,
            root_tmp,
            work=root_work,
            manifest=None,
            profile_id=None,
            agent_type=None,
            artifact_root=None,
            aggregation_input_root=aggregation_inputs,
            orchestration_receipt=receipt,
            worker_python=None,
            qualification_mode=None,
        )
    )
    lines.extend(_feature_lines())
    lines.extend(
        _permission_header(
            SUPERVISOR_PERMISSION_PROFILE,
            "Feedling aggregation supervisor without provisioning credentials",
        )
    )
    supervisor_filesystem = (
        (":minimal", "read"),
        (str(source), "read"),
        (str(artifacts), "deny"),
        (str(provisioning), "deny"),
        (str(manifests), "deny"),
        (str(workers), "deny"),
        (str(worker_outputs), "deny"),
        (str(aggregation_inputs), "read"),
        (str(receipt), "read"),
        (str(root_home), "write"),
        (str(root_tmp), "write"),
        (str(root_work), "write"),
        *((str(runtime), "read") for runtime in runtimes),
    )
    lines.extend(
        f"{_quoted(path)} = {_quoted(access)}" for path, access in supervisor_filesystem
    )
    # The aggregator consumes untrusted model/application evidence but never
    # performs live probes.  Keep its tool network disabled so prompt-injected
    # evidence cannot turn it into a second deployment client or exfiltration
    # path.  Authenticated Codex model transport remains available outside the
    # tool sandbox, as it does for the offline persona judge.
    lines.extend(_disabled_network_lines(SUPERVISOR_PERMISSION_PROFILE))

    for profile_id, agent_type in PROFILE_AGENT_TYPES:
        permission = worker_permission_profile(profile_id)
        own_home, own_tmp, own_work = worker_paths[agent_type]
        lines.extend(
            _permission_header(permission, f"Isolated Feedling worker for {profile_id}")
        )
        filesystem: list[tuple[str, str]] = [
            (":minimal", "read"),
            (str(source), "read"),
            (str(artifacts), "deny"),
            (str(provisioning), "deny"),
            (str(memory_manifest), "deny"),
            (str(manifest_paths[profile_id]), "read"),
            (str(worker_outputs), "deny"),
            (str(aggregation_inputs), "deny"),
            (str(receipt), "deny"),
            (str(own_home), "write"),
            (str(own_tmp), "write"),
            (str(own_work), "write"),
        ]
        filesystem.extend(
            (str(manifest), "deny")
            for other_profile, manifest in manifest_paths.items()
            if other_profile != profile_id
        )
        filesystem.extend(
            (str(workers / other_agent), "deny")
            for _, other_agent in PROFILE_AGENT_TYPES
            if other_agent != agent_type
        )
        filesystem.extend((str(runtime), "read") for runtime in runtimes)
        lines.extend(
            f"{_quoted(path)} = {_quoted(access)}" for path, access in filesystem
        )
        # All product network actions are executed by deterministic parent
        # probes.  Profile workers only request fixed operations and judge the
        # returned facts, so prompt-injected content cannot register accounts,
        # mutate arbitrary users, or escape signed-run cleanup.
        lines.extend(_disabled_network_lines(permission))

    if judge_paths is not None:
        judge_root, judge_home, judge_tmp, judge_work, judge_scratch = judge_paths
        lines.extend(
            _permission_header(
                PERSONA_MEMORY_JUDGE_PERMISSION_PROFILE,
                "Offline semantic judge for blinded persona-memory trajectories",
            )
        )
        judge_filesystem: list[tuple[str, str]] = [
            (":minimal", "read"),
            (str(source), "deny"),
            (str(artifacts), "deny"),
            (str(provisioning), "deny"),
            (str(manifests), "deny"),
            (str(workers), "deny"),
            (str(worker_outputs), "deny"),
            (str(aggregation_inputs), "deny"),
            (str(receipt), "deny"),
            (str(root_home), "deny"),
            (str(root_tmp), "deny"),
            (str(root_work), "deny"),
            (str(codex_home), "deny"),
            (str(judge_scratch), "deny"),
            *((str(runtime), "deny") for runtime in runtimes),
            (str(judge_root), "write"),
            (str(judge_home), "write"),
            (str(judge_tmp), "write"),
            (str(judge_work), "write"),
        ]
        lines.extend(
            f"{_quoted(path)} = {_quoted(access)}"
            for path, access in judge_filesystem
        )
        lines.extend(_disabled_network_lines(PERSONA_MEMORY_JUDGE_PERMISSION_PROFILE))

    return CodexConfigBundle(main="\n".join(lines), profiles=profile_configs)


def write_config(output: Path, content: str) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(output, flags, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        raise CodexConfigError("unable to create run-scoped Codex config") from None
    try:
        metadata = output.lstat()
    except OSError:
        raise CodexConfigError("run-scoped Codex config is unavailable") from None
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise CodexConfigError("run-scoped Codex config permissions are unsafe")


def write_bundle(output: Path, bundle: CodexConfigBundle) -> None:
    created: list[Path] = []
    try:
        for profile_path, content in bundle.profiles.items():
            write_config(profile_path, content)
            created.append(profile_path)
        write_config(output, bundle.main)
        created.append(output)
    except Exception:
        for path in created:
            try:
                path.unlink()
            except OSError:
                pass
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Write isolated independent-process Codex E2E permissions"
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--full-manifest", type=Path, required=True)
    parser.add_argument("--profile-manifest-dir", type=Path, required=True)
    parser.add_argument("--supervisor-home", type=Path, required=True)
    parser.add_argument("--supervisor-tmp", type=Path, required=True)
    parser.add_argument("--supervisor-work", type=Path, required=True)
    parser.add_argument("--worker-root", type=Path, required=True)
    parser.add_argument("--worker-output-root", type=Path, required=True)
    parser.add_argument("--aggregation-input-root", type=Path, required=True)
    parser.add_argument("--orchestration-receipt", type=Path, required=True)
    parser.add_argument(
        "--persona-judge-root",
        type=Path,
        help=(
            "Clean private root containing home/tmp/work for the optional "
            "persona-memory Codex judge profile"
        ),
    )
    parser.add_argument(
        "--persona-judge-scratch-root",
        type=Path,
        help=(
            "Clean private Codex judge invocation root; explicitly denied to "
            "judge tools while the Codex host writes structured output"
        ),
    )
    parser.add_argument("--codex-model", required=True)
    parser.add_argument("--allowed-host", required=True)
    parser.add_argument("--worker-python", type=Path, required=True)
    parser.add_argument(
        "--qualification-mode",
        choices=("release", "diagnostic"),
        default="release",
    )
    parser.add_argument(
        "--runtime-read-root",
        type=Path,
        action="append",
        default=[],
        help="Additional trusted interpreter/dependency root (repeatable)",
    )
    parser.add_argument(
        "--allow-local-binding",
        action="store_true",
        help=(
            "Allow the exact DNS host to resolve to a local/non-public address. "
            "Intended only for explicitly diagnosed local fake-IP proxies; CI "
            "and release qualification must leave this disabled."
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        bundle = build_config_bundle(
            output=args.output,
            source_root=args.source_root,
            artifact_root=args.artifact_root,
            full_manifest=args.full_manifest,
            profile_manifest_dir=args.profile_manifest_dir,
            supervisor_home=args.supervisor_home,
            supervisor_tmp=args.supervisor_tmp,
            supervisor_work=args.supervisor_work,
            worker_root=args.worker_root,
            worker_output_root=args.worker_output_root,
            aggregation_input_root=args.aggregation_input_root,
            orchestration_receipt=args.orchestration_receipt,
            codex_model=args.codex_model,
            allowed_host=args.allowed_host,
            worker_python=args.worker_python,
            qualification_mode=args.qualification_mode,
            runtime_read_roots=args.runtime_read_root,
            allow_local_binding=args.allow_local_binding,
            persona_judge_root=args.persona_judge_root,
            persona_judge_scratch_root=args.persona_judge_scratch_root,
        )
        write_bundle(args.output, bundle)
    except CodexConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except Exception:
        print(
            "ERROR: Codex permission setup encountered an internal error",
            file=sys.stderr,
        )
        return 1
    print("isolated top-level Codex qualification profiles installed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
