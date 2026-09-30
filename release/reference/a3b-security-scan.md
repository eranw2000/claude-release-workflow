Run the code scanners over what this release CHANGES, then apply the gate. This runs HERE,
on the PR head, while the diff still exists. Once `gh pr merge` has run and A4's
`git pull` has landed, the branch's commits ARE the base branch, so any range computed
from the base resolves to nothing and the scan reads zero files.

Check out the candidate so the working tree is the thing being shipped, then scan the
range from the base SHA pinned in step 0. Both prerequisites STOP the release on failure,
because a scan that ran on the wrong tree certifies the wrong commit:

```bash
gh pr checkout <number> || { echo "STOP: checkout failed for PR <number>"; exit 1; }
HEAD_NOW=$(git rev-parse HEAD) || { echo "STOP: HEAD unreadable after checkout"; exit 1; }
[ "$HEAD_NOW" = "$CANDIDATE_SHA" ] || { echo "STOP: candidate mismatch, HEAD=$HEAD_NOW pin=$CANDIDATE_SHA"; exit 1; }
bash ~/.claude/skills/_shared/security-scan.sh --since="$BASE_SHA" "$(git rev-parse --show-toplevel)"
```

A failed checkout leaves the old `HEAD` in place, and the scan would then read a tree
nobody is shipping while A4 merges a head nothing scanned. So the guard is `|| exit`, never
an `echo`: an advisory line scrolls past and the run continues into the merge. Without
`gh`, substitute `git fetch origin <headRefName> && git checkout <headRefName>` and guard
it the same way.

`--since` takes `$BASE_SHA`, the SHA pinned in step 0, and never a literal branch name:
a hardcoded base scans nothing on a `master` or `develop` repo, and re-resolving the ref
here would silently move the range if somebody pushed to the base branch meanwhile.

Getting the order wrong produces a result that looks like a pass and is not. Measured
2026-08-28, the first real use of this step, when the scan ran after the merge and after
the commit: the range resolved to `HEAD...HEAD`, so it scanned nothing and returned
`NOTHING TO SCAN`. That is exit 2 and correctly not a pass, but a reader in a hurry sees
a green step. The order is fixed now, and this is why: the scan's input is a DIFF, and
both a merge and a pull destroy that diff by making the candidate and the base the same
commit. Run it while the two still differ, which is before either.

Four exit codes, and they mean four different things:

- **0, clean.** It scanned something and found nothing. Continue.
- **1, findings.** Everything ran. Read them, and continue only when the gate below is
  satisfied. This BLOCKS until the gate is satisfied.
- **2, incomplete.** Nothing was scanned, or a scanner is missing, failed, or did not
  finish. This is NOT a pass, and it is NOT a block either, so say WHICH of the two it
  was:
  - **Nothing to scan**, the scan's own `NOTHING TO SCAN` line, which is what a docs-only
    or config-only release produces because no changed path matched a source name.
    Report it in the final report as `security scan: NOT APPLICABLE, no source file
    changed`, in those words. Never as clean, never as passed, never as a tick.
  - **A scanner is missing or failed.** Install the named tool and run it again. Shipping
    UNSCANNED is a waiver like any other and takes the register's shape: a one-line reason,
    a named owner and the date, written into the release notes as `shipped UNSCANNED:
    <reason>, owner <name>, <YYYY-MM-DD>` and repeated in the final report. Missing any of
    the three, it is not allowed, and the release stops until the tool runs. A self-report
    with no owner is how a skipped scan becomes nobody's backlog item.
- **3, findings AND incomplete.** Both facts are true and neither may hide the other.
  Treat it as a 1 for the gate, so it BLOCKS, and as a 2 for the report.

**FIRST, read the `origin:` line, because a finding here is not automatically yours.**
The scan lists changed FILES from the diff and semgrep then reads each one END TO END, so
every pre-existing line in a file you touched arrives in this report. Measured 2026-09-02 on
a release that changed 31 files: three findings, all three already live on the base branch, none on a
line the change added. Applied without this distinction the gate below either blocks a
correct release or teaches whoever meets it to wave it through, and both are worse than no
gate. Since that date the scan tags every finding and prints the split:

- **`[NEW]`**, on a line this change adds. The gate below applies in full.
- **`[PRE-EXISTING]`**, in a file this change touched but on a line it did not write. It does
  NOT block the release. **Name it in the final report** as a pre-existing finding, with its
  count, so it reads as a backlog item rather than as a check that passed. It belongs in the
  waiver register or in the project TODO, and it is not this release's job.
- **`[UNKNOWN]`**, meaning no added-line map could be built. **Treat these as `NEW`**, which
  is the direction the tagger itself fails in, deliberately.

**Then the gate: a release needs no unresolved blocking `[NEW]` finding, and every accepted
high finding named in the waiver register** at `~/.claude/skills/_shared/security-standards/baseline.yml`,
with an owner, a justification and an expiry. A `[NEW]` finding kept without a waiver entry is
an unresolved blocking finding; a `[PRE-EXISTING]` one is named in the report instead. Bands are in `security-standards/severity-taxonomy.md`.

**The register is read by the scan, so a waiver actually takes effect.** An entry needs a
`rule_id` that matches the finding (the whole rule name, or its last dotted segment), a
`path` glob, an `owner`, a `reason` and an `expires` date. The scan then prints how many it
waived. Three things it deliberately refuses to honour, each reported out loud rather than
silently: an entry past its expiry, an entry with no expiry at all, and an entry carrying
only a `fingerprint` and no `path`. If PyYAML is missing from
the interpreter, the scan says the register went unread and reports every finding rather
than assuming there was nothing to waive.

**Why this reads the diff and not the repository.** Measured 2026-08-28: semgrep's
p/default ruleset returned 57, 28, 8 and 1 findings on four repos already in production.
A whole-repository gate would have blocked every one of them on its first day, and a
guard the present cannot pass gets switched off by whoever meets it late in the day. The
standing backlog belongs in the waiver register, not in a gate nobody can clear.

**A blocking finding here costs nothing to act on, which is the other half of why the step
moved.** Nothing has merged and nothing has been pushed, so fixing it is a commit on the
branch rather than a revert on the base branch.

**Say what you did.** Name the scan mode, the file count and the verdict in the final
report. A silent skip reads as a check that passed.
