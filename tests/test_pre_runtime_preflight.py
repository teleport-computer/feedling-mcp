from __future__ import annotations

import ast
import importlib
import itertools
import re
from pathlib import Path

import yaml
from alembic.config import Config
from alembic.script import ScriptDirectory

ROOT = Path(__file__).parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
TEE_MIGRATE_WORKFLOW = ROOT / ".github" / "workflows" / "tee-migrate.yml"
PG_DEPLOY_WORKFLOW = ROOT / ".github" / "workflows" / "pg-deploy.yml"
REDIS_DEPLOY_WORKFLOW = ROOT / ".github" / "workflows" / "redis-deploy.yml"
TEST_COMPOSE = ROOT / "deploy" / "docker-compose.phala.test.yaml"
TEST_RUNNER_COMPOSE = ROOT / "deploy" / "docker-compose.phala.runner.yaml"
PROD_COMPOSE = ROOT / "deploy" / "docker-compose.phala.yaml"
PROD_RUNNER_COMPOSE = ROOT / "deploy" / "docker-compose.phala.prod.runner.yaml"
EXPECTED_TEE_HEAD = "0043_divergence_skew"

# The two inventory files that name the shared test CVMs.  Every job that
# reaches one of those machines has to learn its target from here.
TEST_CVM_INVENTORY = ("deploy/test-cvm-id.txt", "deploy/test-runner-cvm-id.txt")
# Any per-environment CVM inventory file, e.g. deploy/pre-runner-cvm-id.txt or
# deploy/prod-runner-cvm-ids.txt.
CVM_INVENTORY = re.compile(r"deploy/[a-z-]*cvm-ids?\.txt")
# ``phala@`` is the npm install pin, not an invocation, so require CLI-style
# whitespace.  A workflow can also reach the CLI indirectly through a repo
# wrapper script, which is why the reaching set below is a closure and not
# this pattern alone.
PHALA_CLI = re.compile(r"(?<![\w@])phala\s")
# Argument positions that receive a CVM target.  ``phala logs`` is excluded on
# purpose: its positional argument is a container, and it names the CVM through
# the ``--cvm-id`` flag that the first pattern already covers.
CVM_TARGET_POSITIONS = (
    re.compile(r"--cvm-id\s+(\S+)"),
    re.compile(r"(?<![\w@])phala\s+(?:cvms\s+\w+|ps|ssh)\s+(\S+)"),
    re.compile(r"wait-cvm-ready\.sh\s+(\S+)"),
)
# The only transformation treated as value preserving.  Anything else — case
# conversion, defaulting, substring expansion — has to unroot the name.
WHITESPACE_STRIP = r"tr -d '\[:space:\]'"
# Statements that leave a branch without reaching the join below it.
BRANCH_TERMINATORS = ("exit", "continue", "break", "return")
# Constructs that can write a variable without ever naming it: ``eval`` and
# indirect assignment, sourcing another file, or a function whose body assigns
# the target and whose call site mentions nothing.  Tracking references cannot
# see through these, so a step containing one forfeits provenance entirely
# rather than reporting a root it cannot stand behind.
# Matched at shell token boundaries rather than line starts: ``true; f() { … }``
# and ``x && . ./s.sh`` put these mid-line, so anchoring on ``^`` would let the
# same construct through by prefixing it with anything at all.
UNSUPPORTED_WRITE = re.compile(
    r"(?<![\w-])(?:eval|source)\s"
    r"|(?:^|[;&|(]|\s)\.\s+\S"
    r"|(?<![\w.-])\w+\s*\(\s*\)\s*\{"
    r"|(?<![\w-])function\s+\w+",
    re.MULTILINE,
)
QUOTED_VARIABLE = re.compile(r'^"\$\{?(\w+)\}?"$')
VARIABLE_REFERENCE = re.compile(r"\$\{?(\w+)\}?")
ASSIGNMENT = re.compile(r"^\s*(?:export\s+)?(\w+)=(.+)$")
FOR_LOOP = re.compile(r"for\s+(\w+)\s+in\s+([^;]+);")
WHILE_READ = re.compile(r"while\s+(?:IFS=\S*\s+)?read\s+-r\s+(\w+)")
GITHUB_OUTPUT_SINK = re.compile(r'>>\s*"?\$\{?GITHUB_OUTPUT')
STEP_OUTPUT_REFERENCE = re.compile(
    r"^\$\{\{\s*steps\.([\w-]+)\.outputs\.([\w-]+)\s*\}\}$"
)
POSITIONAL_PARAMETER = re.compile(r'^\s*(\w+)="?\$\{?\d', re.MULTILINE)
TEST_REF_ATOM = "github.ref == 'refs/heads/test'"


def _pipeline_source(lines: list[str], index: int, depth: int = 12) -> str:
    """The command text feeding the ``while read`` on ``lines[index]``.

    Only the producer actually piped into the loop counts: the text left of
    ``while``, or — for ``} | while read`` — the brace group above it.  A
    backwards window would sweep in unrelated neighbouring commands instead.
    An unmatched group inside ``depth`` yields "", which fails closed.
    """
    head = lines[index].split("while", 1)[0].strip()
    if not head.startswith("}"):
        return head.rstrip("|").strip()
    for cursor in range(index - 1, max(-1, index - depth) - 1, -1):
        if lines[cursor].strip() == "{":
            return "\n".join(lines[cursor + 1 : index])
    return ""


def _source_is_inventory(token: str, files: set[str]) -> bool:
    """Whether a redirect source names an inventory file, directly or via loop."""
    text = token.strip().strip('"')
    if CVM_INVENTORY.fullmatch(text):
        return True
    reference = re.fullmatch(r"\$\{?(\w+)\}?", text)
    return bool(reference and reference.group(1) in files)


def _value_is_inventory_derived(
    value: str, rooted: set[str], files: set[str]
) -> bool:
    """Whether this right-hand side *preserves* an inventory-derived value.

    Decided by the whole expression, never by whether it happens to mention a
    rooted name: ``"${CVM_ID:+literal}"`` references a rooted variable and
    evaluates to a constant.  Only the value-preserving forms below root a
    name; every other transformation unroots it, so an unrecognised shape
    surfaces as a red assertion instead of inheriting provenance it never had.
    """
    text = value.strip()
    if text.startswith('"') and text.endswith('"'):
        text = text[1:-1]

    # Exact copy of an already-rooted variable.
    copied = re.fullmatch(r"\$\{?(\w+)\}?", text)
    if copied:
        return copied.group(1) in rooted

    substitution = re.fullmatch(r"\$\((.*)\)", text, re.S)
    if not substitution:
        return False
    inner = substitution.group(1).strip()

    # Read straight out of an inventory file: $(tr -d '[:space:]' < deploy/…)
    read = re.fullmatch(rf"{WHITESPACE_STRIP}\s*<\s*(\S+)", inner)
    if read:
        return _source_is_inventory(read.group(1), files)

    # Whitespace-only sanitize of a rooted value, and nothing else.
    sanitized = re.fullmatch(
        rf"""printf\s+'%s'\s+"\$\{{?(\w+)\}}?"\s*\|\s*{WHITESPACE_STRIP}""", inner
    )
    if sanitized:
        return sanitized.group(1) in rooted

    # Helper script whose sole argument is an inventory file.
    helper = re.fullmatch(r"deploy/[\w.-]+\.sh\s+(.+)", inner)
    return bool(helper and CVM_INVENTORY.fullmatch(helper.group(1).strip()))


