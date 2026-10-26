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
    from _bash_write_targets import extract_write_targets, strip_heredocs
except Exception:  # missing sibling module: Bash branch degrades to no-op
    extract_write_targets = None
    strip_heredocs = None

# A shell APPEND, read outside heredoc bodies. Appending is how tests get ADDED by shell.
_APPEND_RE = re.compile(r">>|\btee\s+(?:-a\b|--append\b)")

APPEND_NOTE = (
    "TEST-INTEGRITY CHECK on {path}: tests were APPENDED with shell redirection. Add "
    "tests with Edit/Write: this hook's Bash branch is the backstop, not the license.")

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
    """Yield (lineno, sentence) for prose asserting a fixture is the real extreme."""
    seen = set()
    for lineno, text in _prose_spans(src, tree):
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


# Check D2: test prose claims how ANOTHER test runs: a docstring saying "earlier
# tests start real threads that deadlock" is false when those tests run inline on
# the test thread. Same rule as Check D, a
# runtime claim nobody ran, aimed at a different test. The ORDER is what keeps it quiet:
# the other test is named FIRST and the runtime word follows, so "every read runs on a
# thread (the half-done case is `XTests`)" is about this test and stays silent.
_RUNTIME_CLAIM_RE = re.compile(
    r"(?:\b[A-Z][A-Za-z0-9]*Tests\b|(?i:\b(?:earlier|other|later|previous|preceding)"
    r" tests?\b|\btests elsewhere\b))"
    r".*?\b(?:hang|hangs|hung|hanging|deadlock\w*|threads?"
    r"|runs? (?:first|before|after|last))\b")


def _runtime_claims(src, tree):
    """Yield (lineno, sentence) for prose saying how another test runs."""
    seen = set()
    for lineno, text in _prose_spans(src, tree):
        if _FALSIFIER_RE.search(text):
            continue
        for sentence in _SENTENCE_SPLIT.split(text):
            s = " ".join(sentence.split())
            if s and _RUNTIME_CLAIM_RE.search(s):
                key = (lineno, s[:160])
                if key not in seen:
                    seen.add(key)
                    yield key


