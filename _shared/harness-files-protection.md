---
name: harness-files-protection (shared)
purpose: Shared preflight used by /release and /pr-checkpoint to keep Claude / AI-assistant harness files out of any GitHub repo that is not your own personal account.
---

# Harness file protection (preflight before push)

Run this preflight BEFORE any `git push` that targets a remote you do not own personally (a client org, an employer org, a shared team repo). Assistant working notes belong in your own repos, not in someone else's.

## 0. Configure your personal owners (edit this once)

Set the GitHub owner names that count as "yours". Everything else is treated as a client / third-party remote. Replace the examples below with your own accounts and orgs:

```
PERSONAL_OWNERS = ["your-github-username", "your-personal-org"]
```

If you are not sure how to encode this, the rule of thumb is: a remote is personal only if you would be comfortable with your private assistant notes being public in it.

## 1. The harness file set

These files / globs are AI-assistant working notes and config. Never push them to a repo you do not own.

- `CLAUDE.md`, `**/CLAUDE.md`
- `CLAUDE.local.md`
- `MEMORY.md`, `**/MEMORY.md`
- `AGENTS.md`
- `.claude/`
- `.cursor*`
- `.aider*`
- `.windsurf*`
- `.github/copilot-instructions.md`

`README.md` is always public-safe and is the external counterpart. Keep that split.

## 2. Personal vs client remote classification

A remote is **personal** (harness files allowed) if its host+owner matches one of your configured `PERSONAL_OWNERS`. Every other remote is **client** (harness files NOT allowed).

Default to client when unsure. The cost of a false positive is one extra check; the cost of a false negative is leaking internal notes into a repo you do not control.

## 3. Preflight algorithm

For each remote that the upcoming push will touch:

```bash
# 1. Resolve the remote URL
git remote get-url <remote>

# 2. Classify as personal or client (see section 2)
```

If the remote is **personal**: no action, continue.

If the remote is **client**:

```bash
# 3. Check whether any harness file is currently tracked
git ls-files \
  | grep -E '^(CLAUDE\.md|.*\/CLAUDE\.md|CLAUDE\.local\.md|MEMORY\.md|.*\/MEMORY\.md|AGENTS\.md|\.claude/|\.cursor|\.aider|\.windsurf|\.github/copilot-instructions\.md)'
```

- **No matches**: confirm `.gitignore` blocks the full set (section 1). If any glob is missing, append it, stage `.gitignore`, and commit `chore: gitignore AI harness files for shared repo`.
- **Matches found**: STOP the push. Report which files are tracked and ask whether to:
  - (a) `git rm --cached <files>` + add to `.gitignore` + commit + then push (recommended), or
  - (b) push to personal remotes only and skip the client remote this round.

Never silently push harness files to a client remote. Never use `git update-index --skip-worktree` to hide them; that hides the tracking, it does not remove the file from the remote.

## 4. Report line for the release / checkpoint final status

Include one line in the final status block:

```
Harness-file preflight: <N> remote(s) checked, <M> personal / <K> client; client remotes clean.
```

If any client remote was found to have tracked harness files, the line becomes:

```
Harness-file preflight: BLOCKED, <file list> tracked; client push skipped pending decision.
```
