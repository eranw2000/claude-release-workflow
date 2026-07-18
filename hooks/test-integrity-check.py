#!/usr/bin/env python3
"""PostToolUse hook + CLI: catch DEAD and UNREGISTERED tests deterministically.

Two failure shapes drive this. Tests appended after a custom
`if __name__ == "__main__"` runner never bind (the guard's SystemExit halts
execution first), and tests missing from a hand-maintained call list silently
never run. Both read as green from the outside; the suite total is the only
tell, and it is easy to misread.

Wired twice in ~/.claude/settings.json PostToolUse:
- the `^(Write|Edit|MultiEdit)$` matcher (target = tool_input.file_path);
- a `^Bash$` matcher (anchored, so BashOutput never matches). Shell writes
  (heredocs, `>`/`>>`, tee) are how the dead tests actually get in; targets
  come from hooks/_bash_write_targets.py.

Advisory: emits hookSpecificOutput.additionalContext and exits 0. The
hookEventName field is mandatory or Claude Code silently drops the warning.

Also runs standalone: `test-integrity-check.py --file <path>` prints findings
to stdout and exits 1 when any exist (0 clean). A PR validator and one-shot
audits can reuse THIS implementation instead of re-inventing the placement
logic (a grep re-imports the if-__name__-in-a-docstring false positive; AST
does not).

Analysis runs on the final on-disk file (PostToolUse fires after the write).
Python 3.9 compatible (the system python3 may be older than 3.10).
"""

import ast
import json
import os
import re
import sys

try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _bash_write_targets import extract_write_targets
except Exception:  # missing sibling module: Bash branch degrades to no-op
    extract_write_targets = None

UNPARSEABLE = "UNPARSEABLE"
_SKIP_DIRS = {".venv", "venv", "node_modules", "site-packages", ".git"}


def eligible(path):
    base = os.path.basename(path)
    if base == "conftest.py" or not base.endswith(".py"):
        return False
    if _SKIP_DIRS.intersection(path.split(os.sep)):
        return False
    return base.startswith("test_") or base[:-3].endswith("_test")


def _is_main_guard(test):
    """AST match for `__name__ == "__main__"` (either operand order)."""
    if not isinstance(test, ast.Compare) or len(test.ops) != 1:
        return False
    if not isinstance(test.ops[0], ast.Eq):
        return False
    operands = [test.left] + list(test.comparators)
    names = [o.id for o in operands if isinstance(o, ast.Name)]
    consts = [o.value for o in operands if isinstance(o, ast.Constant)]
    return "__name__" in names and "__main__" in consts


def _calls_framework_main(guard):
    """True if the guard body calls pytest.main / unittest.main: those
    re-collect the file themselves, so definition placement cannot kill a
    test and the file needs no checks."""
    for node in ast.walk(guard):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "main" and isinstance(node.func.value, ast.Name):
                if node.func.value.id in ("pytest", "unittest"):
                    return True
    return False


