#!/usr/bin/env python3
"""Run one bounded job on an OpenAI model through the headless Codex CLI.

The single owner of every Codex delegation in this pack. Its callers:
review-round's and pr-checkpoint's codex-reviewer, threat-model, privacy-review,
dependency-review and secrets-remediation.

Why a wrapper rather than a raw `codex exec` line in each skill:

  1. `timeout` is GNU coreutils and is not on every machine (macOS has none), so
     a shell wrapper could exit 127 and record a failure for every run.
     subprocess(timeout=) is the portable answer.
  2. `codex exec` reads stdin when none is given and hangs. This always closes it.
  3. A delegation that silently does not run is worse than no delegation, because
     the caller reports a review that never happened. The child writes to a
     temporary file inside its own run directory, and --out is written ONLY by an
     os.replace after every validation below has passed. So --out exists if and
     only if this wrapper exited 0, which is the contract every caller reads.
  4. The default sandbox is workspace-write with approvals off. A reviewer must
     not be able to edit the tree it is reviewing, so read-only is the default
     here and a caller has to ask for anything wider.

THE ON/OFF SWITCH. CODEX_REVIEW=off (set once in settings.json "env") makes every
call exit 15 at once: nothing is started or written, and the caller reviews with
Claude only and says so. Unset, on or auto try Codex. CODEX_REVIEW_MODEL names the
model for calls that pass no --model and no --profile; with neither, no -m is sent
and Codex uses its own default model.

RUN IDENTITY AND WHAT IS KEPT. Every attempt, successful or not, gets
`run_id = <UTC yyyymmddThhmmssZ>-<label>-<6 hex>` and a directory
`<dirname(out)>/.codex-runs/<run_id>/`. Nothing in there is ever unlinked, so the
history of a job survives. The directory holds:

  answer.tmp          what the child wrote, on every path including failure
  previous-result.md  an older --out file, MOVED here rather than deleted
  previous-receipt.json  the older --out's receipt, moved with it
  request.json        the request record (request v1)
  receipt.json        the execution receipt: `started` at launch, then the
                      final status on EVERY path (succeeded, refused, failed,
                      timeout, interrupted). Hashes and codes only: never the
                      brief, the answer text or the child's stderr.
  child-stderr.txt    the child's stderr on a nonzero exit, written, never printed
  failure.json        run_id, label, reason code and text, exit code, elapsed, sizes
  manifest.json       written only on success: run_id, label, model, cli_version,
                      candidate_sha, elapsed, exit code, sha256 of the answer, a
                      UTC timestamp and the token count when one was reported.
                      cli_version is read from the child's own banner, and only
                      a child that printed none costs a `codex --version` call.

ORDER, and the order is the fix. The lock is taken first, then an old --out is
moved into the run directory, and only then does preflight run. So a preflight
failure cannot leave a stale consumable result behind.

RECEIPT FIRST, ANSWER SECOND. On success the final receipt is written in the run
dir, then published to `<out>.receipt.json` (boundary B1), then the answer is
moved to --out (B2). A crash before B1 publishes nothing; between B1 and B2 it
leaves a receipt with no answer. A consumer does not trust "the file exists": it
runs

    codex_delegate.py verify --out X --request-id ID [--require-certified]

which exits 0 only when the published receipt says succeeded, names ID, is
untampered, and hashes to the answer's exact bytes (1 otherwise, one reason
code printed). Only a typed review is certified. SIGINT and SIGTERM stop the
codex child, record `interrupted`, publish nothing and exit 128+signal.

ONE JOB PER --out. A non-blocking flock is held on `<out>.lock` for the whole
run. A second job on the same --out exits 8 at once and touches nothing, rather
than racing the first one's publish.

--out IS REFUSED (exit 7) when it is a symlink, when its parent is a symlink, or
when it resolves inside --repo. Both sides are realpath'd before the comparison.
The reviewed tree must not gain a file from its own review.

VALIDATION, on a child exit 0, in this order, each with its own exit code:

  empty answer                                            6
  --candidate-sha given and the sha is absent              9
  --output-schema given and the answer is not valid JSON
    carrying schema_version, status, reviewed_head_sha
    and a findings list                                   10
  --output-schema given and status is not "complete"      11
  no schema, and the answer opens by declaring it could
  not do the work                                         11

TYPED REVIEW (--job-type review). Before preflight the adapter resolves --base
and --head to full commits and the logical repo ID (--repo-id, else the
normalised origin URL), writes request.json into the run directory, appends the
four identity fields to the brief and passes review_result_v1.schema.json. The
answer is then judged by delegation_contract.py, not by the table above:
identity compared field by field (9), not finished (11), anything else damaged
(10). An unresolvable request, or --output-schema / --candidate-sha on a typed
review, exits 13 with no model call.

THE PROSE INABILITY TEST AND ITS BOUNDARY. With no schema there is nothing to
parse, so the wrapper reads the FIRST 600 CHARACTERS of the answer and looks,
case-insensitively, for one of five phrases: "could not inspect",
"unable to access", "cannot review", "no access to the repository",
"repository was not available". It is a substring test over a fixed list, so it
is deliberately narrow and it is NOT a general detector. It will miss a
paraphrase ("the working tree was empty", "I had nothing to read"), and it will
fire on a review that happens to QUOTE one of those phrases in its opening. A
caller that needs a reliable answer to "did this job actually do the work" must
pass --output-schema and let the model declare its own status.

Exit codes, all distinct so a caller can say WHY it did not run:
  0  ok, and --out holds a validated answer
  2  codex CLI not installed
  3  no Codex credential
  4  timed out
  5  codex returned an error
  6  ran, but produced no answer
  7  --out path refused (symlink, or inside the reviewed repo)
  8  another job holds the lock on this --out
  9  the answer does not name the candidate SHA that was asked for
 10  the answer does not match the response schema
 11  the answer declares its own work incomplete
 12  the model provider rejected the request (invalid_request_error); only
     its type, code and param are kept, and it is never retried on the same
     model
 13  the request was refused before launch (a typed review whose commits or
     repository identity cannot be resolved); no model call was made
 15  Codex review is switched OFF (CODEX_REVIEW=off, 0, false or no, or any
     word that is not also on, auto, 1, true or yes); nothing was started, and an
     older --out moved into the run history. The caller reviews with Claude only
     and says so. Unset, empty, on and auto try Codex, so a broken login still
     fails loud with 2 or 3
130  interrupted by SIGINT; 143 interrupted by SIGTERM (nothing published)
"""

