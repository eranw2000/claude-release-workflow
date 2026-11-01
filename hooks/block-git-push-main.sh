#!/bin/bash
# Thin delegator. The real logic lives in block-git-push-main.py.
#
# Kept as a .sh so the filename already wired in ~/.claude/settings.json keeps
# working. Point settings.json at this file; it hands off to the Python.
#
# The earlier shell implementation decided from a SUBSTRING match on the command
# text, so three things that were not pushes got blocked: a quoted grep pattern
# containing the phrase, a heredoc writing a script whose text contains a push
# line, and prose ending in one word after "push". This version decides from a
# quote-aware PARSE instead, in block-git-push-main.py.
#
# Try each interpreter a PC or a Mac may have. Only 0 and 2 are verdicts: a Store
# stub or a missing program exits with something else, and any exit but 2 does not
# block, so `exec python3` would quietly switch this guard off.
HOOK="$(dirname "${BASH_SOURCE[0]}")/block-git-push-main.py"
INPUT="$(cat)"
WHY=""
for PY in python3 python "py -3"; do
  $PY "$HOOK" <<< "$INPUT"
  STATUS=$?
  case "$STATUS" in
    0|2) exit "$STATUS" ;;
  esac
  # This interpreter runs, so the hook itself failed: another one would fail the same way.
  if $PY -c '' >/dev/null 2>&1; then
    WHY="this hook crashed (exit $STATUS, $PY)"
    break
  fi
done
[ -n "$WHY" ] || WHY="no working Python, so pushes cannot be checked. Install Python 3"

# Only the command, with quotes and backslashes dropped: `g''it` runs git, and a folder
# named github in the cwd is no reason to refuse `npm run push`.
COMMAND="${INPUT#*\"command\":}"
PLAIN="${COMMAND//[\'\"\\]/}"
case "$PLAIN" in
  *'#allow-push-main'*) exit 0 ;;
esac
[ "${CLAUDE_ALLOW_PUSH_MAIN:-}" = "1" ] && exit 0
case "$PLAIN" in
  *git*push*)
    echo "Blocked: $WHY. This push was not checked, so it was refused." >&2
    exit 2
    ;;
esac
# A warning on exit 0 reaches Claude only through this JSON on stdout.
printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","additionalContext":"block-git-push-main: %s."}}\n' "$WHY"
exit 0
