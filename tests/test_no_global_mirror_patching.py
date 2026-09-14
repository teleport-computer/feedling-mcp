"""T593: mirror batch captures must exclude the process-wide stats writers.

A daemon's execute_many call can land between a test's delete/freeze and its
exact-count assertion. Keep those assertions strict; use capture_mirror_groups
instead of capturing unrelated batches. AST detection survives multiline calls,
import aliases, and monkeypatch's dotted-string form.
"""

import ast
from pathlib import Path


def _dotted(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _dotted(node.value)
        return f"{parent}.{node.attr}" if parent else ""
    return ""


def _offenders_in(tree):
    mirrors = {"mirror", "tee_mirror", "tee_shadow.mirror"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "tee_shadow":
            mirrors.update(a.asname or a.name for a in node.names if a.name == "mirror")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "tee_shadow.mirror":
                    mirrors.add(alias.asname or alias.name)
                elif alias.name == "tee_shadow":
                    mirrors.add(f"{alias.asname or alias.name}.mirror")
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _dotted(node.func).split(".")[-1] != "setattr":
            continue
        kwargs = {k.arg: k.value for k in node.keywords}
        target = node.args[0] if node.args else kwargs.get("target")
        name = node.args[1] if len(node.args) > 1 else kwargs.get("name")
        if (_dotted(target) in mirrors
                and isinstance(name, ast.Constant) and name.value == "execute_many"):
            hits.append(node.lineno)
        elif (isinstance(target, ast.Constant)
              and target.value == "tee_shadow.mirror.execute_many"):
            hits.append(node.lineno)
    return sorted(set(hits))


def test_no_test_patches_process_global_mirror_batches():
    tests = Path(__file__).resolve().parent
    offenders = []
    for path in sorted(tests.rglob("test_*.py")):
        # SyntaxError intentionally fails the guard rather than hiding a file.
        tree = ast.parse(path.read_text(), filename=str(path))
        offenders.extend(f"{path.relative_to(tests)}:{line}" for line in _offenders_in(tree))
    assert not offenders, (
        "Use conftest.capture_mirror_groups(monkeypatch, sink) instead of bare "
        "process-global mirror.execute_many patches:\n" + "\n".join(offenders)
    )


def test_guard_covers_multiline_alias_and_dotted_patches_without_widening_scope():
    for source in (
        'monkeypatch.setattr(mirror, "execute_many", sink.append)',
        'monkeypatch.setattr(\n tee_mirror,\n "execute_many",\n sink.append)',
        'monkeypatch.setattr("tee_shadow.mirror.execute_many", sink.append)',
        'from tee_shadow import mirror as shadow\n'
        'monkeypatch.setattr(shadow, "execute_many", sink.append)',
        'import tee_shadow as tee\n'
        'monkeypatch.setattr(tee.mirror, "execute_many", sink.append)',
        'import tee_shadow.mirror as shadow\n'
        'monkeypatch.setattr(target=shadow, name="execute_many", value=sink.append)',
    ):
        assert _offenders_in(ast.parse(source)), source
    for source in (
        'capture_mirror_groups(monkeypatch, sink)',
        'monkeypatch.setattr(mirror, "execute", sink.append)',
        'monkeypatch.setattr(other, "execute_many", sink.append)',
        'monkeypatch.setattr("other.execute_many", sink.append)',
    ):
        assert not _offenders_in(ast.parse(source)), source