import argparse
import binascii
import datetime
import fcntl
import hashlib
import json
import os
import pathlib
import re
import shutil
import signal
import subprocess
import sys
import time

import delegation_contract as contract
import process_group

# CODEX_REVIEW_MODEL overrides the default for every call that names no model
# and no profile; an explicit --model or --profile still wins.
MODEL = os.environ.get("CODEX_REVIEW_MODEL", "").strip() or None
# Sent on every call that names no effort, so a run never depends on the user's
# Codex config (which --isolated ignores).
EFFORT = "high"
# Where the Codex CLI keeps its login: CODEX_HOME when set, else ~/.codex.
CODEX_HOME = pathlib.Path(os.environ.get("CODEX_HOME") or
                          (pathlib.Path.home() / ".codex")).expanduser()
DEFAULT_TIMEOUT = 1800

RUNS_DIRNAME = ".codex-runs"
CHILD_RECORD = "child-process.json"
# Grace between TERM and KILL for the Codex group. Tests use a
# short one; the supervisor above waits 20 s so it never cuts this one short.
def _stop_grace(raw, default=10.0, ceiling=300.0):
    """A finite grace between 0 and 300 s; anything else (nan, inf, junk,
    negative) falls back to the default, so a stop is always bounded."""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return value if 0 <= value <= ceiling else default


STOP_GRACE = _stop_grace(os.environ.get("CODEX_DELEGATE_STOP_GRACE", "10"))
VERSION_TIMEOUT = 10
INABILITY_WINDOW = 600
INABILITY_PHRASES = (
    "could not inspect",
    "unable to access",
    "cannot review",
    "no access to the repository",
    "repository was not available",
)
SCHEMA_STATUSES = ("complete", "incomplete", "failed")

EXIT_OK = 0
EXIT_NO_CLI = 2
EXIT_NO_CRED = 3
EXIT_TIMEOUT = 4
EXIT_CHILD_FAILED = 5
EXIT_EMPTY = 6
EXIT_UNSAFE_OUT = 7
EXIT_LOCKED = 8
EXIT_IDENTITY = 9
EXIT_MALFORMED = 10
EXIT_INCOMPLETE = 11
EXIT_PROVIDER_REJECTED = 12
EXIT_REQUEST_REFUSED = 13
EXIT_SWITCHED_OFF = 15

# The ON/OFF switch, for a user with no Codex account. Read from the environment,
# which a user sets once in settings.json "env" (Claude Code passes it to every
# subprocess it starts). Only OFF changes anything.
SWITCH_ENV = "CODEX_REVIEW"
SWITCH_ON = ("on", "auto", "1", "true", "yes")
SWITCH_OFF = ("off", "0", "false", "no")
OFF_HINT = f"no Codex account? set {SWITCH_ENV}=off"

def review_switch():
    """("on" | "off", why). Unset reads as on. A value that is neither on nor
    off reads as off, named, because a typo must not start a paid model call."""
    raw = os.environ.get(SWITCH_ENV)
    value = (raw or "").strip().lower()
    if not value or value in SWITCH_ON:
        return "on", f"{SWITCH_ENV}={value or 'unset'}"
    if value in SWITCH_OFF:
        return "off", f"{SWITCH_ENV}={value}"
    return "off", (f"{SWITCH_ENV}={raw.strip()!r} is not on, off or auto, "
                   "so it is read as off")


def retire_for_off(out, repo, label):
    """Under OFF, move an older answer and its receipt into the run history, as a
    real run does first, so "--out exists only after this run succeeded" still
    holds. Creates nothing when there is nothing to move, and leaves a path that
    check_out_path refuses, or that another job holds the lock on, alone."""
    out = pathlib.Path(out).expanduser()
    receipt = pathlib.Path(contract.receipt_path(out))
    if not any(p.exists() or p.is_symlink() for p in (out, receipt)):
        return
    if check_out_path(out, repo)[0]:
        return
    lock_fd = os.open(str(out.parent / (out.name + ".lock")),
                      os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return
        rundir = out.parent / RUNS_DIRNAME / make_run_id(label + "-off")
        rundir.mkdir(parents=True, exist_ok=True)
        if out.exists() or out.is_symlink():
            os.replace(str(out), str(rundir / "previous-result.md"))
        if receipt.exists() or receipt.is_symlink():
            os.replace(str(receipt), str(rundir / "previous-receipt.json"))
    finally:
        os.close(lock_fd)


def switched_off(label, why):
    """The one line every caller sees under OFF. It must never read as a clean
    review: it says nothing ran and that the round is Claude-only."""
    return (f"CODEX {label}: SKIPPED, Codex review is off ({why}). Nothing was "
            "started or written; review with Claude reviewers only, and say so.")


def preflight():
    """Prove Codex can run before a caller commits to the answer.

    Reports WHERE it looked on failure, never a bare 'unavailable'.
    """
    exe = shutil.which("codex")
    if not exe:
        return EXIT_NO_CLI, ("codex CLI not on PATH; install the Codex CLI ("
                             + OFF_HINT + ")")

    auth = CODEX_HOME / "auth.json"
    if not auth.exists():
        return EXIT_NO_CRED, f"no Codex credential at {auth}; run `codex login` ({OFF_HINT})"
    try:
        mode = json.load(open(auth)).get("auth_mode")
    except Exception as exc:                       # noqa: BLE001
        return EXIT_NO_CRED, f"{auth} is unreadable ({exc.__class__.__name__})"
    if not mode:
        return EXIT_NO_CRED, f"{auth} carries no auth_mode; run `codex login`"

    return 0, f"codex at {exe}, auth_mode={mode}"


def parse_tokens(text):
    """Codex writes its banner AND the token summary to STDERR; stdout carries
    only the final answer. Parsing stdout alone makes this field print "unknown"
    on every real run. Read both.
    
    The LAST match wins: stderr also echoes what the model read, so an earlier
    "tokens used" can be text from a file. The CLI prints its summary last."""
    found = re.findall(r"tokens used[^\d]*([\d,]+)", text)
    return found[-1] if found else "unknown"


def safe_label(label):
    """A label reaches the filesystem as part of a run id, so it cannot carry a
    separator. A caller passing `../../etc` must not choose the directory."""
    clean = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(label or "")).strip("-")
    return clean[:40] or "delegation"


def make_run_id(label):
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    rand = binascii.hexlify(os.urandom(3)).decode("ascii")
    return f"{stamp}-{safe_label(label)}-{rand}"


