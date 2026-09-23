#!/usr/bin/env bash
# sync-remotes.sh - bring every MIRROR remote level with origin, and PROVE it.
#
# Owned by the /release skill, which runs it as the last step of a release in any
# project. Also safe to run by hand at any time:
#
#   bash ~/.claude/skills/release/sync-remotes.sh [/path/to/repo]
#
# WHY IT EXISTS
# Every deploy check confirms PRODUCTION is right. None can see a second remote a
# release forgot, because a mirror has nothing downstream of it to report a
# problem. Measured once already: a commit went live and never reached the
# client-facing mirror, which sat a commit behind for two weeks in silence.
#
# WHY IT IS SAFE
# origin is the source of truth. This script only ever copies the commit that
# origin's default branch ALREADY holds, so it can never introduce unreviewed
# code to a company or client repo, and it needs no #allow-push-main bypass. A
# mirror that has DIVERGED makes the copy fail rather than force it.
#
# Exit 0 = every mirror holds the same commit as origin.
# Exit 1 = a mirror is behind or diverged, or a non-personal remote tracks
#          Claude harness files.
# Exit 2 = could not run (no repo, no origin, no default branch, broken self-check).

set -uo pipefail

# Never let a remote hang this on an interactive credential prompt.
export GIT_TERMINAL_PROMPT=0

HARNESS_RE='(^|/)(CLAUDE\.md|MEMORY\.md|AGENTS\.md|CLAUDE_DECISIONS[^/]*|\.claude/|\.cursor|\.aider|\.windsurf|copilot-instructions\.md)'
# Your own GitHub users or orgs, as a regex. Set SYNC_REMOTES_PERSONAL_RE, or edit here.
PERSONAL_RE="${SYNC_REMOTES_PERSONAL_RE:-(your-github-user)}"
CONFIG_REPO="$HOME/.claude"
FAIL=0

# ---------------------------------------------------------------------------
# Which repo. An explicit path wins. Otherwise the git top level of the cwd,
# unless that is the ~/.claude config repo, which is never the one released.
# ---------------------------------------------------------------------------
REPO="${1:-}"
if [ -z "$REPO" ]; then
  TOP=$(git rev-parse --show-toplevel 2>/dev/null)
  if [ -n "$TOP" ] && [ "$(cd "$TOP" && pwd -P)" != "$(cd "$CONFIG_REPO" 2>/dev/null && pwd -P)" ]; then
    REPO="$TOP"
  else
    echo "FATAL: cannot tell which repo you mean: the cwd is not inside a git repo,"
    echo "       or it resolves to $CONFIG_REPO."
    echo "       Pass the repo path: bash $0 /path/to/repo"
    exit 2
  fi
fi

git -C "$REPO" rev-parse --git-dir >/dev/null 2>&1 || { echo "FATAL: $REPO is not a git repo"; exit 2; }
REPO=$(cd "$REPO" && pwd -P)
echo "repo: $REPO"

# ---------------------------------------------------------------------------
# Self-check. A checker that cannot fail reports success on everything, so prove
# BOTH directions of the harness-file pattern before trusting its verdict.
# ---------------------------------------------------------------------------
hits=$(printf 'CLAUDE.md\n.claude/settings.json\nREADME.md\nsrc/views.py\n' | grep -icE "$HARNESS_RE")
if [ "$hits" != "2" ]; then
  echo "FATAL self-check: harness pattern matched $hits of 4 sample names, expected 2."
  echo "The checker is broken, so a 'clean' verdict from it would mean nothing."
  exit 2
fi
echo "self-check: harness pattern matches the 2 bad sample names and ignores the 2 good ones. OK"

git -C "$REPO" remote get-url origin >/dev/null 2>&1 || { echo "FATAL: $REPO has no 'origin' remote"; exit 2; }

# ---------------------------------------------------------------------------
# Which branch. Ask origin what its default is; fall back to main, then master.
# ---------------------------------------------------------------------------
BRANCH=$(git -C "$REPO" symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null | sed 's@^origin/@@')
if [ -z "$BRANCH" ]; then
  for candidate in main master; do
    if [ -n "$(git -C "$REPO" ls-remote origin "refs/heads/$candidate" 2>/dev/null)" ]; then
      BRANCH="$candidate"
      break
    fi
  done
fi
if [ -z "$BRANCH" ]; then
  echo "FATAL: cannot determine origin's default branch (no main, no master)"
  exit 2
fi

ORIGIN_SHA=$(git -C "$REPO" ls-remote origin "refs/heads/$BRANCH" | cut -f1)
if [ -z "$ORIGIN_SHA" ]; then
  echo "FATAL: origin has no $BRANCH branch, or the network call failed"
  exit 2
fi
echo "origin/$BRANCH (live): $ORIGIN_SHA"

LOCAL_SHA=$(git -C "$REPO" rev-parse HEAD 2>/dev/null)
if [ "$LOCAL_SHA" != "$ORIGIN_SHA" ]; then
  echo "note: local HEAD is $LOCAL_SHA, which differs from origin/$BRANCH."
  echo "      Mirrors will match ORIGIN, not your working tree. Use /release to publish local work."
fi
DIRTY=$(git -C "$REPO" status --porcelain | wc -l | tr -d ' ')
[ "$DIRTY" != "0" ] && echo "note: $DIRTY uncommitted file(s) here. Those reach no remote."
echo