def _has_globals_scan(tree):
    """True on a real autodiscovery SCAN: globals().items() / .values().
    The mere presence of globals() is NOT enough: `globals()[name]()` over a
    string list is a hand-list runner and must still get Check B."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in ("items", "values", "keys"):
                inner = node.func.value
                if (isinstance(inner, ast.Call)
                        and isinstance(inner.func, ast.Name)
                        and inner.func.id == "globals"):
                    return True
    return False


def analyze(path):
    """Return (status, findings) for one on-disk test file."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            src = f.read()
    except OSError:
        return ("ok", [])
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        return (UNPARSEABLE, ["line %s: %s" % (e.lineno, e.msg)])

    guard_idx = None
    guard = None
    for i, node in enumerate(tree.body):
        if isinstance(node, ast.If) and _is_main_guard(node.test):
            guard_idx, guard = i, node
            break
    if guard_idx is None:
        return ("ok", [])  # pytest-style file; autodiscovery handles it
    if _calls_framework_main(guard):
        return ("ok", [])

    findings = []

    # Check A: tests defined AFTER the custom runner guard are dead code.
    dead = [
        (n.name, n.lineno)
        for n in tree.body[guard_idx + 1:]
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and n.name.startswith("test_")
    ]
    if dead:
        names = ", ".join("%s (line %d)" % (n, ln) for n, ln in dead)
        findings.append(
            "DEAD tests defined AFTER the __main__ runner guard: " + names
            + ". The guard's exit halts execution before these defs bind, so "
            "auto-discovery can never see them. Move them ABOVE the runner "
            "guard, then confirm the printed suite total INCREASED by the "
            "number added."
        )
    dead_names = {n for n, _ in dead}

    # Check B (best effort): unregistered test in a hand-list runner.
    if not _has_globals_scan(tree):
        tests = [
            n.name for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and n.name.startswith("test_")
        ]
        counts = {
            name: len(re.findall(r"\b" + re.escape(name) + r"\b", src))
            for name in tests
        }
        invoked_by_name = (
            any(c >= 2 for c in counts.values()) or "globals()[" in src
        )
        if invoked_by_name:
            unreg = [
                name for name, c in counts.items()
                if c == 1 and name not in dead_names
            ]
            if unreg:
                findings.append(
                    "UNREGISTERED tests in a hand-list runner file: "
                    + ", ".join(sorted(unreg))
                    + ". Each is defined but its name appears nowhere else in "
                    "the file, so the runner never calls it. Register it in "
                    "the hand-maintained list and confirm the printed suite "
                    "total INCREASED."
                )

    return ("ok", findings)


def _emit(messages):
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": " | ".join(messages),
        }
    }))


def run_cli(path):
    if not os.path.isfile(path):
        print("no such file: " + path)
        return 1
    status, findings = analyze(path)
    if status == UNPARSEABLE:
        print("UNPARSEABLE %s: %s" % (path, "; ".join(findings)))
        return 1
    for f in findings:
        print("%s: %s" % (path, f))
    return 1 if findings else 0


def run_hook():
    try:
        data = json.load(sys.stdin)
    except Exception:
        return
    if not isinstance(data, dict):
        return
    tool = data.get("tool_name") or ""
    tool_input = data.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return

    via_bash = False
    if tool in ("Write", "Edit", "MultiEdit"):
        paths = [tool_input.get("file_path") or ""]
    elif tool == "Bash":
        command = tool_input.get("command") or ""
        # Fast bail before any regex work.
        if (">" not in command and "tee" not in command) or extract_write_targets is None:
            return
        paths = extract_write_targets(command, data.get("cwd"))
        via_bash = True
    else:
        return

    messages = []
    for path in paths:
        if not path or not eligible(path) or not os.path.isfile(path):
            continue
        status, findings = analyze(path)
        if status == UNPARSEABLE:
            # Mid-edit syntax states are normal on the Edit branch; a shell
            # write that breaks a test file is near-certainly a heredoc
            # mis-quote and gets flagged.
            if via_bash:
                messages.append(
                    "TEST-INTEGRITY CHECK on " + path + ": a shell write left "
                    "this test file UNPARSEABLE (" + "; ".join(findings) + "). "
                    "Add tests with Edit/Write, not shell redirection."
                )
        elif findings:
            messages.append(
                "TEST-INTEGRITY CHECK on " + path + ": " + " ".join(findings)
                + (" (This file was written via shell redirection; prefer "
                   "Edit/Write for test files.)" if via_bash else "")
            )
    if messages:
        _emit(messages)


def main():
    if "--file" in sys.argv:
        idx = sys.argv.index("--file")
        if idx + 1 >= len(sys.argv):
            print("usage: test-integrity-check.py --file <path>")
            sys.exit(2)
        sys.exit(run_cli(sys.argv[idx + 1]))
    try:
        run_hook()
    except Exception:
        pass  # advisory hook: never break the tool call
    sys.exit(0)


if __name__ == "__main__":
    main()