def _possibly_written(line: str, names: set[str]) -> set[str]:
    """Names this line might write through any shape not proved value preserving.

    Provenance is killed by default rather than kept: recognising *writes* means
    every unmodelled one (``read -r X <<<``, ``printf -v X``, ``echo a; X=b``, a
    one-line ``if … then X=… fi``) silently preserves a root the runtime has
    already overwritten.  So references are masked out and any surviving bare
    mention of the name is treated as a possible write.  ``export X`` on its own
    re-exports an existing value and is the one exception.
    """
    if re.fullmatch(r"export\s+\w+", line.strip()):
        return set()
    masked = re.sub(r"\$\{?\w+\}?", "", line)
    return {name for name in names if re.search(rf"\b{re.escape(name)}\b", masked)}


def _producer_emits_inventory(command: str, rooted: set[str]) -> bool:
    """Whether one command in a pipeline emits inventory *content*.

    Matched as a whole reader form, because a substring search cannot tell
    content from an argument: ``printf 'hardcoded\\ndeploy/prod-cvm-id.txt'``
    merely names the path while emitting a literal id.
    """
    for reader in (
        r"cat\s+(\S+)",
        r"grep\s+-vE\s+'[^']*'\s+(\S+)",
    ):
        match = re.fullmatch(reader, command)
        if match:
            return bool(CVM_INVENTORY.fullmatch(match.group(1).strip('"')))
    emitted = re.fullmatch(r"""printf\s+'%s\\n'\s+"\$\{?(\w+)\}?\"""", command)
    return bool(emitted and emitted.group(1) in rooted)


def _stream_is_inventory_derived(source: str, rooted: set[str]) -> bool:
    """Whether *every* command feeding a ``while read`` emits inventory content."""
    commands = [line.strip().rstrip("|").strip() for line in source.splitlines()]
    commands = [command for command in commands if command and command != "{"]
    return bool(commands) and all(
        _producer_emits_inventory(command, rooted) for command in commands
    )


def _output_assignments(line: str) -> list[tuple[str, str]]:
    """``(key, value)`` pairs a ``$GITHUB_OUTPUT`` line publishes.

    Each key is tracked against its own right-hand side.  Judging the line as a
    whole lets one rooted value vouch for a literal written beside it.
    """
    pairs = []
    statements = [
        statement.strip().strip("{}").strip()
        for statement in re.split(r";|&&", line.split(">>", 1)[0])
    ]
    statements = [statement for statement in statements if statement]
    for position, statement in enumerate(statements):
        echoed = re.fullmatch(r"""(?:echo|printf)\s+["'](.+)["']""", statement)
        if not echoed:
            continue
        text = echoed.group(1)
        heredoc = re.fullmatch(r"(\w+)<<(\w+)", text)
        if heredoc:
            key, marker = heredoc.groups()
            end = next(
                (
                    cursor
                    for cursor in range(position + 1, len(statements))
                    if re.fullmatch(
                        rf"""(?:echo|printf)\s+["']{marker}["']""", statements[cursor]
                    )
                ),
                None,
            )
            # An unterminated heredoc leaves the body unknown, so publish None
            # and let the caller fail closed.
            body = statements[position + 1 : end] if end is not None else None
            pairs.append((key, None, body))
            continue
        for key, value in re.findall(r"(\w+)=(\S*)", text):
            pairs.append((key, value, None))
    return pairs


def _step_provenance(run: str, seeded: set[str]) -> list[set[str]]:
    """Rooted variables *after each line* of one step, in source order.

    Every binding both kills and generates: a name loses whatever provenance it
    had and only regains it when the new right-hand side is itself value
    preserving.  Order matters, because a rooted read followed by a literal
    reassignment leaves a hardcoded target behind, and a set that only ever
    grows cannot see that.  Sinks are therefore judged against the snapshot at
    their own line rather than against an end-of-step summary.
    """
    lines = run.splitlines()
    if UNSUPPORTED_WRITE.search(run):
        # An indirect write can retarget the CVM without the sink line changing
        # at all, so nothing in this step is trustworthy.
        return [set() for _ in lines]

    rooted = set(seeded)
    files: set[str] = set()
    snapshots: list[set[str]] = []
    # Each frame remembers the state entering a branching construct and the
    # states leaving every branch of it, so the join can intersect them.
    frames: list[dict] = []
    previous = ""

    def close_branch(frame):
        # A branch that exits, continues or breaks never reaches the join, so
        # it must not contribute its state to the intersection.
        if previous.split()[:1] and previous.split()[0] in BRANCH_TERMINATORS:
            return
        frame["exits"].append((set(rooted), set(files)))

    for index, line in enumerate(lines):
        stripped = line.strip()
        head = stripped.split()[0] if stripped.split() else ""

        if head in ("if", "case") and not stripped.endswith(("fi", "esac")):
            frames.append(
                {"entry": (set(rooted), set(files)), "exits": [], "default": False}
            )
        elif head in ("elif", "else") or stripped == ";;":
            if frames:
                close_branch(frames[-1])
                rooted, files = (set(frames[-1]["entry"][0]),
                                 set(frames[-1]["entry"][1]))
                if head == "else":
                    frames[-1]["default"] = True
        elif stripped.startswith("*)"):
            if frames:
                frames[-1]["default"] = True
        elif head in ("fi", "esac") or head == "done":
            if frames:
                frame = frames.pop()
                close_branch(frame)
                # Without an else/default arm, or on a loop that may run zero
                # times, falling straight through is itself a path.
                if not frame["default"]:
                    frame["exits"].append(frame["entry"])
                if frame["exits"]:
                    rooted = set.intersection(*[s[0] for s in frame["exits"]])
                    files = set.intersection(*[s[1] for s in frame["exits"]])
                else:
                    rooted, files = set(), set()
        elif head in ("for", "while") or stripped.endswith("do"):
            frames.append(
                {"entry": (set(rooted), set(files)), "exits": [], "default": False}
            )

        if stripped:
            previous = stripped

        # Names bound by a modelled form on this line; everything else that the
        # line might write is killed below.
        modelled: set[str] = set()

        loop = FOR_LOOP.search(line)
        if loop:
            name, items = loop.groups()
            words = items.split()
            modelled.add(name)
            rooted.discard(name)
            files.discard(name)
            # ``for file in deploy/test-cvm-id.txt …`` binds a path, not an id,
            # so it roots whatever is later read *through* it.
            if words and all(CVM_INVENTORY.fullmatch(word) for word in words):
                files.add(name)

        binding = WHILE_READ.search(line)
        if binding:
            name = binding.group(1)
            source = _pipeline_source(lines, index)
            modelled.add(name)
            rooted.discard(name)
            files.discard(name)
            if _stream_is_inventory_derived(source, rooted):
                rooted.add(name)

        assignment = ASSIGNMENT.match(line)
        if assignment and not loop and not binding:
            name, value = assignment.groups()
            # Evaluate the right-hand side before the kill: ``CVM_ID="$(printf
            # '%s' "$CVM_ID" | tr -d '[:space:]')"`` sanitizes its own value.
            regenerated = _value_is_inventory_derived(value, rooted, files)
            modelled.add(name)
            rooted.discard(name)
            files.discard(name)
            if regenerated:
                rooted.add(name)

        # Default-kill: any rooted name this line could write through a shape
        # not modelled above loses its provenance.  Recognising only the write
        # forms we thought of is what let read/printf -v/one-line-if through.
        for name in _possibly_written(line, (rooted | files) - modelled):
            rooted.discard(name)
            files.discard(name)

        snapshots.append(set(rooted))
    return snapshots


