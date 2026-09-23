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

These files / globs are AI-assistant working notes and config. Never push them to a client / company repo.

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

Default to client when unsure. The cost of a false positive is one extra check; the cost of a false negative is leaking internal notes to a client repo.

## 2b. Git tracks per REPO, not per REMOTE

A repo with both a personal `origin` and a client `company` remote cannot keep a harness file for
one and hide it from the other. There is no per-remote tracking. So protecting the client remote
means untracking the file entirely, which also removes it from the personal remote's copy.

The sequence that makes it safe:

1. **Check the client remote first.** These files are often already THERE, not hypothetical:
   `gh api "repos/<org>/<repo>/git/trees/$(gh api repos/<org>/<repo>/commits/HEAD --jq .sha)?recursive=1" --jq '.tree[].path'`
   On real projects they were live at HEAD more than once.
2. **Preserve content BEFORE untracking.** `git rm --cached` does not delete, move, or merge
   anything: the file stays on disk and stays loaded as harness context, but it stops being backed
   up anywhere. Copy any harness file that exists nowhere else to a private location (for
   example `~/.claude/projects/<X>/CLAUDE.md`) and verify it is byte-identical first. State
   dumps with nothing worth keeping can be untracked as they are.
3. `git rm -r --cached <paths>`, add the full harness block to the repo's `.gitignore`, commit,
   push to origin AND to the client remote. An ignore rule never untracks anything already in
   the index, so a `.gitignore` that lists a path does not prove the path is untracked.
4. **Verify on the remote tree, not locally.** A local `git ls-files` says nothing about what the
   client can see.
5. **History still holds them.** Untracking clears HEAD only. Purging needs a rewrite plus a
   force-push to a client org repo, which is destructive and may hit branch protection, so it is a
   deliberate decision rather than cleanup.

Note the rule does NOT fire on a personal-only repo. A repo whose `origin` is one of your
personal owners and which has no client remote may track a root `CLAUDE.md` legitimately.
Check the remotes before calling a tracked harness file a violation.

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
# 3. Check whether any harness file is currently tracked, at repo root OR nested.
# The (^|.*/) prefix on every alternative is load-bearing: without it a nested
# sub/dir/.claude/settings.json or docs/AGENTS.md slips past a ^-anchored match
# and leaks. git ls-files prints full paths, so match anywhere in the path.
git ls-files \
  | grep -E '(^|.*/)(CLAUDE\.md|CLAUDE\.local\.md|MEMORY\.md|AGENTS\.md|\.claude/|\.cursor|\.aider|\.windsurf|copilot-instructions\.md)'
```

- **No matches**: confirm `.gitignore` actually blocks the full set (section 1). Do not eyeball it; test each glob with `git check-ignore`. For a representative path per entry (e.g. `CLAUDE.md`, `sub/CLAUDE.md`, `.claude/x`, `.cursor/x`), run `git check-ignore -v <path>`; a path that prints nothing is NOT ignored, so append the missing glob, stage `.gitignore`, and commit `chore: gitignore AI harness files for client repo`.
- **Matches found**: STOP the push. Report which files are tracked and ask whether to:
  - (a) `git rm --cached <files>` + add to `.gitignore` + commit + then push (recommended), or
  - (b) push to personal remotes only and skip the client remote this round.

Never silently push harness files to a client remote. Never use `git update-index --skip-worktree` to hide them; that hides the tracking, it does not remove it from the remote.

## 4. Report line for the release/checkpoint final status

Include one line in the final status block:

```
Harness-file preflight: <N> remote(s) checked, <M> personal / <K> client; client remotes clean.
```

If any client remote was found to have tracked harness files, the line becomes:

```
Harness-file preflight: BLOCKED: <file list> tracked; client push skipped pending decision.
```
