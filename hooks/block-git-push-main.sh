#!/bin/bash
# PreToolUse hook for Bash tool calls.
# Blocks `git push` that would land on main / master, redirecting Claude to /release.
#
# Two detection layers:
#   1. Explicit destination: the command names main/master as a push DESTINATION
#      (e.g. `git push origin main`, `git push origin HEAD:main`). Tightened so it
#      does NOT fire on branch names that merely contain the word (feature/main-nav,
#      fix-master-toggle).
#   2. Implicit push on main: a bare `git push` with no refspec (`git push`,
#      `git push origin`, `git push -u`) resolves to the current branch. If the
#      target repo is on main/master, that lands on main without the word ever
#      appearing in the command, so layer 1 alone would miss it.
#
# Bypass for emergencies: set CLAUDE_ALLOW_PUSH_MAIN=1 in the environment, OR
# include #allow-push-main in the command.
#
# Wire it in ~/.claude/settings.json under hooks.PreToolUse matcher "Bash" (see the repo README).
# Protocol: read JSON from stdin, emit JSON to stdout for block-with-feedback, exit 0.

set -u

# Bypass via env var.
if [ "${CLAUDE_ALLOW_PUSH_MAIN:-0}" = "1" ]; then
  exit 0
fi

# Read the tool call envelope.
INPUT=$(cat)
COMMAND=$(echo "$INPUT" | jq -r '.tool_input.command // ""')

# Empty command -> not our concern.
if [ -z "$COMMAND" ]; then
  exit 0
fi

# Bypass via inline comment marker.
if echo "$COMMAND" | grep -qE '#allow-push-main\b'; then
  exit 0
fi

# Only look at commands that actually run `git push` (allowing `git -C <dir> push`,
# `git --no-pager push`, and flags in between).
if ! echo "$COMMAND" | grep -qE 'git[[:space:]]+(-{1,2}[^[:space:]]+[[:space:]]+|-C[[:space:]]+[^[:space:]]+[[:space:]]+)*push\b'; then
  exit 0
fi

# Block the tool call and exit. $1 is a short reason suffix.
# A PreToolUse hook blocks ONLY via exit code 2 with the message on stderr. The old
# stdout {"decision":"block"} / hookSpecificOutput-without-hookEventName form is NOT
# honored for PreToolUse and fails OPEN (the push would be allowed). Confirmed against
# https://code.claude.com/docs/en/hooks.md.
emit_block() {
  local why="$1"
  {
    echo "Blocked: raw \`git push\` to main/master ($why). Use /release, which updates README + project notes and verifies the deploy as part of the same atomic action."
    echo "Push attempted: $COMMAND"
    echo "/release auto-detects trunk mode (commit straight to main) vs PR mode (merge open PRs), then updates docs + pushes + verifies the auto-deploy."
    echo "Bypass for emergencies (use sparingly): set CLAUDE_ALLOW_PUSH_MAIN=1 in the env, OR append #allow-push-main as a comment in the bash command."
  } >&2
  exit 2
}

# --- Layer 1: explicit main/master destination -------------------------------
# Require main/master to sit in a refspec position: preceded by a space or colon
# and followed by a space, colon, or end of the push segment. This matches
# `... main`, `HEAD:main`, `main:staging`, but NOT `feature/main-nav` (slash
# before) or `fix-master-toggle` (hyphen around).
if echo "$COMMAND" | grep -qE 'push\b[^;&|]*[[:space:]:](main|master)([[:space:]:]|$)'; then
  emit_block "explicit main/master destination"
fi

# --- Layer 2: bare/implicit push while on main -------------------------------
# A push is "bare" when, after `push`, there are only flags and at most one token
# (the remote), with no second token (a refspec) before the end of the segment.
# `git push`, `git push origin`, `git push -u origin`, `cd repo && git push`.
if echo "$COMMAND" | grep -qE 'push([[:space:]]+-{1,2}[^[:space:]]+)*([[:space:]]+[^-[:space:]][^[:space:]]*)?[[:space:]]*($|[#;&|])'; then
  # Resolve which repo the push runs in: an explicit `git -C <dir>`, else a
  # leading `cd <dir> &&`, else the hook's own working directory.
  DIR=$(echo "$COMMAND" | sed -nE 's/.*git[[:space:]]+-C[[:space:]]+([^[:space:]]+).*/\1/p')
  if [ -z "$DIR" ]; then
    DIR=$(echo "$COMMAND" | sed -nE 's/^[[:space:]]*cd[[:space:]]+([^&;|]+)[[:space:]]*(&&|;).*/\1/p' | sed -E 's/[[:space:]]+$//')
  fi
  DIR=${DIR:-.}
  DIR="${DIR/#\~/$HOME}"        # expand a leading ~

  BRANCH=$(git -C "$DIR" rev-parse --abbrev-ref HEAD 2>/dev/null)
  if [ "$BRANCH" = "main" ] || [ "$BRANCH" = "master" ]; then
    emit_block "bare push while on $BRANCH"
  fi
fi

# Not a push-to-main -> allow.
exit 0