def _rooted_step_outputs(job: dict) -> set[tuple[str, str]]:
    """``steps.<id>.outputs.<name>`` pairs carrying an inventory-derived value.

    Judged at the moment of the write, so a value clobbered before it is
    published does not export provenance it no longer has.
    """
    rooted = set()
    for step in (job.get("steps") or []):
        if not isinstance(step, dict) or not step.get("id"):
            continue
        run = str(step.get("run") or "")
        snapshots = _step_provenance(run, set())
        lines = run.splitlines()
        for index, line in enumerate(lines):
            if not GITHUB_OUTPUT_SINK.search(line):
                continue
            for key, value, body in _output_assignments(line):
                if value is None:
                    # ``{ echo 'ids<<EOF'; printf '%s\n' "$IDS"; echo 'EOF'; }``
                    # publishes whatever the heredoc body emits -- every
                    # statement of it, not merely one recognisable one.
                    if body and all(
                        _producer_emits_inventory(statement, snapshots[index])
                        for statement in body
                    ):
                        rooted.add((step["id"], key))
                    continue
                copied = re.fullmatch(r"\$\{?(\w+)\}?", value)
                if copied and copied.group(1) in snapshots[index]:
                    rooted.add((step["id"], key))
    return rooted


def _environment_target_variables(wrappers: set[str]) -> set[str]:
    """Env var names a wrapper uses as its CVM target (argv targets excluded)."""
    names = set()
    for wrapper in wrappers:
        body = (ROOT / "deploy" / wrapper).read_text()
        for target in re.findall(
            r'(?<![\w@])phala\s+(?:cvms\s+\w+|ps|ssh)\s+"\$\{?(\w+)\}?"', body
        ):
            if not any(
                m.group(1) == target for m in POSITIONAL_PARAMETER.finditer(body)
            ):
                names.add(target)
    return names


def _run_steps(job: dict) -> list[str]:
    return [
        str(step.get("run") or "")
        for step in (job.get("steps") or [])
        if isinstance(step, dict)
    ]


def _run_text(job: dict) -> str:
    return "\n".join(_run_steps(job))


def _phala_reaching_scripts() -> set[str]:
    """Repo scripts that reach the Phala CLI, directly or through each other."""
    texts = {path.name: path.read_text() for path in (ROOT / "deploy").glob("*.sh")}
    reaching = {name for name, body in texts.items() if PHALA_CLI.search(body)}
    while True:
        grown = {
            name
            for name, body in texts.items()
            if name not in reaching and any(other in body for other in reaching)
        }
        if not grown:
            return reaching
        reaching |= grown


def _cvm_reaching_jobs(jobs: dict, wrappers: set[str]) -> set[str]:
    """Jobs that can reach a CVM, whether they spell ``phala`` or call a wrapper."""
    return {
        name
        for name, job in jobs.items()
        if PHALA_CLI.search(_run_text(job))
        or any(wrapper in _run_text(job) for wrapper in wrappers)
    }


def _condition_requires(condition: str, atom: str) -> bool:
    """True when ``condition`` is unsatisfiable while ``atom`` is false.

    A substring search cannot answer this: ``A || B`` mentions ``A`` while
    letting ``B`` alone enable the job.  Evaluate the boolean structure over
    every assignment of the other atoms instead.
    """
    normalised = " ".join(condition.split())
    # Protect ``!=`` so the operator split below does not tear it apart.
    sentinel = "\x00NE\x00"
    normalised = normalised.replace("!=", sentinel)

    names: dict[str, str] = {}
    expression = []
    for piece in re.split(r"(&&|\|\||!|\(|\))", normalised):
        token = piece.strip()
        if not token:
            continue
        if token == "&&":
            expression.append(" and ")
        elif token == "||":
            expression.append(" or ")
        elif token == "!":
            expression.append(" not ")
        elif token in "()":
            expression.append(token)
        else:
            text = token.replace(sentinel, "!=")
            expression.append(f" {names.setdefault(text, f'a{len(names)}')} ")

    if atom not in names:
        return False
    # ``compile`` rejects a leading space as an indent, so join then strip.
    code = compile("".join(expression).strip(), "<condition>", "eval")
    others = [name for text, name in names.items() if text != atom]
    for bits in itertools.product((False, True), repeat=len(others)):
        environment = dict(zip(others, bits))
        environment[names[atom]] = False
        if eval(code, {"__builtins__": {}}, environment):  # noqa: S307
            return False
    return True


def _head_literal_lines(source: str) -> list[int]:
    return [
        node.lineno
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Constant) and node.value == EXPECTED_TEE_HEAD
    ]


def test_runtime_tee_head_consumers_have_no_literal_pin():
    # Self-prove the AST guard before trusting its production scan. It remains
    # insensitive to formatting and line wrapping because it inspects syntax,
    # not source layout.
    fixture = f"value = {EXPECTED_TEE_HEAD!r}\n"
    assert _head_literal_lines(fixture) == [1]

    for relative in (
        "backend/alembic_tee/__init__.py",
        "backend/admin/plaintext_shadow.py",
        "backend/admin/phase4_cutover.py",
        "backend/db.py",
        "tests/test_plaintext_shadow_schema.py",
    ):
        assert _head_literal_lines((ROOT / relative).read_text()) == [], relative


