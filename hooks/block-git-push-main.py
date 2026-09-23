#!/usr/bin/env python3
"""Block a raw `git push` that would land on main/master, redirecting to /release.

Reached through the thin `block-git-push-main.sh` delegator, which is what
~/.claude/settings.json wires up. Protocol: read the tool-call envelope as JSON
on stdin; a PreToolUse hook blocks ONLY via exit code 2 with the reason on
stderr (the stdout {"decision":"block"} form is NOT honored for PreToolUse and
fails OPEN, confirmed against https://code.claude.com/docs/en/hooks.md).

WHY THIS REPLACED THE SHELL VERSION (2026-08-18)
The shell hook tested the RAW command text for the words "git push", so any
command that merely CONTAINED that phrase was treated as a push. Three measured
false positives in one session, none of them a push:
  1. grep -rniE 'remote|git push|ls-remote' <file>   -> the phrase sat inside a
     SEARCH PATTERN. The block then said "bare push while on main", because the
     config repo happens to be on main.
  2. A heredoc that WROTE a script whose text contains a push line.
  3. Prose. The second layer matched the comment "...from the push output." at
     end of line: `push` + one word + end-of-segment looked like a bare push.

The decision now comes from a quote-aware PARSE, shared with the other guards
in _bash_command_parse.py so the next guard does not have to rediscover it.

Also closed two holes the shell version had:
  - `bash -c "git push origin main"` was allowed. It is now parsed.
  - `git push origin HEAD:refs/heads/main` was allowed. Now normalized.
And one deliberate LOOSENING: `git push origin main:staging` no longer blocks,
because the destination side is staging, so it does not land on main.

Deliberately unchanged: a bare push resolves the target repo's current branch,
because `git push` with no refspec lands on main without the word appearing.

THREE HOLES CLOSED 2026-09-11 (review item F04 / plan item A5)
  1. `git push --all` and `git push --mirror` send every local branch, so they
     land on a protected branch whatever branch happens to be checked out. They
     used to read as a bare push here and were caught only when the current
     branch was main, exactly as the shell version behaved. They are now
     blocked outright unless the bypass marker is present.
  2. The bypass marker is read from the PARSE, not from the raw text. It used
     to be one regex over the whole command, so any quoted string carrying the
     words handed out the bypass: a commit message, a `git config` value, a
     grep pattern. The shared tokenizer DROPS a real comment and keeps a quoted
     string as one token with its quotes removed, so the marker counts only
     when the command text carries it at a word boundary and NO token does.
  3. The protected set is main and master everywhere, as before, PLUS the
     branch that `refs/remotes/<remote>/HEAD` names in the target repo when
     that local ref exists, so a repo whose default branch is `develop` is
     covered too. Reading it is one `git symbolic-ref` on a local ref, cached
     per repo and remote, asked only after a push is detected and only when the
     destination is not already main or master. It never touches the network.

FOUR MORE HOLES CLOSED 2026-09-11 (review round A, items P1 to P4)
  1. `git push origin HEAD` and `git push origin @` read as a push to a branch
     literally called HEAD, which matches nothing, while layer 2 never ran
     because a refspec WAS present. Both lands on whatever branch HEAD names,
     so a source-only HEAD or @ is now resolved against the target repo's
     current branch before the protected tests. A `HEAD:<dst>` refspec was
     always handled, because the destination side is written down.
  2. Three more ways to push every matching branch without naming one.
     `--branches` is an alias of `--all` in this git (`git push -h` says so)
     and joins the everything set. A bare `:` is the matching-branches
     refspec, and a wildcard destination (`refs/heads/*:refs/heads/*`) can
     expand onto a protected branch, so both are treated like `--all`.
  3. The marker is now a comment of ONE LINE and authorizes only the pushes
     on that line. It used to be a boundary search over the whole command,
     so a marker the tokenizer happened to drop ANYWHERE handed out the
     bypass for every push in the payload: inside backticks after the push,
     inside a `$( )` on an earlier line, or on a line of its own. The line's
     substitution bodies and quoted strings are blanked before the search,
     which is what makes a comment belonging to another command context
     invisible here. `#allow-push-maintenance` also matched, because the
     pattern had no END boundary; it does now.
  4. The mirror-image defect, and it REFUSED a sanctioned push:
     `git push origin main;#allow-push-main`. bash reads a `#` straight
     after an operator as a comment, and the pattern accepted only start of
     text or whitespace in front of it. An operator now counts too.

BYPASS: append #allow-push-main to the command, as a real trailing comment
outside any quotes, ON THE SAME LINE as the push it authorizes. That is the
only route reachable from inside a session.
CLAUDE_ALLOW_PUSH_MAIN=1 also works, but ONLY when it is already exported in
the environment Claude Code was launched in: a `VAR=1 cmd` prefix is shell
syntax evaluated when the command runs, and this hook decides before that, so
it never sees it. Shell state does not persist between Bash calls either, so an
`export` in an earlier call does not reach it.
"""
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from _bash_command_parse import (           # noqa: E402
    UnparseableCommand, cd_target, git_call, nested_shell_command,
    positional_args, split_segments, strip_heredocs, strip_prefixes, tokenize,
)

