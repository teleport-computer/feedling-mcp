"""Drift guard: exception text must not reach a debug-trace free-text field.

``GET /v1/debug/trace`` returns ``detail``, ``summary`` and ``explain`` to the
authenticated user, and tracing is on by default for users with no flag blob.
So anything a producer puts in those fields is, in the default configuration,
tenant-readable. A raw exception string is not ours to hand over: it can carry a
DSN, an internal host, SQL, or a provider response body.

``debug_trace._safe_detail`` cannot enforce this. It sees only characters, and a
closed error code and an exception message are the same character class — the
same reason T344 rejected a shape rule for ``status_reason``. Only the producer
knows provenance, so the contract lives at the write site and this file is what
keeps it from drifting. It had already drifted once *within a single file*:
``memory_core``'s search lane carried a closed category while its fetch lane
carried ``str(e)``.

Scope is deliberately wider than the leak that prompted it:

- both producers. ``backend`` calls ``debug_trace.trace_event``; the resident
  consumer reaches the same table over HTTP via ``_emit_debug_trace``. A guard
  that knew only the first would be blind to an entire second producer, which is
  the failure mode T344 was itself an instance of.
- all three fields, not just ``detail``. Only ``detail`` has a sanitizer, so
  ``summary`` and ``explain`` are the *less* guarded surfaces, not the safer.

What counts as carrying exception text is decided by walking the value, not by
matching names: the handler binding is often one letter, so no name-substring
heuristic can see ``str(e)``.
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).parent.parent
SCAN_ROOTS = (ROOT / "backend", ROOT / "tools")

FIELDS = ("detail", "summary", "explain")

# Producers are derived, not listed: a hand-written list is exactly how a whole
# writer goes unaudited. ``EXPECTED_PRODUCERS`` is only a liveness check on the
# derivation — if the walk breaks, the scan would otherwise pass by finding
# nothing to scan.
EXPECTED_PRODUCERS = {
    "trace_event",           # backend, the direct writer
    "_emit_debug_trace",     # resident consumer, over HTTP
    "_emit_v2_debug_trace",  # V2 hosted worker
    "_trace_enclave",        # enclave call metadata
}

# Safe fields are declared per type, never as one shared set of field *names*.
# A shared set is how an exemption granted to one contract gets spent by another:
# ``reason`` is a closed token on the backend's ``VisionObserverError`` and is
# ``str(body.get("reason"))`` — arbitrary upstream text — on the consumer's
# ``VisionObserverFailure``. The two are only alike in spelling, and the guard
# reads spelling.
#
# Everything absent from a type's set is treated as carrying exception text, so
# the default for a newly added attribute is "not safe".

# Exception types that *are* the sanitized contract rather than a raw failure.
# Each resolves its code through ``error_contract``, so catching one and reading
# its code is the intended shape; the raw text lives on other attributes of the
# same object, which is why the barrier is per-attribute rather than per-object.
SANCTIONED_FAILURE_TYPES: dict[str, frozenset[str]] = {
    # backend/hosted/vision_observer.py. ``reason`` is exempt here and only here:
    # the sole backend construction site that supplies it takes it from a class
    # constant, which ``test_backend_vision_reason_argument_is_closed`` asserts
    # structurally rather than by pinning today's value.
    "VisionObserverError": frozenset(
        {"error_code", "status_code", "retryable", "reason"}
    ),
    # tools/chat_resident_consumer.py. ``reason``/``model``/``provider`` all come
    # from the HTTP response body; ``_sanitize_thinking_meta`` strips thinking
    # markup and truncates, which is not a closed set. ``detail`` is body text too.
    "VisionObserverFailure": frozenset({"error_class", "status_code"}),
    "ImageGenerationFailure": frozenset({"error_class", "status_code"}),
}

# Declared classifiers, mapped to the safe fields of the object each returns.
# They take an exception and return a contract object whose *code* fields come
# from ``error_contract`` while the raw text is segregated onto a separate
# attribute (``VisionObserverError.upstream_detail``, ``AgentErrorNotice.detail``)
# precisely so a trace cannot serialize it by accident.
SANITIZER_RESULT_FIELDS: dict[str, frozenset[str]] = {
    "classify_vision_error": SANCTIONED_FAILURE_TYPES["VisionObserverError"],
    # AgentErrorNotice(error_class=spec.code, blame=spec.blame,
    #                  user_text=spec.text(language), detail=str(exc)[:200])
    "classify_agent_error": frozenset({"error_class", "blame", "user_text"}),
}

# Producers that may still carry exception text, each with the reason it is
# tolerable. Empty is the intended state: an entry here is a decision, and the
# test names the site so the decision has to be made rather than defaulted into.
SITES_ALLOWED_TO_CARRY_EXCEPTION_TEXT: dict[str, str] = {}


def _sources() -> list[Path]:
    return sorted(
        p for root in SCAN_ROOTS if root.exists()
        for p in root.rglob("*.py") if p.is_file()
    )


def _parsed() -> list[tuple[Path, ast.AST]]:
    out = []
    for path in _sources():
        try:
            out.append((path, ast.parse(path.read_text(encoding="utf-8"),
                                        filename=str(path))))
        except SyntaxError:
            continue
    return out


def _derive_producers() -> dict[str, str]:
    """Every function that writes this table's free-text fields.

    Two shapes, because the writers are layered: a function whose signature
    takes the three fields, and a thin wrapper that forwards ``**kwargs`` into
    one. The wrapper matters — its own signature mentions none of the fields, so
    a signature-only rule would scan the wrapper and skip all of its call sites.

    Residual gap, stated rather than papered over: a writer that renames the
    fields, or that hand-builds the event dict and posts it, is invisible here.
    The dict-literal scan below covers the second of those.
    """
    producers: dict[str, str] = {}
    forwards: list[tuple[str, str, str]] = []
    for path, tree in _parsed():
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            where = f"{path.relative_to(ROOT)}:{node.lineno}"
            params = {a.arg for a in node.args.args + node.args.kwonlyargs}
            if set(FIELDS).issubset(params):
                producers[node.name] = where
                continue
            if node.args.kwarg is None:
                continue
            forwarded = node.args.kwarg.arg
            for call in ast.walk(node):
                if not isinstance(call, ast.Call):
                    continue
                if not any(kw.arg is None
                           and getattr(kw.value, "id", None) == forwarded
                           for kw in call.keywords):
                    continue
                callee = (getattr(call.func, "id", None)
                          or getattr(call.func, "attr", None) or "")
                forwards.append((node.name, callee, where))
    changed = True
    while changed:
        changed = False
        for name, callee, where in forwards:
            if callee in producers and name not in producers:
                producers[name] = where
                changed = True
    return producers


def _is_exception_class_name(node: ast.AST) -> bool:
    """``type(e).__name__`` — the class name, never the message.

    Sanctioned by the supervisor as the one reduction that may keep naming the
    exception: it is drawn from the set of classes that exist in the process, it
    carries no message, and it is the main triage handle. Losing it would trade
    a bounded disclosure for an unattributable bucket.
    """
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "__name__"
        and isinstance(node.value, ast.Call)
        and getattr(node.value.func, "id", None) == "type"
    )


def _carries_exception_text(node: ast.AST, tainted: set[str],
                            sanitized: dict[str, frozenset[str]]) -> bool:
    """Does evaluating this expression put exception text in the result?"""
    if isinstance(node, ast.Name):
        return node.id in tainted
    if _is_exception_class_name(node):
        return False
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        if node.value.id in sanitized:
            # Reading a code off a classifier result is the whole point of
            # having classified. Reading anything else off it is not — and which
            # fields count as codes depends on *which* contract this name holds.
            return node.attr not in sanitized[node.value.id]
    if isinstance(node, ast.IfExp):
        # The test only selects a branch; its value cannot appear in the result.
        return any(
            _carries_exception_text(part, tainted, sanitized)
            for part in (node.body, node.orelse)
        )
    return any(
        _carries_exception_text(child, tainted, sanitized)
        for child in ast.iter_child_nodes(node)
    )


def _store_targets(target: ast.AST) -> set[str]:
    """Names an assignment writes through.

    Includes ``obj`` in ``obj[k] = ...`` and ``obj.a = ...``: the common laundering
    shape is ``detail["reason"] = str(e)`` followed by ``trace_event(detail=detail)``,
    where the call site never names the exception at all.
    """
    return {n.id for n in ast.walk(target) if isinstance(n, ast.Name)}


def _module_str_consts(tree: ast.AST) -> dict[str, str]:
    """Module-level ``NAME = "literal"`` bindings, for resolving field names.

    ``setattr(recorder, _LEDGER_LANE_ATTR, ...)`` is a store to a statically
    known field; refusing to resolve the constant would make it indistinguishable
    from a genuinely computed field name and cost the whole derivation.
    """
    consts: dict[str, str] = {}
    for stmt in getattr(tree, "body", []):
        if (isinstance(stmt, ast.Assign)
                and isinstance(stmt.value, ast.Constant)
                and isinstance(stmt.value.value, str)):
            for target in stmt.targets:
                if isinstance(target, ast.Name):
                    consts[target.id] = stmt.value.value
    return consts


def _const_str(node: ast.AST | None, consts: dict[str, str]) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return consts.get(node.id)
    return None


def _instance_dict_owner(node: ast.AST) -> ast.AST | None:
    """``obj`` in ``obj.__dict__`` or ``vars(obj)``, else ``None``."""
    if isinstance(node, ast.Attribute) and node.attr == "__dict__":
        return node.value
    if (isinstance(node, ast.Call)
            and getattr(node.func, "id", None) == "vars"
            and len(node.args) == 1):
        return node.args[0]
    return None


def _attribute_stores(
    node: ast.AST, consts: dict[str, str],
) -> list[tuple[ast.AST, str | None, ast.AST | None]]:
    """Every spelling of "write an attribute on an object", as (obj, field, value).

    ``field`` is ``None`` when the name written is computed, i.e. the store
    happens but cannot be attributed to one field. ``value`` is ``None`` when it
    is not a single expression (augmented assignment yields a computed value
    whatever the operand is, so ``x.reason += "lit"`` is not a literal store).

    One helper rather than each caller matching the shapes it happens to think
    of: the round-3 finding was that ``_closed_reason_types`` counted
    ``setattr(obj, "f", v)`` as a store while ``_revoked_sanitized_names`` only
    matched ``obj.f = v``, and a laundering site walked straight through the gap
    between two places that were supposed to agree. Sharing the recogniser is
    what makes that class of gap impossible rather than fixed once.
    """
    stores: list[tuple[ast.AST, str | None, ast.AST | None]] = []

    if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
        targets = (node.targets if isinstance(node, ast.Assign)
                   else [node.target])
        value = None if isinstance(node, ast.AugAssign) else node.value
        for target in targets:
            if isinstance(target, ast.Attribute):
                stores.append((target.value, target.attr, value))
            elif isinstance(target, ast.Subscript):
                owner = _instance_dict_owner(target.value)
                if owner is not None:
                    stores.append(
                        (owner, _const_str(target.slice, consts), value))
        return stores

    if isinstance(node, ast.Call):
        func = node.func
        if getattr(func, "id", None) == "setattr" and len(node.args) == 3:
            return [(node.args[0], _const_str(node.args[1], consts),
                     node.args[2])]
        if getattr(func, "attr", None) == "__setattr__":
            # ``object.__setattr__(obj, "f", v)``, the frozen-dataclass idiom.
            if len(node.args) == 3:
                return [(node.args[0], _const_str(node.args[1], consts),
                         node.args[2])]
            # ``obj.__setattr__("f", v)``.
            if len(node.args) == 2:
                return [(func.value, _const_str(node.args[0], consts),
                         node.args[1])]
    return stores


def _closed_reason_types(
    parsed: list[tuple[Path, ast.AST]],
) -> dict[str, str]:
    """Classes whose ``reason`` is the same fixed string on every instance.

    A class-level ``reason = "literal"`` only fixes the *default*. On its own it
    is not a closed set, it is a closed set until one line assigns over it — so
    a candidate is disqualified by any store to the attribute that is not itself
    a string literal:

    - ``self.reason = <anything non-literal>`` inside the class, the shape every
      other ``reason``-bearing class in this repo uses (five of them do);
    - ``obj.reason = <anything non-literal>`` outside any class, which cannot be
      attributed to a particular type from the AST, so it disqualifies every
      candidate rather than being waved through;
    - any other spelling of the same store (see ``_attribute_stores``), including
      ``setattr`` with a field name that resolves through a module constant.

    Residual, stated rather than papered over: a store whose field name is only
    known at runtime — ``setattr(obj, name, val)`` inside a reflection wrapper —
    is *not* treated as a possible ``reason`` store. Treating it as one is the
    sound reading, but it is unusable: two such lines exist today in a probe's
    hand-rolled monkeypatch shim, and counting them would disqualify every
    candidate forever, on a fact about reflection rather than about ``reason``.
    The exemption this derivation feeds has a direct control on the one site that
    spends it (``test_backend_vision_reason_argument_is_closed``), so the residual
    is a hole in defence-in-depth, not in the load-bearing check.
    """
    candidates: dict[str, str] = {}
    disqualified: set[str] = set()
    unattributable: list[str] = []

    for path, tree in parsed:
        try:
            where = str(path.relative_to(ROOT))
        except ValueError:
            where = str(path)
        classes = [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
        for node in classes:
            for stmt in node.body:
                if (isinstance(stmt, ast.Assign)
                        and any(getattr(t, "id", None) == "reason"
                                for t in stmt.targets)
                        and isinstance(stmt.value, ast.Constant)
                        and isinstance(stmt.value.value, str)):
                    candidates[node.name] = f"{where}:{node.lineno}"

        owned = {id(inner): node.name
                 for node in classes for inner in ast.walk(node)}
        consts = _module_str_consts(tree)
        for node in ast.walk(tree):
            for obj, field, value in _attribute_stores(node, consts):
                if field != "reason":
                    continue
                if (isinstance(value, ast.Constant)
                        and isinstance(value.value, str)):
                    continue
                owner = owned.get(id(node))
                if owner is not None and getattr(obj, "id", None) == "self":
                    disqualified.add(owner)
                else:
                    unattributable.append(f"{where}:{node.lineno}")

    if unattributable:
        # Cannot tell which type is being written to, so no candidate survives.
        return {}
    return {name: where for name, where in candidates.items()
            if name not in disqualified}


def _type_names(node: ast.AST | None) -> set[str]:
    if node is None:
        return set()
    items = node.elts if isinstance(node, ast.Tuple) else [node]
    return {getattr(t, "attr", None) or getattr(t, "id", None) or "?" for t in items}


def _narrowing_types(name: str, node: ast.AST,
                     parents: dict[ast.AST, ast.AST]) -> set[str]:
    """Classes ``name`` is narrowed to on the path that reaches ``node``.

    Both shapes the repo uses: ``except T as name``, and an enclosing
    ``if isinstance(name, T):`` with ``node`` in the taken branch. Deliberately
    path-based rather than function-wide — ``classify_vision_error`` narrows the
    same binding to two different classes in two branches, so a function-wide
    union would report a check that does not govern this site.
    """
    found: set[str] = set()
    cur: ast.AST = node
    parent = parents.get(cur)
    while parent is not None:
        if isinstance(parent, ast.ExceptHandler) and parent.name == name:
            found |= _type_names(parent.type)
        elif isinstance(parent, ast.If) and any(cur is s for s in parent.body):
            test = parent.test
            if (isinstance(test, ast.Call)
                    and getattr(test.func, "id", None) == "isinstance"
                    and len(test.args) == 2
                    and getattr(test.args[0], "id", None) == name):
                found |= _type_names(test.args[1])
        if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
            break
        cur, parent = parent, parents.get(parent)
    return found


def _is_sanitizer_call(node: ast.AST) -> frozenset[str] | None:
    if not isinstance(node, ast.Call):
        return None
    callee = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
    return SANITIZER_RESULT_FIELDS.get(callee)


def _revoked_sanitized_names(
    handler: ast.ExceptHandler, consts: dict[str, str] | None = None,
) -> tuple[set[str], dict[str, set[str]]]:
    """What a handler does to its own names that invalidates an exemption.

    Returns ``(rebound, overwritten)``: names bound to something that is not a
    classifier call, and per name the attributes assigned over.

    The taint walk is flow-insensitive by design — it has to see text that
    arrives by *any* path, so it deliberately does not model statement order.
    That makes it unable to distinguish ``notice = classify(e)`` followed by
    ``notice = e`` from the reverse, and keying the exemption on the name alone
    would keep it in both: one by outliving the rebinding, the other by granting
    it retroactively to a write that already happened. Rather than claim an
    ordering it does not compute, a rebound name loses the exemption for the
    whole handler.

    Attribute stores are handled per field, not per object, for the same reason
    the barrier itself is: ``failure.model = model`` (a real site, fixing route
    metadata that was never in the safe set) must not cost ``failure`` the
    exemption on ``error_code``, while ``notice.error_class = str(e)`` must cost
    exactly ``error_class``.
    """
    consts = consts or {}
    rebound: set[str] = set()
    overwritten: dict[str, set[str]] = {}
    for node in ast.walk(handler):
        # Attribute stores first, in every spelling, via the shared recogniser.
        stores = _attribute_stores(node, consts)
        for obj, field, value in stores:
            if not isinstance(obj, ast.Name):
                continue
            if isinstance(value, ast.Constant):
                continue
            if field is None:
                # A computed field name cannot be attributed to one field, so
                # the object loses every exemption rather than none.
                rebound.add(obj.id)
            else:
                overwritten.setdefault(obj.id, set()).add(field)

        targets: list[ast.AST] = []
        value: ast.AST | None = None
        if isinstance(node, ast.Assign):
            targets, value = list(node.targets), node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        elif isinstance(node, ast.AugAssign):
            targets, value = [node.target], node.value
        elif isinstance(node, ast.NamedExpr):
            targets, value = [node.target], node.value
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            targets = [node.target]
        elif isinstance(node, ast.withitem) and node.optional_vars is not None:
            targets = [node.optional_vars]
        else:
            continue
        sanitizing = (not isinstance(node, ast.AugAssign)
                      and value is not None
                      and _is_sanitizer_call(value) is not None)

        def record(target: ast.AST) -> None:
            # Shape-directed rather than a walk: the base name of ``x.attr = v``
            # is not being rebound, only one of its fields is being written, and
            # conflating the two costs the exemption for an unrelated field.
            # Attribute-shaped targets are already handled above.
            if isinstance(target, (ast.Tuple, ast.List)):
                for element in target.elts:
                    record(element)
            elif isinstance(target, ast.Name) and not sanitizing:
                rebound.add(target.id)

        for target in targets:
            record(target)
    return rebound, overwritten


def _analyze_handler(
    handler: ast.ExceptHandler, consts: dict[str, str] | None = None,
) -> tuple[set[str], dict[str, frozenset[str]]]:
    """Fixed point: which names in this handler hold exception-derived text."""
    tainted = {handler.name}
    sanitized: dict[str, frozenset[str]] = {}
    rebound, overwritten = _revoked_sanitized_names(handler, consts)

    def grant(name: str, fields: frozenset[str]) -> frozenset[str] | None:
        if name in rebound:
            return None
        return fields - overwritten.get(name, set())
    caught = [
        getattr(t, "attr", None) or getattr(t, "id", None)
        for t in (handler.type.elts if isinstance(handler.type, ast.Tuple)
                  else [handler.type] if handler.type else [])
    ]
    # Only when every caught type is one, so a tuple mixing a raw exception in
    # does not inherit the exemption. Across a tuple the bound name may hold
    # either type, so it gets the intersection: a field is safe only if it is
    # safe on every type that could be bound here.
    if caught and all(name in SANCTIONED_FAILURE_TYPES for name in caught):
        granted = grant(handler.name, frozenset.intersection(
            *(SANCTIONED_FAILURE_TYPES[name] for name in caught)
        ))
        if granted is not None:
            sanitized[handler.name] = granted
    changed = True
    while changed:
        changed = False
        for node in ast.walk(handler):
            value: ast.AST | None = None
            targets: list[ast.AST] = []
            if isinstance(node, ast.Assign):
                value, targets = node.value, list(node.targets)
            elif isinstance(node, ast.AugAssign):
                value, targets = node.value, [node.target]
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                value, targets = node.value, [node.target]
            if value is None:
                continue

            sanitizer_fields = _is_sanitizer_call(value)
            # A classifier result is tracked as tainted *and* sanitized: the code
            # fields of the contract it returns are exempt, everything else on it
            # still counts.
            if sanitizer_fields is None and not _carries_exception_text(
                value, tainted, sanitized
            ):
                continue
            for target in targets:
                for name in _store_targets(target):
                    if sanitizer_fields is not None and name not in sanitized:
                        granted = grant(name, sanitizer_fields)
                        if granted is not None:
                            sanitized[name] = granted
                            changed = True
                    if name not in tainted:
                        tainted.add(name)
                        changed = True

        for node in ast.walk(handler):
            # ``detail.update({...str(e)...})`` mutates in place, so the taint
            # lands on a name that is never an assignment target.
            if not isinstance(node, ast.Call):
                continue
            if not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in ("update", "setdefault", "append", "extend"):
                continue
            carries = any(
                _carries_exception_text(a, tainted, sanitized) for a in node.args
            ) or any(
                _carries_exception_text(k.value, tainted, sanitized)
                for k in node.keywords
            )
            if not carries:
                continue
            for name in _store_targets(node.func.value):
                if name not in tainted:
                    tainted.add(name)
                    changed = True
    return tainted, sanitized


def _field_values(node: ast.Call | ast.Dict) -> list[tuple[str, ast.AST]]:
    """The (field, value) pairs a producer call or an event dict writes.

    A ``detail`` dict literal is unpacked per key so a failure names the
    offending key rather than the whole payload.
    """
    out: list[tuple[str, ast.AST]] = []

    def add(field: str, value: ast.AST) -> None:
        if field == "detail" and isinstance(value, ast.Dict):
            for key, inner in zip(value.keys, value.values):
                shown = key.value if isinstance(key, ast.Constant) else "**"
                out.append((f"detail[{shown}]", inner))
        else:
            out.append((field, value))

    if isinstance(node, ast.Call):
        for kw in node.keywords:
            if kw.arg in FIELDS:
                add(kw.arg, kw.value)
    else:
        for key, value in zip(node.keys, node.values):
            if isinstance(key, ast.Constant) and key.value in FIELDS:
                add(key.value, value)
    return out


def _scan_tree(tree: ast.AST, label: str,
               producers: set[str]) -> list[tuple[str, str, str]]:
    hits: list[tuple[str, str, str]] = []
    consts = _module_str_consts(tree)
    for handler in [
        n for n in ast.walk(tree)
        if isinstance(n, ast.ExceptHandler) and n.name
    ]:
        tainted, sanitized = _analyze_handler(handler, consts)
        for node in ast.walk(handler):
            if isinstance(node, ast.Call):
                name = (getattr(node.func, "id", None)
                        or getattr(node.func, "attr", None))
                if name not in producers:
                    continue
            elif isinstance(node, ast.Dict):
                # A hand-built event payload posted to /v1/debug/trace/event,
                # which never names a producer function at all. Two of the three
                # fields is enough to identify the shape without matching every
                # incidental dict that happens to have a "detail" key.
                keys = {k.value for k in node.keys
                        if isinstance(k, ast.Constant) and isinstance(k.value, str)}
                if len(keys & set(FIELDS)) < 2:
                    continue
            else:
                continue
            for field, value in _field_values(node):
                if _carries_exception_text(value, tainted, sanitized):
                    hits.append((f"{label}:{node.lineno}", field,
                                 ast.unparse(value)[:70]))
    return hits


def _scan_repo() -> list[tuple[str, str, str]]:
    producers = set(_derive_producers())
    hits: list[tuple[str, str, str]] = []
    for path, tree in _parsed():
        hits.extend(_scan_tree(tree, str(path.relative_to(ROOT)), producers))
    return sorted(set(hits))


def _scan_snippet(src: str) -> list[tuple[str, str, str]]:
    return _scan_tree(ast.parse(src), "<snippet>", set(EXPECTED_PRODUCERS))


# --- the analyzer must be shown to work before its silence means anything ----

_LAUNDERED = """
try:
    pass
