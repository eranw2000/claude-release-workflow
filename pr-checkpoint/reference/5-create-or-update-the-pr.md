This skill is meant to be re-run on the same branch as you keep working, so **check for an existing PR first**. `gh pr create` errors out (`a pull request for branch ... already exists`) on the second run otherwise.

```bash
EXISTING=$(gh pr view "$BRANCH" --json number,url,state -q 'select(.state=="OPEN") | .url' 2>/dev/null)
```

- **A PR already exists** (`$EXISTING` is non-empty): the branch push in step 3 already updated it. Do NOT create a new one. Optionally refresh the PR body with `gh pr edit "$BRANCH" --body "..."` if the changes since last time warrant it. Reuse `$EXISTING` as the PR URL and continue.
- **No PR yet**: create one with a HEREDOC body, pinning the base to the resolved `$BASE`:

```bash
gh pr create --draft --base "$BASE" --head "$BRANCH" --title "<title under 70 chars>" --body "$(cat <<'EOF'
## What
...
## Why
...
## Status
**Checkpoint**: opened for local testing and review.
## Changes
...
## Test plan
- [ ] ...
EOF
)"
```

If the project doesn't use draft PRs, drop `--draft`.

Capture the PR URL (the new one, or `$EXISTING`).