PROTECTED = ("main", "master")

# `git push` flags that consume the NEXT token as their value. Deliberately NOT
# --force-with-lease: it is either bare or `=<ref>`, never a separate token, and
# listing it here would swallow the remote and miss an explicit `main`.
PUSH_VALUE_FLAGS = {"--repo", "-o", "--push-option"}

# Flags that push every branch, so the destination is not in the command text.
# --branches is an alias of --all; `git push -h` says so in this git.
PUSH_EVERYTHING_FLAGS = {"--all", "--branches", "--mirror"}

# A source-only refspec that means "the branch HEAD names", which has to be
# read from the target repo. `git push --dry-run --porcelain origin HEAD` on a
# repo checked out on master prints `HEAD:refs/heads/master`.
HEAD_ALIASES = ("HEAD", "@")

MARKER = "#allow-push-main"
# The marker has to be a WHOLE comment word. A `#` glued to the end of another
# word is not a comment to bash either, and accepting it there let
# `main#allow-push-main` read as a bypass. The END boundary matters just as
# much: without it `#allow-push-maintenance` handed out the bypass. An
# OPERATOR in front of the `#` counts, because bash reads
# `git push origin main;#allow-push-main` as a push plus a comment.
MARKER_AS_COMMENT = re.compile(
    r"(?:^|(?<=[\s;&|()]))" + re.escape(MARKER) + r"(?=\s|$)")

# A line whose last operator is one of these carries on into the next line,
# because bash waits for the right-hand side. Tested against the BLANKED line,
# so a `|` inside a quoted pattern cannot be read as a continuation.
CONTINUES_LINE = re.compile(r"(?:&&|\|\||\||&|\\)[ \t]*$")

# A remote NAME. A URL or a path ("git@github.com:o/r.git", "../other") can
# never be half of a local refs/remotes/<remote>/HEAD ref, so recognising the
# name shape here saves a subprocess that could only fail.
REMOTE_NAME = re.compile(r"^[A-Za-z0-9._][A-Za-z0-9._/-]*$")

RAW_COMMAND = ""

# The raw-text fallback, used only once a parse has already failed. No distance
# cap and it crosses newlines, so a long option or a continued line cannot carry
# a push past it. Same shape as secret-scan-git.py's.
LOOKS_LIKE_PUSH_RE = re.compile(r"\bgit\b[\s\S]*?\bpush\b")

# (directory, remote) -> branch name or None. One subprocess per pair per call.
_REMOTE_HEAD_CACHE = {}