def test_tee_migrate_has_one_head_after_runtime_v2_alignment():
    cfg = Config(str(ROOT / "backend" / "alembic_tee" / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "backend" / "alembic_tee"))
    script = ScriptDirectory.from_config(cfg)

    assert script.get_heads() == [EXPECTED_TEE_HEAD]
    runtime_head = importlib.import_module("alembic_tee").current_head()
    assert runtime_head == EXPECTED_TEE_HEAD
    assert (
        script.get_revision(EXPECTED_TEE_HEAD).down_revision
        == "0042_perceptkit_retraction"
    )
    assert (
        script.get_revision("0042_perceptkit_retraction").down_revision
        == "0041_perceptkit_mirror_source"
    )
    assert (
        script.get_revision("0041_perceptkit_mirror_source").down_revision
        == "0040_perceptkit_objects"
    )
    assert (
        script.get_revision("0040_perceptkit_objects").down_revision
        == "0039_distill_artifact_ledger"
    )
    assert (
        script.get_revision("0039_distill_artifact_ledger").down_revision
        == "0038_v2_wake_followup_marker"
    )
    assert (
        script.get_revision("0036_lane_rollup_access_paths").down_revision
        == "0035_contract_rejection_stats"
    )
    assert (
        script.get_revision("0035_contract_rejection_stats").down_revision
        == "0034_v1_lane_outcome_counts"
    )
    assert (
        script.get_revision("0033_trace_events").down_revision
        == "0032_v2_job_recovery_events"
    )
    assert (
        script.get_revision("0032_v2_job_recovery_events").down_revision
        == "0031_merge_voice_primary"
    )
    assert set(
        script.get_revision("0031_merge_voice_primary").down_revision
    ) == {
        "0029_plaintext_shadow_merge",
        "0030_voice_call_sessions_primary",
    }
    assert set(
        script.get_revision("0029_plaintext_shadow_merge").down_revision
    ) == {
        "0028_trace_write_stats_health",
        "0027_plaintext_shadow_gates",
    }
    assert (
        script.get_revision("0027_plaintext_shadow_gates").down_revision
        == "0026_plaintext_shadow_control"
    )
    assert (
        script.get_revision("0026_plaintext_shadow_control").down_revision
        == "0025_lane_rollup_voice"
    )
    assert (
        script.get_revision("0028_trace_write_stats_health").down_revision
        == "0027_trace_write_stats"
    )
    assert (
        script.get_revision("0027_trace_write_stats").down_revision
        == "0026_chat_daily_rollup"
    )
    assert (
        script.get_revision("0026_chat_daily_rollup").down_revision
        == "0025_lane_rollup_voice"
    )
    assert (
        script.get_revision("0030_voice_call_sessions_primary").down_revision
        == "0025_lane_rollup_voice"
    )
    assert (
        script.get_revision("0025_lane_rollup_voice").down_revision
        == "0024_lane_rollup_safe_ts"
    )
    assert (
        script.get_revision("0024_lane_rollup_safe_ts").down_revision
        == "0023_lane_daily_rollup"
    )
    assert (
        script.get_revision("0023_lane_daily_rollup").down_revision
        == "0022_v2_wake_outcomes"
    )
    assert (
        script.get_revision("0022_v2_wake_outcomes").down_revision
        == "0021_agent_jobs_available_at"
    )
    assert (
        script.get_revision("0021_agent_jobs_available_at").down_revision
        == "0020_v2_first_chat_activation"
    )
    assert (
        script.get_revision("0020_v2_first_chat_activation").down_revision
        == "0019_v2_worker_pool_heartbeats"
    )
    assert (
        script.get_revision("0019_v2_worker_pool_heartbeats").down_revision
        == "0018_v2_wake_shadow_decisions"
    )
    assert (
        script.get_revision("0018_v2_wake_shadow_decisions").down_revision
        == "0017_voice_primary_alignment"
    )
    # The prepared-head pin must name whichever revision is CURRENTLY head —
    # a cutover that replays a stale pin re-arms the old head and the preflight
    # then waves through a database that is one migration behind. Derive it
    # from get_heads() so adding a revision without advancing its pin fails
    # here instead of at cutover time.
    (head,) = script.get_heads()
    migration = script.get_revision(head).module
    assert f"'[\"{head}\"]'::jsonb" in migration._UPDATE_PREPARED_HEAD


def _job(source: str, name: str, next_name: str) -> str:
    return source.split(f"\n  {name}:\n", 1)[1].split(f"\n  {next_name}:\n", 1)[0]


def test_pre_main_deploy_waits_for_complete_runtime_preflight():
    source = WORKFLOW.read_text()
    deploy = _job(source, "deploy-pre-cvm", "deploy-pre-runner-cvm")
    header = "\n".join(deploy.splitlines()[:8])
    assert "validate-pre-runtime-prerequisites" in header


def test_deployment_branch_pushes_cannot_cancel_a_partial_release_unit():
    source = WORKFLOW.read_text()
    concurrency = source.split("\njobs:\n", 1)[0]
    assert "github.event_name == 'pull_request'" in concurrency
    assert "cancel-in-progress: true" not in concurrency


def test_release_preflights_reject_unnormalized_sandbox_provider_values():
    source = WORKFLOW.read_text()
    assert source.count("sandbox provider must be exactly disabled or e2b") == 3


def test_preflight_validates_entire_two_cvm_release_before_mutation():
    source = WORKFLOW.read_text()
    preflight = _job(
        source,
        "validate-pre-runtime-prerequisites",
        "deploy-cvm",
    )
    for required in (
        "deploy/pre-cvm-id.txt",
        "deploy/pre-runner-cvm-id.txt",
        'if [ "$MAIN_CVM_ID" = "$RUNNER_CVM_ID" ]',
        "PRE_DATABASE_URL",
        "TEST_FEEDLING_RUNTIME_TOKEN_SECRET",
        "PRE_MAIN_API_URL",
        "PRE_MAIN_ENCLAVE_URL",
        "PRE_E2B_API_KEY",
        "PRE_FEEDLING_V2_E2B_TEMPLATE",
        'phala cvms get "$CVM_ID"',
        "Build and verify the content-addressed E2B template",
        "deploy/e2b/runtime-v2/template-tag.txt",
        "feedling feedling-agent-runner",
        "docker manifest inspect",
        "no pre CVM was changed",
    ):
        assert required in preflight


def test_preflight_validates_tee_startup_migration_authorization_before_mutating_a_cvm():
    source = WORKFLOW.read_text()
    preflight = _job(
        source,
        "validate-pre-runtime-prerequisites",
        "deploy-cvm",
    )

    for required in (
        "PRE_FEEDLING_DATABASE_SCHEMA == 'tee'",
        "PRE_TEE_MIGRATION_DSN",
        "PRE_TEE_PG_CA_PEM",
        "APP_DATABASE_URL",
        "owner_fingerprint != app_fingerprint",
        'owner_user != "feedling_owner"',
        'app_user != "app"',
        "pg_has_role(current_user, 'feedling_owner', 'member')",
        "PRE_DATABASE_URL app role must inherit feedling_owner",
        "No PRE CVM was changed",
    ):
        assert required in preflight

    schema_gate = preflight.index(
        "Validate PRE TEE startup migration authorization before mutating either CVM"
    )
    image_gate = preflight.index(
        "Require both Runtime V2 images before mutating either CVM"
    )
    assert schema_gate < image_gate


def test_pre_release_gates_keep_tee_migration_out_of_ci_preflight():
    preflight = _job(
        WORKFLOW.read_text(),
        "validate-pre-runtime-prerequisites",
        "deploy-cvm",
    )
    tee_migrate = TEE_MIGRATE_WORKFLOW.read_text()

    assert "db.init_schema()" not in preflight
    assert "SELECT version_num FROM alembic_tee_version" not in preflight

    assert 'os.environ["FEEDLING_DATABASE_SCHEMA"] = "tee"' in tee_migrate
    assert 'os.environ["DATABASE_URL"] = os.environ["TEE_MIGRATION_DATABASE_URL"]' in tee_migrate
    assert "db.init_schema()" in tee_migrate

    assert "Assert PRE application startup contract" in tee_migrate


def test_tee_migrate_exposes_backend_package_to_upgrade_step():
    source = TEE_MIGRATE_WORKFLOW.read_text()
    step = source.split(
        "      - name: Run alembic_tee upgrade head\n", 1
    )[1].split("\n      - name:", 1)[0]

    assert "PYTHONPATH: backend" in step


def test_preflight_is_triggered_by_both_cvm_inventory_files():
    source = WORKFLOW.read_text()
    detection = _job(
        source,
        "detect-cvm-changes-pre",
        "validate-pre-runtime-prerequisites",
    )
    assert "deploy/pre-cvm-id.txt" in detection
    assert "deploy/pre-runner-cvm-id.txt" in detection


def test_test_main_deploy_waits_for_the_same_release_unit_preflight():
    source = WORKFLOW.read_text()
    deploy = _job(source, "deploy-test-cvm", "deploy-test-runner-cvm")
    assert "validate-test-runtime-prerequisites" in "\n".join(
        deploy.splitlines()[:8]
    )
    preflight = _job(
        source,
        "validate-test-runtime-prerequisites",
        "validate-prod-runner-topology",
    )
    for required in (
        "deploy/test-cvm-id.txt",
        "deploy/test-runner-cvm-id.txt",
        "feedling feedling-agent-runner",
        "no test CVM was changed",
        'phala cvms get "$CVM_ID"',
        "Build and verify the test E2B template",
    ):
        assert required in preflight


