#!/usr/bin/env python3
"""Shared quote-aware parsing for hooks that inspect a Bash command.

WHY THIS EXISTS (2026-08-18)
Two guards independently decided what a command DOES by matching substrings and
regexes against the raw command text, and both produced the same false positive
on the same day, on the same command:

    grep -rniE 'remote|git push|ls-remote' <file> | head

block-git-push-main.sh saw the words "git push" and called it a push.
A second guard split the text on "|", which cut INSIDE the quoted
pattern, so one piece began with "git push" and looked like a bare git command.

Neither was a git command at all. The phrase was an argument to grep. Fixing
each guard separately would have left the next guard, and the next command
shape, exposed. So the rule lives here instead: decide from a PARSE.

  1. strip heredoc bodies, because a script being WRITTEN is not a script being
     RUN (reused from _bash_write_targets, which already solved this),
  2. tokenize the whole command once, QUOTE-AWARE, so a quoted pattern is one
     token and a `#` comment disappears,
  3. only then split on operator TOKENS, which can no longer fall inside quotes.

Import from here rather than re-deriving it. A guard that cries wolf trains its
reader to discount it, and the next warning is the real one.
"""
import os
import re
import shlex

_HERE = os.path.dirname(os.path.abspath(__file__))

try:
    import sys
    sys.path.insert(0, _HERE)
    from _bash_write_targets import strip_heredocs
except Exception:                                       # pragma: no cover
    def strip_heredocs(command):
        """Fallback: leave the text alone. Heredoc bodies stay in scope, which
        can only cause a false POSITIVE, never a missed command."""
        return command, []

# Operator tokens that end one command and begin the next.
OPERATORS = {";", "|", "||", "&&", "&", "(", ")", "\n"}

# What shlex(punctuation_chars=True) uses, plus the newline. shlex groups a run
# of these into ONE token, so `&&` followed by a newline arrives as `"&&\n"`;
# the run test in split_segments accepts any such run rather than a fixed set.
PUNCTUATION_CHARS = "();<>|&\n"

# Shell keywords and wrappers that can sit in front of the real command word.
PREFIXES = {
    "if", "then", "else", "elif", "fi", "do", "done", "while", "until", "for",
    "!", "time", "sudo", "command", "nohup", "exec", "env",
}
SHELLS = {"bash", "sh", "zsh", "dash", "ksh"}

ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

# git global flags that consume the NEXT token as their value.
GIT_GLOBAL_VALUE_FLAGS = {"-C", "-c", "--namespace", "--exec-path", "--work-tree", "--git-dir"}


class UnparseableCommand(Exception):
    """The text could not be tokenized, usually an unbalanced quote."""


def tokenize(text):
    """Quote-aware token list. Operators come back as their own tokens."""
    lex = shlex.shlex(text, posix=True, punctuation_chars=PUNCTUATION_CHARS)
    lex.whitespace_split = True
    # A newline ENDS a command, so it must come back as its own token. shlex
    # counts it as ordinary whitespace by default, which silently welded two
    # commands into one segment: `git status` newline `git push origin main`
    # parsed as a single `git status` call with four extra arguments, and every
    # guard built on this parser went quiet. Measured 2026-08-28 over this
    # machine's own history, 180 of 649 real commits were invisible for it.
    #
    # Setting `whitespace` alone does NOT work and is worse than the bug: the
    # newline then has no meaning at all and welds into the neighbouring word,
    # giving the token `status\ngit`, so the second command's NAME disappears.
    # It has to be a punctuation character, which is what emits it separately.
    # \r stays whitespace so a CRLF command does not leave a stray \r behind.
    lex.whitespace = " \t\r"
    try:
        return list(lex)
    except ValueError as exc:
        raise UnparseableCommand(str(exc))


def split_segments(tokens):
    """Split a token list into one token list per command."""
    segments, current = [], []
    for tok in tokens:
        if tok in OPERATORS or (tok and all(c in "|&;\n" for c in tok)):
            if current:
                segments.append(current)
                current = []
            continue
        current.append(tok)
    if current:
        segments.append(current)
    return segments


def command_segments(command):
    """The commands a Bash string would actually run, as token lists.

    Raises UnparseableCommand when the text cannot be tokenized.
    """
    scannable, _bodies = strip_heredocs(command)
    return split_segments(tokenize(scannable))


# Options of a wrapper word that consume the NEXT token as their value, so
# `sudo -u root git commit` reaches `git` rather than stopping at `-u`.
WRAPPER_VALUE_FLAGS = {
    "sudo": {"-u", "-g", "-h", "-p", "-U", "-C", "-D", "-R", "-r", "-t", "-T"},
    "env": {"-u", "-C", "-S", "-P"},
    "exec": {"-a"},
}


def _env_split_string(head, flag, rest):
    """For env's -S / --split-string, the words its string splits into, else None.

    Consumes the value from `rest` when it is a separate token. A string the
    tokenizer cannot read falls back to a whitespace split, so the command words
    stay visible and no caller has to handle a new exception here.
    """
    if head != "env":
        return None
    if flag in ("-S", "--split-string"):
        if not rest:
            return None
        value = rest.pop(0)
    elif flag.startswith("--split-string="):
        value = flag[len("--split-string="):]
    elif flag.startswith("-S") and len(flag) > 2:
        value = flag[2:]
    else:
        return None
    try:
        return tokenize(value)
    except UnparseableCommand:
        return value.split()


