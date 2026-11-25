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
    "!", "time", "sudo", "command", "nohup", "exec", "env", "nice", "timeout", "gtimeout",
    "{",
}
# `(` stays out on purpose. split_segments already ends a command there, so only a caller
# reading raw tokens could reach it, and to such a caller a `cd` inside `( )` would look
# like it moved the shell. It does not: `then (cd /tmp/a); fi; rm -rf x` deletes from here.
SHELLS = {"bash", "sh", "zsh", "dash", "ksh"}

ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

# git global flags that consume the NEXT token as their value.
GIT_GLOBAL_VALUE_FLAGS = {"-C", "-c", "--namespace", "--exec-path", "--work-tree", "--git-dir"}


class UnparseableCommand(Exception):
    """The text could not be tokenized, usually an unbalanced quote."""


_WORD_START = " \t\r\n;|&()<>"


def strip_comments(text):
    r"""Drop `#` comments, keeping the newline that ends each one.

    shlex's own comment handling reads the rest of the line with readline(),
    which also eats the newline, so the NEXT line welded onto the commented one:
    `git push origin main #allow-push-main` then `git ls-remote origin` parsed
    as one push of `git` and `ls-remote`.
    It also ended a word at any `#`, where bash starts a comment only at the
    start of a word, so `a#b` and `$#` were cut short.

    Word start is tracked as state, not read back from the last character kept:
    after `\ ` or `\;` the last character is a plain space or `;`, yet the
    escape keeps it inside the word, so `echo foo\ #; git commit` is ONE echo
    argument followed by a real commit, not a comment.
    """
    out, i, n = [], 0, len(text)
    single = double = False
    at_start = True
    while i < n:
        c = text[i]
        if single:
            out.append(c)
            single = c != "'"
        elif double:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 1
            elif c == '"':
                double = False
        elif c == "\\" and i + 1 < n:
            out.append(c)
            out.append(text[i + 1])
            i += 1
            at_start = False
        elif c == "#" and at_start:
            end = text.find("\n", i)
            if end < 0:
                break
            i = end
            continue
        else:
            single = c == "'"
            double = c == '"'
            out.append(c)
            at_start = c in _WORD_START
        i += 1
    return "".join(out)


def tokenize(text):
    """Quote-aware token list. Operators come back as their own tokens."""
    lex = shlex.shlex(strip_comments(text), posix=True, punctuation_chars=PUNCTUATION_CHARS)
    lex.whitespace_split = True
    lex.commenters = ""
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


def _ends_a_command(tok):
    """True for an operator token, including a run shlex grouped into one token.

    A run can mix brackets with operators (`);`, `)&&`, `;(`, `)\n`) and ends a command,
    so `(cd /tmp); git push origin main` is two commands.
    A run of brackets alone (`((`, `))`) is arithmetic, so it stays a word.
    At this point a quoted `';'` also ends a command.
    """
    if tok in OPERATORS:
        return True
    return bool(tok) and all(c in "()|&;\n" for c in tok) and bool(tok.strip("()"))


def split_segments(tokens):
    """Split a token list into one token list per command."""
    segments, current = [], []
    for tok in tokens:
        if _ends_a_command(tok):
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
    # GNU coreutils manual: `-n adjustment`, `--adjustment=adjustment`, and "an obsolete
    # option syntax -adjustment" (a lone flag, so nothing to consume).
    "nice": {"-n", "--adjustment"},
    # GNU coreutils manual: `timeout [option]... duration command [arg]...`
    "timeout": {"-k", "-s", "--kill-after", "--signal"},
    "gtimeout": {"-k", "-s", "--kill-after", "--signal"},
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


def strip_prefixes(tokens, stop_at=()):
    """Drop shell keywords, wrappers, their options and VAR=value assignments.

    A word in `stop_at` is kept as the command, for a caller asking about that wrapper.
    """
    out = list(tokens)
    while out:
        head = out[0]
        if head in stop_at:
            break
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
            if head in ("timeout", "gtimeout") and out:
                out.pop(0)  # the duration, always the first word after the options
            continue
        if ENV_ASSIGN.match(head):
            out.pop(0)
            continue
        break
    return out


def program_name(token):
    """A program's plain name on any OS: C:\\Git\\cmd\\git.exe and git are both git."""
    name = os.path.basename(token.replace("\\", "/"))
    return name[:-4].lower() if name.lower().endswith(".exe") else name


def nested_shell_command(tokens):
    """For `bash -c "<text>"` (also `-lc`, `-ec`, behind a wrapper), the text."""
    tokens = strip_prefixes(tokens)
    if not tokens or program_name(tokens[0]) not in SHELLS:
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
    if not tokens or program_name(tokens[0]) != "git":
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


# Which folders a command could run in.
#
# A guard reads a `cd` in a straight line of commands the usual way. A `cd` inside brackets,
# a block, a pipe or behind a wrapper may or may not move the shell, so for those commands the guard
# ALSO judges the command in every folder it could be in. It counts brackets and blocks instead of
# following the shell's grammar, and plain is an allow-list, so what it does not recognise is not
# plain, and the result is one more folder judged.

