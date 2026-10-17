**Preflight (mandatory):** before any push, run the harness-files protection check from `~/.claude/skills/_shared/harness-files-protection.md`. For every remote you do not own personally, verify that no Claude / AI-assistant harness files (`CLAUDE.md`, `MEMORY.md`, `AGENTS.md`, `.claude/`, etc.) are tracked, and that `.gitignore` covers the full set. If any harness file is tracked on a client remote, STOP the push and surface the file list. Never push harness files to a repo you do not own.

Push to origin. Then push to each remote named in `git config --get-all release.mirror`, if there are any. Report the result of each. Never push to any other remote in `git remote -v` (an upstream, a fork, an archive).

**This push targets the base branch, so it trips the `block-git-push-main.sh` guard hook.** That is expected here (this is the sanctioned release path). Append the documented marker to the push so the hook lets it through:

```bash
git push origin "$BASE" #allow-push-main
```

Keep the marker unquoted and after the args (quoting it makes git read it as a refspec). If the auto-mode classifier still blocks the push (it can, independently of the hook), ask the user to run the push themselves with `! git push`, rather than fighting it.

If a push is rejected because the remote moved, fetch + rebase and retry. A rebased FEATURE branch needs `git push --force-with-lease` (never a plain `--force`). There is no equivalent on the base branch: never force-push it, however certain you are that nobody else pushed. Stop and surface it instead. This matches the Guardrails section below, which had said the opposite of the old wording here.

**Then prove every MIRROR actually landed it. Mandatory whenever the repo lists a mirror in `git config release.mirror`:**

```bash
bash ~/.claude/skills/release/sync-remotes.sh <repo path>
```

A mirror is a remote you named once with `git -C <repo> config --add release.mirror <remote-name>`. Every other remote (an upstream, a fork, an archive) is listed in the output and never pushed. Exit 0 means every listed mirror holds the same commit as origin's default branch. Exit 1 means one is behind, has diverged, is not a remote, or is a non-personal remote with Claude harness files tracked. **The release is not done until it exits 0.** Paste its output into the final report.

Why this is not optional: the deploy check in step 3 confirms PRODUCTION is right, and nothing anywhere confirms a mirror, because a mirror has nothing downstream of it to report a problem. Measured once already, a commit went live and never reached the client-facing mirror, which sat a commit behind for two weeks in silence.

It needs no `#allow-push-main`: origin is the source of truth and the script only ever copies the commit origin ALREADY holds, so it cannot introduce unreviewed code to a company or client repo. It reads LIVE refs with `git ls-remote`, never the local tracking branches. It also reports, without changing anything, whether origin carries a second push URL so that every push fans out by itself.
