Every finding THE USER CHOSE TO ACT ON in step 3a gets REPRODUCED by executing the
failing input through the real path before any fix is written. A finding that does not
reproduce is marked **disputed**, with the exact command you ran; it is never silently
accepted (fixing a non-bug) or silently dropped (losing a real one). Anything the user
deferred is RECORDED where a reader will meet it, not fixed. Respect the
side-effect guard: no reproduction against production, live services, or real
credentials; a side-effectful reproduction stays unreproduced and is adjudicated by
reading, with that stated.

**When two agents independently report the same finding, treat it as the priority.**
They ran in parallel from different prompts and different checklists, so agreement
is a signal rather than duplication. Both convergent findings on 2026-07-22 were
real and both had production consequences; on 2026-07-27 the two that converged
were the PR body being quantitatively false and the schema disclosure. Never dedupe
a convergent pair down to one line and lose that it was found twice.

Also check `git status` in the repo after the agents return, before writing any fix.
A reviewer killed mid-mutation has left the tree mutated, and building a fix on top
of that state ships the mutation.