def test_phala_reaching_wrapper_scripts_are_enumerated():
    """``phala`` in ci.yml is not the only way to reach a CVM.

    ``deploy/wait-cvm-ready.sh`` and ``deploy/publish-compose-hash.sh`` shell
    out to the CLI themselves, so a job that only calls a wrapper still touches
    the machine while spelling no ``phala`` at all.  Derive the wrapper set
    from the scripts instead of listing it, and prove here that the derivation
    both finds the real callers and rejects scripts that merely name Phala in a
    hostname or comment.
    """
    reaching = _phala_reaching_scripts()

    assert {"wait-cvm-ready.sh", "publish-compose-hash.sh"} <= reaching, sorted(
        reaching
    )
    # These mention "phala" only inside dstack hostnames and prose.  If they
    # start matching, the pattern has stopped discriminating and every
    # downstream denominator silently inflates.
    assert reaching.isdisjoint(
        {"attestation-gate.sh", "check-prod-runner-topology.sh", "verify-redis.sh"}
    ), sorted(reaching)


def test_every_cvm_reaching_job_takes_its_target_from_the_deploy_inventory():
    """Make the inventory anchor *complete*, not merely wider than the pin.

    ``test_every_test_cvm_touching_job_is_locked_to_the_test_branch`` derives
    its denominator by looking for test inventory files.  That is only sound
    while no job can name a CVM any other way: a job that passed a literal id —
    whether inline, or alongside an unrelated inventory read — would touch the
    shared machine while staying invisible to the scan, which is the same
    "narrower proxy" failure the pin denominator had.

    So trace each target back to its source rather than checking how it looks:
    a name that merely *is* a shell variable proves nothing, because a step can
    assign one a literal and pass that instead.  Every CVM target — positional
    arguments and the environment variable ``publish-compose-hash.sh`` reads
    alike — must be traceable to an inventory read in its own step, whether
    directly, through a loop, or through a step output.
    """
    # Prove the tracer before trusting it on the workflow.  Its worth is not
    # that it roots the real reads, but that it *stops* rooting a name the
    # moment provenance is broken -- including transformations that still
    # mention a rooted variable.
    read = "CVM_ID=\"$(tr -d '[:space:]' < deploy/test-cvm-id.txt)\""
    assert _step_provenance(read, set())[-1] == {"CVM_ID"}
    # A later assignment kills the root; a set that only grows cannot see this.
    assert _step_provenance(f"{read}\nCVM_ID=literal", set())[-1] == set()
    # ...and the snapshot before the clobber still shows it, so sinks are judged
    # at their own line rather than against the end of the step.
    assert _step_provenance(f"{read}\nCVM_ID=literal", set())[0] == {"CVM_ID"}
    # Referencing a rooted name is not inheriting its value.
    assert _step_provenance(f'{read}\nT="${{CVM_ID:+literal}}"', set())[-1] == {
        "CVM_ID"
    }
    # An unknown transformation unroots rather than passing provenance through.
    assert _step_provenance(f'{read}\nT="$(printf %s "$CVM_ID" | rev)"', set())[-1] == {
        "CVM_ID"
    }
    # Value-preserving forms that must keep working: exact copy and a
    # whitespace-only sanitize of an already-rooted value.
    assert _step_provenance(f'{read}\nT="$CVM_ID"', set())[-1] == {"CVM_ID", "T"}
    assert _step_provenance(
        f"{read}\nT=\"$(printf '%s' \"$CVM_ID\" | tr -d '[:space:]')\"", set()
    )[-1] == {"CVM_ID", "T"}
    # A seeded step-env value is killable by the run text just like any other.
    assert _step_provenance("echo hi", {"FEEDLING_CVM_ID"})[-1] == {"FEEDLING_CVM_ID"}
    assert _step_provenance("FEEDLING_CVM_ID=literal", {"FEEDLING_CVM_ID"})[-1] == set()

    # Provenance at a join is a *must* property: one branch reaching the sink
    # with a literal is enough to poison it, however the other branch reads.
    join = (
        'if [ "$USE_ALT" = 1 ]; then\n'
        "CVM_ID=literal\n"
        "else\n"
        f"{read}\n"
        "fi\n"
        "echo done"
    )
    assert _step_provenance(join, set())[-1] == set()
    # Both branches rooted still joins to rooted, so the analysis is not simply
    # unrooting everything it sees a branch around.
    both = f'if [ "$USE_ALT" = 1 ]; then\n{read}\nelse\n{read}\nfi\necho done'
    assert _step_provenance(both, set())[-1] == {"CVM_ID"}
    # A branch that exits never reaches the join, which is why the workflow's
    # own "if empty then exit 1 fi" guards do not unroot their reads.
    guarded = f'{read}\nif [ -z "$CVM_ID" ]; then\necho bad\nexit 1\nfi\necho done'
    assert _step_provenance(guarded, set())[-1] == {"CVM_ID"}

    # Provenance is killed by any write shape not proved value preserving, so
    # the model does not have to enumerate every way shell can assign a name.
    for overwrite in (
        'if [ "$USE_ALT" = 1 ]; then CVM_ID=literal; fi',
        'echo "rotating"; CVM_ID=literal',
        "read -r CVM_ID <<< 'literal'",
        "printf -v CVM_ID '%s' 'literal'",
        "mapfile -t CVM_ID < other.txt",
        "CVM_ID+=suffix",
    ):
        assert _step_provenance(f"{read}\n{overwrite}", set())[-1] == set(), overwrite
    # ...while pure reads and a bare re-export keep it, so the default kill is
    # not simply unrooting everything it cannot parse.
    for preserving in (
        'phala cvms restart "$CVM_ID" --api-token "$KEY"',
        './deploy/wait-cvm-ready.sh "$CVM_ID" 900',
        'echo "restarting ${CVM_ID}"',
        "export CVM_ID",
    ):
        assert _step_provenance(f"{read}\n{preserving}", set())[-1] == {
            "CVM_ID"
        }, preserving

    # Indirect writes retarget the CVM without the sink line changing, so a
    # step containing one forfeits provenance rather than vouching for a value
    # it cannot follow.  Reference tracking is blind to both of these.
    for indirect in (
        "retarget() {\nCVM_ID=literal\n}\n" + read + "\nretarget",
        f"TARGET_NAME=CVM_ID\n{read}\neval \"$TARGET_NAME=literal\"",
        "source ./other.sh",
        ". ./other.sh",
        # Recognised at token boundaries, not line starts: any prefix at all
        # would otherwise smuggle the same construct past a ``^`` anchor.
        "true; retarget() { CVM_ID=literal; }\n" + read + "\nretarget",
        "if true; then retarget() { CVM_ID=literal; }; fi\n" + read + "\nretarget",
        "true && . ./other.sh",
        "function retarget {\nCVM_ID=literal\n}\n" + read + "\nretarget",
    ):
        assert _step_provenance(indirect, set())[-1] == set(), indirect
    # ...while a script invocation is not a dot command, and Python in a
    # heredoc is not a shell function definition.
    for benign in (
        './deploy/wait-cvm-ready.sh "$CVM_ID" 900',
        "python3 - <<'PY'\ndef main():\n    pass\nPY",
    ):
        assert _step_provenance(f"{read}\n{benign}", set())[-1] == {"CVM_ID"}, benign

    # A producer that merely *names* an inventory path does not emit its
    # contents; only whole recognised reader forms count.
    assert _producer_emits_inventory("cat deploy/test-cvm-id.txt", set())
    assert not _producer_emits_inventory(
        "printf '%s\\n' 'literal' 'deploy/test-cvm-id.txt'", set()
    )

    # Each published output key is tracked against its own value, so a rooted
    # one written beside a literal cannot vouch for it.
    mixed = _output_assignments(
        'printf \'decoy=%s\\ncvm_id=literal\\n\' "$CVM_ID" >> "$GITHUB_OUTPUT"'
    )
    assert ("cvm_id", "$CVM_ID", None) not in mixed, mixed
    plain = _output_assignments('echo "cvm_id=$CVM_ID" >> "$GITHUB_OUTPUT"')
    assert plain == [("cvm_id", "$CVM_ID", None)], plain
    # ...and the rooting decision itself is per key, not per line: here both
    # keys parse, so only the one whose own value is rooted may be published.
    # A line-level rule would let ``decoy`` vouch for the literal beside it.
    published = _rooted_step_outputs(
        {
            "steps": [
                {
                    "id": "cvmid",
                    "run": f'{read}\necho "decoy=$CVM_ID cvm_id=literal"'
                    ' >> "$GITHUB_OUTPUT"',
                }
            ]
        }
    )
    assert published == {("cvmid", "decoy")}, published

    jobs = yaml.safe_load(WORKFLOW.read_text())["jobs"]
    reaching = _cvm_reaching_jobs(jobs, _phala_reaching_scripts())

    # Pin every current member: with a ``>=`` floor over a subset, any omitted
    # job could drop out of the derivation one at a time and stay green.
    assert reaching >= {
        "deploy-cvm",
        "deploy-pre-cvm",
        "deploy-pre-runner-cvm",
        "deploy-prod-runner-cvm",
        "deploy-test-cvm",
        "deploy-test-runner-cvm",
        "maintain-test-cvm",
        "prod-plaintext-shadow-gate2",
        "validate-pre-runtime-prerequisites",
        "validate-prod-runner-topology",
        "validate-test-runtime-prerequisites",
    }, sorted(reaching)

    environment_targets = _environment_target_variables(_phala_reaching_scripts())
    assert environment_targets == {"FEEDLING_CVM_ID"}, sorted(environment_targets)

    targets = 0
    for name in sorted(reaching):
        job = jobs[name]
        assert CVM_INVENTORY.search(_run_text(job)), (
            f"{name} can reach a CVM without reading a deploy/*-cvm-id*.txt "
            "inventory file, so its environment cannot be derived"
        )
        outputs = _rooted_step_outputs(job)
        for step in (job.get("steps") or []):
            if not isinstance(step, dict):
                continue
            run = str(step.get("run") or "")
            # Reported separately from the target checks below, because the
            # provenance those rely on is what this forfeits.
            unsupported = UNSUPPORTED_WRITE.search(run)
            assert not unsupported, (
                f"{name} reaches a CVM from a step using {unsupported.group(0)!r}, "
                "which can rewrite the target without naming it; this step "
                "cannot establish where its CVM id came from"
            )
            environment = {
                key: str(value) for key, value in (step.get("env") or {}).items()
            }
            seeded = {
                key
                for key, value in environment.items()
                if (match := STEP_OUTPUT_REFERENCE.match(value.strip()))
                and (match.group(1), match.group(2)) in outputs
            }
            snapshots = _step_provenance(run, seeded)

            for index, line in enumerate(run.splitlines()):
                # Provenance is read at the sink's own line: a target rooted
                # earlier and overwritten since is no longer inventory derived.
                rooted = snapshots[index]
                for position in CVM_TARGET_POSITIONS:
                    for target in position.findall(line):
                        targets += 1
                        variable = QUOTED_VARIABLE.match(target)
                        assert variable and variable.group(1) in rooted, (
                            f"{name} aims a CVM command at {target!r}, which "
                            "this step does not derive from a "
                            "deploy/*-cvm-id*.txt inventory read at that point; "
                            "a target must be traceable to the inventory, not "
                            "merely look like a variable"
                        )
                # publish-compose-hash.sh reads its target out of the
                # environment, so the run text never shows it as an argument.
                if not any(wrapper in line for wrapper in _phala_reaching_scripts()):
                    continue
                for wrapper_target in sorted(environment_targets):
                    if wrapper_target not in environment and wrapper_target not in (
                        set().union(*snapshots) if snapshots else set()
                    ):
                        continue
                    targets += 1
                    assert wrapper_target in rooted, (
                        f"{name} passes {wrapper_target}="
                        f"{environment.get(wrapper_target)!r} to a wrapper that "
                        "runs the Phala CLI, and that value is not traceable to "
                        "an inventory read where the wrapper is invoked"
                    )
    # Target extraction that silently stopped matching would vacuously satisfy
    # the loops above, so hold its yield to the count observed on this workflow.
    assert targets >= 24, targets