except RuntimeError as e:
    detail = {"counts": 1}
    detail["reason"] = str(e)[:80]
    trace_event(store, subsystem="s", type="t", detail=detail)
"""

_DIRECT = """
try:
    pass
except RuntimeError as e:
    trace_event(store, subsystem="s", type="t", detail={"reason": str(e)[:80]})
"""

_MUTATED_IN_PLACE = """
try:
    pass
except RuntimeError as e:
    detail = {}
    detail.update({"reason": repr(e)})
    _emit_debug_trace("s", "t", detail=detail)
"""

_SUMMARY_AND_EXPLAIN = """
try:
    pass
except RuntimeError as e:
    trace_event(store, subsystem="s", type="t", summary=str(e),
                explain=f"failed: {e}")
"""

_CLASS_NAME_ONLY = """
try:
    pass
except RuntimeError as e:
    trace_event(store, subsystem="s", type="t",
                detail={"reason": "readside_unavailable",
                        "error_class": type(e).__name__})
"""

_CLASSIFIED_CODE = """
try:
    pass
except RuntimeError as e:
    notice = classify_agent_error(e)
    _emit_debug_trace("s", "t", detail={"error_class": notice.error_class})
"""

_CLASSIFIED_RAW_FIELD = """
try:
    pass
except RuntimeError as e:
    notice = classify_agent_error(e)
    _emit_debug_trace("s", "t", detail={"detail": notice.detail})