_MAX_FOLDERS = 16
_MAX_NESTING = 3
_OPENS_BLOCK = {"if", "for", "while", "until", "case", "select"}
_CLOSES_BLOCK = {"fi", "done", "esac", "}"}
# The next word after one of these is still a command.
_KEEPS_POSITION = {"if", "then", "do", "else", "elif", "while", "until", "!", "time", "{"}
# These set variables. A word after one is an argument, never a command, so `export cd X` is no cd.
_DECLARES = {"export", "declare", "typeset", "readonly", "local"}
_TEST_COMMANDS = {"[", "[[", "test", "true", "false"}
_VARIABLE = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?")
_MASKED = re.compile(r"QX(\d+)QX")


def _mask_quotes(text):
    """Replace quoted text and escaped characters with a plain word, so only real syntax is counted.

    Returns (masked text, the original pieces). Raises UnparseableCommand on an open quote.
    """
    out, pieces, i, n = [], [], 0, len(text)

    def piece(value):
        pieces.append(value)
        out.append("QX%dQX" % (len(pieces) - 1))

    while i < n:
        c = text[i]
        if c == "\\" and i + 1 < n:
            if text[i + 1] != "\n":     # a backslash and a newline join two lines
                piece(text[i + 1])
            i += 2
        elif c == "$" and text[i + 1:i + 2] == "'":
            j = i + 2
            while j < n and text[j] != "'":
                j += 2 if text[j] == "\\" else 1
            if j >= n:
                raise UnparseableCommand("open $' quote")
            piece(text[i + 2:j])
            i = j + 1
        elif c == "'":
            j = text.find("'", i + 1)
            if j < 0:
                raise UnparseableCommand("open ' quote")
            piece(text[i + 1:j])
            i = j + 1
        elif c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            if j >= n:
                raise UnparseableCommand('open " quote')
            piece(text[i + 1:j])
            i = j + 1
        else:
            out.append(c)
            i += 1
    return "".join(out), pieces


def _is_punctuation(tok):
    return bool(tok) and all(c in PUNCTUATION_CHARS for c in tok)


def _operator(tok):
    return tok.strip("\n()")


def _touches_pipe_or_background(tokens, start):
    """True when the next operator is a pipe, or a `&` ends this and-or list."""
    first = True
    i = start
    while i < len(tokens):
        tok = tokens[i]
        i += 1
        if not _ends_a_command(tok):
            continue
        op = _operator(tok)
        if not tok.strip("()"):
            continue
        if first and op in ("|", "|&"):
            return True
        first = False
        if op == "&":
            return True
        if op not in ("&&", "||", "|", "|&"):
            return False
    return False