def test_every_test_cvm_touching_job_is_locked_to_the_test_branch():
    """The shared test CVMs may only be reached from the auditable test branch.

    The denominator is every job with a step that reads a *test* CVM inventory
    file — not just the jobs that write a release pin.  A job that deployed a
    test CVM while skipping ``pin-runtime-release.sh`` (the semantics #417's
    preview wanted) still has to learn which machine to hit, so it lands in
    this set and has to justify its own branch gate.  Anchoring on the pin
    instead made such a job structurally invisible.

    ``ci.yml`` gates test jobs on ``github.ref``.  The manual PG/Redis workflows
    instead check out the branch selected by their environment input and resolve
    the same environment's infrastructure inventory before every CVM sink;
    both forms are part of this guard's scope.
    """
    # Prove the domination check before trusting it: a mention of the test ref
    # is not the same as a dependence on it.
    assert _condition_requires(TEST_REF_ATOM, TEST_REF_ATOM)
    assert _condition_requires(f"{TEST_REF_ATOM} && github.event_name == 'push'", TEST_REF_ATOM)
    assert not _condition_requires(
        f"{TEST_REF_ATOM} || github.event_name == 'workflow_dispatch'", TEST_REF_ATOM
    )
    assert not _condition_requires(
        f"({TEST_REF_ATOM} || github.event_name == 'workflow_dispatch')"
        " && inputs.operation == 'restart-test-cvm'",
        TEST_REF_ATOM,
    )
    assert not _condition_requires("github.event_name == 'push'", TEST_REF_ATOM)

    jobs = yaml.safe_load(WORKFLOW.read_text())["jobs"]
    touching = {
        name
        for name, job in jobs.items()
        if any(inventory in _run_text(job) for inventory in TEST_CVM_INVENTORY)
    }
    # Adding or removing a test-CVM job should fail this self-erasure floor and
    # require an intentional update, including for maintenance-only access.
    assert touching == {
        "deploy-test-cvm",
        "deploy-test-runner-cvm",
        "maintain-test-cvm",
        "validate-test-runtime-prerequisites",
    }, sorted(touching)

    for name in sorted(touching):
        condition = " ".join(str(jobs[name].get("if") or "").split())
        assert _condition_requires(condition, TEST_REF_ATOM), (
            f"{name} reaches a shared test CVM on runs where github.ref is not "
            f"refs/heads/test: {condition!r}"
        )

    expected_refs = {
        "${{ inputs.environment == 'prod' && 'main' || "
        "inputs.environment == 'pre' && 'pre' || 'test' }}",
        "${{ inputs.environment == 'prod' && 'main' || "
        "(inputs.environment == 'pre' && 'pre' || 'test') }}",
    }
    for path, service in (
        (PG_DEPLOY_WORKFLOW, "pg"),
        (REDIS_DEPLOY_WORKFLOW, "redis"),
    ):
        workflow = yaml.safe_load(path.read_text())
        triggers = workflow[True] if True in workflow else workflow["on"]
        assert set(triggers) == {"workflow_dispatch"}, path.name

        manual_jobs = workflow["jobs"]
        reaching = _cvm_reaching_jobs(manual_jobs, _phala_reaching_scripts())
        assert reaching == {"deploy"}, (path.name, sorted(reaching))
        steps = manual_jobs["deploy"]["steps"]

        checkouts = [
            (index, step)
            for index, step in enumerate(steps)
            if str(step.get("uses") or "").startswith("actions/checkout@")
        ]
        assert len(checkouts) == 1, path.name
        checkout_index, checkout = checkouts[0]
        assert checkout.get("with", {}).get("ref") in expected_refs, path.name

        resolvers = [
            (index, step)
            for index, step in enumerate(steps)
            if step.get("id") == "cvm"
        ]
        assert len(resolvers) == 1, path.name
        resolve_index, resolver = resolvers[0]
        assert checkout_index < resolve_index, path.name
        assert resolver.get("env", {}).get("ENVIRONMENT") == "${{ inputs.environment }}", (
            path.name,
            resolver.get("name"),
        )
        resolve_run = str(resolver.get("run") or "")
        assert (
            f'F="deploy/${{ENVIRONMENT}}-{service}-cvm-id.txt"' in resolve_run
        ), path.name
        cvm_assignments = [
            line.strip()
            for line in resolve_run.splitlines()
            if re.match(r"^\s*CVM_ID=", line)
        ]
        assert cvm_assignments == [
            'CVM_ID=$(grep -v \'^#\' "$F" | tr -d \'[:space:]\' | head -1 || true)'
        ], path.name
        output_line = 'echo "id=$CVM_ID" >> "$GITHUB_OUTPUT"'
        assert resolve_run.index(cvm_assignments[0]) < resolve_run.index(output_line), (
            path.name,
            resolver.get("name"),
        )

        targets = 0
        for index, step in enumerate(steps):
            run = str(step.get("run") or "")
            step_targets = [
                target
                for line in run.splitlines()
                for position in CVM_TARGET_POSITIONS
                for target in position.findall(line)
            ]
            if not step_targets:
                continue
            targets += len(step_targets)
            assert index > resolve_index, path.name
            assert step.get("env", {}).get("CVM_ID") == "${{ steps.cvm.outputs.id }}", (
                path.name,
                step.get("name"),
            )
            assert not re.search(r"^\s*(?:export\s+)?CVM_ID=", run, re.MULTILINE), (
                path.name,
                step.get("name"),
            )
            for target in step_targets:
                variable = QUOTED_VARIABLE.fullmatch(target)
                assert variable and variable.group(1) == "CVM_ID", (
                    path.name,
                    step.get("name"),
                    target,
                )
        assert targets >= 2, (path.name, targets)