def _prose_spans(src, tree):
    """(lineno, text) for every docstring and every block of `#` comment lines.

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
    return spans


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


def _check_runtime_claims(tree, src):
    claims = list(_runtime_claims(src, tree))
    if not claims:
        return []
    shown = "; ".join("line %d: %r" % (ln, s) for ln, s in claims[:3])
    more = "" if len(claims) <= 3 else " (+%d more)" % (len(claims) - 3)
    return [
        "UNVERIFIED RUNTIME CLAIM about another test: " + shown + more
        + ". This sentence says how ANOTHER test runs (its thread, a hang, a "
        "deadlock, its order), and a docstring cannot fail. Run the command that "
        "would falsify it before keeping it: read that test's set-up for what it "
        "patches (an inline stand-in runs on the calling thread), or run the module "
        "and watch. Then write the command into the sentence ('verified by "
        "running ...') or narrow it to what you checked "
        "first."
    ]


# Check E: a test that READS its input from one background job's scratch folder
# (`~/.claude/jobs/<8 hex>/`). That folder is deleted with the job, so the test later
# crashes or silently loses the case, and a crash takes every other case in the file
# with it. Only a READ is flagged: a path handed to the code under test as data, or a
# folder the test writes into itself, is fine.
_SCRATCH_PATH = re.compile(r"/\.claude/jobs/[0-9a-f]{8}/")
_SCRATCH_READERS = {"open", "Path", "PurePath", "listdir", "scandir", "walk", "glob",
                    "iglob", "copy", "copy2", "copyfile", "copytree", "load", "loads",
                    "read_text", "read_bytes"}


def _call_name(func):
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _is_sys_path_add(func):
    return (isinstance(func, ast.Attribute) and func.attr in ("insert", "append")
            and isinstance(func.value, ast.Attribute) and func.value.attr == "path"
            and isinstance(func.value.value, ast.Name) and func.value.value.id == "sys")


def _check_scratch_reads(tree):
    def scratch_in(node, tainted):
        for sub in ast.walk(node):
            if (isinstance(sub, ast.Constant) and isinstance(sub.value, str)
                    and _SCRATCH_PATH.search(sub.value)):
                return sub.value
            if isinstance(sub, ast.Name) and sub.id in tainted:
                return tainted[sub.id]
        return None

    # A name bound to a scratch path, or to an expression built from one
    # (os.path.join(BASE, "x")), is tainted. Iterate to a fixed point so the order
    # of assignments does not matter.
    tainted = {}
    assigns = [n for n in ast.walk(tree) if isinstance(n, ast.Assign)]
    changed = True
    while changed:
        changed = False
        for node in assigns:
            hit = scratch_in(node.value, tainted)
            if not hit:
                continue
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id not in tainted:
                    tainted[target.id] = hit
                    changed = True

    findings, seen = [], set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not (_call_name(node.func) in _SCRATCH_READERS or _is_sys_path_add(node.func)):
            continue
        hit = None
        for arg in list(node.args) + [k.value for k in node.keywords]:
            hit = scratch_in(arg, tainted)
            if hit:
                break
        if hit and (node.lineno, hit) not in seen:
            seen.add((node.lineno, hit))
            findings.append(
                "SCRATCH-PATH READ at line %d: the test reads %r, inside one background "
                "job's scratch folder. That folder is deleted with the job, so this test "
                "will later crash or lose the case, and a crash stops every other case in "
                "the file. Commit the input beside the test (a fixtures folder) and read "
                "it from there." % (node.lineno, hit))
    return findings


# Check G: a Django test that only checks what a page does NOT show, for text that no
# test in the file ever shows present. assertNotContains requires a 200, but it passes on
# any 200 page that never renders the subject (another mode, another state), so it cannot
# tell "fixed" from "not there". A presence check proves the page can show it: in the same
# test, or a contrast twin in the same file that asserts the same text present.
_ABSENCE_ONLY = {"assertNotContains"}
_PRESENCE = {"assertContains", "assertInHTML", "assertTemplateUsed", "assertRedirects"}


def _text_arg(call):
    """The literal text an assertContains/assertNotContains looks for, or None."""
    arg = call.args[1] if len(call.args) > 1 else next(
        (k.value for k in call.keywords if k.arg == "text"), None)
    return arg.value if isinstance(arg, ast.Constant) and isinstance(arg.value, str) else None


def _needle_arg(call):
    """The literal text an assertIn(text, page) looks for, or None (a variable needle proves nothing)."""
    arg = call.args[0] if call.args else None
    return arg.value if isinstance(arg, ast.Constant) and isinstance(arg.value, str) else None


def _check_absence_only(tree):
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
    shown = {_text_arg(c) for c in calls if _call_name(c.func) == "assertContains"}
    # assertIn('<text>', page) on the decoded page shows the text present too
    # (assertIn('id="busy"', page) then assertNotContains is not absence-only).
    shown |= {_needle_arg(c) for c in calls if _call_name(c.func) == "assertIn"}
    findings = []
    for fn in ast.walk(tree):
        if not (isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and fn.name.startswith("test")):
            continue
        mine = [n for n in ast.walk(fn) if isinstance(n, ast.Call)]
        if _PRESENCE.intersection(_call_name(c.func) for c in mine):
            continue
        # Shown when a sibling shows it present, alone or INSIDE a longer text: a page showing
        # "skipped 2 files" also shows "skipped". One way only: the absent text inside a shown
        # one, never a shown text inside the absent one.
        never = [t for t in (_text_arg(c) for c in mine if _call_name(c.func) in _ABSENCE_ONLY)
                 if t is not None and not any(s and t in s for s in shown)]
        if never:
            findings.append(
                "ABSENCE-ONLY at line %d: %s checks only that %r is NOT on the page, and no "
                "test in this file shows a page that has it. A page that never renders it "
                "(another mode, another state) passes. Add an assertContains for something "
                "that must be there, such as the field's label, or a contrast test where it "
                "appears." % (fn.lineno, fn.name, never[0]))
    return findings


# The page-text variable names an ordering check reads, and what a bare name looks like.
# Few ordering assertions over a page string use a bare-name needle, so the
# check is quiet.
_PAGE_VARS = frozenset({"body", "html", "text", "page", "content", "markup", "rendered"})
_BARE_NAME = re.compile(r"^[A-Za-z_][\w-]*$")
_ORDERING = frozenset({"assertLess", "assertGreater", "assertLessEqual", "assertGreaterEqual"})


def _page_index_needles(node):
    """Literal needles of `<page>.index('...')` calls inside one assertion argument."""
    out = []
    for sub in ast.walk(node):
        if not (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)
                and sub.func.attr in ("index", "find") and sub.args
                and isinstance(sub.args[0], ast.Constant) and isinstance(sub.args[0].value, str)):
            continue
        owner = sub.func.value
        on_page = (isinstance(owner, ast.Name) and owner.id in _PAGE_VARS) or (
            isinstance(owner, ast.Call) and _call_name(owner.func) == "decode")
        if on_page:
            out.append(sub.args[0].value)
    return out


def _check_bare_name_order(tree):
    """An ORDER asserted on a page by finding a bare name.

    A bare name can sit in a style rule, a script or an attribute anywhere above
    the element, so the order measured may be the stylesheet's; a needle with
    markup syntax (`<h2 class="title`) cannot.
    """
    findings = []
    for call in ast.walk(tree):
        if not (isinstance(call, ast.Call) and _call_name(call.func) in _ORDERING):
            continue
        bare = [n for a in call.args for n in _page_index_needles(a) if _BARE_NAME.match(n)]
        if bare:
            findings.append(
                "BARE-NAME ORDER at line %d: this orders the page by finding %r, a bare name "
                "that a <style> rule, a script or an attribute higher up the page can also "
                "hold, so the order measured may be the stylesheet's, not the element's. "
                "Search for markup instead, such as '<h2 class=\"%s'." % (call.lineno, bare[0], bare[0]))
    return findings


# A test NAME that claims what the page shows, and the reads that would show it.
# It fires only when every assertion reads the request and none reads the page.
_RENDER_NAME = re.compile(r"(drawn|draws|rendered|renders|render_|shown|shows|displayed|appears)")
_PAGE_ATTRS = frozenset({"content", "context", "text", "json", "rendered_content"})
_PAGE_CALLS = frozenset({"assertContains", "assertNotContains", "assertTemplateUsed",
                         "assertInHTML", "assertRedirects", "assertTemplateNotUsed"})


def _reads_request(node):
    """`response.wsgi_request.X` or `request.X`: a fact about the request, not the page."""
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Attribute):
            continue
        chain, n = [], sub
        while isinstance(n, ast.Attribute):
            chain.append(n.attr)
            n = n.value
        if "wsgi_request" in chain or (isinstance(n, ast.Name) and n.id == "request"):
            return True
    return False


def _reads_page(fn):
    for sub in ast.walk(fn):
        if isinstance(sub, ast.Attribute) and sub.attr in _PAGE_ATTRS:
            return True
        if isinstance(sub, ast.Call):
            name = _call_name(sub.func) or ""
            if name in _PAGE_CALLS or "render" in name:
                return True
    return False


def _check_render_name_request_only(tree):
    """A test whose name says what the page shows, asserting only the request.

    A test named "is drawn in <language>" that asserts only a locale attribute of
    the request stays green when the translation is never activated, because
    that attribute is set on its own line.
    A request attribute is set apart from what draws the page, so it cannot see the page.
    """
    findings = []
    for fn in ast.walk(tree):
        if not (isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
                and fn.name.startswith("test") and _RENDER_NAME.search(fn.name)):
            continue
        asserts = [c for c in ast.walk(fn) if isinstance(c, ast.Call)
                   and (_call_name(c.func) or "").startswith("assert")]
        if not asserts or _reads_page(fn):
            continue
        if any(_reads_request(c) for c in asserts):
            findings.append(
                "RENDER-NAME, REQUEST-ONLY at line %d: %s says what the page shows, but its "
                "assertions read only the request (wsgi_request / request attributes), never "
                "the page. A request attribute is set apart from what draws the page, so a "
                "page drawn wrong passes. Assert on the rendered page (response.content, "
                "assertContains, the context), or record what is active at render time."
                % (fn.lineno, fn.name))
    return findings


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

    # Checks C, D and E run on EVERY eligible test file, including pytest-style ones
    # that the runner-guard checks below deliberately skip.
    findings = list(_check_real_path_defaults(path, tree))
    findings.extend(_check_scratch_reads(tree))
    findings.extend(_check_corpus_claims(tree, src))
    findings.extend(_check_runtime_claims(tree, src))
    findings.extend(_check_corpus_scaled_expectations(tree))
    findings.extend(_check_absence_only(tree))
    findings.extend(_check_bare_name_order(tree))
    findings.extend(_check_render_name_request_only(tree))

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


def _once_scope(payload):
    """The key a "once per session" warning is remembered under: the session, plus the agent
    when a subagent is the caller. A subagent's hooks carry the parent's session_id with
    `agent_id` set (Claude Code hooks docs).
    So the key adds the agent, and a helper's warning is kept apart from the main
    thread's.
    """
    session = str(payload.get("session_id") or "")
    agent = payload.get("agent_id")
    return session + "@" + str(agent) if session and agent else session


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


# Check F: a script that hands out PASS/FAIL verdicts, wherever it lives (a tools/
# script is not a test file, so Checks A to E never see it).
# A check that cannot fail reads exactly like one that passed. This fires the
# first time a PASS/FAIL script is written in a session, which is the moment a
# control is cheapest to add.
VERDICT_STATE_DIR = os.environ.get("VERDICT_STATE_DIR") or os.path.expanduser(
    "~/.claude/hooks/.verdict-script-seen")
_VERDICT_TOKENS = (re.compile(r"""["']PASS["']"""), re.compile(r"""["']FAIL["']"""))
VERDICT_NOTE = (
    "VERIFICATION SCRIPT {path} hands out PASS/FAIL ({n} verdict line(s): {lines}). "
    "Before trusting any PASS it prints, give EACH check a control that MUST FAIL and run "
    "it once: the old code (run from its own folder, or `python -m` loads the new one), a "
    "mutation of the guarded code, or an injected bad sample. Ask per check: could this "
    "PASS if the behaviour were broken? (timing that never races, a file read before it "
    "can exist, a kill that takes the case it should test with it). "
    "(Shown once per file per session.)")