"""


def test_analyzer_sees_every_laundering_shape():
    """Positive controls, one per shape the analyzer claims to cover.

    A scan that silently stops matching passes the repo-wide assertion below
    while guarding nothing, so each shape is pinned against a synthetic source
    rather than against the repo, which is expected to be clean.
    """
    assert _scan_snippet(_DIRECT), "missed exception text written inline"
    assert _scan_snippet(_LAUNDERED), (
        "missed exception text assigned into a dict the call site names instead "
        "of the exception — the shape a call-site scan cannot see"
    )
    assert _scan_snippet(_MUTATED_IN_PLACE), (
        "missed exception text carried in by an in-place mutation"
    )
    fields = {field for _, field, _ in _scan_snippet(_SUMMARY_AND_EXPLAIN)}
    assert fields == {"summary", "explain"}, fields
    assert _scan_snippet(_CLASSIFIED_RAW_FIELD), (
        "missed the raw-text attribute of a classifier result; the classifiers "
        "segregate it exactly because it must not reach a trace"
    )


def test_analyzer_does_not_flag_the_sanctioned_reductions():
    """Negative controls. Over-flagging costs the guard its credibility.

    These two are the shapes the write sites are *supposed* to use, so if either
    were flagged the fix for a real hit would be to weaken the guard.
    """
    assert not _scan_snippet(_CLASS_NAME_ONLY), (
        "type(e).__name__ carries no message and is the sanctioned reduction"
    )
    assert not _scan_snippet(_CLASSIFIED_CODE), (
        "a code read off a declared classifier is the intended shape"
    )


def test_scan_reaches_both_producers_and_both_trees():
    """The denominator, asserted by membership rather than only by count.

    ``tools/`` is not incidental: the resident consumer is a full second
    producer into this table, and a guard scoped to ``backend/`` would be blind
    to it — which is the exact shape of the miss this guard exists to prevent.
    """
    scanned = {str(p.relative_to(ROOT)) for p in _sources()}
    assert "backend/debug_trace.py" in scanned
    assert "tools/chat_resident_consumer.py" in scanned
    assert sum(1 for p in scanned if p.startswith("tools/")) >= 5, scanned

    handlers = sum(
        1 for _, tree in _parsed() for node in ast.walk(tree)
        if isinstance(node, ast.ExceptHandler) and node.name
    )
    # Narrowing the walk drops this sharply; only a bound handler can be
    # analyzed, so this is the guard's real denominator.
    assert handlers >= 800, handlers


_SANCTIONED_FAILURE_CAUGHT = """
try:
    pass