# The commit must exist locally before it can be copied to a mirror.
git -C "$REPO" fetch --quiet origin "$BRANCH" 2>/dev/null
if ! git -C "$REPO" cat-file -e "${ORIGIN_SHA}^{commit}" 2>/dev/null; then
  echo "FATAL: cannot fetch $ORIGIN_SHA from origin, so it cannot be mirrored"
  exit 2
fi

# Only remotes named in `git config release.mirror` are mirrors. One line per
# remote:  git -C <repo> config --add release.mirror <remote-name>
# Every other remote (an upstream, a fork, an archive) is listed and left alone.
MIRRORS=""
for R in $(git -C "$REPO" config --get-all release.mirror 2>/dev/null); do
  if [ "$R" = "origin" ]; then
    echo "note: release.mirror names origin, which is the source, not a mirror. Skipped."
  elif git -C "$REPO" remote get-url "$R" >/dev/null 2>&1; then
    MIRRORS="$MIRRORS $R"
  else
    echo "FAIL: release.mirror names '$R', which is not a remote here."
    FAIL=1
  fi
done
for R in $(git -C "$REPO" remote | grep -v '^origin$'); do
  case " $MIRRORS " in
    *" $R "*) ;;
    *) echo "note: remote '$R' is not listed in release.mirror, so it is left alone." ;;
  esac
done
MIRRORS=$(echo $MIRRORS)
if [ -z "$MIRRORS" ]; then
  echo "No mirror remotes configured. Nothing to sync."
  exit "$FAIL"
fi

# ---------------------------------------------------------------------------
# Advisory only, never a change: report whether a push to origin already fans
# out to the mirrors by itself (two pushurl entries on origin).
# ---------------------------------------------------------------------------
PUSHURLS=$(git -C "$REPO" config --get-all remote.origin.pushurl 2>/dev/null | wc -l | tr -d ' ')
if [ "$PUSHURLS" -lt 2 ] 2>/dev/null; then
  echo "note: a push to origin here reaches origin only. To make EVERY push fan out,"
  echo "      add both URLs as push URLs (origin's own first, or you lose it):"
  echo "        git -C $REPO remote set-url --add --push origin <origin-url>"
  echo "        git -C $REPO remote set-url --add --push origin <mirror-url>"
  echo
fi

for R in $MIRRORS; do
  URL=$(git -C "$REPO" remote get-url "$R")
  echo "=== mirror '$R' -> $URL"

  # A company or client remote must never receive Claude harness files.
  if ! echo "$URL" | grep -qiE "$PERSONAL_RE"; then
    TRACKED=$(git -C "$REPO" ls-files | grep -iE "$HARNESS_RE")
    if [ -n "$TRACKED" ]; then
      echo "  BLOCKED: this is not a personal remote and these harness files are tracked:"
      echo "$TRACKED" | sed 's/^/    /'
      echo "  Remove them from git before mirroring to $R."
      FAIL=1
      continue
    fi
    echo "  harness files: none tracked. OK for a non-personal remote."
  fi

  BEFORE=$(git -C "$REPO" ls-remote "$R" "refs/heads/$BRANCH" 2>/dev/null | cut -f1)
  if [ "$BEFORE" = "$ORIGIN_SHA" ]; then
    echo "  already level with origin at $ORIGIN_SHA"
  else
    echo "  behind or diverged (holds '${BEFORE:-nothing}'), mirroring $ORIGIN_SHA"
    if ! git -C "$REPO" push "$R" "$ORIGIN_SHA:refs/heads/$BRANCH" 2>&1 | sed 's/^/    /'; then
      echo "  MIRROR FAILED. The reason is in the lines above: an unreachable host, no"
      echo "  permission, or a rejected non-fast-forward, which means this mirror carries"
      echo "  its own commits. Read them before doing anything forceful."
      FAIL=1
      continue
    fi
  fi

  # Verify from the live ref, never from the command output.
  AFTER=$(git -C "$REPO" ls-remote "$R" "refs/heads/$BRANCH" 2>/dev/null | cut -f1)
  if [ "$AFTER" = "$ORIGIN_SHA" ]; then
    echo "  VERIFIED: $R/$BRANCH = $AFTER"
  else
    echo "  FAIL: $R/$BRANCH is '${AFTER:-unreadable}', expected $ORIGIN_SHA"
    FAIL=1
  fi

  # Control row: a ref that must not exist. Rows here mean the read is lying.
  CTRL=$(git -C "$REPO" ls-remote "$R" "refs/heads/definitely-no-such-branch-xyz" 2>/dev/null | wc -l | tr -d ' ')
  if [ "$CTRL" != "0" ]; then
    echo "  FATAL: control ref returned $CTRL rows, so ls-remote output is not trustworthy"
    exit 2
  fi
  echo "  control row: absent branch returned 0 rows, so the read can tell a difference. OK"
  echo
done

if [ "$FAIL" = "0" ]; then
  echo "RESULT: every mirror is level with origin/$BRANCH at $ORIGIN_SHA"
  exit 0
fi
echo "RESULT: at least one mirror is NOT in sync. See the lines marked FAIL or BLOCKED above."
exit 1