def top_level_lines(text):
    """The command's LINES, each as a (raw, blanked) pair.

    A line break counts only when the newline is outside quotes and outside a
    `$( )` or backtick substitution, and a line ending in a continuation
    operator carries on into the next one, because bash does. So the pairs are
    the units bash would apply a `#` comment to.

    `blanked` is the same text with the body of every substitution AND every
    quoted string replaced by spaces, newlines kept so nothing shifts. That is
    what the marker is searched in, because a `#allow-push-main` inside `$( )`,
    inside backticks or inside a quoted string is not a comment of this line to
    bash either. `raw` is what the push search parses, since a push inside a
    substitution is still a push that runs.
    """
    lines = []
    raw, blank = [], []
    single = double = backtick = False
    depth = 0                     # $( ) nesting
    i, n = 0, len(text)

    def keep(ch, hidden):
        raw.append(ch)
        blank.append(" " if (hidden and ch != "\n") else ch)

    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        # Computed BEFORE this character is classified, so the closing quote,
        # backtick or paren of a hidden run is blanked with the run it ends.
        hidden = depth > 0 or backtick or single or double

        if ch == "\\" and not single and nxt:
            keep(ch, hidden)
            keep(nxt, hidden)
            i += 2
            continue

        if single:
            if ch == "'":
                single = False
        elif ch == "$" and nxt == "(":
            # A substitution counts inside double quotes too, and never inside
            # single quotes, which the branch above has already taken.
            depth += 1
            keep(ch, True)
            keep(nxt, True)
            i += 2
            continue
        elif double:
            if ch == '"':
                double = False
        elif ch == "'":
            single = True
        elif ch == '"':
            double = True
        elif ch == "`":
            backtick = not backtick
        elif ch == ")" and depth > 0:
            depth -= 1
        elif ch == "\n" and depth == 0 and not backtick:
            if CONTINUES_LINE.search("".join(blank)):
                keep(ch, False)
            else:
                lines.append(("".join(raw), "".join(blank)))
                raw, blank = [], []
            i += 1
            continue

        keep(ch, hidden)
        i += 1

    if raw:
        lines.append(("".join(raw), "".join(blank)))
    return lines


# Stands in for one `$( )` or backtick substitution inside a word. It has no
# punctuation, so the tokenizer keeps it inside the word it sits in.
SUBSTITUTION = "__SUBSTITUTION__"


def flatten_substitutions(line):
    """(flat_line, bodies): every top-level substitution replaced by one word.

    The shared tokenizer treats `(`, `)` and `|` as command operators, so a
    substitution holding a pipe cut the push it sat in: `--force-with-lease=b:
    sha$(git rev-parse x | cut -c8-) origin topic` became `... sha$` then
    `cut -c8-) origin topic`, the push lost its refspecs, and it was blocked as
    a bare push while on main (2026-09-15). The body is returned separately,
    because a push inside a substitution still runs. Single quotes hide a
    substitution; double quotes do not.
    """
    flat, bodies, body = [], [], []
    single = double = backtick = False
    depth = 0
    i, n = 0, len(line)
    while i < n:
        ch = line[i]
        nxt = line[i + 1] if i + 1 < n else ""
        if depth == 0 and not backtick:
            if ch == "\\" and not single and nxt:
                flat.append(ch + nxt)
                i += 2
                continue
            if single:
                single = ch != "'"
            elif ch == "$" and nxt == "(":
                depth, body = 1, []
                i += 2
                continue
            elif ch == "`":
                backtick, body = True, []
                i += 1
                continue
            elif double:
                double = ch != '"'
            elif ch == "'":
                single = True
            elif ch == '"':
                double = True
            flat.append(ch)
        elif backtick:
            if ch == "`":
                backtick = False
                bodies.append("".join(body))
                flat.append(SUBSTITUTION)
            else:
                body.append(ch)
        else:
            if ch == "$" and nxt == "(":
                depth += 1
                body.append(ch + nxt)
                i += 2
                continue
            if ch == ")":
                depth -= 1
                if depth == 0:
                    bodies.append("".join(body))
                    flat.append(SUBSTITUTION)
                    i += 1
                    continue
            body.append(ch)
        i += 1
    if depth or backtick:
        # Unbalanced: hand back the line untouched and let the caller's
        # conservative unparseable path decide.
        return line, []
    return "".join(flat), bodies