except VisionObserverError as failure:
    trace_event(store, subsystem="s", type="t", summary=failure.error_code,
                detail={"error_class": failure.error_code,
                        "retryable": failure.retryable})
"""

_SANCTIONED_FAILURE_RAW_FIELD = """
try:
    pass
except VisionObserverError as failure:
    trace_event(store, subsystem="s", type="t",
                detail={"upstream": failure.upstream_detail})
"""

_MIXED_CATCH = """
try:
    pass
except (VisionObserverError, RuntimeError) as failure:
    trace_event(store, subsystem="s", type="t",
                detail={"error_class": failure.error_code})
"""

# The consumer's failure contracts. Their code field is closed, but ``reason``,
# ``model`` and ``provider`` are read straight off the HTTP response body at
# tools/chat_resident_consumer.py:3262, so they carry whatever the peer sent.
_CONSUMER_FAILURE_REASON = """
try:
    pass
except VisionObserverFailure as failure:
    _emit_debug_trace("s", "t", detail={"reason": failure.reason})
"""

_CONSUMER_FAILURE_PROVIDER_META = """
try:
    pass
except ImageGenerationFailure as failure:
    _emit_debug_trace("s", "t", detail={"model": failure.model,
                                        "provider": failure.provider})
"""

_BACKEND_VISION_REASON = """
try:
    pass
