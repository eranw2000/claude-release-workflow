#!/bin/bash
# security-scan.sh - the one place that runs the code scanners, so the pre-commit check
# and the release step cannot drift apart.
#
# Owns two of the five scanner types:
#   static analysis      semgrep, ruleset p/default
#   dependency exposure  osv-scanner, over committed lockfiles and manifests
#
# The other three are owned elsewhere and this script does not repeat them:
#   secrets   ~/.claude/hooks/secret-scan.sh (gitleaks on the staged diff, blocking)
#   config    nothing, on purpose: measured 2026-08-28 at zero Terraform, zero Helm,
#             zero Kubernetes and one Bicep file across the estate
#   image     nothing, on purpose: revisit the day a client asks for image provenance
#
# Usage:
#   security-scan.sh --staged      [repo]   fast, pre-commit: staged SOURCE only
#   security-scan.sh --since=<ref> [repo]   ship-time: only what this release changes
#   security-scan.sh --all         [repo]   deliberate audit: every tracked source file
#
# --staged deliberately does NOT scan manifests. Measured 2026-08-28: a requirements file
# pinned to current versions still returns known vulnerabilities, so a dependency warning
# on every commit is a wall of rows plus a network round trip, and a guard that cries wolf
# gets switched off. Dependency exposure is a release-time question and runs under --since
# and --all.
#
# --since is the release mode, and the reason is measured. On 2026-08-28 semgrep's
# p/default ruleset returned 57, 28, 8 and 1 findings on four repos that are already in
# production. A gate the present cannot pass is not protecting a standard, it is blocking
# the future while the present violates it. So the release gate reads what the release
# CHANGES, and the standing backlog goes to the waiver register in security-standards/
# baseline.yml rather than stopping a ship nobody could ever start.
#
# Exit codes. The difference between clean and incomplete is the whole point:
#   0  scanned something, found nothing
#   1  found something, and everything ran
#   2  could not scan (a tool is missing, a scanner failed, or nothing was scanned)
#   3  found something AND something did not run. Both facts matter, so neither is
#      allowed to hide the other behind a single code.
#
# A run that scanned zero files NEVER reports clean. Measured while writing this:
# semgrep pointed at a directory outside a git repository scans zero files and prints
# "Scan completed successfully", which reads exactly like a pass. The scanned count
# below is read back from semgrep's own report rather than from the list handed to it.

set -u

MODE="${1:-}"
REPO="${2:-$PWD}"

SEMGREP_CONFIG="${SECURITY_SCAN_SEMGREP_CONFIG:-p/default}"
# Batch size for an explicit target list. A single argument list long enough to be split
# by the kernel would run semgrep several times, each overwriting the previous report, so
# the batching is done here where the results can be summed.
BATCH="${SECURITY_SCAN_BATCH:-400}"
BASELINE="${SECURITY_SCAN_BASELINE:-$HOME/.claude/skills/_shared/security-standards/baseline.yml}"

SOURCE_RE='\.(py|js|jsx|ts|tsx|java|cs|go|rb|php)$'
MANIFEST_RE='(^|/)(requirements[^/]*\.txt|poetry\.lock|Pipfile\.lock|package-lock\.json|yarn\.lock|pnpm-lock\.yaml|go\.mod|go\.sum|Gemfile\.lock|pom\.xml|composer\.lock|packages\.lock\.json)$'

# BSD sed has no \x1b, so build the escape byte rather than writing it in the pattern.
ESC=$(printf '\033')
strip_ansi() { sed "s/${ESC}\[[0-9;]*m//g"; }

SINCE_REF=""
case "$MODE" in
  --staged|--all) ;;
  --release) MODE="--all" ;;   # older name, kept working
  --since=*)
    SINCE_REF="${MODE#--since=}"
    [ -n "$SINCE_REF" ] || { echo "security-scan: --since needs a ref, e.g. --since=origin/main" >&2; exit 2; }
    ;;
  *)
    echo "usage: security-scan.sh --staged|--since=<ref>|--all [repo]" >&2
    exit 2
    ;;
esac

