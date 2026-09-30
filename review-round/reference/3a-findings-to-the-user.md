### 3a. Every mode: take the findings to the user BEFORE fixing any of them

**A reviewer's verdict is information for the user, not a work order for you.** Report,
then STOP and let them choose. This step is not satisfied by mentioning the findings on
the way past them.

Give them, in this order:

1. **Is the deliverable already in the user's hands and working?** CHECK it, never assume:
   does this diff touch product code at all, is it already merged or deployed, does
   the live thing answer. Lead with that sentence. If it works and is already there,
   every finding below is optional by definition.
2. **What each finding TOUCHES**: product code, or test and harness code. Reviewers
   grade by defect severity and cannot see who is waiting. A Critical in a checking
   script and a Critical in a shipped rule are not the same thing to the user.
3. **Whether the POLICY each finding cites actually HOLDS in this repository.** A
   finding fails in two independent ways: the INSTANCE ("this file contains X") and
   the POLICY ("X is forbidden here"). Step 4 below reproduces the instance, and
   that is the half everyone remembers. **Nothing asked the second question, and the
   second question is the one that decides whether a finding blocks a release or is
   a tidy-up.** So for any finding resting on a rule, a convention, a policy, a
   baseline or a "despite the repository's ..." clause: run that rule's own check
   over WHAT IS ALREADY COMMITTED, and give the user the number. If the shipped tree
   would fail it too, the finding is a backlog item and the POLICY is what needs a
   decision, not the branch.

   Measured on a real project. A reviewer returned BLOCK because a branch committed a
   few sensitive identifiers "despite the repository's explicit baseline policy",
   quoting a real `.gitignore` line and showing them in the diff. Every fact was true
   and the instance reproduced. One command showed there was no such baseline: the
   main branch already carried the same kind of data, and the cited rule covered a
   narrower case. So the branch was held for nothing.
   **The refutation was worth more than the fix**: measuring the policy raised a
   larger question no reviewer had asked, which went to the user as a decision.

   This is "measure a new guard against what is already live", applied to a guard
   somebody else CITES rather than one you author. A finding arriving with
   file-and-line evidence is exactly when it is least likely to be run.
4. **What fixing them costs**, in the user's units: roughly how long, and what it delays.
5. **A recommendation**, then wait.

**Round two and beyond needs the user to say so explicitly. "Re-review" means once.**
If a round produces fixes, do not launch the next round on your own initiative. **If a
fix contains a NEW mistake of its own, stop immediately and say so**: a different
mistake each round, rather than a repeat, means the work has outgrown solo iteration,
and the honest move is to offer to work through it together, not another round.

Measured on a real project before this step existed: four requested fixes became six
rounds and 1,030 lines of new checking code against zero lines of product code, and a
customer's team lost a day of testing while the product sat already merged in their QA
and answering correctly. Every finding in the last three rounds was in the harness.