except VisionObserverError as failure:
    trace_event(store, subsystem="s", type="t", detail={"reason": failure.reason})
"""

_MIXED_SANCTIONED_CATCH = """
try:
    pass
except (VisionObserverError, VisionObserverFailure) as failure:
    trace_event(store, subsystem="s", type="t", detail={"reason": failure.reason})
"""


def test_safe_fields_are_scoped_to_the_type_that_earned_them():
    """``reason`` is closed on one contract and raw body text on another.

    The two classes spell the attribute the same way, so a guard holding one set
    of safe field *names* would hand the backend contract's exemption to the
    consumer's and let arbitrary upstream text through under a trusted name. The
    exemption belongs to the type, not to the word.
    """
    assert _scan_snippet(_CONSUMER_FAILURE_REASON), (
        "VisionObserverFailure.reason is str(body.get('reason')) passed through "
        "_sanitize_thinking_meta, which truncates but does not close the set"
    )
    assert _scan_snippet(_CONSUMER_FAILURE_PROVIDER_META), (
        "model/provider come from the same response body as reason"
    )
    assert not _scan_snippet(_BACKEND_VISION_REASON), (
        "VisionObserverError.reason is closed at every backend construction "
        "site; test_backend_vision_reason_argument_is_closed is what holds it so"
    )
    assert _scan_snippet(_MIXED_SANCTIONED_CATCH), (
        "a tuple catch binds either type, so the safe set is the intersection "
        "and reason drops out of it"
    )


_REBOUND_AFTER_CLASSIFY = """
try:
    pass