def verdict_lines(src):
    """Line numbers carrying a quoted PASS or FAIL token, or [] when the file lacks either."""
    if not all(t.search(src) for t in _VERDICT_TOKENS):
        return []
    return [i for i, line in enumerate(src.splitlines(), 1)
            if any(t.search(line) for t in _VERDICT_TOKENS)]


def _first_seen(state_dir, session_id, path):
    """True the first time this session meets `path` in `state_dir`."""
    safe = "".join(c for c in str(session_id or "unknown") if c.isalnum() or c in "-_")
    state = os.path.join(state_dir, (safe or "unknown") + ".json")
    try:
        seen = json.load(open(state))
    except Exception:
        seen = []
    if path in seen:
        return False
    try:
        os.makedirs(state_dir, exist_ok=True)
        with open(state, "w") as fh:
            json.dump(seen + [path], fh)
    except Exception:
        pass
    return True


def verdict_note(path, session_id):
    if not path.endswith(".py") or _SKIP_DIRS.intersection(path.split(os.sep)):
        return None
    try:
        src = open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return None
    lines = verdict_lines(src)
    if not lines or not _first_seen(VERDICT_STATE_DIR, session_id, path):
        return None
    shown = ", ".join(str(n) for n in lines[:6]) + (" ..." if len(lines) > 6 else "")
    return VERDICT_NOTE.format(path=path, n=len(lines), lines=shown)


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
    appended = False
    if tool in ("Write", "Edit", "MultiEdit"):
        paths = [tool_input.get("file_path") or ""]
    elif tool == "Bash":
        command = tool_input.get("command") or ""
        # Fast bail before any regex work.
        if (">" not in command and "tee" not in command) or extract_write_targets is None:
            return
        paths = extract_write_targets(command, data.get("cwd"))
        via_bash = True
        appended = bool(_APPEND_RE.search(
            strip_heredocs(command)[0] if strip_heredocs else command))
    else:
        return

    messages = []
    for path in paths:
        if path and os.path.isfile(path):
            note = verdict_note(path, _once_scope(data))
            if note:
                messages.append(note)
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
        elif via_bash and appended:
            # Warn on a clean append too.
            messages.append(APPEND_NOTE.format(path=path))
        if tool == "Edit":
            name = fixture_edit_function(path, tool_input.get("old_string") or "",
                                         tool_input.get("new_string") or "")
            if name and _first_fixture_note(_once_scope(data), path):
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