def test_test_release_jobs_only_run_for_pushes_to_test():
    """The push-triggered release unit additionally may only move on a push.

    Derive the release unit from its release-pin behavior and dependency graph,
    not from the ``if`` expressions under test or mutable labels.  Otherwise
    changing one job to a dispatch-only condition could make it disappear from
    the assertion and turn the guard green by deleting its own offender.

    The pin-writing denominator here is deliberately narrower than "touches a
    test CVM": it selects the jobs that ship a release, which are the only ones
    that must be push-triggered.  Maintenance access is covered by
    ``test_every_test_cvm_touching_job_is_locked_to_the_test_branch``.
    """
    jobs = yaml.safe_load(WORKFLOW.read_text())["jobs"]

    deploy_jobs = {
        name
        for name, job in jobs.items()
        if any(
            "./deploy/pin-runtime-release.sh test" in str(step.get("run") or "")
            for step in job.get("steps", [])
            if isinstance(step, dict)
        )
    }
    # Adding/removing pin-writing deploy jobs should fail this self-erasure
    # floor and require an intentional update.
    assert deploy_jobs == {"deploy-test-cvm", "deploy-test-runner-cvm"}, sorted(
        deploy_jobs
    )

    def needs(job: dict) -> set[str]:
        raw = job.get("needs")
        return {raw} if isinstance(raw, str) else set(raw or [])

    needs_by_job = {name: needs(job) for name, job in jobs.items()}
    shared_needs = set.intersection(
        *(needs_by_job[name] for name in sorted(deploy_jobs))
    )
    change_detectors = {
        name
        for name in shared_needs
        if "cvm" in (jobs[name].get("outputs") or {})
    }
    assert change_detectors, "test deploy jobs must share a CVM change detector"

    prerequisite_jobs = {
        dependency
        for deploy_name in deploy_jobs
        for dependency in needs_by_job[deploy_name]
        if needs_by_job.get(dependency, set()) & change_detectors
    }
    assert prerequisite_jobs, "test release unit must retain its prerequisite gate"

    release_jobs = deploy_jobs | change_detectors | prerequisite_jobs
    push_to_test = "github.ref == 'refs/heads/test' && github.event_name == 'push'"
    cvm_changed = " && ".join(
        f"needs.{name}.outputs.cvm == 'true'"
        for name in sorted(change_detectors)
    )
    for name in release_jobs:
        actual = " ".join(str(jobs[name].get("if") or "").split())
        expected = (
            push_to_test
            if name in change_detectors
            else f"{push_to_test} && {cvm_changed}"
        )
        assert actual == expected, name


def test_test_deploys_when_the_hosted_v1_consumer_changes():
    source = WORKFLOW.read_text()
    detection = _job(
        source,
        "detect-cvm-changes-test",
        "validate-test-runtime-prerequisites",
    )
    assert "tools/chat_resident_consumer.py" in detection


def test_test_stage_a_keeps_rds_primary_and_tee_shadow_wiring():
    source = WORKFLOW.read_text()
    main = _job(source, "deploy-test-cvm", "deploy-test-runner-cvm")
    runner = _job(source, "deploy-test-runner-cvm", "deploy-pre-cvm")

    assert "${{ secrets.TEST_DATABASE_URL }}" in main
    assert "${{ secrets.TEST_TEE_DATABASE_URL }}" in main
    assert "${{ secrets.TEST_FEEDLING_TEE_DUAL_WRITE }}" in main
    assert "${{ secrets.TEST_DATABASE_URL }}" in runner
    assert "PRE_DATABASE_URL" not in main
    assert "PRE_DATABASE_URL" not in runner


def test_test_deploys_forward_one_database_schema_selector_to_both_cvms():
    source = WORKFLOW.read_text()
    main = _job(source, "deploy-test-cvm", "deploy-test-runner-cvm")
    runner = _job(source, "deploy-test-runner-cvm", "deploy-pre-cvm")

    selector = "${{ vars.TEST_FEEDLING_DATABASE_SCHEMA || 'rds' }}"
    for deploy in (main, runner):
        assert "FEEDLING_DATABASE_SCHEMA:" in deploy
        assert selector in deploy
        assert '-e "FEEDLING_DATABASE_SCHEMA=$FEEDLING_DATABASE_SCHEMA"' in deploy