except RuntimeError as e:
    notice = classify_agent_error(e)
    notice = e
    _emit_debug_trace("s", "t", detail={"error_class": notice.error_class})
"""

_CLASSIFIED_AFTER_THE_WRITE = """
try:
    pass
except RuntimeError as e:
    notice = e
    _emit_debug_trace("s", "t", detail={"error_class": notice.error_class})
    notice = classify_agent_error(e)
"""

_FIELD_OVERWRITTEN_ON_A_CLASSIFIER_RESULT = """
try:
    pass
except RuntimeError as e:
    notice = classify_agent_error(e)
    notice.error_class = str(e)
    _emit_debug_trace("s", "t", detail={"error_class": notice.error_class})
"""


_SETATTR_ONTO_AN_EXEMPT_FIELD = """
try:
    pass
except VisionObserverError as failure:
    setattr(failure, "reason", failure.upstream_detail)
    trace_event(store, subsystem="s", type="t", detail={"reason": failure.reason})
"""

_SETATTR_WITH_A_COMPUTED_FIELD_NAME = """
try:
    pass
except VisionObserverError as failure:
    setattr(failure, chosen_field, failure.upstream_detail)
    trace_event(store, subsystem="s", type="t",
                detail={"error_class": failure.error_code})
"""

_SETATTR_OF_A_LITERAL = """
try:
    pass
except VisionObserverError as failure:
    setattr(failure, "reason", "output_truncated")
    trace_event(store, subsystem="s", type="t", detail={"reason": failure.reason})
"""


def test_setattr_is_the_same_store_as_an_attribute_assignment():
    """The sanctioned failures are mutable, so the exempt field is writable.

    ``VisionObserverError`` is a plain ``RuntimeError`` subclass and
    ``upstream_detail`` is explicitly the provider response body, kept off the
    public shape for that reason. Recognising ``obj.attr = v`` but not
    ``setattr(obj, "attr", v)`` leaves an identical store that the guard cannot
    see — and this file already treats the two as equivalent when deriving
    closed reason types, so seeing only one here was an inconsistency, not a
    judgement about which shape matters.
    """
    assert _scan_snippet(_SETATTR_ONTO_AN_EXEMPT_FIELD), (
        "setattr wrote the provider response body onto the exempt reason field"
    )
    assert _scan_snippet(_SETATTR_WITH_A_COMPUTED_FIELD_NAME), (
        "a computed field name cannot be attributed to one field, so no "
        "exemption on that object may survive"
    )
    assert not _scan_snippet(_SETATTR_OF_A_LITERAL), (
        "writing a literal does not make the field carry exception text"
    )


_STORE_SPELLINGS = {
    "attribute assignment": "{obj}.reason = {text}",
    "augmented assignment": "{obj}.reason += {text}",
    "setattr, literal field": 'setattr({obj}, "reason", {text})',
    "setattr, field via module constant": "setattr({obj}, _FIELD, {text})",
    "bound dunder": '{obj}.__setattr__("reason", {text})',
    "unbound dunder": 'object.__setattr__({obj}, "reason", {text})',
    "instance dict": '{obj}.__dict__["reason"] = {text}',
    "vars()": 'vars({obj})["reason"] = {text}',
}

_REVOCATION_CORPUS = """
_FIELD = "reason"
try:
    pass
except VisionObserverError as failure:
    {store}
    trace_event(store, subsystem="s", type="t", detail={{"reason": failure.reason}})
