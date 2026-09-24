#!/usr/bin/env python3
"""PostToolUse hook + CLI: catch DEAD, UNREGISTERED, and REAL-PATH-DEFAULT tests.

It checks three shapes of test. Tests appended after a custom
`if __name__ == "__main__"` runner never bind (the guard's SystemExit
halts execution first), and tests missing from a hand-maintained call
list never run. Both read as green from the outside; the suite total is
the only tell.

The third (Check C) is a test that calls a destructive helper without
overriding a parameter whose default resolves to the user's REAL data,
such as a backup directory. It is harmless while a guard in the helper
holds, and destroys real data the moment that guard is broken, for
example by a mutation run. A deterministic check catches this where a
written rule is easy to forget.

Wired twice in ~/.claude/settings.json PostToolUse:
- the `^(Write|Edit|MultiEdit)$` matcher (target = tool_input.file_path);
- a `^Bash$` matcher (anchored, so BashOutput never matches). Shell writes
  (heredocs, `>`/`>>`, tee) are how the dead tests actually got in; targets
  come from hooks/_bash_write_targets.py.

Advisory: emits hookSpecificOutput.additionalContext and exits 0. The
hookEventName field is mandatory or Claude Code silently drops the warning.

Also runs standalone: `test-integrity-check.py --file <path>` prints findings
to stdout and exits 1 when any exist (0 clean). pr-validator and one-shot
audits reuse THIS implementation instead of re-inventing the placement logic
(a grep re-imports the if-__name__-in-a-docstring false positive; AST does not).

Analysis runs on the final on-disk file (PostToolUse fires after the write).
Python 3.9 compatible (the system python3 may be Xcode's 3.9).
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
    # `tests.py` is DJANGO'S DEFAULT NAME, one per app. It matches neither the
    # `test_` prefix nor the `_test` suffix, so it is listed by name.
    return (base.startswith("test_") or base[:-3].endswith("_test")
            or base in ("tests.py", "test.py"))


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


_DESTRUCTIVE = {
    "rmtree", "remove", "unlink", "rmdir", "removedirs", "truncate", "replace",
}
_WRITE_MODES = ("w", "a", "x")


def _real_path_expr(node, consts, depth=0):
    """True if this expression resolves to a REAL filesystem path (not a temp one).

    Recognizes expanduser(...), absolute/'~' string literals, os.path.join rooted
    at one of those, and module-level constants assigned from any of the above.
    """
    if depth > 6 or node is None:
        return False
    if isinstance(node, ast.Call):
        name = node.func.attr if isinstance(node.func, ast.Attribute) else (
            node.func.id if isinstance(node.func, ast.Name) else "")
        if name == "expanduser":
            return True
        if name in ("join", "abspath", "realpath", "normpath") and node.args:
            return _real_path_expr(node.args[0], consts, depth + 1)
        return False
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value.startswith("/") or node.value.startswith("~")
    if isinstance(node, ast.Name) and node.id in consts:
        return _real_path_expr(consts[node.id], consts, depth + 1)
    return False


def _is_destructive_fn(fn):
    """True if the function body deletes, or opens something for writing."""
    for n in ast.walk(fn):
        if not isinstance(n, ast.Call):
            continue
        name = n.func.attr if isinstance(n.func, ast.Attribute) else (
            n.func.id if isinstance(n.func, ast.Name) else "")
        if name in _DESTRUCTIVE:
            return True
        if name == "open":
            mode = None
            if len(n.args) > 1 and isinstance(n.args[1], ast.Constant):
                mode = n.args[1].value
            for kw in n.keywords:
                if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                    mode = kw.value.value
            if isinstance(mode, str) and mode[:1] in _WRITE_MODES:
                return True
    return False


def _risky_params(mod_tree):
    """{func_name: [(index, param_name)]} for destructive functions whose
    parameter DEFAULT points at a real path."""
    consts = {}
    for node in mod_tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name):
            consts[node.targets[0].id] = node.value
    out = {}
    for node in mod_tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not _is_destructive_fn(node):
            continue
        params = [a.arg for a in node.args.args]
        defaults = node.args.defaults
        risky = []
        # defaults align to the TAIL of the positional parameter list
        offset = len(params) - len(defaults)
        for i, d in enumerate(defaults):
            if _real_path_expr(d, consts):
                risky.append((offset + i, params[offset + i]))
        for a, d in zip(node.args.kwonlyargs, node.args.kw_defaults):
            if _real_path_expr(d, consts):
                risky.append((None, a.arg))
        if risky:
            out[node.name] = risky
    return out


def _check_real_path_defaults(path, tree):
    """Check C: a TEST that calls a destructive function without overriding a
    parameter whose default points at the user's real filesystem.

    Example: a test calls archive_and_remove() without passing
    archive_dir, whose default is the real backup dir. Harmless while the
    guard holds (the test only exercises a REFUSAL path); if the guard is
    ever broken, the test overwrites the real backup with its own tar.
    """
    directory = os.path.dirname(os.path.abspath(path))
    modules, direct = {}, {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            sib = os.path.join(directory, node.module.split(".")[-1] + ".py")
            if os.path.isfile(sib):
                for a in node.names:
                    direct[a.asname or a.name] = (sib, a.name)
        elif isinstance(node, ast.Import):
            for a in node.names:
                sib = os.path.join(directory, a.name.split(".")[-1] + ".py")
                if os.path.isfile(sib):
                    modules[a.asname or a.name] = sib

    cache = {}

    def risky_for(sib):
        if sib not in cache:
            try:
                with open(sib, encoding="utf-8", errors="replace") as f:
                    cache[sib] = _risky_params(ast.parse(f.read()))
            except (OSError, SyntaxError):
                cache[sib] = {}
        return cache[sib]

    findings = []
    seen = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name) and node.func.id in direct:
            sib, real = direct[node.func.id]
        elif isinstance(node.func, ast.Attribute) and \
                isinstance(node.func.value, ast.Name) and \
                node.func.value.id in modules:
            sib, real = modules[node.func.value.id], node.func.attr
        else:
            continue
        risky = risky_for(sib).get(real)
        if not risky:
            continue
        # a **kwargs splat could supply anything; stay quiet rather than cry wolf
        if any(kw.arg is None for kw in node.keywords):
            continue
        given = {kw.arg for kw in node.keywords}
        for idx, pname in risky:
            if pname in given:
                continue
            if idx is not None and len(node.args) > idx:
                continue
            key = (real, pname, node.lineno)
            if key in seen:
                continue
            seen.add(key)
            findings.append(
                "REAL-PATH DEFAULT reached from a test: line %d calls %s() "
                "without passing %r, whose default in %s points at a real "
                "filesystem path, and %s deletes or overwrites. Pass an explicit "
                "throwaway path (tempfile.TemporaryDirectory). This is safe-looking "
                "when the call sits on a branch you expect to be REFUSED: a mutation "
                "run that disables the guard makes it write for real (2026-07-31, "
                "cost a 43MB backup)."
                % (node.lineno, real, pname, os.path.basename(sib), real)
            )
    return findings


# Check D: a test's prose claims its fixture is the REAL extreme, and no command ever
# checked. Measured 2026-08-20 on a real project (a code-reviewer finding):
# a read-time guard's fixture docstring said the sizes were "every field's observed
# maximum across all stored findings" and that "no real finding is this large". Five of
# the six fields were BELOW the real maximum and three real findings assembled a larger
# email than the fixture's 572 words, the largest at 617. So the guard was undersized in
# exactly the direction that matters, it was green, and the sentence beside it read as
# measured to everyone who opened the file, including the next session.
#
# A mutation harness cannot see this: it tests code against tests and tests no prose.
# The rule it belongs to already exists ("name the command that would falsify this
# sentence, and run it") and its worked example was a hand-written script nothing
# invokes. A rule that only exists as prose gets read once.
#
# The PAIR is what keeps this quiet. A superlative alone is ordinary test vocabulary
# ("asserts max_retries is 3") and a reality word alone is just context. Only a sentence
# claiming a superlative ABOUT REAL DATA is asserting a measurement.
_SUPERLATIVE_RE = re.compile(
    r"\b(maxim(?:um|a)|minim(?:um|a)|max|min|largest|biggest|longest|widest|greatest"
    r"|smallest|shortest|worst[- ]case|upper bound|lower bound)\b",
    re.IGNORECASE)
_REALITY_RE = re.compile(
    r"\b(real|actual|production|prod|live|corpus|stored|observed|on disk"
    r"|in the database|in the db)\b", re.IGNORECASE)
# A negative universal is the same claim worn inside out: "No single real finding is
# this large". It needs its own pair rule, and the first version of this one got BOTH
# halves wrong against the real file, which is why the replay had to be run rather than
# reasoned about. It required `no` immediately before the reality word, so the actual
# sentence ("no SINGLE real finding") did not match; and it asked for nothing else, so
# an ordinary repo convention ("no live network, mock at the seams") DID. It fired on
# the one sentence that was not a claim and stayed silent on the one that was.
_NO_REAL_RE = re.compile(
    r"\bno\b[^.]{0,24}?\b(?:real|actual|stored|production|live)\b"
    r"|\bnothing\b[^.]{0,24}?\b(?:real|in production|in the corpus|in the database)\b",
    re.IGNORECASE)
# ...and the size half, without which "no live network" is a finding.
_EXTREME_RE = re.compile(
    r"\b(?:this|as)\s+(?:large|big|long|wide|heavy|slow)\b"
    r"|\b(?:larger|bigger|longer|wider|heavier|slower|greater|exceeds?|over)\b",
    re.IGNORECASE)
# Sentences are split AFTER the block is joined into one line, never on the newline
# itself. A real sentence is often hard-wrapped, as in "No single real finding
# is this / large, so the fixture is already pessimistic", and a
# newline-splitting version would see "this" and "large" as different
# sentences, so the pair rule would never fire. Splitting on a period
# followed by whitespace also leaves `0.70` and `3.1` intact, which matters because
# these docstrings are full of measurements.
_SENTENCE_SPLIT = re.compile(r"\.\s+")
_COMMENT_RE = re.compile(r"^\s*#+\s?(.*)$")
# The way OUT of this finding, and the reason it is not just nagging. A claim that has
# been checked stays checked, so a span naming the command that would falsify it is
# exempt. That is the rule's actual instruction ("name the command that would falsify
# this sentence, and run it") turned into something the file itself records, which the
# next reader can re-run. Without an exemption the check fires hardest on the files
# that already did the work, and a warning that cannot be satisfied gets filtered out.
_FALSIFIER_RE = re.compile(
    r"\bfalsifier:|\bfalsified by\b|\bverified by (?:running|the command)\b"
    r"|\bmeasured by\b|\bre-?measure with\b", re.IGNORECASE)


def _corpus_claims(src, tree):
    """Yield (lineno, sentence) for prose asserting a fixture is the real extreme.

    Docstrings come from the AST, so a string merely assigned to a variable is not
    mistaken for prose. Comments are read from the source, because a `#` line has no
    AST node at all and is exactly where a fixture's justification gets written.
    """
    spans = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef,
                                 ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", None)
        if not body or not isinstance(body[0], ast.Expr):
            continue
        val = body[0].value
        if isinstance(val, ast.Constant) and isinstance(val.value, str):
            spans.append((getattr(val, "lineno", 1), val.value))
    # Consecutive comment lines are ONE span, for the same reason: a justification
    # written above a fixture wraps across several `#` lines, and reading them one at
    # a time splits the claim in half.
    block, start = [], None
    for i, line in enumerate(src.splitlines(), 1):
        m = _COMMENT_RE.match(line)
        if m:
            if start is None:
                start = i
            block.append(m.group(1))
            continue
        if block:
            spans.append((start, " ".join(block)))
            block, start = [], None
    if block:
        spans.append((start, " ".join(block)))

    seen = set()
    for lineno, text in spans:
        if _FALSIFIER_RE.search(text):
            continue
        # Split the RAW span. Normalizing whitespace here first would work too, and
        # that is the problem: with both in place the split pattern stops mattering,
        # a mutation putting `\n` back into it survives, and the test named for the
        # wrapped sentence passes either way. One defence, pinned by one test.
        for sentence in _SENTENCE_SPLIT.split(text):
            s = " ".join(sentence.split())
            if not s:
                continue
            paired = _SUPERLATIVE_RE.search(s) and _REALITY_RE.search(s)
            negative = _NO_REAL_RE.search(s) and (_EXTREME_RE.search(s)
                                                  or _SUPERLATIVE_RE.search(s))
            if paired or negative:
                key = (lineno, s[:120])
                if key not in seen:
                    seen.add(key)
                    yield key


def _expected_side_has_len(node):
    """True when this side is ARITHMETIC containing a len() call.

    A bare `len(x)` is not arithmetic and is not flagged: `assert len(rows) == 6`
    is a count being pinned, which is exactly right. What this hunts is a len()
    buried in a computed expected value, `9144 * 3 * len(job["results"])`.
    """
    if not isinstance(node, ast.BinOp):
        return False
    return any(
        isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "len"
        for n in ast.walk(node)
    )


def _is_bare_len(node):
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "len"
    )


def _check_corpus_scaled_expectations(tree):
    """Check E. An expected value that SCALES WITH THE CORPUS is a rate, not a bound.

    Measured 2026-09-07 on a real project. A wait budget was counted per PERSON where it
    should have been per BATCH, and the test guarding it read:

        assert sum(clock.slept) == 9144 * 3 * len(job["results"])

    The row count is IN the expected value, so the assertion AGREES with the defect
    and would go red if the defect were fixed. It survived a review round and a
    mutation run: the bound CONSTANT was mutated and correctly killed, which made the
    coverage look real, while nothing mutated where the counter lived. Its 2-row
    fixture is what made 45.7 hours read as bounded; at 6 rows the right and wrong
    rules give visibly different numbers.

    Deliberately narrow, because a static check CANNOT see whether a fixture separates
    two candidate rules. That was designed and REFUTED by measurement in 2026-08
    (in a fixture whose two candidate orderings AGREE). This asks
    the one question that IS statically visible: does the expected side scale with the
    input. Measured before shipping: 3 hits across 1,433 real test files.
    """
    findings = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assert):
            continue
        test = node.test
        if not (isinstance(test, ast.Compare) and len(test.ops) == 1):
            continue
        for side in (test.left, test.comparators[0]):
            other = test.comparators[0] if side is test.left else test.left
            # Two collections compared to each other is an ordinary size relation.
            if _expected_side_has_len(side) and not _is_bare_len(other):
                findings.append(
                    "EXPECTED VALUE SCALES WITH THE CORPUS at line %d. The expected "
                    "side of this assertion is arithmetic containing len(...), so it "
                    "grows with the input. If the thing under test is a BOUND, a "
                    "LIMIT, a BUDGET or a CAP, this asserts a RATE instead and will "
                    "agree with an off-by-scope defect rather than catching it "
                    "(2026-09-07, a real project: a per-batch wait budget implemented "
                    "per-person, guarded by a test that multiplied by the row count "
                    "and passed). If it really is a rate, say so in the test name or "
                    "docstring and move on. If it is a bound, the row count must not "
                    "appear, and the fixture needs more rows than the bound so the "
                    "right and wrong rules give different numbers."
                    % node.lineno
                )
                break
    return findings


def _check_corpus_claims(tree, src):
    claims = list(_corpus_claims(src, tree))
    if not claims:
        return []
    shown = "; ".join("line %d: %r" % (ln, s) for ln, s in claims[:3])
    more = "" if len(claims) <= 3 else " (+%d more)" % (len(claims) - 3)
    return [
        "UNVERIFIED CORPUS CLAIM in test prose: " + shown + more
        + ". This sentence asserts a MEASUREMENT against real data, and a docstring "
        "cannot fail. Run the command that would falsify it, which here means "
        "computing the real extreme from the corpus and comparing it against the "
        "fixture, then either keep the sentence because it held or narrow it to what "
        "this file actually pins. A fixture wrongly believed to be the worst case "
        "makes the guard UNDERSIZED and green in the one direction that matters "
        "(2026-08-20, measured on a real project)."
    ]


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

    # Checks C and D run on EVERY eligible test file, including pytest-style ones
    # that the runner-guard checks below deliberately skip.
    findings = list(_check_real_path_defaults(path, tree))
    findings.extend(_check_corpus_claims(tree, src))
    findings.extend(_check_corpus_scaled_expectations(tree))

    guard_idx = None
    guard = None
    for i, node in enumerate(tree.body):
        if isinstance(node, ast.If) and _is_main_guard(node.test):
            guard_idx, guard = i, node
            break
    if guard_idx is None:
        return ("ok", findings)  # pytest-style file; autodiscovery handles it
    if _calls_framework_main(guard):
        return ("ok", findings)

    # Check A: tests defined AFTER the custom runner guard are dead code.
    dead = [
        (n.name, n.lineno)
        for n in tree.body[guard_idx + 1:]
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and n.name.startswith("test_")
    ]
    # CLASSES too, not just module-level functions. unittest and Django put
    # every test inside a TestCase subclass, so in those codebases the dead
    # shape that actually occurs is a dead CLASS, and scanning only for
    # FunctionDef meant this check could not fire on the layout it most needed
    # to cover. Proved with a two-way control: a module-level function after
    # the guard was caught, an identical class after the guard was not.
    #
    # Only classes that CONTAIN a test method count, so a helper or a fixture
    # class defined after the guard is not flagged.
    dead += [
        (n.name, n.lineno)
        for n in tree.body[guard_idx + 1:]
        if isinstance(n, ast.ClassDef)
        and any(
            isinstance(b, (ast.FunctionDef, ast.AsyncFunctionDef))
            and b.name.startswith("test_")
            for b in n.body
        )
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


FIXTURE_STATE_DIR = os.environ.get("FIXTURE_EDIT_STATE_DIR") or os.path.expanduser(
    "~/.claude/hooks/.fixture-edit-seen")
FIXTURE_NOTE = (
    "FIXTURE EDIT in {path} (inside {name}): a fixture is a witness, not an obstacle. "
    "If a NEW check made tests fail, run the OLD fixture against the NEW code once and "
    "read the failure list: does production ever produce the shape the fixture had? "
    "If yes, the check is wrong, not the fixture. And would this fixture have to change "
    "when the product reaches a state it is designed to reach (a flag, a status, a "
    "rollout stage, a shipped config file)? Then it asserts today's release, not the "
    "contract. (Shown once per file per session.)")


def fixture_edit_function(path, old, new):
    """The helper a modifying Edit landed in, or None.

    Located in the saved file, never in old_string: an Edit can change lines
    inside a helper without touching its `def` line, so a text match on the
    edit alone would miss it. `main` is excluded
    because board-style test files keep every check inside it."""
    if not [l for l in old.splitlines() if l.strip() and l not in new.splitlines()]:
        return None
    try:
        src = open(path).read()
        tree = ast.parse(src)
    except Exception:
        return None
    at = src.find(new) if new else -1
    if at < 0:
        return None
    line = src[:at].count("\n") + 1
    enclosing = [n for n in ast.walk(tree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and n.lineno <= line <= (n.end_lineno or n.lineno)]
    if not enclosing:
        return None
    outer = max(enclosing, key=lambda n: (n.end_lineno or n.lineno) - n.lineno)
    if outer.name.startswith("test_") or outer.name == "main":
        return None
    return outer.name


def _first_fixture_note(session_id, path):
    """True the first time this session edits a fixture in `path`."""
    safe = "".join(c for c in str(session_id or "unknown") if c.isalnum() or c in "-_")
    state = os.path.join(FIXTURE_STATE_DIR, (safe or "unknown") + ".json")
    try:
        seen = json.load(open(state))
    except Exception:
        seen = []
    if path in seen:
        return False
    try:
        os.makedirs(FIXTURE_STATE_DIR, exist_ok=True)
        with open(state, "w") as fh:
            json.dump(seen + [path], fh)
    except Exception:
        pass
    return True


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
        if tool == "Edit":
            name = fixture_edit_function(path, tool_input.get("old_string") or "",
                                         tool_input.get("new_string") or "")
            if name and _first_fixture_note(data.get("session_id"), path):
                messages.append(FIXTURE_NOTE.format(path=path, name=name))
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
