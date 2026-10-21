#!/usr/bin/env python3
"""The record formats of a Codex delegation, and every check on them.

Pure functions: no network, no model, no subprocess. The adapter
(`codex_delegate.py`) imports this module to build a request and to judge the
answer; a consumer imports it to check a published result without importing
the launcher. One owner per rule: nothing here is restated anywhere else.

Every refusal is a ContractError carrying a reason CODE (stable, for machines)
and a detail (one plain line, for people). The adapter maps the code to its
exit status; this module knows nothing about exit statuses.
"""

import hashlib
import json
import pathlib
import re
import urllib.parse
import uuid

REQUEST_VERSION = 1
REVIEW_RESULT_VERSION = "review-result/1"

JOB_TYPES = ("review", "plan", "legacy")
RESULT_CONTRACTS = {
    "review": REVIEW_RESULT_VERSION,
    "plan": "plan-draft",
    "legacy": "legacy",
}
IDENTITY_FIELDS = ("request_id", "repo_id", "base_sha", "head_sha")
REVIEW_STATUSES = ("complete", "incomplete", "failed")
# The raw severity word stays in the finding; this table is the only place it
# is given a meaning.
SEVERITY_MAP = {
    "Critical": "blocking",
    "Warning": "needs-disposition",
    "Suggestion": "advisory",
}


class ContractError(ValueError):
    """A record that must not be used. `code` is the stable reason code."""

    def __init__(self, code, detail):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def build_request(*, job_type, repo_id, checkout, base_sha, head_sha, brief,
                  model, timeout_seconds, request_id=None, profile=None,
                  effort=None, profile_reason=None, smoke=False):
    """Request v1. The SHAs must already be full: resolving
    them is the adapter's job, because only it may run git.
    
    `brief_sha256` is over the caller's brief as given, before the adapter
    appends the identity block, so it can be reproduced from the brief file."""
    if job_type not in JOB_TYPES:
        raise ContractError("bad-request", f"unknown job type {job_type!r}")
    return {
        "request_version": REQUEST_VERSION,
        "request_id": request_id or uuid.uuid4().hex,
        "job_type": job_type,
        "repo_id": repo_id,
        "checkout": checkout,
        "base_sha": base_sha,
        "head_sha": head_sha,
        "brief_sha256": hashlib.sha256(brief.encode("utf-8")).hexdigest(),
        "profile": profile,
        "model": model,
        "effort": effort,
        "profile_reason": profile_reason,
        "smoke": bool(smoke),
        "timeout_seconds": timeout_seconds,
        "result_contract": RESULT_CONTRACTS[job_type],
    }


RECEIPT_VERSION = 1
TERMINAL_STATUSES = ("started", "succeeded", "refused", "failed", "timeout",
                     "interrupted")