def strip_prefixes(tokens):
    """Drop shell keywords, wrappers, their options and VAR=value assignments."""
    out = list(tokens)
    while out:
        head = out[0]
        if head in PREFIXES:
            out.pop(0)
            value_flags = WRAPPER_VALUE_FLAGS.get(head, set())
            while out and out[0].startswith("-") and out[0] != "-":
                flag = out.pop(0)
                if flag == "--":
                    break
                # `env -S 'git commit'` splits its string into the command words,
                # so the string IS the command, and it is parsed as one.
                split = _env_split_string(head, flag, out)
                if split is not None:
                    out = split + out
                    continue
                if flag in value_flags and out:
                    out.pop(0)
            continue
        if ENV_ASSIGN.match(head):
            out.pop(0)
            continue
        break
    return out


def nested_shell_command(tokens):
    """For `bash -c "<text>"` (also `-lc`, `-ec`, behind a wrapper), the text."""
    tokens = strip_prefixes(tokens)
    if not tokens or os.path.basename(tokens[0]) not in SHELLS:
        return None
    saw_c = False
    i = 1
    while i < len(tokens):
        tok = tokens[i]
        if tok in ("-o", "+o", "-O", "+O"):
            i += 2
            continue
        if tok == "--":
            i += 1
            break
        if len(tok) > 1 and tok[0] in "-+" and not tok.startswith("--"):
            if tok[0] == "-" and "c" in tok[1:]:
                saw_c = True
            i += 1
            continue
        if tok.startswith("--"):
            i += 1
            continue
        break
    if saw_c and i < len(tokens):
        return tokens[i]
    return None


def git_call(tokens):
    """Parse one segment as a git invocation.

    Returns a dict with subcommand, args, explicit_target, dash_c and dash_c_all, or None
    when this segment does not run git.
    """
    tokens = strip_prefixes(tokens)
    if not tokens or os.path.basename(tokens[0]) != "git":
        return None

    dash_c = None
    dash_c_all = []
    explicit_target = False
    i = 1
    while i < len(tokens):
        tok = tokens[i]
        if tok in GIT_GLOBAL_VALUE_FLAGS:
            if tok == "-C" and i + 1 < len(tokens):
                dash_c = tokens[i + 1]
                dash_c_all.append(tokens[i + 1])
                explicit_target = True
            elif tok in ("--git-dir", "--work-tree"):
                explicit_target = True
            i += 2
            continue
        if tok.startswith("--git-dir=") or tok.startswith("--work-tree="):
            explicit_target = True
            i += 1
            continue
        if tok.startswith("-"):
            i += 1
            continue
        break
    if i >= len(tokens):
        return None
    return {
        "subcommand": tokens[i],
        "args": tokens[i + 1:],
        "explicit_target": explicit_target,
        "dash_c": dash_c,
        # Every -C in order. git applies each relative one to the previous, so
        # `git -C a -C b` runs in a/b; `dash_c` keeps only the last for older readers.
        "dash_c_all": dash_c_all,
    }


def positional_args(args, value_flags=()):
    """Positional arguments only, skipping flags and the values they consume."""
    value_flags = set(value_flags)
    out = []
    i = 0
    while i < len(args):
        tok = args[i]
        if tok in value_flags:
            i += 2
            continue
        if tok.startswith("-"):
            i += 1
            continue
        out.append(tok)
        i += 1
    return out


def cd_target(tokens):
    """The argument of a `cd` in this segment, or None."""
    stripped = strip_prefixes(tokens)
    if stripped and stripped[0] == "cd" and len(stripped) > 1:
        return stripped[1]
    return None


def pipeline_stages(command):
    """Pipelines in `command`, as lists of token-lists, one entry per pipeline.

    `split_segments` deliberately discards the joining operator, so it cannot
    tell `a | tail` from `a && tail`. This keeps the `|` boundaries, which is
    what a check about output buffering needs: only the LAST stage of a
    pipeline reaches the terminal, so only that stage decides whether the
    earlier stages' output is visible while they are still running.

    Heredoc bodies are stripped first, so a pipe written inside a generated
    script is not read as a pipe belonging to this call.

    Raises UnparseableCommand when the text cannot be tokenized.
    """
    scannable, _bodies = strip_heredocs(command)
    tokens = tokenize(scannable)

    pipelines, current, stage = [], [], []
    for tok in tokens:
        # A pipe at the END of a line still pipes: bash continues the pipeline
        # onto the next line. shlex groups the run, so it arrives as "|\n" and
        # a bare `== "|"` test would stop seeing it as a pipe at all.
        is_pipe = bool(tok) and tok.replace("\n", "") == "|"
        is_other_op = (not is_pipe) and (
            tok in OPERATORS or (tok and all(c in "|&;\n" for c in tok))
        )
        if is_pipe:
            if stage:
                current.append(stage)
                stage = []
            continue
        if is_other_op:
            if stage:
                current.append(stage)
                stage = []
            if len(current) > 1:
                pipelines.append(current)
            current = []
            continue
        stage.append(tok)
    if stage:
        current.append(stage)
    if len(current) > 1:
        pipelines.append(current)
    return pipelines