def check_out_path(out, repo):
    """Refuse a destination the caller cannot safely consume.

    A symlink at --out, or at its parent, means the publish would write through
    to somewhere nobody named. A destination inside --repo means the review
    writes into the tree it is reviewing, which the read-only sandbox exists to
    prevent. Both paths are realpath'd, because two different strings can be one
    directory.
    """
    if out.is_symlink():
        return EXIT_UNSAFE_OUT, f"--out is a symlink ({out}); name the real file"
    if out.parent.is_symlink():
        return (EXIT_UNSAFE_OUT,
                f"the parent of --out is a symlink ({out.parent}); name the real "
                "directory")
    if repo:
        repo_real = os.path.realpath(str(pathlib.Path(repo).expanduser()))
        out_real = os.path.realpath(str(out))
        inside = (out_real == repo_real
                  or out_real.startswith(repo_real.rstrip(os.sep) + os.sep))
        if inside:
            return (EXIT_UNSAFE_OUT,
                    f"--out ({out_real}) is inside --repo ({repo_real}); a review "
                    "must not write into the tree it reviews")
    return 0, ""


def write_failure(rundir, run_id, label, reason_code, reason_text, exit_code,
                  elapsed, answer_bytes, stderr_lines, stdout_lines):
    """Record why an attempt produced nothing consumable.

    The child's stderr TEXT is deliberately not in here. It can carry a token or
    a key, and this file sits beside the caller's output directory. The count and
    the byte size are enough to tell a crash from a quiet refusal; the text is in
    child-stderr.txt when there was any.
    """
    payload = {
        "run_id": run_id,
        "label": label,
        "reason_code": reason_code,
        "reason": reason_text,
        "exit_code": exit_code,
        "elapsed_seconds": round(elapsed, 3),
        "answer_bytes": answer_bytes,
        "child_stderr_lines": stderr_lines,
        "child_stdout_lines": stdout_lines,
        "utc": datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"),
    }
    (rundir / "failure.json").write_text(json.dumps(payload, indent=2,
                                                    sort_keys=True))


def find_inability(answer):
    """Return the phrase the answer opens with, or None. See the module docstring
    for why this is a fixed five-phrase list over the first 600 characters."""
    head = answer[:INABILITY_WINDOW].lower()
    for phrase in INABILITY_PHRASES:
        if phrase in head:
            return phrase
    return None


def validate_schema(answer):
    """Check a schema-mode answer. Returns (exit_code, reason_code, reason)."""
    try:
        doc = json.loads(answer)
    except Exception as exc:                       # noqa: BLE001
        return (EXIT_MALFORMED, "malformed",
                f"the answer is not JSON ({exc.__class__.__name__})")
    if not isinstance(doc, dict):
        return (EXIT_MALFORMED, "malformed",
                f"the answer is JSON {type(doc).__name__}, not an object")
    for field in ("schema_version", "status", "reviewed_head_sha"):
        if not doc.get(field):
            return (EXIT_MALFORMED, "malformed",
                    f"the answer carries no {field}")
    if not isinstance(doc.get("findings"), list):
        return (EXIT_MALFORMED, "malformed",
                "the answer's findings field is not a list")
    status = doc["status"]
    if status not in SCHEMA_STATUSES:
        return (EXIT_MALFORMED, "malformed",
                f"status={status!r} is not one of {', '.join(SCHEMA_STATUSES)}")
    if status != "complete":
        return (EXIT_INCOMPLETE, "self-declared-incomplete",
                f"the answer declares status={status}")
    return 0, "", ""


# A reason code from the contract module mapped to this adapter's exit status.
REVIEW_EXIT = {
    "identity-mismatch": EXIT_IDENTITY,
    "incomplete": EXIT_INCOMPLETE,
}


def judge_typed(job_type, answer, request):
    """A typed job's answer against its request, by that job type's own rule
    (delegation_contract.validate_result). (exit, reason_code, reason); exit 0
    means it may be published. Any refusal the table above does not name is a
    malformed answer, exit 10."""
    try:
        contract.validate_result(job_type, answer, request)
    except contract.ContractError as exc:
        return REVIEW_EXIT.get(exc.code, EXIT_MALFORMED), exc.code, exc.detail
    return 0, "", ""


def parse_cli_version(text):
    """The version out of the child's own banner, or None.

    The banner is already captured, so reading it costs nothing and it names the
    binary that actually produced THIS answer. Asking `codex --version` at
    publish time spawns a second process on every successful job and would
    report whatever is installed by then, which is not the same claim after a
    `codex update` mid-run. Measured on codex-cli 0.150.1, the first stderr line
    is "OpenAI Codex v0.150.1".
    """
    m = re.search(r"Codex\s+v?([0-9][^\s]*)", text)
    return m.group(1) if m else None


def cli_version():
    """Fallback for a child that printed no banner. Best effort: a version
    nobody could read is recorded as unknown rather than as an empty string
    that reads like a missing field."""
    try:
        proc = subprocess.run(["codex", "--version"], stdin=subprocess.DEVNULL,
                              capture_output=True, text=True,
                              timeout=VERSION_TIMEOUT)
    except Exception:                              # noqa: BLE001
        return "unknown"
    if proc.returncode != 0:
        return "unknown"
    line = (proc.stdout or "").strip().splitlines()
    return line[0].strip() if line and line[0].strip() else "unknown"


def pin_prompt(prompt, candidate_sha):
    """Tell the model the one thing --candidate-sha will check, so no brief can omit it.
    
    Without this line a complete, correct review can still exit 9 because the brief
    never asked the model to name the commit. The check lives here, so the
    instruction does too. With no candidate sha the prompt is passed through
    unchanged."""
    if not candidate_sha:
        return prompt
    return (prompt.rstrip("\n") + "\n\n"
            f"This run is bound to commit {candidate_sha}. Write that commit id IN FULL "
            f"somewhere in your answer (run `git rev-parse {candidate_sha}` if it is "
            "short). An answer that does not name it is refused and discarded.\n")


REVIEW_SCHEMA = pathlib.Path(__file__).resolve().parent / "review_result_v1.schema.json"
PROFILES_FILE = pathlib.Path(__file__).resolve().parent / "model_profiles.json"


