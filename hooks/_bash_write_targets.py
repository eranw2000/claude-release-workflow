#!/usr/bin/env python3
"""Shared parser: which files does a Bash command WRITE via redirection or tee?

Used by _bash_command_parse.py, which strips heredoc bodies with it, and by
hooks that check files a Bash command writes. Exists because Edit/Write hooks never fire on shell
writes (heredocs, `>`/`>>`, tee): that blind spot is exactly how dead tests
were appended with `cat >>` on 2026-07-17.

Rules of the road (advisory hooks, so precision beats recall):
- Heredoc BODIES are stripped before scanning, so a heredoc that writes a
  script whose body contains '>' is never misread as a redirect target.
  Bodies are returned separately for callers that scan written content.
- Relative targets resolve against a leading `cd <dir> &&` prefix first,
  then the hook envelope's cwd. A candidate that does not exist on disk is
  DROPPED, not guessed at: PostToolUse fires after a successful write, so
  the real target exists. Degrading silently is deliberate.
- Python 3.9 compatible (the system python3 may be Xcode's 3.9).
"""

import os
import re

_HEREDOC_OPEN = re.compile(r"<<(-?)\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\2")
_REDIRECT = re.compile(r"(?:^|[\s;&|({])(?:\d?>>?|&>>?)\s*([^\s;&|<>()]+)")
_TEE = re.compile(r"\btee\s+([^;|&\n]+)")
_CD_PREFIX = re.compile(r"^\s*cd\s+(?:\"([^\"]+)\"|'([^']+)'|(\S+))\s*&&")


def strip_heredocs(command):
    """Return (command with heredoc bodies removed, list of body strings)."""
    kept = []
    bodies = []
    queue = []      # (delimiter, strip_leading_tabs) still awaiting a body
    current = None  # lines of the body being collected
    for line in command.split("\n"):
        if queue:
            delim, strip_tabs = queue[0]
            probe = line.lstrip("\t") if strip_tabs else line
            if probe == delim:
                bodies.append("\n".join(current or []))
                current = None
                queue.pop(0)
            else:
                if current is None:
                    current = []
                current.append(line)
            continue
        for m in _HEREDOC_OPEN.finditer(line):
            queue.append((m.group(3), m.group(1) == "-"))
        kept.append(line)
    if current is not None:  # unterminated heredoc: rest of command is body
        bodies.append("\n".join(current))
    return "\n".join(kept), bodies


def heredoc_bodies_by_target(command, cwd=None):
    """Pair each heredoc BODY with the file its OWN opener line redirects to.

    Returns [(target_or_None, body)]. A body whose opener line carries no
    redirect (`python3 - <<'PY'`) gets None, because it is a script fed to a
    program and is not written anywhere.

    Exists because a caller that scans written CONTENT has to know which file
    the content went to. Concatenating every body and blaming every target
    makes a verifier heredoc in the same call look like part of the document
    it verifies, which is how ai-signal-check warned twice about clean files
    on 2026-09-01.
    """
    cd_dir = None
    m = _CD_PREFIX.match(command)
    if m:
        cd_dir = _expand(m.group(1) or m.group(2) or m.group(3))
        if not os.path.isabs(cd_dir) and cwd:
            cd_dir = os.path.join(cwd, cd_dir)

    def _line_target(line):
        raw = []
        for r in _REDIRECT.finditer(line):
            raw.append(r.group(1))
        for r in _TEE.finditer(line):
            for tok in r.group(1).split():
                if tok.startswith("-"):
                    continue
                if tok in ("<", ">", ">>"):
                    break
                raw.append(tok)
        for tok in raw:
            tok = _clean_token(tok)
            if not tok or tok.startswith(("&", "(", "$(")):
                continue
            resolved = _resolve(tok, cd_dir, cwd)
            if resolved and not resolved.startswith("/dev/"):
                return resolved
        return None

    pairs = []
    queue = []      # (delimiter, strip_leading_tabs, target) awaiting a body
    current = None
    pending = None  # target of the heredoc currently being collected
    for line in command.split("\n"):
        if queue:
            delim, strip_tabs, target = queue[0]
            probe = line.lstrip("\t") if strip_tabs else line
            if probe == delim:
                pairs.append((target, "\n".join(current or [])))
                current = None
                queue.pop(0)
            else:
                if current is None:
                    current = []
                    pending = target
                current.append(line)
            continue
        opens = list(_HEREDOC_OPEN.finditer(line))
        if opens:
            target = _line_target(line)
            for om in opens:
                queue.append((om.group(3), om.group(1) == "-", target))
    if current is not None:  # unterminated heredoc: rest of command is body
        pairs.append((pending, "\n".join(current)))
    return pairs


def _clean_token(tok):
    tok = tok.strip()
    if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in ("'", '"'):
        tok = tok[1:-1]
    return tok


def _expand(path):
    path = os.path.expanduser(path)
    home = os.environ.get("HOME")
    if home:
        if path.startswith("${HOME}"):
            path = home + path[len("${HOME}"):]
        elif path.startswith("$HOME"):
            path = home + path[len("$HOME"):]
    return path


def _resolve(target, cd_dir, cwd):
    target = _expand(target)
    if os.path.isabs(target):
        return target
    candidates = []
    if cd_dir:
        candidates.append(os.path.join(cd_dir, target))
    if cwd:
        candidates.append(os.path.join(cwd, target))
    for cand in candidates:
        if os.path.exists(cand):
            return cand
    return None  # cannot place it: drop rather than guess


def extract_write_targets(command, cwd=None):
    """Absolute paths the command writes via >, >>, 2>, &>, or tee."""
    stripped, _bodies = strip_heredocs(command)

    cd_dir = None
    m = _CD_PREFIX.match(stripped)
    if m:
        cd_dir = _expand(m.group(1) or m.group(2) or m.group(3))
        if not os.path.isabs(cd_dir) and cwd:
            cd_dir = os.path.join(cwd, cd_dir)

    raw = []
    for m in _REDIRECT.finditer(stripped):
        raw.append(m.group(1))
    for m in _TEE.finditer(stripped):
        for tok in m.group(1).split():
            if tok.startswith("-"):
                continue
            if tok in ("<", ">", ">>"):
                break  # a redirect after tee's operands; _REDIRECT covers it
            raw.append(tok)

    targets = []
    for tok in raw:
        tok = _clean_token(tok)
        if not tok or tok.startswith(("&", "(", "$(")):
            continue  # 2>&1 / process substitution / command substitution
        resolved = _resolve(tok, cd_dir, cwd)
        if not resolved or resolved.startswith("/dev/"):
            continue  # device sinks (/dev/null, /dev/stderr) are not writes
        if resolved not in targets:
            targets.append(resolved)
    return targets
