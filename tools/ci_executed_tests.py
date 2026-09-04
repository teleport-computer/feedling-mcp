"""Which test files does CI actually execute?

The discovery guard used to answer this with
``grep -oE 'tests/test_*.py' .github/workflows/ci.yml``, i.e. **"the filename
appears somewhere in the workflow text"**. That is not the same question. A name
mentioned in a comment, in a step *name*, or in an unrelated shell command
counted as covered, so a file could stop being executed without the guard ever
going red — the exact failure this repository keeps rediscovering (a test that
never runs is indistinguishable from a test that passes).

This module answers the intended question: a file is covered when some workflow
step runs a command that **executes** it — as an argument to ``pytest``, or as a
script (``python tests/test_api.py …``, which is how the multi-tenant runner
works).

Getting that right needs real shell tokenisation, not a line regex. A first cut
here scanned whole lines once it saw the word ``pytest`` and was wrong in five
distinct ways found in review — ``echo x.py && pytest y.py`` credited ``x.py``,
so did text after a ``|`` or ``&&``, and an inline ``# pytest z.py`` comment
credited ``z.py``. Every one of those recreates the very hole this module
exists to close, so the parse now splits the script into command segments on
shell operators (``&&``, ``||``, ``;``, ``|``, ``&``) with quote and comment
awareness, and only a segment whose own command word is pytest (or python
running a test file) contributes names.

A script this parser cannot tokenise, or a test run through some other wrapper,
yields *no* coverage for those files: the guard then fails closed — a false red
an author must resolve — rather than false green, which is the failure mode
being fixed. ``tests/test_pytest_coverage_ratchet.py`` imports these helpers so
the guard and its regression test can never drift onto two criteria again.
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
from pathlib import Path
from typing import Iterable

import yaml

ROOT = Path(__file__).resolve().parents[1]
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"

_TEST_FILE = re.compile(r"^tests/test_[A-Za-z0-9_]+\.py$")
_TEST_FILE_ANYWHERE = re.compile(r"tests/test_[A-Za-z0-9_]+\.py")
# Shell operators that end one command and begin another.
_SEGMENT_BREAKS = {"&&", "||", ";", "|", "&", "(", ")", ";;", ";;&", ";&",
                   "|&", "\n"}
_PYTHON = re.compile(r"^(?:.*/)?python[0-9.]*$")
_PYTEST = re.compile(r"^(?:.*/)?pytest$")
# ``FOO=bar cmd`` — environment prefixes precede the real command word.
_ENV_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


_TRAILING_OPERATOR = re.compile(r"(&&|\|\||\||&)\s*$")
# ``<<EOF`` / ``<<-EOF`` / ``<<'EOF'`` / ``<<"EOF"`` / ``<<\EOF`` (all quote the
# delimiter; the backslash form is as valid as the others and was missed once).
_HEREDOC_START = re.compile(
    r"(<<-?)\s*\\?[\'\"]?([A-Za-z_][A-Za-z0-9_]*)[\'\"]?"
)
# Any ``<<`` that is not a here-string ``<<<``; used to notice a heredoc whose
# delimiter form this parser does not recognise, instead of reading its body as
# commands.
_HEREDOC_OPERATOR = re.compile(r"<<(?!<)")
# Shell control flow this parser does not model. Everything from the first such
# keyword onward is abandoned: `if false; then pytest x; fi` executes nothing,
# and reachability is exactly what is not being tracked.
_CONTROL_KEYWORDS = {
    "if", "then", "elif", "else", "fi", "case", "esac", "for", "while",
    "until", "do", "done", "select", "function", "{", "}", "[[", "]]",
}
# Redirections: the operand is a destination, not something pytest runs.
# ``>&`` / ``>|`` / ``<>`` are valid too — missing any of them credited the
# destination as an executed test, so the set is enumerated rather than guessed.
_REDIRECT = re.compile(
    r"^(?:[0-9]*(?:>>|>&|>\||<>|<<<|<<|<|>)|&>>|&>)$"
)
# Any punctuation-only token that is neither a segment break nor a redirection is
# shell grammar this parser does not model. Guessing there is how false greens
# get in, so such a script is abandoned entirely.
_PUNCTUATION_ONLY = re.compile(r"^[<>|&;()]+$")
# pytest options whose operand names a file that is explicitly NOT run.
_EXCLUDING_OPTIONS = {"--ignore", "--ignore-glob", "--deselect"}
# pytest modes that collect without executing anything.
_NON_EXECUTING_FLAGS = {
    "--collect-only", "--co", "--setup-only", "--setup-plan",
    "--fixtures", "--fixtures-per-test",
}
# pytest options are whitelisted, matching the strict policy used for shell and
# python: the real workflow uses exactly one (``-v``), so an unrecognised option
# means semantics nobody here has modelled — abandon rather than guess whether
# it suppresses execution.
_PYTEST_ZERO_ARITY = {
    "-v", "-vv", "-q", "-qq", "-x", "-s", "-ra", "-rA", "--no-header",
    "--disable-warnings", "--strict-markers",
}
_PYTEST_VALUE_TAKING = {"-p", "-k", "-m", "-n", "--maxfail", "--tb", "--junitxml"}
# Python options consumed BEFORE the script name. Zero-arity ones may be skipped;
# anything else (``-X opt``, ``-W spec``, an unknown flag) means the next token is
# not necessarily the script — ``python -X tests/a.py tests/b.py`` runs *b*, and
# a "skip dashes, take the first non-option" scan would credit *a*.
_PY_ZERO_ARITY = {
    "-b", "-bb", "-B", "-d", "-E", "-i", "-I", "-O", "-OO", "-P", "-q",
    "-s", "-S", "-u", "-v", "-vv", "-x", "-3",
}


def _is_terminator(line: str, operator: str, terminator: str) -> bool:
    """Bash ends a heredoc only on an exact terminator line.

    ``<<`` requires the word alone with no leading whitespace; ``<<-`` strips
    leading TABS only (never spaces).
    """
    return (line.lstrip("\t") if operator == "<<-" else line) == terminator


def _strip_heredocs(script: str) -> str | None:
    """Remove heredoc bodies; their lines are data, not commands.

    ``python - <<'PY' … PY`` embeds a whole program. Parsed as shell it looks
    like a pile of commands, so a ``pytest tests/x.py`` line *inside* the body
    would be credited as an execution. The real workflow has seven such steps.
    """
    out: list[str] = []
    lines = script.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        out.append(line)
        matches = list(_HEREDOC_START.finditer(line))
        if len(matches) != len(_HEREDOC_OPERATOR.findall(line)):
            # A heredoc whose delimiter form we do not recognise. Its body would
            # be read as commands, so give up on the script instead.
            return None
        terminators = [(m.group(1), m.group(2)) for m in matches]
        index += 1
        # ``cat <<A <<B`` queues two bodies in order; tracking only the first
        # left the second body being read as commands. And the terminator match
        # is exact for ``<<`` (bash only strips leading TABS, and only for
        # ``<<-``) — using .strip() ended the body at an indented ``EOF`` that
        # bash treats as data, handing the following lines back as commands.
        for operator, terminator in terminators:
            while index < len(lines) and not _is_terminator(
                lines[index], operator, terminator
            ):
                index += 1
            if index < len(lines):  # drop the terminator line too
                index += 1
    return "\n".join(out)


def _logical_lines(script: str) -> list[str] | None:
    """Split into command lines, quote-aware. ``None`` = give up on the script.

    Splitting per physical line was wrong in a way worth spelling out: when a
    quoted string spans lines, the line holding the opening quote fails to
    tokenise and gets skipped — and parsing then *resumes inside the string*,
    treating its remaining lines as commands. Skipping one line is not failing
    closed when the following lines are that line's data. So quote state is
    tracked across the whole script here, and an unterminated quote gives up on
    the entire script rather than on one line.

    A ``\\``-continuation is not a boundary (shlex turns it into a literal
    newline; treating that as a boundary once shredded the real workflow from
    452 covered files to 10). A bare newline is a boundary, as is a line ending
    in an operator continuing onto the next.
    """
    stripped = _strip_heredocs(script)
    if stripped is None:
        return None
    text = re.sub(r"\\\n\s*", " ", stripped)
    lines: list[str] = []
    current: list[str] = []
    quote: str | None = None
    escaped = False
    for char in text:
        if escaped:
            current.append(char)
            escaped = False
            continue
        if char == "\\" and quote != "'":
            current.append(char)
            escaped = True
            continue
        if quote:
            current.append(char)
            if char == quote:
                quote = None
            continue
        if char in "'\"":
            quote = char
            current.append(char)
            continue
        if char == "\n":
            lines.append("".join(current))
            current = []
            continue
        current.append(char)
    if quote is not None:
        return None  # unterminated quote — fail closed for the whole script
    lines.append("".join(current))

    joined: list[str] = []
    pending = ""
    for raw in lines:
        pending = f"{pending} {raw.strip()}".strip() if pending else raw.strip()
        if not pending:
            continue
        if _TRAILING_OPERATOR.search(pending):
            continue
        joined.append(pending)
        pending = ""
    if pending:
        joined.append(pending)
    return joined


def _tokenize(line: str) -> list[str] | None:
    """Shell-tokenise one logical line; ``None`` when it cannot be parsed.

    ``punctuation_chars`` makes shlex emit ``&&``/``||``/``|``/redirections as
    their own tokens instead of folding them into words, and ``commenters='#'``
    drops trailing comments while respecting quotes.
    """
    lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = "#"
    try:
        return list(lexer)
    except ValueError:
        return None


def _segments(tokens: Iterable[str]) -> Iterable[list[str]]:
    current: list[str] = []
    for token in tokens:
        if token in _SEGMENT_BREAKS:
            if current:
                yield current
            current = []
            continue
        current.append(token)
    if current:
        yield current


class UnsupportedShell(Exception):
    """Grammar this parser does not model — the caller abandons the script."""


def _drop_redirections(words: list[str]) -> list[str]:
    """Remove ``> file`` pairs; refuse punctuation we have not modelled."""
    kept: list[str] = []
    skip_next = False
    for word in words:
        if skip_next:
            skip_next = False
            continue
        if _REDIRECT.match(word):
            skip_next = True
            continue
        if _PUNCTUATION_ONLY.match(word):
            raise UnsupportedShell(word)
        kept.append(word)
    return kept


def _pytest_targets(args: list[str]) -> set[str]:
    """Files a pytest command actually executes.

    ``--collect-only`` runs nothing, and ``--ignore``/``--deselect`` operands are
    named precisely so they will *not* run; crediting either would recreate the
    "named but never executed" hole from the other direction.
    """
    targets: set[str] = set()
    skip_next = False
    non_executing = False
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if not arg.startswith("-"):
            if _TEST_FILE.match(arg):
                targets.add(arg)
            continue
        option = arg.split("=", 1)[0]
        if option in _NON_EXECUTING_FLAGS:
            non_executing = True
            continue
        if option in _EXCLUDING_OPTIONS:
            skip_next = "=" not in arg
            continue
        if option in _PYTEST_ZERO_ARITY:
            continue
        if option in _PYTEST_VALUE_TAKING:
            skip_next = "=" not in arg
            continue
        raise UnsupportedShell(option)
    return set() if non_executing else targets


def _executed_by_segment(segment: list[str]) -> set[str]:
    """Test files this one command executes (empty for every other command)."""
    words = _drop_redirections(segment)
    while words and _ENV_ASSIGNMENT.match(words[0]):
        words.pop(0)
    if not words:
        return set()

    command, args = words[0], words[1:]
    if _PYTEST.match(command):
        return _pytest_targets(args)
    if _PYTHON.match(command):
        if args[:2] == ["-m", "pytest"]:
            return _pytest_targets(args[2:])
        # ``python tests/test_api.py <url> --multi-tenant`` — a custom runner.
        # Only the script itself runs; later arguments are its own parameters.
        for arg in args:
            if not arg.startswith("-"):
                return {arg} if _TEST_FILE.match(arg) else set()
            if arg in _PY_ZERO_ARITY:
                continue
            # A value-taking or unknown pre-script option: the next token may be
            # its value rather than the script. Refuse instead of guessing.
            raise UnsupportedShell(arg)
    return set()


def _run_scripts(workflow: dict) -> Iterable[str]:
    for job in (workflow.get("jobs") or {}).values():
        for step in (job.get("steps") or []):
            if isinstance(step, dict) and isinstance(step.get("run"), str):
                yield step["run"]


def executed_in_script(script: str) -> set[str]:
    """Test files one ``run:`` script executes.

    Public so callers (e.g. the step-label check) do not reach into private
    parsing helpers and pin themselves to internals.
    """
    lines = _logical_lines(script)
    if lines is None:
        return set()
    found: set[str] = set()
    for line in lines:
        tokens = _tokenize(line)
        if tokens is None:
            # Unsupported syntax: give up on the WHOLE script. Skipping just this
            # line would let its continuation lines be read as commands.
            return set()
        if any(token in _CONTROL_KEYWORDS for token in tokens):
            # Stop here rather than abandoning the whole script: the real
            # workflow's one such step runs pytest *before* its later `if`
            # (measured), so a forward stop keeps genuine coverage while nothing
            # after an unmodelled construct is ever credited.
            break
        conditional = any(token in ("&&", "||") for token in tokens)
        for segment in _segments(tokens):
            try:
                executed = _executed_by_segment(segment)
            except UnsupportedShell:
                return set()
            if executed and conditional:
                # ``true || pytest x`` and ``false && pytest x`` both exit 0 with
                # pytest never running. Treating &&/|| as plain separators would
                # credit a file that is provably not executed — the very hole
                # this module closes. Reachability is not modelled, so a
                # conditional list containing pytest is abandoned. The real
                # workflow has none (measured), so this costs no real coverage.
                return set()
            found |= executed
    return found


def executed_test_files(workflow_path: Path = CI_WORKFLOW) -> set[str]:
    """Test files some CI step actually executes."""
    workflow = yaml.safe_load(workflow_path.read_text())
    found: set[str] = set()
    for script in _run_scripts(workflow):
        found |= executed_in_script(script)
    return found


def mentioned_test_files(workflow_path: Path = CI_WORKFLOW) -> set[str]:
    """Every filename appearing anywhere in the workflow — the OLD criterion.

    Kept so the gap between "named" and "executed" stays measurable rather than
    asserted.
    """
    return set(_TEST_FILE_ANYWHERE.findall(workflow_path.read_text()))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workflow", type=Path, default=CI_WORKFLOW)
    parser.add_argument(
        "--json", action="store_true", help="emit the covered set as JSON"
    )
    parser.add_argument(
        "--ghosts",
        action="store_true",
        help="list files named in the workflow but never executed",
    )
    args = parser.parse_args(argv)

    executed = executed_test_files(args.workflow)
    if args.ghosts:
        for name in sorted(mentioned_test_files(args.workflow) - executed):
            print(name)
        return 0
    if args.json:
        print(json.dumps(sorted(executed)))
        return 0
    for name in sorted(executed):
        print(name)
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main())
