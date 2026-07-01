---
name: local-docker-rebuild (shared)
purpose: Detect whether a local Docker container is running for the current repo and rebuild it from the working tree. Used by /pr-checkpoint (rebuild from the feature branch for local testing) and /release (rebuild after deploy so localhost matches prod).
---

# Local Docker rebuild protocol

This step is optional. It only does something if the repo runs locally in Docker. If your project does not use Docker, the caller skips this silently.

## When to skip silently

```bash
docker info >/dev/null 2>&1 || skip
```

If `docker` is not installed or the daemon is not running, skip this step silently and continue.

## Detection (in order)

1. **Compose file in repo root**: look for `docker-compose.yml`, `docker-compose.yaml`, `compose.yml`, or `compose.yaml`.
2. **Compose project up**: from the repo dir, run `docker compose ps --status running --quiet`. If it returns container IDs, this repo IS running locally via compose.
3. **Fallback (no compose)**: if there is a `Dockerfile` but no compose file, run `docker ps --format '{{.Names}}\t{{.Image}}'` and match by image or container name containing the repo basename (case-insensitive).

If nothing is detected, say so in one line and move on. Do NOT start a stopped container that was not running before, only update what was already live.

## Rebuild

- **Compose case**: from the repo dir, run `docker compose up -d --build`. Only changed services restart; bind-mounted code is picked up automatically.
- **Non-compose fallback**: rebuild the image with `docker build -t <existing-image-tag> .` then `docker restart <container-name>`. If the existing run command is not recoverable from `docker inspect`, report what you found and ask before recreating.

## Verify

- Wait a few seconds.
- Check container status: `docker compose ps` or `docker ps --filter name=...`. Confirm `Up` / healthy.
- Tail the last ~20 log lines: `docker compose logs --tail=20` or `docker logs --tail=20 <name>`. Surface any boot errors.

## Reporting

Report the result in one line:
- `rebuilt and restarted: <container/service name>, status=Up`
- `no local container found for this repo, skipped`
- `Docker not running, skipped`
- On error: `Docker rebuild failed: <reason>`, but do NOT roll back the caller (the PR / push already happened).