def line_grants_bypass(blanked_line, segments):
    """True only when this LINE carries #allow-push-main as its own comment.

    Two independent tests, and both must hold. The blanked line has to carry
    the marker as a whole comment word, which no substitution body and no
    quoted string can satisfy. And no TOKEN of the line may contain it, which
    is the belt: the shared tokenizer drops a real comment and returns a quoted
    string as one token with its quotes removed, so a marker inside a commit
    message, a `git config` value, a grep pattern or a nested `bash -c` string
    survives tokenizing and is refused.

    A marker inside a heredoc body is not in scope at all, because a script
    being WRITTEN is not a command being run; strip_heredocs has already taken
    those bodies out. A marker on ANOTHER line grants nothing here, which is
    the whole point: it authorizes the pushes it annotates, not the payload.
    """
    if not MARKER_AS_COMMENT.search(blanked_line):
        return False
    return not any(MARKER in tok for segment in segments for tok in segment)


def push_invocations(segments, depth=0, cd_dir=None):
    """Yield (git_call, cd_dir) for every real `git push` in these segments.

    `cd_dir` seeds the working directory, because the caller now walks one
    LINE at a time and a `cd` on an earlier line still governs this one.
    """
    for segment in segments:
        target = cd_target(segment)
        if target:
            cd_dir = target

        call = git_call(segment)
        if call is not None:
            if call["subcommand"] == "push":
                yield call, cd_dir
            continue

        inner = nested_shell_command(strip_prefixes(segment))
        if not inner:
            continue
        if depth >= 2:
            # Out of nesting budget with text still to run. Unknown is not clean.
            if LOOKS_LIKE_PUSH_RE.search(inner):
                block(RAW_COMMAND, "a git push nested deeper than this guard follows")
            continue
        try:
            nested = split_segments(tokenize(inner))
        except UnparseableCommand:
            if LOOKS_LIKE_PUSH_RE.search(inner):
                block(RAW_COMMAND, "a nested command could not be parsed, blocking conservatively")
            continue
        for found_call, found_dir in push_invocations(nested, depth + 1):
            yield found_call, (found_dir or cd_dir)


def push_flags(args):
    """The flag names of a `git push`, with the values they consume skipped."""
    flags = set()
    i = 0
    while i < len(args):
        tok = args[i]
        if tok in PUSH_VALUE_FLAGS:
            i += 2
            continue
        if tok.startswith("-"):
            flags.add(tok.split("=", 1)[0])
        i += 1
    return flags


def pushes_matching_branches(refspec):
    """True for a refspec that sends every matching branch rather than one.

    A bare `:` is git's matching-branches refspec, so it lands on main
    wherever main is already on the remote, with nothing in the command text
    naming it.
    """
    return refspec.lstrip("+").strip() == ":"


def refspec_destination(refspec, head_branch=None):
    """The branch a refspec LANDS ON, which is the part after the colon.

    With no colon the source IS the destination, and a source of HEAD or @
    lands on whatever branch HEAD names, so `head_branch` (the target repo's
    current branch, read by the caller only when it is needed) stands in for
    it. A `HEAD:<dst>` refspec needs none of this: the destination side is
    written down.
    """
    spec = refspec.lstrip("+")
    if ":" in spec:
        dest = spec.split(":", 1)[1]
    else:
        dest = spec
        if dest.strip() in HEAD_ALIASES and head_branch:
            dest = head_branch
    dest = dest.strip()
    if dest.startswith("refs/heads/"):
        dest = dest[len("refs/heads/"):]
    return dest


