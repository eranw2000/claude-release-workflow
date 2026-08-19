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
exec python3 "$(dirname "${BASH_SOURCE[0]}")/block-git-push-main.py"