def resolve_model(model, profile, effort, profile_reason, smoke):
    """(model, effort) to send, or ContractError. No profile: `model` or the
    default MODEL (None sends no -m), at `effort` or the default EFFORT. A profile
    decides both, and is refused (never defaulted) when it is unknown, when the
    caller's model or effort disagrees with it, when it is still locked behind the
    smoke, or when it needs a reason and got none."""
    if effort is not None and not re.fullmatch(contract.EFFORT_PATTERN, effort):
        raise contract.ContractError("bad-effort", f"effort {effort!r} is not a plain word")
    if not profile:
        return model or MODEL, effort or EFFORT
    prof =contract.load_profile(PROFILES_FILE, profile)
    if model is not None and model != prof["model"]:
        raise contract.ContractError(
            "conflicting-flags",
            f"model {model!r} disagrees with profile {profile!r} ({prof['model']})")
    if effort is not None and effort != prof["effort"]:
        raise contract.ContractError(
            "conflicting-flags",
            f"effort {effort!r} disagrees with profile {profile!r} ({prof['effort']})")
    if prof["status"] == "pending-smoke" and not smoke:
        raise contract.ContractError(
            "profile-locked",
            f"profile {profile!r} is pending its smoke; only a --smoke call may use it")
    if prof["requires_reason"] and not (profile_reason and profile_reason.strip()):
        raise contract.ContractError(
            "reason-required", f"profile {profile!r} needs a non-empty --profile-reason")
    return prof["model"], prof["effort"]


def parse_banner(text):
    """(observed model, observed effort) from the child's banner, else
    "unknown" for each. The real CLI prints `model: X` and `reasoning effort: Y`
    on stderr, even on a refused call."""
    m = re.search(r"^model: (\S+)", text, re.M)
    e = re.search(r"^reasoning effort: (\S+)", text, re.M)
    return (m.group(1) if m else "unknown"), (e.group(1) if e else "unknown")