cd "$REPO" 2>/dev/null || { echo "security-scan: no such directory: $REPO" >&2; exit 2; }
git rev-parse --show-toplevel >/dev/null 2>&1 || {
  echo "security-scan: $REPO is not a git repository, so there is no file list to scan." >&2
  exit 2
}

TMP=$(mktemp -d "${TMPDIR:-/tmp}/security-scan.XXXXXX") || exit 2
trap 'rm -rf "$TMP"' EXIT

# ---------------------------------------------------------------- repository identity
# Every waiver in the register names the repository it belongs to, because a path is not
# unique across the estate: `editor/sso_views.py` waived in one repo must never suppress
# the same path in another. These are the names an entry's `repo:` may carry, computed
# from the repository actually being scanned:
#   - the working tree's own directory name (the recommended form: every repository has
#     one, including a clone with no remote)
#   - every remote URL, verbatim
#   - each remote URL's last segment with a trailing .git removed
# If this list comes out empty the reporter waives nothing and says so, which is the
# fail-closed direction.
TOPLEVEL=$(git rev-parse --show-toplevel 2>/dev/null)
{
  [ -n "$TOPLEVEL" ] && basename "$TOPLEVEL"
  git remote -v 2>/dev/null | awk '{print $2}' | while IFS= read -r u; do
    [ -n "$u" ] || continue
    printf '%s\n' "$u"
    b=${u%.git}
    printf '%s\n' "${b##*/}"
  done
} 2>/dev/null | sed '/^[[:space:]]*$/d' | sort -u > "$TMP/repo_ids.txt"

# ---------------------------------------------------------------- collect the targets
# -z throughout. Under git's default core.quotePath a path with a non-ASCII or special
# character comes back QUOTED ("pa\303\251th"), the test for it on disk then fails, and
# the file is dropped from the list with no message. A partial scan that reads as a full
# one is the failure this whole script is written against.
if [ "$MODE" = "--staged" ]; then
  git diff --cached --name-only --diff-filter=ACM -z > "$TMP/changed" 2>/dev/null
elif [ -n "$SINCE_REF" ]; then
  git rev-parse --verify "$SINCE_REF" >/dev/null 2>&1 || {
    echo "security-scan: cannot resolve ref '$SINCE_REF'. Nothing was scanned." >&2
    exit 2
  }
  git diff --name-only --diff-filter=ACM -z "$SINCE_REF"...HEAD > "$TMP/changed" 2>/dev/null
else
  git ls-files -z > "$TMP/changed" 2>/dev/null
fi

SOURCE_FILES=()
MANIFEST_FILES=()
while IFS= read -r -d '' f; do
  [ -n "$f" ] || continue
  [ -f "$f" ] || continue          # a rename staged as a delete plus an add
  if printf '%s' "$f" | grep -Eq "$SOURCE_RE"; then
    SOURCE_FILES+=("$f")
  elif printf '%s' "$f" | grep -Eq "$MANIFEST_RE"; then
    MANIFEST_FILES+=("$f")
  fi
done < "$TMP/changed"

