#!/bin/bash
# PreToolUse hook for Bash tool calls.
# Blocks `git push` commands that target main / master, redirecting Claude to the /release skill.
# Bypass for emergencies: set CLAUDE_ALLOW_PUSH_MAIN=1 in the environment, OR include #allow-push-main in the command.
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

# Match patterns that push to main or master:
#   git push ... main
#   git push ... master
#   git push ... HEAD:main
#   git push ... <local>:main
# Negative-match: don't trigger on branch names that merely contain "main" (e.g. "remain", "domain", "maintenance").
# The \b word boundaries + the explicit `main|master` alternation keep this tight.
if echo "$COMMAND" | grep -qE 'git[[:space:]]+push\b.*\b(main|master)\b'; then
  jq -n \
    --arg cmd "$COMMAND" \
    '{
      decision: "block",
      reason: "Blocked: raw `git push` to main/master. Use the /release skill to update README + project notes and verify the deploy as part of the same atomic action.",
      hookSpecificOutput: {
        permissionDecision: "deny",
        additionalContext: ("Push attempted: " + $cmd + "\n\nWhy this is blocked: releases should always go through the /release skill, never a raw git push. /release auto-detects whether this is a trunk-mode project (commit straight to main) or a PR-mode project (merge open PRs), then updates docs + pushes + verifies the auto-deploy.\n\nBypass for emergencies (use sparingly):\n  - Set CLAUDE_ALLOW_PUSH_MAIN=1 in the env, OR\n  - Append #allow-push-main as a comment in the bash command itself.")
      }
    }'
  exit 0
fi

# Not a push-to-main -> allow.
exit 0