def _read_cds(text, nesting=0):
    """(any cd not plain, cd targets, needs the home folder, {variable: [written values]})."""
    masked, pieces = _mask_quotes(strip_comments(text))

    def unmask(tok):
        return _MASKED.sub(lambda m: pieces[int(m.group(1))], tok)

    tokens = tokenize(masked)
    not_plain = needs_home = never = False
    targets, assigns = [], {}
    depth = block = ticks = plain_count = plain_groups = 0
    at_start, after_close, declaring = True, False, False
    prev_op, seg_head, left_head = "", None, None
    i, n = 0, len(tokens)
    while i < n:
        tok = tokens[i]
        i += 1
        if _is_punctuation(tok):
            depth = max(0, depth + tok.count("(") - tok.count(")"))
            if _ends_a_command(tok):
                at_start, after_close = True, tok.endswith(")")
                prev_op, left_head, seg_head = _operator(tok), seg_head, None
            elif "(" in tok:
                at_start, after_close = True, False
            elif not tok.strip(")"):
                at_start, after_close = True, True
            else:
                i += 1                  # a redirect, and the word it points at
            continue
        ticks += tok.count("`")
        if tok == "{" and not at_start:
            block += 1
            at_start = True
            continue
        if not at_start:
            if declaring and ENV_ASSIGN.match(tok):
                name, _, value = tok.partition("=")
                assigns.setdefault(name, []).append(unmask(value))
            continue
        closed, after_close = after_close, False
        if ENV_ASSIGN.match(tok):
            name, _, value = tok.partition("=")
            assigns.setdefault(name, []).append(unmask(value))
            continue
        doubtful = (prev_op in ("|", "|&", "||")
                    or (prev_op == "&&" and left_head in _TEST_COMMANDS))
        if tok == "{" and not (depth or block or doubtful or tokens[max(0, i - 2)].strip() == "()"):
            # A group on its own runs in this shell, so a cd in it moves the shell...
            plain_groups += 1
        elif tok == "}" and plain_groups:
            # ...unless the group is piped or sent to the background, which is known only here.
            plain_groups -= 1
            not_plain = not_plain or _touches_pipe_or_background(tokens, i)
        elif tok in _OPENS_BLOCK or tok == "{":
            block += 1
        elif tok in _CLOSES_BLOCK:
            block = max(0, block - 1)
        if tok in _KEEPS_POSITION:
            continue
        if tok == "builtin":
            never = True
            continue
        at_start = False
        declaring = tok in _DECLARES
        if declaring:
            seg_head = tok
            continue
        if tok in ("case", "function"):
            never = True
        if tok in ("pushd", "popd"):
            not_plain = True
            words = [unmask(t) for t in tokens[i:i + 4] if not _is_punctuation(t) and not t.startswith(("-", "+"))]
            if tok == "pushd" and words:
                targets.append(words[0])
            continue
        if tok == "eval":
            not_plain = True
            j = i
            while j < n and not _ends_a_command(tokens[j]):
                j += 1
            if nesting < _MAX_NESTING:
                _, inner, home, inner_vars = _read_cds(" ".join(unmask(t) for t in tokens[i:j]), nesting + 1)
                targets += inner
                needs_home = needs_home or home
                for k, v in inner_vars.items():
                    assigns.setdefault(k, []).extend(v)
            continue
        if tok != "cd":
            seg_head = tok
            continue
        seg_head = "cd"
        j, args = i, []
        while j < n and not _is_punctuation(tokens[j]):
            args.append(tokens[j])
            j += 1
        if args and j < n and args[-1].isdigit() and not _ends_a_command(tokens[j]):
            args.pop()                  # the 2 of `2>/dev/null`
        operands = [a for a in args if not a.startswith("-")]
        if operands:
            targets.append(unmask(operands[-1]))
        elif "-" not in args:
            needs_home = True
        if (len(args) == 1 and not args[0].startswith("-")
                and not (depth or block or ticks % 2 or never or closed)
                and prev_op not in ("|", "|&", "||")
                and not (prev_op == "&&" and left_head in _TEST_COMMANDS)
                and not _touches_pipe_or_background(tokens, i)):
            plain_count += 1
        else:
            not_plain = True

    segments = split_segments(tokens)
    hits = 0
    for seg in segments:
        target = cd_target(seg)
        if target:
            hits += 1
            if not target.startswith("-"):
                targets.append(unmask(target))
        inner = nested_shell_command(seg)
        if inner and nesting < _MAX_NESTING:
            inner_plain, inner_targets, home, inner_vars = _read_cds(unmask(inner), nesting + 1)
            not_plain = not_plain or inner_plain
            targets += inner_targets
            needs_home = needs_home or home
            for k, v in inner_vars.items():
                assigns.setdefault(k, []).extend(v)
    # A cd the guards read, in a place this pass did not call plain (`nice -n 5 cd B`).
    if hits > plain_count:
        not_plain = True
    return not_plain, targets, needs_home, assigns


def _expansions(target, assigns):
    """Every way a target's variables can be read from the command or the environment."""
    done, todo = [], [target]
    for _ in range(4):
        following = []
        for item in todo:
            m = _VARIABLE.search(item)
            if not m:
                done.append(item)
                continue
            values = assigns.get(m.group(1)) or ([os.environ[m.group(1)]] if m.group(1) in os.environ else [])
            for value in values:
                following.append(item[:m.start()] + value + item[m.end():])
        todo = following[:_MAX_FOLDERS]
    return [d for d in done if "$" not in d and "`" not in d]


def _extra_folders(command, cwd):
    scannable, _bodies = strip_heredocs(command)
    try:
        readings = [_read_cds(scannable)]
    except UnparseableCommand:
        readings = []
        for line in scannable.split("\n"):
            try:
                reading = _read_cds(line)
            except UnparseableCommand:
                continue
            # Part of this text cannot be read, so no line of it can be vouched for as plain.
            readings.append((bool(reading[1]) or reading[0],) + reading[1:])
    if not any(r[0] for r in readings):
        return []
    assigns = {}
    for r in readings:
        for k, v in r[3].items():
            assigns.setdefault(k, []).extend(v)
    folders = [cwd]

    def add(path):
        if path not in folders and len(folders) < _MAX_FOLDERS:
            folders.append(path)

    if any(r[2] for r in readings):
        add(os.path.expanduser("~"))
    for r in readings:
        for target in r[1]:
            for item in _expansions(target, assigns):
                item = os.path.expanduser(item)
                if os.path.isabs(item):
                    add(os.path.normpath(item))
                else:
                    for base in list(folders):
                        add(os.path.normpath(os.path.join(base, item)))
    return folders


def extra_folders(command, cwd):
    """Every folder a guard must also judge `command` in, or [] when each `cd` in it is plain.

    The first entry is `cwd`. A guard runs its usual check first, then the same check once per
    folder here, ignoring the `cd`s, and keeps the loudest answer. Never raises: any failure here
    returns `cwd` alone, so the guard still has its usual reading.
    """
    try:
        return _extra_folders(command, cwd)
    except Exception:  # noqa: BLE001 - a guard must never fail because of this helper
        return [cwd]