def request_sha256(request):
    """The hash of a request record, over its canonical JSON (sorted keys, no
    whitespace), so a receipt's embedded copy can be checked for tampering."""
    canon = json.dumps(request, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def build_receipt(*, run_id, request, terminal_status, reason_code, exit_code,
                  certified, answer_sha256, answer_bytes, command_mode,
                  argv_model, argv_effort, cli_version, tokens, provider_error,
                  started_utc, finished_utc, observed_model="unknown",
                  observed_effort="unknown", untracked_waived=None):
    """Execution receipt v1. It carries hashes, sizes and
    codes only: never the brief, the answer text or the child's stderr."""
    if terminal_status not in TERMINAL_STATUSES:
        raise ContractError("bad-receipt", f"unknown status {terminal_status!r}")
    return {
        "receipt_version": RECEIPT_VERSION,
        "run_id": run_id,
        "request": request,
        "request_sha256": request_sha256(request),
        "terminal_status": terminal_status,
        "reason_code": reason_code,
        "exit_code": exit_code,
        "certified": bool(certified),
        "answer_sha256": answer_sha256,
        "answer_bytes": answer_bytes,
        "command_mode": command_mode,
        "argv_model": argv_model,
        "argv_effort": argv_effort,
        "observed_model": observed_model,
        "observed_effort": observed_effort,
        "cli_version": cli_version,
        "tokens": tokens,
        "provider_error": provider_error,
        "started_utc": started_utc,
        "finished_utc": finished_utc,
        # The untracked paths a --allow-untracked run read but no commit holds.
        # Empty is the normal case; a non-empty list is the recorded waiver that
        # says this answer was NOT produced from the commit alone (re-review 3).
        "untracked_waived": sorted(untracked_waived or []),
    }


def receipt_path(out):
    """Where the receipt for an answer at `out` is published."""
    out = str(out)
    return out + ".receipt.json"


def check_publication(out, expect_request_id, require_certified=False):
    """The consumer's check: the answer at `out` may be used only
    when its published receipt strict-loads, says succeeded, names the
    expected request, is untampered, and hashes to the answer's exact bytes.
    Returns the receipt; raises ContractError with one reason code otherwise.
    The answer existing is never enough."""
    rpath = pathlib.Path(receipt_path(out))
    if not rpath.is_file():
        raise ContractError("no-receipt", f"no published receipt at {rpath}")
    try:
        receipt = strict_loads(rpath.read_text())
    except (OSError, UnicodeDecodeError) as exc:
        raise ContractError("bad-receipt", f"{rpath} unreadable ({exc.__class__.__name__})")
    except ContractError as exc:
        raise ContractError("bad-receipt", f"{rpath}: {exc.detail}")
    # `type(...) is int`: True == 1 in Python, so `!=` alone accepts a boolean.
    if not (isinstance(receipt, dict) and type(receipt.get("receipt_version")) is int
            and receipt["receipt_version"] == RECEIPT_VERSION):
        raise ContractError("bad-receipt", f"{rpath} is not a receipt v{RECEIPT_VERSION}")
    request = receipt.get("request")
    if not isinstance(request, dict) or receipt.get("request_sha256") != request_sha256(request):
        raise ContractError("request-hash-mismatch",
                            "the receipt's request does not match its request_sha256")
    if receipt.get("terminal_status") != "succeeded":
        raise ContractError("not-succeeded",
                            f"terminal_status is {receipt.get('terminal_status')!r}")
    if request.get("request_id") != expect_request_id:
        raise ContractError("request-id-mismatch",
                            f"the receipt is for request {request.get('request_id')!r}, "
                            f"not {expect_request_id!r}")
    apath = pathlib.Path(out)
    if not apath.is_file():
        raise ContractError("no-answer", f"no answer at {apath}")
    answer = apath.read_bytes()
    # The hash first: a changed answer is named as a changed answer.
    if hashlib.sha256(answer).hexdigest() != receipt.get("answer_sha256"):
        raise ContractError("hash-mismatch",
                            "the answer's bytes do not match the receipt's answer_sha256")
    # A succeeded receipt must agree with itself: exit 0, a finish time, and the
    # true size of the answer. Exact ints, since a boolean passes `== 0`.
    for field, ok in (("exit_code", type(receipt.get("exit_code")) is int
                       and receipt["exit_code"] == 0),
                      ("finished_utc", _nonempty_str(receipt.get("finished_utc"))),
                      ("answer_bytes", type(receipt.get("answer_bytes")) is int
                       and receipt["answer_bytes"] == len(answer))):
        if not ok:
            raise ContractError("inconsistent-receipt",
                                f"a succeeded receipt with {field}={receipt.get(field)!r}")
    if require_certified:
        if receipt.get("certified") is not True or request.get("job_type") != "review":
            raise ContractError("not-certified",
                                "the receipt is not certified (only a typed review is)")
        # The flag is the adapter's word; the bytes must also BE a valid review
        # for the request the receipt embeds.
        try:
            validate_result("review", answer.decode("utf-8"), request)
        except (UnicodeDecodeError, ContractError) as exc:
            raise ContractError("not-certified",
                                f"the answer is not a valid review for its request ({exc})")
    return receipt


PROFILE_STATUSES = ("enabled", "pending-smoke")
# An effort goes into `-c model_reasoning_effort="<effort>"`, so it must be a
# plain word: a quote or a space would change what the CLI parses.
EFFORT_PATTERN = r"[a-z]{1,20}"


def load_profile(path, name):
    """The named profile from a profiles file, checked, or ContractError.
    A missing, unreadable or malformed file refuses; it is never defaulted
    because a silent default is a silent model switch."""
    try:
        doc = strict_loads(pathlib.Path(path).read_text())
    except (OSError, UnicodeDecodeError) as exc:
        raise ContractError("bad-profile-file",
                            f"{path} unreadable ({exc.__class__.__name__})")
    except ContractError as exc:
        raise ContractError("bad-profile-file", f"{path}: {exc.detail}")
    profiles = doc.get("profiles") if isinstance(doc, dict) else None
    if not isinstance(profiles, dict):
        raise ContractError("bad-profile-file", f"{path} has no profiles object")
    if name not in profiles:
        raise ContractError("unknown-profile",
                            f"no profile {name!r}; known: " + ", ".join(sorted(profiles)))
    prof = profiles[name]
    if not (isinstance(prof, dict) and _nonempty_str(prof.get("model"))
            and (prof.get("effort") is None
                 or (isinstance(prof.get("effort"), str)
                     and re.fullmatch(EFFORT_PATTERN, prof["effort"])))
            and prof.get("status") in PROFILE_STATUSES
            and isinstance(prof.get("requires_reason"), bool)):
        raise ContractError("bad-profile-file", f"profile {name!r} in {path} is malformed")
    return prof


PROVIDER_REJECTION_TYPE = "invalid_request_error"
_PROVIDER_FIELD_MAX = 200


def parse_provider_error(text):
    """{type, code, param} of the first provider error object in the child's
    stderr, or None. The real CLI prints `ERROR: {"type":"error","status":400,
    "error":{"type":...,"code":...,"param":...,"message":...}}` (code and param
    may be absent, and then are None).
    
    Only these three fields are kept, as strings, never the message: the full
    text can carry a secret."""
    decoder = json.JSONDecoder()
    for line in (text or "").splitlines():
        # Only the CLI's own `ERROR:` line. Anything else on stderr can be text
        # the model read and echoed, and must not decide the exit code.
        if not line.startswith("ERROR:"):
            continue
        start = line.find("{")
        if start < 0:
            continue
        try:
            doc, _ = decoder.raw_decode(line[start:])
        except (ValueError, RecursionError):
            continue
        err = doc.get("error") if isinstance(doc, dict) else None
        if not (isinstance(err, dict) and isinstance(err.get("type"), str)):
            continue

        def field(key):
            value = err.get(key)
            return value[:_PROVIDER_FIELD_MAX] if isinstance(value, str) else None

        return {"type": field("type"), "code": field("code"), "param": field("param")}
    return None


def normalise_repo_url(url):
    """The logical repo ID an origin URL names, or None.
    
    Strips the scheme, any credentials and port, a trailing `/` and `.git`;
    turns scp-style `git@host:owner/name` into `host/owner/name`; lowercases
    the host only. A local path or a file:// URL names a CHECKOUT, not a
    repository, so it gives None: the caller refuses rather than guess."""
    url = (url or "").strip()
    if not url:
        return None
    if "://" in url:
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in ("https", "http", "ssh", "git", "git+ssh"):
            return None
        host, path = parts.hostname, parts.path
    else:
        m = re.fullmatch(r"(?:[^@/:]+@)?([^/:]+):(?!/)(.+)", url)
        if not m:
            return None
        host, path = m.group(1), m.group(2)
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    if not host or not path:
        return None
    return f"{host.lower()}/{path}"


def strict_loads(text):
    """json.loads, minus what it forgives: a duplicate key (the LAST value
    silently wins, so a second `head_sha` could overwrite a correct one) and the
    non-JSON constants NaN and Infinity."""

    def no_duplicates(pairs):
        seen = {}
        for key, value in pairs:
            if key in seen:
                raise ContractError("duplicate-key",
                                    f"the key {key!r} appears twice in one object")
            seen[key] = value
        return seen

    def no_constants(name):
        raise ContractError("malformed", f"{name} is not valid JSON")

    try:
        return json.loads(text, object_pairs_hook=no_duplicates,
                          parse_constant=no_constants)
    except ContractError:
        raise
    except ValueError as exc:
        # `.msg` exists on JSONDecodeError only. json.loads also raises a PLAIN
        # ValueError, for an integer past the interpreter's digit-conversion
        # limit (Python 3.11+), and reading .msg on that raised AttributeError,
        # which escaped every caller's `except ContractError` (re-review 3).
        raise ContractError("malformed",
                            f"the answer is not JSON ({getattr(exc, 'msg', exc)})")
    except RecursionError:
        raise ContractError("malformed", "the JSON is nested too deeply to read")


def severity_class(word):
    """What a reviewer's raw severity word means for release. The
    word itself stays in the finding; an unknown word is refused, never given
    a default meaning."""
    if word not in SEVERITY_MAP:
        raise ContractError("unknown-severity",
                            f"severity {word!r} is not one of " + ", ".join(SEVERITY_MAP))
    return SEVERITY_MAP[word]


def validate_result(job_type, answer, request):
    """Judge the answer TEXT for `job_type` against its request.
    Returns None when it may be published; raises ContractError otherwise.
    Each job type has its own rule, and only `review` certifies:
      review      strict JSON, review-result/1, identity field by field
      plan        non-empty text (a draft stays a draft)
    A legacy call (no job type) never reaches this function."""
    if job_type == "review":
        _validate_review(strict_loads(answer), request)
    elif job_type == "plan":
        if not answer.strip():
            raise ContractError("empty", "the plan draft is empty")
    else:
        raise ContractError("bad-request", f"no validator for job type {job_type!r}")


def _is_str_list(value):
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


def _nonempty_str(value):
    return isinstance(value, str) and bool(value.strip())


def _validate_finding(i, finding):
    where = f"findings[{i}]"
    if not isinstance(finding, dict):
        raise ContractError("malformed-finding", f"{where} is not an object")
    for key in ("id", "file"):
        if not isinstance(finding.get(key), str):
            raise ContractError("malformed-finding", f"{where}.{key} is not a string")
    for key in ("title", "failure_scenario"):
        if not _nonempty_str(finding.get(key)):
            raise ContractError("malformed-finding", f"{where}.{key} is missing or empty")
    severity = finding.get("severity")
    if not isinstance(severity, str):
        raise ContractError("malformed-finding", f"{where}.severity is not a string")
    try:
        severity_class(severity)
    except ContractError as exc:
        raise ContractError(exc.code, f"{where}: {exc.detail}")
    if "line" not in finding:
        raise ContractError("malformed-finding", f"{where}.line is missing")
    line = finding["line"]
    # `type(...) is int`, never isinstance: True is an int to isinstance.
    if line is not None and not (type(line) is int and line >= 1):
        raise ContractError("malformed-finding",
                            f"{where}.line {line!r} is not a whole number >= 1 or null")


def _validate_review(doc, request):
    """Structure first, then identity, then completeness. A damaged answer is
    not trusted even for its identity; a wrong identity outranks a status."""
    if not isinstance(doc, dict):
        raise ContractError("malformed",
                            f"the answer is JSON {type(doc).__name__}, not an object")
    version = doc.get("schema_version")
    if version != REVIEW_RESULT_VERSION:
        raise ContractError("unknown-version",
                            f"schema_version {version!r} is not {REVIEW_RESULT_VERSION!r}")
    echoed = doc.get("request")
    if not isinstance(echoed, dict) or set(echoed) != set(IDENTITY_FIELDS):
        raise ContractError("malformed",
                            "request must be an object with exactly "
                            + ", ".join(IDENTITY_FIELDS))
    status = doc.get("status")
    if status not in REVIEW_STATUSES:
        raise ContractError("unknown-status",
                            f"status {status!r} is not one of " + ", ".join(REVIEW_STATUSES))
    coverage = doc.get("coverage")
    if not (isinstance(coverage, dict) and _is_str_list(coverage.get("files_read"))
            and _is_str_list(coverage.get("checks_not_run"))):
        raise ContractError("malformed",
                            "coverage must hold files_read and checks_not_run as "
                            "lists of strings")
    findings = doc.get("findings")
    if not isinstance(findings, list):
        raise ContractError("malformed", "findings is not a list")
    for i, finding in enumerate(findings):
        _validate_finding(i, finding)

    # Field by field, with ==. The rest of the answer is never searched for a
    # commit: a finding that QUOTES the right head is not the answer naming it.
    for field in IDENTITY_FIELDS:
        if echoed[field] != request[field]:
            raise ContractError(
                "identity-mismatch",
                f"request.{field} is {echoed[field]!r}, the request says "
                f"{request[field]!r}")

    if status != "complete":
        raise ContractError("incomplete", f"the answer declares status={status}")
