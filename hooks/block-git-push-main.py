#!/usr/bin/env python3
"""Block a raw `git push` that would land on main/master, redirecting to /release.

Reached through the thin `block-git-push-main.sh` delegator, which is what
~/.claude/settings.json wires up. Protocol: read the tool-call envelope as JSON
on stdin; a PreToolUse hook blocks ONLY via exit code 2 with the reason on
stderr (the stdout {"decision":"block"} form is NOT honored for PreToolUse and
fails OPEN, confirmed against https://code.claude.com/docs/en/hooks.md).

WHY THIS REPLACED THE SHELL VERSION
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

KNOWN HOLE, left alone on purpose: `git push --all` / `--mirror` pushes every
branch, so it can land on main from a feature branch. It reads as a bare push
here and is only caught when the current branch is main, exactly as the shell
version behaved. Tightening that changes behaviour rather than fixing a bug, so
it is left as a deliberate decision for whoever adopts this hook.

BYPASS: append #allow-push-main to the command, or set CLAUDE_ALLOW_PUSH_MAIN=1.
"""
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from _bash_command_parse import (           # noqa: E402
    UnparseableCommand, cd_target, command_segments, git_call,
    nested_shell_command, positional_args, split_segments, strip_prefixes,
    tokenize,
)

PROTECTED = ("main", "master")

# `git push` flags that consume the NEXT token as their value. Deliberately NOT
# --force-with-lease: it is either bare or `=<ref>`, never a separate token, and
# listing it here would swallow the remote and miss an explicit `main`.
PUSH_VALUE_FLAGS = {"--repo", "-o", "--push-option"}

RAW_COMMAND = ""


def push_invocations(segments, depth=0):
    """Yield (git_call, cd_dir) for every real `git push` in these segments."""
    cd_dir = None
    for segment in segments:
        target = cd_target(segment)
        if target:
            cd_dir = target

        call = git_call(segment)
        if call is not None:
            if call["subcommand"] == "push":
                yield call, cd_dir
            continue

        if depth >= 2:
            continue
        inner = nested_shell_command(strip_prefixes(segment))
        if not inner:
            continue
        try:
            nested = split_segments(tokenize(inner))
        except UnparseableCommand:
            continue
        for found_call, found_dir in push_invocations(nested, depth + 1):
            yield found_call, (found_dir or cd_dir)


def refspec_destination(refspec):
    """The branch a refspec LANDS ON, which is the part after the colon."""
    spec = refspec.lstrip("+")
    dest = spec.split(":", 1)[1] if ":" in spec else spec
    dest = dest.strip()
    if dest.startswith("refs/heads/"):
        dest = dest[len("refs/heads/"):]
    return dest


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


def block(command, why):
    sys.stderr.write(
        "Blocked: raw `git push` to main/master (%s). Use /release, which updates "
        "README + project CLAUDE.md and verifies the deploy as part of the same "
        "atomic action.\n"
        "Push attempted: %s\n"
        "/release auto-detects trunk mode (commit straight to main) vs PR mode "
        "(merge open PRs), then updates docs + pushes + verifies the auto-deploy.\n"
        "Bypass for emergencies (use sparingly): set CLAUDE_ALLOW_PUSH_MAIN=1 in "
        "the env, OR append #allow-push-main as a comment in the bash command.\n"
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
    if re.search(r"#allow-push-main\b", command):
        sys.exit(0)

    cwd = data.get("cwd") or os.getcwd()

    try:
        segments = command_segments(command)
    except UnparseableCommand:
        # Usually an unbalanced quote. Fall back to the old substring test, so an
        # odd real push is still caught. Over-blocking is the safe direction here.
        if re.search(r"\bgit\b[^\n]{0,80}\bpush\b", command):
            block(command, "command could not be parsed, blocking conservatively")
        sys.exit(0)

    for call, cd_dir in push_invocations(segments):
        positionals = positional_args(call["args"], PUSH_VALUE_FLAGS)
        refspecs = positionals[1:] if positionals else []

        # --- Layer 1: a refspec that LANDS ON main/master ---
        for refspec in refspecs:
            if refspec_destination(refspec) in PROTECTED:
                block(command, "explicit main/master destination")

        # --- Layer 2: bare push, which resolves to the current branch ---
        if not refspecs:
            branch = current_branch(resolve_dir(call["dash_c"], cd_dir, cwd))
            if branch in PROTECTED:
                block(command, "bare push while on %s" % branch)

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
        if re.search(r"\bgit\b[^\n]{0,80}\bpush\b", RAW_COMMAND or ""):
            sys.stderr.write(
                "The command mentions a push, so it is blocked rather than waved "
                "through. Re-run with #allow-push-main if it is safe.\n")
            sys.exit(2)
        sys.exit(0)