def test_test_compose_forwards_database_schema_to_every_database_client():
    main = TEST_COMPOSE.read_text()
    backend = main.split("\n  backend:\n", 1)[1].split("\n  serve-worker:\n", 1)[0]
    worker = main.split("\n  serve-worker:\n", 1)[1]
    runner = TEST_RUNNER_COMPOSE.read_text()
    selector = 'FEEDLING_DATABASE_SCHEMA: "${FEEDLING_DATABASE_SCHEMA:-rds}"'

    assert selector in backend
    assert selector in worker
    assert selector in runner


def test_test_preflight_validates_tee_startup_migration_authorization_before_mutating_either_cvm():
    preflight = _job(
        WORKFLOW.read_text(),
        "validate-test-runtime-prerequisites",
        "validate-prod-runner-topology",
    )

    for required in (
        "TEST_FEEDLING_DATABASE_SCHEMA == 'tee'",
        "TEST_TEE_MIGRATION_DSN",
        "TEST_TEE_PG_CA_PEM",
        "APP_DATABASE_URL",
        "owner_fingerprint != app_fingerprint",
        'owner_user != "feedling_owner"',
        'app_user != "app"',
        "pg_has_role(current_user, 'feedling_owner', 'member')",
        "TEST_DATABASE_URL app role must inherit feedling_owner",
        "No TEST CVM was changed",
    ):
        assert required in preflight

    schema_gate = preflight.index(
        "Validate TEST TEE startup migration authorization before mutating either CVM"
    )
    image_gate = preflight.index(
        "Require both Runtime V2 images before mutating either CVM"
    )
    assert schema_gate < image_gate


def test_test_preflight_rejects_noncanonical_database_schema_selector():
    preflight = _job(
        WORKFLOW.read_text(),
        "validate-test-runtime-prerequisites",
        "validate-prod-runner-topology",
    )
    complete_config = preflight.split(
        "- name: Require complete Runtime V2 configuration", 1
    )[1].split("\n      - name:", 1)[0]

    assert "FEEDLING_DATABASE_SCHEMA:" in complete_config
    assert "${{ vars.TEST_FEEDLING_DATABASE_SCHEMA || 'rds' }}" in complete_config
    assert 'case "$FEEDLING_DATABASE_SCHEMA" in' in complete_config
    assert "rds|tee)" in complete_config
    assert "must be exactly rds or tee" in complete_config


def test_prod_deploys_forward_one_database_schema_selector_to_every_database_client():
    source = WORKFLOW.read_text()
    validator = _job(
        source, "validate-prod-runner-topology", "detect-cvm-changes-pre"
    )
    main = _job(source, "deploy-cvm", "deploy-test-cvm")
    runner = _job(source, "deploy-prod-runner-cvm", "notify-lark-prod-deploy")

    assert "${{ vars.PROD_FEEDLING_DATABASE_SCHEMA || 'rds' }}" in validator
    assert (
        "database_schema: ${{ steps.prod_release_config.outputs.database_schema }}"
        in validator
    )
    assert (
        "FEEDLING_DATABASE_SCHEMA: ${{ needs.validate-prod-runner-topology."
        "outputs.database_schema }}"
    ) in main
    assert (
        "FEEDLING_DATABASE_SCHEMA:      ${{ needs.deploy-cvm.outputs."
        "database_schema }}"
    ) in runner
    for deploy in (main, runner):
        assert "FEEDLING_DATABASE_SCHEMA:" in deploy
        assert '-e "FEEDLING_DATABASE_SCHEMA=$FEEDLING_DATABASE_SCHEMA"' in deploy


def test_prod_runner_runs_after_main_deploy_even_when_optional_ancestor_skips():
    source = WORKFLOW.read_text()
    runner = _job(source, "deploy-prod-runner-cvm", "notify-lark-prod-deploy")
    header = "\n".join(runner.splitlines()[:14])

    assert "always()" in header
    assert "needs.deploy-cvm.result == 'success'" in header
    assert "needs.detect-cvm-changes.outputs.cvm == 'true'" in header


def test_prod_compose_forwards_database_schema_to_every_database_client():
    main = PROD_COMPOSE.read_text()
    backend = main.split("\n  backend:\n", 1)[1].split("\n  serve-worker:\n", 1)[0]
    worker = main.split("\n  serve-worker:\n", 1)[1]
    runner = PROD_RUNNER_COMPOSE.read_text()
    selector = 'FEEDLING_DATABASE_SCHEMA: "${FEEDLING_DATABASE_SCHEMA:-rds}"'

    assert selector in backend
    assert selector in worker
    assert selector in runner


def test_prod_preflight_validates_tee_startup_migration_authorization_before_mutating_any_cvm():
    preflight = _job(
        WORKFLOW.read_text(),
        "validate-prod-runner-topology",
        "detect-cvm-changes-pre",
    )

    for required in (
        "steps.prod_release_config.outputs.database_schema == 'tee'",
        "PROD_TEE_MIGRATION_DSN",
        "PROD_TEE_PG_CA_PEM",
        "APP_DATABASE_URL",
        "owner_fingerprint != app_fingerprint",
        'owner_user != "feedling_owner"',
        'app_user != "app"',
        "pg_has_role(current_user, 'feedling_owner', 'member')",
        "DATABASE_URL app role must inherit feedling_owner",
        "No production CVM was changed",
    ):
        assert required in preflight

    schema_gate = preflight.index(
        "Validate PROD TEE startup migration authorization before mutating any CVM"
    )
    image_gate = preflight.index(
        "Require both production images before mutating either CVM"
    )
    assert schema_gate < image_gate


def test_prod_preflight_checks_owner_and_app_migration_roles():
    preflight = _job(
        WORKFLOW.read_text(),
        "validate-prod-runner-topology",
        "detect-cvm-changes-pre",
    )
    schema_step = preflight.split(
        "- name: Validate PROD TEE startup migration authorization before mutating any CVM",
        1,
    )[1].split("\n      - name:", 1)[0]

    assignment = (
        'owner_user = str(conn.execute("SELECT current_user").fetchone()[0])'
    )
    enforcement = 'if owner_user != "feedling_owner":'
    assert assignment in schema_step
    assert schema_step.index(assignment) < schema_step.index(enforcement)
    assert 'if app_user != "app":' in schema_step
    assert "if not app_can_migrate:" in schema_step


def test_prod_preflight_rejects_invalid_selector_and_stale_shadow_wiring():
    preflight = _job(
        WORKFLOW.read_text(),
        "validate-prod-runner-topology",
        "detect-cvm-changes-pre",
    )
    complete_config = preflight.split(
        "- name: Require complete production Runtime V2 configuration", 1
    )[1].split("\n      - name:", 1)[0]

    assert "FEEDLING_DATABASE_SCHEMA:" in complete_config
    assert "${{ vars.PROD_FEEDLING_DATABASE_SCHEMA || 'rds' }}" in complete_config
    assert 'case "$FEEDLING_DATABASE_SCHEMA" in' in complete_config
    assert "rds|tee)" in complete_config
    assert "PROD_FEEDLING_DATABASE_SCHEMA must be exactly rds or tee" in complete_config
    assert "PROD_TEE_DATABASE_URL must be empty for TEE primary" in complete_config
    assert "PROD_FEEDLING_TEE_DUAL_WRITE must be empty for TEE primary" in complete_config