def resolve_commit(checkout, rev, name):
    """The full 40-hex commit a revision names in `checkout`, or ContractError.
    A short id is resolved here, before launch, so the identity compare after
    the run can be exact."""
    if not rev:
        raise contract.ContractError("unresolvable-commit",
                                     f"a typed review needs --{name}")
    try:
        proc = subprocess.run(
            ["git", "-C", checkout, "rev-parse", "--verify", "--quiet",
             f"{rev}^{{commit}}"],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise contract.ContractError(
            "unresolvable-commit",
            f"--{name} {rev!r}: git did not answer ({exc.__class__.__name__})")
    sha = proc.stdout.strip()
    if proc.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise contract.ContractError(
            "unresolvable-commit", f"--{name} {rev!r} names no commit in {checkout}")
    return sha


def check_checkout_is_head(checkout, head_sha, allow_untracked=False):
    """The model reads the checkout, so a review certifies `head_sha` only when
    the checkout IS that commit with nothing uncommitted, untracked files
    included. Raises ContractError before any launch.
    
    Returns the untracked paths that were WAIVED, so the receipt can record
    them; an empty list when the checkout was clean."""
    def git_out(*args):
        try:
            proc = subprocess.run(["git", "-C", checkout, *args],
                                  stdin=subprocess.DEVNULL, capture_output=True,
                                  text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise contract.ContractError(
                "checkout-not-at-head", f"git did not answer ({exc.__class__.__name__})")
        if proc.returncode != 0:
            raise contract.ContractError(
                "checkout-not-at-head", f"git {args[0]} failed in {checkout}")
        return proc.stdout
    at = git_out("rev-parse", "HEAD").strip()
    if at != head_sha:
        raise contract.ContractError(
            "checkout-not-at-head",
            f"{checkout} is at {at}, not the reviewed head {head_sha}")
    if git_out("status", "--porcelain", "--untracked-files=no").strip():
        raise contract.ContractError(
            "checkout-dirty",
            f"{checkout} has uncommitted changes to tracked files, so the model "
            "would not be reading the reviewed head")
    # Untracked files refuse by DEFAULT, because Codex reads the working
    # DIRECTORY, not a git object, so an untracked file
    # is readable by the model and belongs to no commit. An untracked root
    # AGENTS.md is instructions (`--ignore-rules` is isolated mode only), and an
    # untracked source file is content the identity fields cannot bind. Letting
    # them through made a certified review of `head_sha` a false statement.
    # `--allow-untracked` is a RECORDED waiver, never a weakened guard: the
    # paths are returned for the receipt and printed where the operator sees
    # them. `git status` already omits ignored paths, so genuinely ignored build
    # output never reaches here.
    untracked = sorted(
        line[3:] for line in
        git_out("status", "--porcelain", "--untracked-files=all").splitlines()
        if line.startswith("?? "))
    # A tracked symlink is the same hole: git records only the link
    # text, so the model follows it to content no commit contains unless the
    # target is itself a tracked path inside the checkout.
    entries = [e.split("\t", 1) for e in git_out("ls-files", "-s", "-z").split("\0") if e]
    tracked = {path for _meta, path in entries}
    root = os.path.realpath(checkout)
    for meta, path in entries:
        if not meta.startswith("120000 "):
            continue
        target = os.path.realpath(os.path.join(root, path))
        rel = os.path.relpath(target, root)
        bound = (not rel.startswith(os.pardir) and
                 (rel in tracked or any(t.startswith(rel + "/") for t in tracked)))
        if not bound:
            untracked.append(f"symlink {path} -> {target}")
    if untracked and not allow_untracked:
        shown = ", ".join(untracked[:5]) + ("..." if len(untracked) > 5 else "")
        raise contract.ContractError(
            "checkout-untracked",
            f"{checkout} has {len(untracked)} untracked file(s) or escaping "
            f"symlink(s) the model can read but no commit contains ({shown}); "
            "commit them, remove them, ignore them, or pass --allow-untracked "
            "to record the waiver")
    return untracked


def origin_url(checkout):
    """The checkout's `origin` URL, or None. Only the adapter runs git."""
    try:
        proc = subprocess.run(
            ["git", "-C", checkout, "remote", "get-url", "origin"],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


def resolve_review_request(prompt, repo, model, timeout, repo_id, base, head,
                           request_id, settings, allow_untracked=False,
                           waived=None):
    """Build the request v1 for a typed review, or raise ContractError. Runs
    before preflight and before any model call, so a refusal costs nothing.

    `waived` is an optional list the waived untracked paths are written into,
    so the caller can put them in the receipt without changing the request (and
    so every receipt written before this existed still hashes correctly)."""
    if not repo:
        raise contract.ContractError("unresolvable-repo",
                                     "a typed review needs --repo")
    checkout = os.path.realpath(str(pathlib.Path(repo).expanduser()))
    if not repo_id:
        repo_id = contract.normalise_repo_url(origin_url(checkout))
    if not repo_id:
        raise contract.ContractError(
            "unresolvable-repo",
            f"no --repo-id, and the origin of {checkout} names no repository "
            "(missing, a local path, or a file URL)")
    base_sha = resolve_commit(checkout, base, "base")
    head_sha = resolve_commit(checkout, head, "head")
    untracked = check_checkout_is_head(checkout, head_sha, allow_untracked)
    if waived is not None:
        waived.extend(untracked)
    return contract.build_request(
        job_type="review", repo_id=repo_id, checkout=checkout,
        base_sha=base_sha, head_sha=head_sha,
        brief=prompt, model=model, timeout_seconds=timeout,
        request_id=request_id, **settings)


def identity_block(request):
    """Appended to a typed review's brief: the four identity fields the answer
    must echo verbatim, so no brief can forget to ask for them."""
    echo = {f: request[f] for f in contract.IDENTITY_FIELDS}
    return ("\n\nAnswer with ONE JSON object matching the response schema "
            f"({contract.REVIEW_RESULT_VERSION}). Copy this object into its "
            "`request` field exactly, byte for byte:\n"
            + json.dumps(echo, indent=2) + "\n")


def time_budget_block(timeout):
    """Appended to a typed review's brief, before the identity block, so no brief
    can leave out the clock. A review brief without one can run to the cap and
    answer nothing; a brief that carries a budget finishes. The cap lives here, so
    the budget does too."""
    minutes = max(1, int(timeout or DEFAULT_TIMEOUT) // 60)
    write_by = max(1, minutes * 2 // 3)
    return ("\n\nTIME BUDGET: this run is stopped after "
            f"{minutes} minutes and an unfinished answer is discarded. Write your "
            f"answer by minute {write_by} even if some spots are unchecked, and list "
            "those as unchecked. Prefer reading code to long commands, and open a "
            "standards file only for the rule a finding needs.")


def sandbox_block():
    """Appended to a typed review's brief, beside the time budget. The read-only
    sandbox has no writable temp dir, so a brief that asks for pytest gets a stop
    before collection and an honest status=incomplete (exit 11, uncertified).
    Here the rule cannot be left out."""
    return ("\n\nSANDBOX: this run is read-only with no writable temporary "
            "directory, so do not run the test suite (pytest and anything else that "
            "needs a temp dir stops before it starts). The caller runs the suite "
            "outside the sandbox. You may still call a single test function directly. "
            "List what you could not run under checks_not_run, and still return status "
            "complete when the review itself is done.")


def run(prompt, repo, out, sandbox, timeout, model, label, isolated=False,
        candidate_sha=None, output_schema=None, *, job_type=None, repo_id=None,
        base=None, head=None, request_id=None, profile=None,
        reasoning_effort=None, profile_reason=None, smoke=False,
        allow_untracked=False):
    state, why = review_switch()
    if state == "off":
        print(switched_off(label, why), file=sys.stderr)
        retire_for_off(out, repo, label)
        return EXIT_SWITCHED_OFF

    out = pathlib.Path(out).expanduser()

    code, detail = check_out_path(out, repo)
    if code:
        print(f"CODEX {label}: REFUSED, {detail}", file=sys.stderr)
        return code

    out.parent.mkdir(parents=True, exist_ok=True)

    lock_path = out.parent / (out.name + ".lock")
    lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(lock_fd)
        print(f"CODEX {label}: LOCKED, another job holds {out} "
              f"(lock at {lock_path}); nothing was touched", file=sys.stderr)
        return EXIT_LOCKED
    try:
        return _run_locked(prompt, repo, out, sandbox, timeout, model, label,
                           isolated, candidate_sha, output_schema, job_type,
                           repo_id, base, head, request_id, profile,
                           reasoning_effort, profile_reason, smoke,
                           allow_untracked)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


class Interrupted(BaseException):
    """Raised by main()'s SIGINT/SIGTERM handler. A BaseException, so no
    `except Exception` on the way up mistakes it for an internal error."""

    def __init__(self, signum):
        super().__init__(signum)
        self.signum = signum


def _raise_interrupted(signum, frame):
    raise Interrupted(signum)


def _boundary(name):
    """A named point in the publication order. A no-op; tests replace it (it is
    looked up as a module global at every call) to crash the process there for
    real, so each boundary's promise is proven rather than argued."""


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_json_atomic(path, doc):
    """Temp file in the same directory, fsync, rename: a reader sees the old
    file or the whole new one, never half of it."""
    path = pathlib.Path(path)
    tmp = path.parent / (path.name + ".tmp")
    with open(str(tmp), "w") as fh:
        fh.write(json.dumps(doc, indent=2, sort_keys=True))
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(str(tmp), str(path))


def legacy_request(prompt, repo, model, timeout, job_type, request_id, settings):
    """The request record for a call that is not a typed review. Nothing here
    can refuse, and nothing here starts a process: a legacy call runs exactly
    as before, it just gets a record. Its repo_id stays null; only a typed review
    needs one."""
    checkout = (os.path.realpath(str(pathlib.Path(repo).expanduser()))
                if repo else None)
    return contract.build_request(
        job_type=job_type or "legacy",
        repo_id=None,
        checkout=checkout, base_sha=None, head_sha=None, brief=prompt,
        model=model, timeout_seconds=timeout, request_id=request_id, **settings)


def _run_locked(prompt, repo, out, sandbox, timeout, model, label, isolated,
                candidate_sha, output_schema, job_type, repo_id, base, head,
                request_id, profile, reasoning_effort, profile_reason, smoke,
                allow_untracked=False):
    started = time.time()
    started_utc = utc_now()
    run_id = make_run_id(label)
    rundir = out.parent / RUNS_DIRNAME / run_id
    rundir.mkdir(parents=True, exist_ok=True)
    answer_tmp = rundir / "answer.tmp"

    # An older answer is MOVED, never unlinked: the history is kept and the path
    # is empty from here on, whatever happens next.
    if out.exists() or out.is_symlink():
        os.replace(str(out), str(rundir / "previous-result.md"))
    # Its receipt goes with it, so neither half of the previous attempt can be
    # read as this attempt's.
    old_receipt = pathlib.Path(contract.receipt_path(out))
    if old_receipt.exists() or old_receipt.is_symlink():
        os.replace(str(old_receipt), str(rundir / "previous-receipt.json"))

    # The request is only known once resolved; a refusal before that records
    # the bare request (no commits, no repo ID) so its receipt still says what
    # was asked for and by whom.
    held = {"request": None, "final": False, "effort": reasoning_effort,
            "observed": ("unknown", "unknown"), "provider_error": None,
            "untracked_waived": []}

    def receipt(status, reason_code, exit_code, certified=False,
                answer_sha256=None, answer_bytes=None, cli="unknown",
                tokens="unknown", final=None):
        request = held["request"] or contract.build_request(
            job_type=job_type or "legacy", repo_id=None,
            checkout=os.path.realpath(str(pathlib.Path(repo).expanduser()))
            if repo else None,
            base_sha=None, head_sha=None, brief=prompt, model=model,
            timeout_seconds=timeout, request_id=request_id, profile=profile,
            effort=held["effort"], profile_reason=profile_reason, smoke=smoke)
        doc = contract.build_receipt(
            run_id=run_id, request=request, terminal_status=status,
            reason_code=reason_code, exit_code=exit_code, certified=certified,
            answer_sha256=answer_sha256, answer_bytes=answer_bytes,
            command_mode="isolated" if isolated else "ordinary",
            argv_model=model, argv_effort=held["effort"] or "inherited",
            cli_version=cli, tokens=tokens,
            provider_error=held["provider_error"],
            started_utc=started_utc,
            finished_utc=None if status == "started" else utc_now(),
            observed_model=held["observed"][0],
            observed_effort=held["observed"][1],
            untracked_waived=held["untracked_waived"])
        write_json_atomic(rundir / "receipt.json", doc)
        held["final"] = status != "started" if final is None else final
        return doc

    def fail(reason_code, reason, exit_code, headline, answer_bytes=0,
             stderr_lines=0, stdout_lines=0):
        """Every refusal after the run directory exists ends here: one line,
        failure.json as before, and a final receipt in the run dir only."""
        print(f"CODEX {label}: {headline}, {reason} (run {run_id})", file=sys.stderr)
        write_failure(rundir, run_id, label, reason_code, reason, exit_code,
                      time.time() - started, answer_bytes, stderr_lines,
                      stdout_lines)
        status = {EXIT_REQUEST_REFUSED: "refused",
                  EXIT_TIMEOUT: "timeout"}.get(exit_code, "failed")
        receipt(status, reason_code, exit_code, answer_bytes=answer_bytes or None)
        return exit_code

    def body():
        nonlocal output_schema, model
        # A typed review is resolved BEFORE preflight: an unresolvable commit or
        # repository refuses here, with no model call.
        try:
            model, held["effort"] = resolve_model(model, profile, reasoning_effort,
                                                  profile_reason, smoke)
            settings = {"profile": profile, "effort": held["effort"],
                        "profile_reason": profile_reason, "smoke": smoke}
            # A typed job is judged by its own rule, so a legacy flag would be
            # silently dropped: refuse it instead.
            if job_type:
                for flag, given in (("--output-schema", output_schema),
                                    ("--candidate-sha", candidate_sha)):
                    if given:
                        raise contract.ContractError(
                            "conflicting-flags",
                            f"{flag} does not apply to --job-type {job_type}")
            if job_type == "review":
                request = resolve_review_request(
                    prompt, repo, model, timeout, repo_id, base, head,
                    request_id, settings, allow_untracked,
                    held["untracked_waived"])
                if held["untracked_waived"]:
                    print(f"CODEX {label}: WAIVER, this review reads "
                          f"{len(held['untracked_waived'])} untracked file(s) "
                          "that no commit contains; they are named in the "
                          "receipt's untracked_waived", file=sys.stderr)
                output_schema = str(REVIEW_SCHEMA)
            else:
                request = legacy_request(prompt, repo, model, timeout, job_type,
                                         request_id, settings)
        except contract.ContractError as exc:
            return fail(exc.code, exc.detail, EXIT_REQUEST_REFUSED,
                        f"REQUEST REFUSED, {exc.code}")
        typed = job_type == "review"
        held["request"] = request
        write_json_atomic(rundir / "request.json", request)

        receipt("started", None, None)
        _boundary("started")

        code, detail = preflight()
        if code:
            return fail("preflight", detail, code, "NOT RUN")

        # The model block goes LAST, after every config flag, so isolated mode
        # cannot strip it. No model named anywhere: no -m, and Codex uses its own
        # default model.
        cmd = ["codex", "exec",
               "--sandbox", sandbox,
               "--skip-git-repo-check",
               "--color", "never",
               "-o", str(answer_tmp),
               ]
        if output_schema:
            cmd += ["--output-schema", str(pathlib.Path(output_schema).expanduser())]
        if isolated:
            # Opt-in: ignore the user's Codex config, rules and history. Auth still
            # uses CODEX_HOME.
            cmd += ["--ignore-user-config", "--ignore-rules", "--ephemeral"]
        if repo:
            cmd += ["-C", str(pathlib.Path(repo).expanduser().resolve())]
        if model:
            cmd += ["-m", model]
        if held["effort"]:
            cmd += ["-c", f'model_reasoning_effort="{held["effort"]}"']
        if typed:
            cmd.append(prompt.rstrip("\n") + time_budget_block(timeout) + sandbox_block()
                       + identity_block(request))
        else:
            cmd.append(pin_prompt(prompt, candidate_sha))

        try:
            proc, stdout_text, stderr_text = run_codex_group(cmd, timeout, rundir,
                                                             run_id)
        except subprocess.TimeoutExpired:
            return fail("timeout", f"no answer within {timeout}s", EXIT_TIMEOUT,
                        f"TIMED OUT after {timeout}s", _size(answer_tmp))
        stderr_text = stderr_text or ""
        stdout_text = stdout_text or ""
        held["observed"] = parse_banner(stderr_text + "\n" + stdout_text)
        stderr_lines = len(stderr_text.splitlines())
        stdout_lines = len(stdout_text.splitlines())

        if proc.returncode != 0:
            # The child's stderr can carry a credential, so it is never printed.
            # One line naming the class and the size, and the text goes to a file.
            klass = "child-signal" if proc.returncode < 0 else "child-nonzero"
            if stderr_text:
                (rundir / "child-stderr.txt").write_text(stderr_text)
            # Only type, code and param are kept. A rejection is exit 12 and is
            # never retried on the same model: the identical request would be
            # rejected again.
            err = contract.parse_provider_error(stderr_text)
            held["provider_error"] = err
            if err and err["type"] == contract.PROVIDER_REJECTION_TYPE:
                # code and param go to the receipt only: they are copied from
                # child stderr, which is never printed.
                return fail("provider-rejected",
                            f"type={err['type']}, code and param in the receipt",
                            EXIT_PROVIDER_REJECTED,
                            f"PROVIDER REJECTED rc={proc.returncode}, {stderr_lines} "
                            f"stderr lines withheld, see {rundir}",
                            _size(answer_tmp), stderr_lines, stdout_lines)
            return fail(klass, f"codex exited {proc.returncode}", EXIT_CHILD_FAILED,
                        f"FAILED class={klass} rc={proc.returncode}, {stderr_lines} "
                        f"stderr lines withheld, see {rundir}",
                        _size(answer_tmp), stderr_lines, stdout_lines)

        # Read as BYTES: the receipt hashes exactly what gets published, and an
        # invalid byte must not crash the run. A legacy answer is checked on a
        # decoded copy where an undecodable byte becomes U+FFFD. A typed answer
        # is decoded strictly: what is validated must be what is published.
        raw = answer_tmp.read_bytes() if answer_tmp.exists() else b""
        if job_type:
            try:
                answer = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                return fail("malformed", f"the answer is not valid UTF-8 (byte {exc.start})",
                            EXIT_MALFORMED, "MALFORMED", len(raw), stderr_lines,
                            stdout_lines)
        else:
            answer = raw.decode("utf-8", errors="replace")
        if not answer.strip():
            return fail("empty", "the answer is empty", EXIT_EMPTY,
                        "RAN BUT RETURNED NOTHING", _size(answer_tmp), stderr_lines,
                        stdout_lines)

        if candidate_sha and candidate_sha.lower() not in answer.lower():
            return fail("identity-mismatch",
                        f"the answer does not name candidate sha {candidate_sha}",
                        EXIT_IDENTITY, "IDENTITY MISMATCH", len(answer),
                        stderr_lines, stdout_lines)

        if job_type:
            code, reason_code, reason = judge_typed(job_type, answer, request)
        elif output_schema:
            code, reason_code, reason = validate_schema(answer)
        else:
            code, reason_code, reason = 0, "", ""
            phrase = find_inability(answer)
            if phrase:
                code, reason_code = EXIT_INCOMPLETE, "self-declared-incomplete"
                reason = (f"the answer opens by saying it {phrase!r}; see the module "
                          "docstring for this test's boundary")
        if code:
            headline = ("SELF-DECLARED INCOMPLETE" if reason_code == "self-declared-incomplete"
                        else reason_code.upper())
            return fail(reason_code, reason, code, headline, len(answer),
                        stderr_lines, stdout_lines)

        elapsed = time.time() - started
        # The summary is on stderr; stdout carries the answer, which may quote
        # the phrase, so it is read only when stderr has none.
        tokens = parse_tokens(stderr_text)
        if tokens == "unknown":
            tokens = parse_tokens(stdout_text)
        version = parse_cli_version(stderr_text + stdout_text) or cli_version()
        digest = hashlib.sha256(raw).hexdigest()

        # The publication order: the final receipt in the run dir, then
        # B1 publishes the receipt, then B2 publishes the answer. A crash before B1
        # publishes nothing; between B1 and B2 leaves a receipt with no answer,
        # which verify refuses; after B2 both exist and agree.
        doc = receipt("succeeded", "", EXIT_OK, certified=typed,
                      answer_sha256=digest, answer_bytes=len(raw), cli=version,
                      tokens=tokens, final=False)
        # Not final until the answer is published: a stop before B2 must
        # rewrite this run-dir receipt as interrupted or failed. Passed IN,
        # never reset after, so a signal between the two cannot leave it final.
        _boundary("receipt-written")
        write_json_atomic(contract.receipt_path(out), doc)
        _boundary("after-B1")
        os.replace(str(answer_tmp), str(out))
        held["final"] = held["published"] = True
        _boundary("after-B2")

        manifest = {
            "run_id": run_id,
            "label": label,
            "model": model,
            "cli_version": version,
            "candidate_sha": candidate_sha,
            "elapsed_seconds": round(elapsed, 3),
            "exit_code": EXIT_OK,
            "answer_sha256": digest,
            "answer_chars": len(answer),
            "tokens": tokens,
            "out": str(out),
            "utc": utc_now(),
        }
        (rundir / "manifest.json").write_text(json.dumps(manifest, indent=2,
                                                         sort_keys=True))
        print(f"CODEX {label}: OK, model={model or 'codex-default'}, "
              f"sandbox={sandbox}, tokens={tokens}, "
              f"{len(answer)} chars -> {out} (run {run_id})")
        return EXIT_OK

    # ANY exception nobody planned for still ends with a final receipt, so a run
    # dir never holds a `started` receipt for an attempt that is over. The
    # error is re-raised: it must surface, never read as a quiet result.
    try:
        return body()
    except Interrupted as exc:
        # run_codex_group has already stopped the whole Codex group on its way
        # out, so the group is empty before this receipt is
        # written. Nothing is published.
        code = 128 + exc.signum
        # Ask the FILES, not a flag: the signal can land between the rename and
        # any bookkeeping.
        published = held.get("published")
        if not published:
            try:
                contract.check_publication(
                    out, (held["request"] or {}).get("request_id"))
                published = True
            except (contract.ContractError, OSError):
                published = False
        if published:
            # After B2 the answer and its receipt are both published and agree,
            # so the result stands; exit 0 keeps "--out exists iff exit 0".
            print(f"CODEX {label}: signal {exc.signum} arrived after the result "
                  f"was published; the result stands (run {run_id})", file=sys.stderr)
            return EXIT_OK
        if not held["final"]:
            receipt("interrupted", "interrupted", code)
        print(f"CODEX {label}: INTERRUPTED by signal {exc.signum}, nothing "
              f"published (run {run_id})", file=sys.stderr)
        return code
    except Exception:
        if not held["final"]:
            receipt("failed", "internal-error", 1)
        raise


def run_codex_group(cmd, timeout, rundir, run_id, env=None, cwd=None):
    """Start Codex as the leader of its own process group, record the group in
    child-process.json at once, and wait. On a timeout or ANY exception (an
    Interrupted signal included) the whole group is stopped, TERM then KILL
    (process_group.stop_group), BEFORE the exception travels on to whoever
    writes the receipt. Returns (proc, stdout, stderr). `env` and `cwd` are
    passed to Popen; None inherits the caller's."""
    # stdin closed: codex blocks reading it otherwise.
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, start_new_session=True,
                            env=env, cwd=cwd)
    ident, record = None, None
    try:
        # Inside the protected block from the first instruction after the
        # launch: a signal or a failed write here still stops Codex.
        ident = process_group.identify(proc.pid)
        record = {"version": 1, "run_id": run_id,
                  "pid": ident["pid"], "pgid": ident["pgid"], "start": ident["start"],
                  "written_utc": utc_now(), "stop": None}
        write_json_atomic(rundir / CHILD_RECORD, record)
        out, err = proc.communicate(timeout=timeout)
        return proc, out, err
    except BaseException:
        _teardown(proc, ident, record, rundir)
        raise
    finally:
        for pipe in (proc.stdout, proc.stderr):
            try:
                pipe.close()
            except (OSError, AttributeError):
                pass


def _teardown(proc, ident, record, rundir):
    """Stop the Codex group once, with further SIGINT/SIGTERM ignored so a
    second signal cannot cut the cleanup short (the first exception is still
    the one that travels on). If the group stop did not end cleanly, the direct
    child is killed too."""
    saved = {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            saved[sig] = signal.signal(sig, signal.SIG_IGN)
        except ValueError:
            pass  # not the main thread: handlers cannot change, cleanup still runs
    try:
        stop = None
        if ident is not None:
            stop = process_group.stop_group(ident, grace=STOP_GRACE, leader=proc)
        if (stop is None or stop["outcome"] != "stopped") and proc.poll() is None:
            proc.kill()
            proc.wait()
        if record is not None:
            record["stop"] = stop
            try:
                write_json_atomic(rundir / CHILD_RECORD, record)
            except OSError:
                pass  # the record is evidence; failing to update it must not stop cleanup
        try:
            proc.communicate(timeout=2)
        except (subprocess.TimeoutExpired, ValueError, OSError):
            pass  # an escaped descendant can hold a pipe open; do not wait on it
    finally:
        for sig, handler in saved.items():
            signal.signal(sig, handler)


def _size(path):
    try:
        return path.stat().st_size
    except OSError:
        return 0


def verify_main(argv):
    """`codex_delegate.py verify --out X --request-id ID [--require-certified]`.
    Exit 0 when the answer at X may be used, 1 otherwise, printing one reason
    code. Makes no model call and changes nothing."""
    ap = argparse.ArgumentParser(prog="codex_delegate.py verify")
    ap.add_argument("--out", required=True)
    ap.add_argument("--request-id", required=True)
    ap.add_argument("--require-certified", action="store_true")
    a = ap.parse_args(argv)
    try:
        rec = contract.check_publication(pathlib.Path(a.out).expanduser(),
                                         a.request_id, a.require_certified)
    except contract.ContractError as exc:
        print(f"VERIFY REFUSED {exc.code}: {exc.detail}")
        return 1
    print(f"VERIFY OK: run {rec.get('run_id')}, certified={rec.get('certified')}")
    return 0


def main():
    if sys.argv[1:2] == ["verify"]:
        return verify_main(sys.argv[2:])
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--prompt")
    ap.add_argument("--prompt-file")
    ap.add_argument("--repo")
    ap.add_argument("--out")
    ap.add_argument("--sandbox", default="read-only",
                    choices=["read-only", "workspace-write"])
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    ap.add_argument("--model", default=None,
                    help="model id (default: CODEX_REVIEW_MODEL, else Codex's own "
                         "default); refused when it disagrees with --profile")
    ap.add_argument("--profile", help="a named profile from model_profiles.json")
    ap.add_argument("--effort", help="reasoning effort, sent as its own -c setting")
    ap.add_argument("--profile-reason",
                    help="why this profile; required by profiles that say so")
    ap.add_argument("--smoke", action="store_true",
                    help="the bounded smoke call; the only way to use a "
                         "pending-smoke profile")
    ap.add_argument("--isolated", action="store_true",
                    help="ignore the user Codex config, rules and history")
    ap.add_argument("--allow-untracked", action="store_true",
                    help="review a checkout that has untracked files the model "
                         "can read but no commit contains; the paths are "
                         "recorded in the receipt as untracked_waived")
    ap.add_argument("--label", default="delegation")
    ap.add_argument("--candidate-sha",
                    help="the commit the answer must name, or it is refused")
    ap.add_argument("--output-schema",
                    help="a JSON schema file passed to codex; the answer is then "
                         "parsed and must declare status=complete")
    ap.add_argument("--check", action="store_true",
                    help="verify Codex is usable and exit; makes no model call")
    # A typed request: each job type has its own answer check, and only
    # `review` can be certified.
    ap.add_argument("--job-type", choices=["review", "plan"])
    ap.add_argument("--repo-id")
    ap.add_argument("--base")
    ap.add_argument("--head")
    ap.add_argument("--request-id")
    a = ap.parse_args()

    if a.check:
        state, why = review_switch()
        if state == "off":
            print("CODEX SWITCHED OFF: " + why)
            return EXIT_SWITCHED_OFF
        code, detail = preflight()
        print(("CODEX AVAILABLE: " if code == 0 else "CODEX UNAVAILABLE: ") + detail)
        return code

    # OFF is decided before the brief or any other input is read, so a user with
    # no Codex account never sees an error about a file Codex would have used.
    state, why = review_switch()
    if state == "off":
        print(switched_off(a.label, why), file=sys.stderr)
        if a.out:
            retire_for_off(a.out, a.repo, a.label)
        return EXIT_SWITCHED_OFF

    if not a.out:
        ap.error("--out is required")
    if (a.prompt is None) == (a.prompt_file is None):
        ap.error("give exactly one of --prompt or --prompt-file")

    if a.prompt is not None:
        prompt = a.prompt
    else:
        pf = pathlib.Path(a.prompt_file).expanduser()
        if not pf.is_file():
            ap.error(f"no prompt file at {pf}")
        prompt = pf.read_text()
    if not prompt.strip():
        ap.error("the prompt is empty")

    if a.output_schema:
        sf = pathlib.Path(a.output_schema).expanduser()
        if not sf.is_file():
            ap.error(f"no output schema at {sf}")

    # Handlers live here and never in run(): a caller that calls run()
    # in-process owns its own signals.
    signal.signal(signal.SIGINT, _raise_interrupted)
    signal.signal(signal.SIGTERM, _raise_interrupted)
    try:
        return _cli_run(a, prompt)
    except Interrupted as exc:
        return 128 + exc.signum


def _cli_run(a, prompt):
    return run(prompt, a.repo, a.out, a.sandbox, a.timeout, a.model, a.label,
               isolated=a.isolated, candidate_sha=a.candidate_sha,
               output_schema=a.output_schema, job_type=a.job_type,
               repo_id=a.repo_id, base=a.base, head=a.head,
               request_id=a.request_id, profile=a.profile,
               reasoning_effort=a.effort, profile_reason=a.profile_reason,
               smoke=a.smoke, allow_untracked=a.allow_untracked)


if __name__ == "__main__":
    sys.exit(main())