"""

_DERIVATION_CORPUS = '''
_FIELD = "reason"


class Closed(RuntimeError):
    reason = "output_truncated"

    def __init__(self, text):
        {store}
'''


def test_both_consumers_recognise_the_same_store_spellings():
    """The two places that ask "was this attribute written to" must agree.

    Not a second test of ``setattr``: the round-3 finding was not that one
    spelling was missing, it was that two functions meant to model the same
    operation had each grown their own idea of what a store looks like. A guard
    that is fixed one spelling at a time stays exactly as strong as whoever last
    read it, so both consumers now share ``_attribute_stores`` and this pins the
    corpus they must both handle — including the frozen-dataclass dunder form and
    ``__dict__``/``vars()`` writes, which are in this repo and which neither
    consumer saw before.

    Each spelling is checked from both ends, because they fail differently: the
    revocation side must stop trusting the field, and the derivation side must
    stop calling the class constant closed.
    """
    missed_revocation: list[str] = []
    missed_derivation: list[str] = []

    for label, spelling in _STORE_SPELLINGS.items():
        laundered = _REVOCATION_CORPUS.format(
            store=spelling.format(obj="failure", text="failure.upstream_detail"))
        if not _scan_snippet(laundered):
            missed_revocation.append(label)

        overridden = _DERIVATION_CORPUS.format(
            store=spelling.format(obj="self", text="text"))
        tree = ast.parse(overridden)
        if "Closed" in _closed_reason_types([(Path("<corpus>"), tree)]):
            missed_derivation.append(label)

    assert not missed_revocation, (
        "these spellings write exception text onto an exempt field without "
        f"costing the exemption: {missed_revocation}"
    )
    assert not missed_derivation, (
        "these spellings assign over the class constant, so the attribute is "
        f"not closed, but the derivation still calls it closed: {missed_derivation}"
    )


def test_store_corpus_is_not_vacuously_red():
    """The corpus must pass a clean value through, or it proves nothing.

    Every spelling above is asserted to trip both consumers. If they tripped on
    the shape alone rather than on what is written, the whole corpus would be a
    test of the string ``reason`` and would say nothing about provenance.
    """
    still_trusted: list[str] = []
    still_closed: list[str] = []

    for label, spelling in _STORE_SPELLINGS.items():
        if "+=" in spelling:
            # Concatenation is a computed value whatever the operand is.
            continue
        clean = _REVOCATION_CORPUS.format(
            store=spelling.format(obj="failure", text='"output_truncated"'))
        if _scan_snippet(clean):
            still_trusted.append(label)

        literal = _DERIVATION_CORPUS.format(
            store=spelling.format(obj="self", text='"output_truncated"'))
        tree = ast.parse(literal)
        if "Closed" not in _closed_reason_types([(Path("<corpus>"), tree)]):
            still_closed.append(label)

    appended = _REVOCATION_CORPUS.format(
        store=_STORE_SPELLINGS["augmented assignment"].format(
            obj="failure", text='"output_truncated"'))
    assert _scan_snippet(appended), (
        "appending even a literal yields a computed value, so the field is no "
        "longer the closed string the exemption was granted for"
    )

    assert not still_trusted, (
        f"writing a literal is not laundering, but these flagged: {still_trusted}"
    )
    assert not still_closed, (
        "writing the same literal keeps the attribute closed, but these "
        f"disqualified it: {still_closed}"
    )


def test_sanitized_exemption_does_not_survive_a_rebinding():
    """The exemption belongs to a value, and a name is not a value.

    The taint walk is flow-insensitive, so it cannot order these two statements;
    if the exemption were keyed on the name alone it would be granted either by
    outliving the rebinding or, worse, retroactively to a write that already
    happened. Both are laundering channels, so a name that is ever bound to
    anything other than a classifier call loses the exemption for the whole
    handler.
    """
    assert _scan_snippet(_REBOUND_AFTER_CLASSIFY), (
        "the name was rebound to the raw exception after being classified"
    )
    assert _scan_snippet(_CLASSIFIED_AFTER_THE_WRITE), (
        "classifying afterwards must not reach back and exempt an earlier write"
    )
    assert _scan_snippet(_FIELD_OVERWRITTEN_ON_A_CLASSIFIER_RESULT), (
        "the exempt field itself was overwritten with exception text"
    )
    # The shape the exemption exists for still passes, so this is a barrier and
    # not just a blanket refusal.
    assert not _scan_snippet(_CLASSIFIED_CODE)


_CLOSED_REASON_CLASS = """
class Truncated(RuntimeError):
    reason = "output_truncated"

    def __init__(self):
        super().__init__(self.reason)
"""

_OVERRIDDEN_REASON_CLASS = """
class Truncated(RuntimeError):
    reason = "output_truncated"

    def __init__(self, reason=""):
        self.reason = reason or self.reason
        super().__init__(self.reason)
"""

_SETATTR_REASON = """
class Truncated(RuntimeError):
    reason = "output_truncated"

def poke(exc, text):
    setattr(exc, "reason", text)
