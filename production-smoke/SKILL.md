---
name: production-smoke
description: Post-deploy smoke test of a live production service. Verifies the deploy landed (live commit matches what /release pushed), hits the health endpoint and 2-4 key user-facing endpoints, checks the DEBUG-off 404 behavior, and scans recent host logs for tracebacks and 5xx. Read-only, never mutates the service; reports pass/fail per check and recommends rollback or fix-forward on failure. Use after /release, or when the user says "production smoke", "smoke test prod", "verify the deploy", "is prod healthy", "check the live site after the release".
disable-model-invocation: false
---

# Production smoke: verify the live service after a deploy

Run a short read-only check battery against the production service and report pass/fail per check. This is the step after `/release`: release verifies the deploy *happened*; this skill verifies the app *works*.

Strictly read-only: GET requests, log reads, and host-API reads only. Never trigger a deploy, change an env var, restart a service, or roll back on your own. On failure, report and recommend; the user decides.

## Steps

### 1. Identify the target service: positively

Resolve the service from the project's notes (README, project `CLAUDE.md`, or wherever the project records it): live URL, host service id, expected key endpoints.

If the project has a near-identical sibling (a base service and a fork / variant that share history, on separate URLs), anchor on the exact live URL of the service that was just released, not the directory you were launched in. If the conversation includes the `/release` that preceded this run, use its service; if ambiguous, ask which URL before testing. Smoke-testing the wrong sibling produces a false "all green" for the service that actually changed.

State the resolved service URL and host service id in one line before running checks.

### 2. Verify the deploy landed

If the host has a deploy API (Render, Vercel, Netlify, etc.):
- Latest deploy status is `live` / ready.
- The deployed commit SHA matches the repo's `origin/main` HEAD (or the commit `/release` reported pushing). A live-but-stale deploy is a FAIL, not a pass.
- Host gotcha (Render): deploy GET responses can contain raw `\n`; parse leniently.

If the project has no deploy API, verify via whatever the host exposes, or skip with an explicit "deploy-match check skipped, no API" line. Never silently skip.

### 3. Run the smoke checks

All plain GETs with a short timeout (10s), no auth secrets in command lines or output.

1. **Health / homepage**: the health endpoint if the project has one, else `/`. Expect 200.
2. **Key endpoints**: 2-4 user-facing routes that matter to users (not admin). Expect 200 (or the documented redirect). If the project notes list none, test `/` plus the login page and note that the project should name its smoke routes.
3. **Auth page loads**: the login route returns 200, if the app has one.
4. **DEBUG is off** (framework-dependent, e.g. Django): a bogus URL (`/__smoke_bogus_404__/`) returns a plain 404, not a framework debug page. A debug page in prod is a FAIL with a security flag.
5. **Released feature spot-check** (when run right after /release): if the release had a user-visible change with a checkable signature (a new route, a changed response field, a new static asset version), verify it is actually serving. This catches "deploy live but feature missing".

### 4. Scan recent logs

Pull the last ~15 minutes of the service's logs. Look for tracebacks, 5xx responses, repeated warnings, and OOM/restart events. A handful of routine 404s from crawlers is noise; a traceback or any 5xx since the deploy is a FAIL.

### 5. Report

A tight per-check list: PASS / FAIL / SKIPPED with one line of evidence each (status code, commit SHA match, log line). Then the verdict:

- **All green**: say so plainly, done.
- **Any FAIL**: state what is broken and the likely blast radius, then recommend ONE of:
  - **Rollback** (redeploy the previous commit) when the failure is user-facing and the cause is not obvious.
  - **Fix-forward** when the cause is clear and small.
  Do not execute either without the user's go-ahead. Quote the failing evidence so the decision is informed.

If any check was skipped (no health endpoint, no host API access), list it explicitly. A skipped check is not a passed check.

## Notes

- Frontend-affecting releases: this skill checks HTTP behavior, not rendering. If the release shipped UI changes, verify them in a browser separately; note in the report if that did not happen pre-release.
- This skill is deploy-triggered and narrow: run it right after `/release`, not as a general health dashboard.