def refspec_has_head_source(refspec):
    """True for a source-only HEAD or @, the shape that needs resolving."""
    spec = refspec.lstrip("+")
    return ":" not in spec and spec.strip() in HEAD_ALIASES


def resolve_dir(dash_c, cd_dir, cwd):
    """Which repo the push runs in: `git -C <dir>`, else a `cd <dir>`, else cwd."""
    target = dash_c or cd_dir
    if not target:
        return cwd
    target = os.path.expanduser(target.strip())
    if "$" in target:
        return cwd
    return target if os.path.isabs(target) else os.path.join(cwd, target)


def current_branch(directory):
    try:
        proc = subprocess.run(
            ["git", "-C", directory, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
    except Exception:
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


def remote_head_branch(directory, remote):
    """The branch refs/remotes/<remote>/HEAD points at in this repo, or None.

    `symbolic-ref` reads the local ref file and never contacts the remote, and
    `-q` keeps it silent when the ref was never written (a clone made before
    `git remote set-head`, a repo with no remote, a remote given as a URL).
    """
    key = (directory, remote)
    if key in _REMOTE_HEAD_CACHE:
        return _REMOTE_HEAD_CACHE[key]

    branch = None
    if remote and REMOTE_NAME.match(remote):
        try:
            proc = subprocess.run(
                ["git", "-C", directory, "symbolic-ref", "-q", "--short",
                 "refs/remotes/%s/HEAD" % remote],
                capture_output=True, text=True, timeout=5,
            )
        except Exception:
            proc = None
        if proc is not None and proc.returncode == 0:
            out = proc.stdout.strip()
            prefix = remote + "/"
            if out.startswith(prefix):
                out = out[len(prefix):]
            branch = out or None

    _REMOTE_HEAD_CACHE[key] = branch
    return branch


def block(command, why):
    sys.stderr.write(
        "Blocked: raw `git push` to a protected branch (%s). Use /release, which "
        "updates README + project CLAUDE.md and verifies the deploy as part of "
        "the same atomic action.\n"
        "Push attempted: %s\n"
        "/release auto-detects trunk mode (commit straight to main) vs PR mode "
        "(merge open PRs), then updates docs + pushes + verifies the auto-deploy.\n"
        "Bypass for emergencies (use sparingly): append #allow-push-main as a "
        "real comment at the end of the bash command, outside any quotes. That "
        "is the only route that works from inside a session: this hook decides "
        "BEFORE the shell runs, so a CLAUDE_ALLOW_PUSH_MAIN=1 prefix on this "
        "same command never reaches it. The env var works only when already "
        "exported in the launching environment.\n"
        % (why, command)
    )
    sys.exit(2)


def main():
    global RAW_COMMAND
    if os.environ.get("CLAUDE_ALLOW_PUSH_MAIN") == "1":
        sys.exit(0)
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    command = ((data.get("tool_input") or {}).get("command") or "")
    RAW_COMMAND = command
    if not command.strip():
        sys.exit(0)

    cwd = data.get("cwd") or os.getcwd()
    scannable, _bodies = strip_heredocs(command)

    # One LINE at a time, because the bypass marker is a comment and a comment
    # belongs to its own line. `cd_dir` carries across lines, since a `cd` on
    # an earlier line still decides which repo a later push runs in.
    cd_dir = None
    for raw_line, blanked_line in top_level_lines(scannable):
        try:
            flat_line, bodies = flatten_substitutions(raw_line)
            segments = split_segments(tokenize(flat_line))
            # A push inside a substitution runs too, so each body is searched
            # as commands of its own. Kept apart from `segments` because a `cd`
            # inside a substitution does not move the line's directory.
            body_segments = []
            for body in bodies:
                inner_flat, inner_bodies = flatten_substitutions(body)
                body_segments += split_segments(tokenize(inner_flat))
                for inner in inner_bodies:
                    body_segments += split_segments(tokenize(inner))
        except UnparseableCommand:
            # Usually an unbalanced quote. Fall back to the old substring test,
            # so an odd real push is still caught. Over-blocking is the safe
            # direction here, and it is why the marker is not consulted: a line
            # nobody could parse is a line whose quoting nobody can vouch for.
            if LOOKS_LIKE_PUSH_RE.search(raw_line):
                block(command, "command could not be parsed, blocking conservatively")
            continue

        bypassed = line_grants_bypass(blanked_line, segments + body_segments)

        for call, call_cd in push_invocations(segments + body_segments, cd_dir=cd_dir):
            if bypassed:
                continue
            positionals = positional_args(call["args"], PUSH_VALUE_FLAGS)
            refspecs = positionals[1:] if positionals else []
            remote = positionals[0] if positionals else "origin"
            directory = resolve_dir(call["dash_c"], call_cd, cwd)

            # --- Layer 0: the flags that send every branch, so no destination
            # is written down anywhere in the command text ---
            everything = push_flags(call["args"]) & PUSH_EVERYTHING_FLAGS
            if everything:
                block(command, "%s sends every branch, so it lands on %s too"
                      % (sorted(everything)[0], "/".join(PROTECTED)))

            # One subprocess at most, and only for the refspec shape that
            # cannot be read without asking the repo.
            head_branch = None
            if any(refspec_has_head_source(r) for r in refspecs):
                head_branch = current_branch(directory)

            # --- Layer 1: a refspec that LANDS ON a protected branch ---
            for refspec in refspecs:
                # A destination built by a substitution is unknown until it
                # runs, and `$(git branch --show-current)` IS the branch the
                # repo sits on, so it is judged like a bare push.
                if SUBSTITUTION in refspec_destination(refspec, head_branch=head_branch):
                    if current_branch(directory) in PROTECTED:
                        block(command, "a destination built by command "
                                       "substitution, while on %s"
                              % current_branch(directory))
                    continue
                if pushes_matching_branches(refspec):
                    block(command, "a `:` refspec pushes every matching "
                                   "branch, so it lands on %s too"
                          % "/".join(PROTECTED))
                dest = refspec_destination(refspec, head_branch=head_branch)
                if "*" in dest:
                    block(command, "the wildcard destination in %s expands "
                                   "onto every matching branch, %s included"
                          % (refspec, "/".join(PROTECTED)))
                if dest in PROTECTED:
                    block(command, "explicit %s destination" % dest)
                # Only now is the extra subprocess worth it: main and master are
                # already settled above, so this asks only about anything else.
                if dest and dest == remote_head_branch(directory, remote):
                    block(command, "%s is the default branch (%s/HEAD) of the "
                                   "target repo" % (dest, remote))

            # --- Layer 2: bare push, which resolves to the current branch ---
            if not refspecs:
                branch = current_branch(directory)
                if branch in PROTECTED:
                    block(command, "bare push while on %s" % branch)
                if branch and branch == remote_head_branch(directory, remote):
                    block(command, "bare push while on %s, the default branch "
                                   "(%s/HEAD) of this repo" % (branch, remote))

        for segment in segments:
            target = cd_target(segment)
            if target:
                cd_dir = target

    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:
        # A crashing guard must not wedge every Bash call, but it must also not
        # wave a real push through. So it fails OPEN for ordinary commands and
        # fails CLOSED only for text that looks like a push.
        sys.stderr.write("block-git-push-main crashed: %r\n" % (exc,))
        if LOOKS_LIKE_PUSH_RE.search(RAW_COMMAND or ""):
            sys.stderr.write(
                "The command mentions a push, so it is blocked rather than waved "
                "through. Re-run with #allow-push-main if it is safe.\n")
            sys.exit(2)
        sys.exit(0)