N_SOURCE=${#SOURCE_FILES[@]}
N_MANIFEST=${#MANIFEST_FILES[@]}

# ---------------------------------------------------------------- added-line map
# A diff-scoped scan lists changed FILES and semgrep then reads each one END TO END, so
# every pre-existing line in a file you touched arrives as a finding of yours. Measured
# 2026-09-02 on a release that changed 31 files: 3 findings, all 3 already live on main,
# none on a line the change added. Without this map the report cannot tell the two apart,
# and both readings are wrong: block a correct release, or learn to wave the gate through.
#
# So record which lines this change ADDS, per file, and let the reporter tag each finding.
# It FAILS TO "unknown", never to "pre-existing": a map that could not be built must not
# make a genuinely new finding read as somebody else's backlog.
: > "$TMP/added.json"
if [ "$MODE" != "--all" ] && [ "$N_SOURCE" -gt 0 ]; then
  if [ "$MODE" = "--staged" ]; then
    git diff --cached --unified=0 -- "${SOURCE_FILES[@]}" > "$TMP/added.diff" 2>/dev/null
  else
    git diff --unified=0 "$SINCE_REF"...HEAD -- "${SOURCE_FILES[@]}" > "$TMP/added.diff" 2>/dev/null
  fi
  # An EMPTY diff is the tell that the map could NOT be built, and it must not become an
  # empty map: an empty map answers "not an added line" for every finding, so every one of
  # them reads as PRE-EXISTING and a genuinely new one is waved through. Caught by mutation
  # 2026-09-02, in this very fix. A diff that is non-empty but yields no added lines for
  # some file is a different thing and is a legitimate answer.
  if [ -s "$TMP/added.diff" ]; then
  python3 - "$TMP/added.diff" > "$TMP/added.json" 2>/dev/null <<'PYADD'
import json, re, sys
# Parse a unified=0 diff into {path: [added line numbers in the NEW file]}.
path, out = None, {}
hunk = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
for line in open(sys.argv[1], errors="ignore"):
    if line.startswith("+++ "):
        p = line[4:].strip()
        path = None if p == "/dev/null" else (p[2:] if p.startswith("b/") else p)
        continue
    m = hunk.match(line)
    if m and path:
        start = int(m.group(1))
        count = 1 if m.group(2) is None else int(m.group(2))
        out.setdefault(path, []).extend(range(start, start + count))
print(json.dumps(out))
PYADD
  fi
  [ -s "$TMP/added.json" ] || : > "$TMP/added.json"
fi

# --staged does not scan dependencies. Say so rather than letting a listed count read as
# a scanned one.
SKIP_DEPS=0
if [ "$MODE" = "--staged" ] && [ "$N_MANIFEST" -gt 0 ]; then
  SKIP_DEPS=1
fi

echo "security-scan ($MODE) in $REPO"
echo "  source files listed: $N_SOURCE"
if [ "$SKIP_DEPS" -eq 1 ]; then
  echo "  manifests listed:    $N_MANIFEST (not scanned: dependency exposure runs at release)"
else
  echo "  manifests listed:    $N_MANIFEST"
fi

if [ "$N_SOURCE" -eq 0 ] && { [ "$N_MANIFEST" -eq 0 ] || [ "$SKIP_DEPS" -eq 1 ]; }; then
  echo "  NOTHING TO SCAN. This is not a pass. No path matched a source name."
  exit 2
fi

FINDINGS=0
COULD_NOT=0

# ---------------------------------------------------------------------------- semgrep
if [ "$N_SOURCE" -gt 0 ]; then
  if ! command -v semgrep >/dev/null 2>&1; then
    echo "  SEMGREP MISSING. $N_SOURCE source files went unscanned. Install with: brew install semgrep"
    COULD_NOT=1
  else
    SG_FAIL=0
    NBATCH=0
    if [ "$MODE" = "--all" ]; then
      # The whole repository IS the target, so let semgrep walk what git tracks.
      semgrep --config "$SEMGREP_CONFIG" --metrics=off --quiet --error --json \
        . > "$TMP/semgrep.0.json" 2>"$TMP/semgrep.err"
      SG=$?
      [ "$SG" -le 1 ] || SG_FAIL=$SG
      NBATCH=1
    else
      # An explicit list, batched. Passing "." here instead would scan files that are
      # NOT in the change, while the caller describes these as the files being committed.
      i=0
      while [ "$i" -lt "$N_SOURCE" ]; do
        BATCH_FILES=("${SOURCE_FILES[@]:$i:$BATCH}")
        semgrep --config "$SEMGREP_CONFIG" --metrics=off --quiet --error --json \
          "${BATCH_FILES[@]}" > "$TMP/semgrep.$NBATCH.json" 2>>"$TMP/semgrep.err"
        SG=$?
        [ "$SG" -le 1 ] || SG_FAIL=$SG
        NBATCH=$((NBATCH + 1))
        i=$((i + BATCH))
      done
    fi

    if [ "$SG_FAIL" -ne 0 ]; then
      echo "  SEMGREP FAILED (exit $SG_FAIL). Treat the $N_SOURCE source files as unscanned."
      head -8 "$TMP/semgrep.err" | sed 's/^/    /'
      COULD_NOT=1
    else
      python3 - "$TMP" "$NBATCH" "$BASELINE" "$TMP/added.json" "$TMP/repo_ids.txt" <<'PY' > "$TMP/semgrep.txt" 2>/dev/null
import datetime, fnmatch, json, os, sys

tmp, nbatch, baseline_path = sys.argv[1], int(sys.argv[2]), sys.argv[3]
added_path = sys.argv[4] if len(sys.argv) > 4 else ""
repo_ids_path = sys.argv[5] if len(sys.argv) > 5 else ""

# {path: set(added line numbers)}. Absent or unreadable means "cannot tell", which is a
# THIRD answer and never "pre-existing": labelling an unknown finding as somebody else's
# backlog is the one direction that hides a real one.
added, added_known = {}, False
if added_path and os.path.exists(added_path) and os.path.getsize(added_path) > 0:
    try:
        added = {k: set(v) for k, v in json.load(open(added_path)).items()}
        added_known = True
    except Exception:
        added, added_known = {}, False

results, scanned = [], set()
for i in range(nbatch):
    p = os.path.join(tmp, f"semgrep.{i}.json")
    try:
        d = json.load(open(p))
    except Exception:
        print("PARSE_FAIL")
        raise SystemExit(0)
    results.extend(d.get("results", []))
    scanned.update(d.get("paths", {}).get("scanned", []))

# ---- waiver register. Failing to read it must never look like nothing was waived.
suppressed, notes = 0, []
entries = []
yaml = None
if os.path.exists(baseline_path):
    try:
        import yaml
    except Exception:
        notes.append("BASELINE UNREAD: PyYAML is not available to this interpreter, so no "
                     "waiver was applied. Every finding below is reported unsuppressed.")
    if yaml is not None:
        try:
            doc = yaml.safe_load(open(baseline_path)) or {}
            entries = doc.get("suppressions") or []
        except Exception as exc:
            notes.append(f"BASELINE UNREAD: it did not parse ({exc.__class__.__name__}). "
                         "No waiver was applied.")

today = datetime.date.today()

# The names this repository answers to, written by the shell above. Empty means the
# identity could not be established, and then NOTHING is waived: a waiver that cannot be
# tied to a repository would suppress the same path everywhere.
repo_ids = set()
if repo_ids_path and os.path.exists(repo_ids_path):
    try:
        repo_ids = {l.strip() for l in open(repo_ids_path) if l.strip()}
    except OSError:
        repo_ids = set()
if entries and not repo_ids:
    notes.append("REPO IDENTITY UNKNOWN: the repository being scanned could not be named, "
                 "so no waiver was applied. Every finding below is reported unsuppressed.")


def refusal(entry):
    """Why this entry may not be applied at all, or None when it is well formed.

    A refusal is REPORTED and never silent. An entry nobody can act on is worse than no
    entry at all, because the finding it names reads as consciously accepted when in fact
    nothing is suppressing it.
    """
    if not str(entry.get("rule_id") or "").strip():
        return "it names no rule_id, so there is nothing to match"
    if not str(entry.get("repo") or "").strip():
        return ("it names no repo:, and a path is not unique across the estate, so applying "
                "it would waive the same path in every repository")
    if not str(entry.get("path") or "").strip():
        if entry.get("fingerprint"):
            return ("it carries only a fingerprint, which nothing in this harness emits, "
                    "so there is no path to match")
        return "it names no path glob, so there is nothing to match"
    return None


def matches(entry, res):
    """A finding is waived when the repo matches AND the rule matches AND the path glob
    matches. Every entry reaching here has already passed `refusal`.

    A `fingerprint` entry is NOT honoured: nothing in this harness emits one, so an entry
    carrying only a fingerprint waives nothing and is refused above.
    """
    if str(entry.get("repo") or "").strip() not in repo_ids:
        return False
    rid = str(entry.get("rule_id") or "")
    if not rid:
        return False
    check = res.get("check_id", "")
    parts = check.split(".")
    if not (check == rid or (parts and parts[-1] == rid) or rid in parts):
        return False
    glob = entry.get("path")
    if not glob:
        return False
    path = res.get("path", "")
    return fnmatch.fnmatch(path, glob) or fnmatch.fnmatch(os.path.basename(path), glob)


# Refusals are reported ONCE, before any finding is looked at, so a malformed entry is
# named on a clean scan too. Naming it only when a finding happened to match it would
# hide exactly the entry that is doing nothing.
usable = []
for i, entry in enumerate(entries):
    if not isinstance(entry, dict):
        notes.append(f"WAIVER REFUSED (entry {i + 1}): it is not a mapping, so it has no fields.")
        continue
    why = refusal(entry)
    if why:
        notes.append(
            f"WAIVER REFUSED: {entry.get('rule_id') or '(no rule_id)'} on "
            f"{entry.get('path') or '(no path)'} in repo {entry.get('repo') or '(no repo)'}: "
            f"{why}. It waives nothing and any matching finding is reported."
        )
        continue
    usable.append(entry)

kept = []
for res in results:
    hit = None
    for entry in usable:
        if not matches(entry, res):
            continue
        exp = entry.get("expires")
        if not exp:
            notes.append(f"WAIVER WITHOUT EXPIRY ignored: {entry.get('rule_id')} on "
                         f"{entry.get('path')} in repo {entry.get('repo')}. "
                         "The finding is reported.")
            continue
        try:
            expd = exp if isinstance(exp, datetime.date) else datetime.date.fromisoformat(str(exp))
        except ValueError:
            notes.append(f"UNREADABLE EXPIRY on waiver {entry.get('rule_id')}: {exp!r}. "
                         "The finding is reported.")
            continue
        if expd < today:
            notes.append(f"EXPIRED WAIVER ignored: {entry.get('rule_id')} on "
                         f"{entry.get('path')} in repo {entry.get('repo')} expired {expd}. "
                         "The finding is reported.")
            continue
        hit = entry
        break
    if hit is None:
        kept.append(res)
    else:
        suppressed += 1


def origin(res):
    """NEW when the finding sits on a line this change adds, PRE-EXISTING when it does
    not, UNKNOWN when no added-line map was available."""
    if not added_known:
        return "UNKNOWN"
    line = res.get("start", {}).get("line")
    if not isinstance(line, int):
        return "UNKNOWN"
    path = res.get("path", "")
    lines = added.get(path)
    if lines is None:
        lines = added.get(os.path.basename(path), set())
    return "NEW" if line in lines else "PRE-EXISTING"


tags = [origin(r) for r in kept]
n_new = tags.count("NEW")
n_old = tags.count("PRE-EXISTING")
n_unknown = tags.count("UNKNOWN")

print(f"COUNT {len(kept)} SCANNED {len(scanned)} SUPPRESSED {suppressed} "
      f"NEW {n_new} PREEXISTING {n_old} UNKNOWN {n_unknown}")
for n in notes:
    print(f"  ! {n}")
for r, tag in list(zip(kept, tags))[:20]:
    sev = r.get("extra", {}).get("severity", "?")
    rid = r.get("check_id", "?").split(".")[-1]
    line = r.get("start", {}).get("line", "?")
    print(f"  [{sev}] [{tag}] {r.get('path','?')}:{line} {rid}")
if len(kept) > 20:
    print(f"  ... and {len(kept)-20} more")
PY
      HEAD_LINE=$(head -1 "$TMP/semgrep.txt")
      case "$HEAD_LINE" in
        COUNT*)
          COUNT=$(echo "$HEAD_LINE" | awk '{print $2}')
          SCANNED=$(echo "$HEAD_LINE" | awk '{print $4}')
          SUPPRESSED=$(echo "$HEAD_LINE" | awk '{print $6}')
          if [ "$SCANNED" -eq 0 ]; then
            echo "  SEMGREP SCANNED ZERO FILES. That is not clean. Check the target list."
            COULD_NOT=1
          else
            SUP_NOTE=""
            [ "$SUPPRESSED" -gt 0 ] && SUP_NOTE=" ($SUPPRESSED waived by baseline.yml)"
            N_NEW=$(echo "$HEAD_LINE" | awk '{print $8}')
            N_OLD=$(echo "$HEAD_LINE" | awk '{print $10}')
            N_UNK=$(echo "$HEAD_LINE" | awk '{print $12}')
            if [ "$COUNT" -gt 0 ]; then
              echo "  semgrep: $COUNT finding(s) over $SCANNED file(s) scanned$SUP_NOTE"
              # Say which of them this change actually introduced. A diff-scoped scan reads
              # each changed file end to end, so a finding here is not automatically yours.
              if [ "${N_UNK:-0}" -gt 0 ]; then
                echo "  origin: $N_NEW on lines this change adds, $N_OLD pre-existing, $N_UNK UNKNOWN."
                echo "          UNKNOWN means no added-line map was built, so treat those as YOURS."
              elif [ "${N_NEW:-0}" -eq 0 ] && [ "${N_OLD:-0}" -gt 0 ]; then
                echo "  origin: NONE of them sits on a line this change adds. All $N_OLD are"
                echo "          pre-existing in files it touched, so they are a backlog item"
                echo "          rather than something this change introduced. Say so in the"
                echo "          report; do not silently treat them as cleared."
              else
                echo "  origin: $N_NEW on lines this change adds, $N_OLD pre-existing."
              fi
              FINDINGS=1
            else
              echo "  semgrep: clean over $SCANNED file(s) scanned ($SEMGREP_CONFIG)$SUP_NOTE"
            fi
            tail -n +2 "$TMP/semgrep.txt"
          fi
          ;;
        *)
          echo "  SEMGREP OUTPUT UNREADABLE. Treat the $N_SOURCE source files as unscanned."
          COULD_NOT=1
          ;;
      esac
    fi
  fi
