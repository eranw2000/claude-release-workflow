### 0. Project-specific pre-push gate (optional)

If your repo defines a pre-push validation of its own (a health check, a link checker, an index or asset builder, a content lint), run it here and BLOCK on failure. This is the place to catch anything that would ship broken content or a broken build to prod.

```bash
# examples: replace with your project's actual gate, skip if none
make check        # or: npm run lint && npm test
python3 tools/validate.py --check
```

If the gate regenerated any tracked artifact (an index, a lockfile, a build manifest), stage and commit it as part of this release. If the gate fails, STOP, fix it, rerun until it passes, then continue. Skip silently for projects that define no such gate.
