**Skip this step when the repo has no deploy target.** A repo with no deploy file has
no deploy to verify, so say that in one line and go to step 4. When the repo DOES
deploy, this check is mandatory and the rest of this step applies in full.

Checking `status: live` is not enough. The autoDeploy webhook can silently miss a push, so the host keeps serving the OLD commit while still reporting the deploy `live`. Always verify the live deploy's commit equals the commit you just pushed.

```bash
PUSHED=$(git rev-parse HEAD)          # or: git ls-remote origin "$BASE" | cut -f1
```

- **Render**: wait briefly, then read the latest deploy for the service. Confirm both `status == live` AND `commit.id == $PUSHED`. If the commit does not match within ~1 minute, the webhook missed it: trigger a deploy manually via `POST /v1/services/{id}/deploys` and re-verify. (Deploy GET responses can contain raw `\n`; parse with `json.loads(..., strict=False)`.) Surface the URL.
- **Vercel / Netlify**: same idea, confirm the live deployment's commit SHA matches `$PUSHED`, not just that a deploy is "ready". Surface the URL.
- If no auto-deploy is configured, say so.
- **Wait on a check's EXIT STATUS** (`until <check>; do sleep 15; done`), never grep its text: a grep can match nothing for ever and reads like a slow deploy.

A live-but-stale deploy (status live, commit mismatch) is a FAILED release, not a passed one. Report it as such and either re-trigger or hand off.