"""


def test_closed_reason_derivation_rejects_a_dynamic_override(tmp_path):
    """A class constant fixes the default, not the attribute.

    Deriving "closed" from the presence of ``reason = "literal"`` alone is a
    proxy narrower than the fact it stands for: one ``self.reason = <arg>`` in
    ``__init__`` turns the same class into a free-text carrier while the constant
    is still sitting there. Five classes in this repo already assign ``reason``
    that way, so this is the common shape, not a hypothetical.
    """
    def derive(src: str) -> dict[str, str]:
        return _closed_reason_types([(tmp_path / "m.py", ast.parse(src))])

    assert "Truncated" in derive(_CLOSED_REASON_CLASS)
    assert "Truncated" not in derive(_OVERRIDDEN_REASON_CLASS), (
        "self.reason is assigned from a constructor argument, so instances do "
        "not all carry the constant"
    )
    assert "Truncated" not in derive(_SETATTR_REASON), (
        "an unattributable store to .reason disqualifies every candidate, since "
        "the AST cannot say which type is being written to"
    )


def test_backend_vision_reason_argument_is_closed():
    """What the ``VisionObserverError.reason`` exemption is actually resting on.

    The exemption is only defensible while every backend site that supplies
    ``reason=`` supplies a value from a closed set, so this checks the
    construction sites rather than the current value of the attribute. A future
    ``reason=body["reason"]``, or ``reason=`` taken off an exception whose class
    does not fix it as a constant, fails here — which is the failure this guard
    would otherwise absorb silently, since the scan trusts the attribute by name.

    "Closed" is derived, not listed: a class-level ``reason = "..."`` constant.
    Today that is ``visual_transport.VisualOutputTruncated``.
    """
    closed_types = _closed_reason_types(_parsed())
    assert "VisualOutputTruncated" in closed_types, (
        f"the closed-reason derivation found {sorted(closed_types)}; if the "
        "class constant moved, this check no longer constrains anything"
    )

    checked: list[str] = []
    open_valued: list[str] = []
    for path, tree in _parsed():
        parents = {child: node for node in ast.walk(tree)
                   for child in ast.iter_child_nodes(node)}
        for call in ast.walk(tree):
            if not isinstance(call, ast.Call):
                continue
            callee = (getattr(call.func, "id", None)
                      or getattr(call.func, "attr", None))
            if callee != "VisionObserverError":
                continue
            arg = next((k.value for k in call.keywords if k.arg == "reason"), None)
            if arg is None:
                continue
            where = f"{path.relative_to(ROOT)}:{call.lineno}"
            checked.append(where)
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                continue
            if not (isinstance(arg, ast.Attribute) and arg.attr == "reason"
                    and isinstance(arg.value, ast.Name)):
                open_valued.append(f"{where} reason={ast.unparse(arg)[:60]}")
                continue
            narrowed = _narrowing_types(arg.value.id, call, parents)
            if not narrowed or not narrowed <= set(closed_types):
                open_valued.append(
                    f"{where} reason={ast.unparse(arg)[:60]} "
                    f"(narrowed to {sorted(narrowed) or 'nothing'})"
                )

    assert checked, "found no VisionObserverError(reason=...) site to check"
    assert not open_valued, (
        "these supply VisionObserverError.reason from something that is not a "
        "closed set, so the reason exemption in SANCTIONED_FAILURE_TYPES no "
        f"longer holds and must be removed: {open_valued}"
    )


def test_sanctioned_failure_types_earn_their_exemption():
    """The exemption is granted for a property, so check the property holds.

    These classes are exempt because they resolve their code through
    ``error_contract`` — that is what makes the code a closed set rather than
    whatever the provider said. If someone later assigns the constructor
    argument straight through, the exemption would be laundering raw text under
    a name this guard trusts, so it has to fail here.
    """
    checked: dict[str, bool] = {}
    for _, tree in _parsed():
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            if node.name not in SANCTIONED_FAILURE_TYPES:
                continue
            resolves = any(
                isinstance(call.func, ast.Attribute)
                and call.func.attr in ("require_spec", "resolve_untrusted")
                for call in ast.walk(node) if isinstance(call, ast.Call)
            )
            assigns_from_spec = any(
                isinstance(assign, ast.Assign)
                and any(
                    isinstance(t, ast.Attribute)
                    and t.attr in ("error_code", "error_class")
                    for t in assign.targets
                )
                and isinstance(assign.value, ast.Attribute)
                and assign.value.attr == "code"
                for assign in ast.walk(node)
            )
            checked[node.name] = resolves and assigns_from_spec
    assert set(checked) == set(SANCTIONED_FAILURE_TYPES), checked
    unearned = [name for name, ok in checked.items() if not ok]
    assert not unearned, (
        "these are exempted as sanitized failure contracts but no longer resolve "
        f"their code field through error_contract: {unearned}"
    )


def test_sanctioned_failure_barrier_is_field_scoped():
    """Catching a sanitized failure exempts its codes, not the whole object."""
    assert not _scan_snippet(_SANCTIONED_FAILURE_CAUGHT)
    assert _scan_snippet(_SANCTIONED_FAILURE_RAW_FIELD), (
        "upstream_detail holds the provider response body; it is kept off the "
        "public failure shape precisely so a trace cannot serialize it"
    )
    assert _scan_snippet(_MIXED_CATCH), (
        "a tuple catch that also binds a raw exception must not inherit the "
        "exemption — the bound name may hold either type"
    )


def test_producer_derivation_is_live():
    """The derivation must still find the writers we know exist.

    Deriving the producer set is what keeps a newly added writer from going
    unaudited, but a derivation that quietly matches nothing would make the
    invariant below vacuous while staying green. These four are named so that a
    broken walk, or a producer whose signature was refactored out of range,
    fails here instead.
    """
    derived = _derive_producers()
    missing = EXPECTED_PRODUCERS - set(derived)
    assert not missing, (
        f"the producer derivation stopped finding {missing}; every call site of "
        f"those writers is now unscanned. Found: {sorted(derived)}"
    )


def test_no_trace_field_carries_exception_text():
    """The invariant.

    ``detail``/``summary``/``explain`` are returned to the authenticated user and
    tracing defaults to on, so an exception string here is disclosed by default.
    Reduce it at the write site: a closed category, plus ``type(e).__name__`` if
    the class is needed for triage.
    """
    leaked = {
        f"{where} {field}": expr
        for where, field, expr in _scan_repo()
        if where.split(":")[0] not in SITES_ALLOWED_TO_CARRY_EXCEPTION_TEXT
    }
    assert not leaked, (
        "these write exception-derived text into a tenant-readable debug-trace "
        "field; replace it with a closed category (and type(e).__name__ if the "
        f"class is needed), or declare the site with a reason: {leaked}"
    )
