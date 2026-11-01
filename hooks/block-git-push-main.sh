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
for PY in python3 python "py -3"; do
  $PY "$HOOK" <<< "$INPUT"
  STATUS=$?
  case "$STATUS" in
    0|2) exit "$STATUS" ;;
  esac
done
# No working Python. Refuse what this hook exists to stop, and let everything else
# through, or a PC without Python could not run any shell command at all.
case "$INPUT" in
  *git*push*)
    echo "Blocked: no working Python, so this push could not be checked. Install Python 3 and re-run." >&2
    exit 2
    ;;
esac
echo "block-git-push-main: no working Python, so pushes will be refused until Python 3 is installed." >&2
exit 0