fi

# ------------------------------------------------------------------------ osv-scanner
if [ "$N_MANIFEST" -gt 0 ] && [ "$SKIP_DEPS" -eq 0 ]; then
  if ! command -v osv-scanner >/dev/null 2>&1; then
    echo "  OSV-SCANNER MISSING. $N_MANIFEST manifest(s) went unscanned. Install with: brew install osv-scanner"
    COULD_NOT=1
  else
    OSV_ARGS=()
    for m in "${MANIFEST_FILES[@]}"; do OSV_ARGS+=(--lockfile "$m"); done
    osv-scanner scan source "${OSV_ARGS[@]}" > "$TMP/osv.txt" 2>&1
    OSV=$?
    if [ "$OSV" -eq 1 ]; then
      echo "  osv-scanner: known vulnerabilities in $N_MANIFEST manifest(s)"
      strip_ansi < "$TMP/osv.txt" | grep -E "^Total |^\| https" | head -20 | sed 's/^/    /'
      FINDINGS=1
    elif [ "$OSV" -eq 0 ]; then
      echo "  osv-scanner: clean over $N_MANIFEST manifest(s)"
    else
      echo "  OSV-SCANNER FAILED (exit $OSV). Treat the $N_MANIFEST manifest(s) as unscanned."
      strip_ansi < "$TMP/osv.txt" | tail -6 | sed 's/^/    /'
      COULD_NOT=1
    fi
  fi
fi

# ----------------------------------------------------------------------------- verdict
# Findings and incomplete are independent facts. A run that found something AND failed to
# run something must not report either one alone, so it gets its own code.
if [ "$FINDINGS" -eq 1 ] && [ "$COULD_NOT" -eq 1 ]; then
  echo "  VERDICT: findings, AND incomplete. Something did not run, so the list above is"
  echo "           not the whole picture. Bands and owners are in"
  echo "           ~/.claude/skills/_shared/security-standards/security-standards.md"
  exit 3
fi
if [ "$FINDINGS" -eq 1 ]; then
  echo "  VERDICT: findings. Bands and owners are in"
  echo "           ~/.claude/skills/_shared/security-standards/security-standards.md"
  exit 1
fi
if [ "$COULD_NOT" -eq 1 ]; then
  echo "  VERDICT: incomplete. Something did not run, so this is not a clean result."
  exit 2
fi
echo "  VERDICT: clean."
exit 0
