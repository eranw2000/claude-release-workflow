**First: a test that replaces one a finding called vacuous or untested, or that guards a
bound, gets the four numbered steps under "So when a finding says something is untested"
below, BEFORE it is written.** They sit far down this file, so a partial read misses them.

Prove each new test by mutation (break the guard, confirm exactly that test fails,
restore by re-applying the code, never `git checkout`) or by contrast (the same
input with the gate un-armed produces the opposite outcome). A test with several
assertions is proven PER ASSERTION: one red assertion proves nothing about its neighbour,
because the first assertion to fail can carry every revert. Confirm the printed
suite total increased by exactly the number of tests added. Never add tests via
shell redirection (`cat >>`, `tee -a`); use Edit/Write so the test-integrity hook
sees them.

**A test for a REPORTED defect must also go red on the code that HAD the defect, not only on a
mutation of your fix.** Check it out (or paste its lines into the test's harness) and run the
test there. A mutation that deletes your new guard keeps everything else your fix changed, and
the original code differs in exactly those things: wording, structure, the order of writes.

**When the fix re-implements a behaviour (a streaming or faster version, a port, a second copy), the
test's oracle is the function it must agree with, never a sibling implementation.** A sibling written
from the same reading shares the same mistake, so the two agree and the test cannot fail. Compare with
the real function, over many generated inputs when the input space is wide.

**A fix to a rule that READS TEXT (a regex, a word list, an intent or same-thing test) is not
proven by the reviewer's one sentence.** That sentence is one member of a phrasing family, and a
fix written to it agrees with it by construction. Before re-review, run the changed rule over
(a) the project's corpus of REAL messages when it keeps one (a golden master; report how many
moved and read each move), and (b) at least four siblings of the reproduction that you did not
derive from your fix: other punctuation, case, subject (we / a company / "exporters"), a
question versus a statement, a two-word name for a code. Put the siblings in the test. Each
sibling that changes verdict is a finding before the reviewer sees the fix.

**A test written to close an "X IS UNTESTED" finding is the single most likely place to
write a vacuous one, and mutation is the only thing that sees it.** Measured on a real
project: a reviewer reported that an ordering band had no test; the test written
minutes later to close that finding put its subject BELOW the group it was compared
against, so the wrong implementation answered identically and the new test passed with the
rule deleted. Seventh instance of that shape on that project, and the first one authored
inside the remedy for a finding that named it. The mechanism is not carelessness: such a
test is written fast, against a fixture chosen to make the INTENDED answer easy to state,
under the belief that the shape is now front of mind.

**So when a finding says something is untested, AND whenever the thing under test is a
BOUND, a LIMIT, a BUDGET or a CAP, do this before writing the test:**

**That second trigger was added 2026-09-07, because the first one alone let a defect
through.** The original wording fired only on a finding that named something untested, so a
test written from scratch alongside its own guard never met it. On a real project a per-batch wait
budget was implemented per PERSON and its test asserted
`sum(slept) == 9144 * 3 * len(job["results"])`, putting the corpus size into the expected
value so the assertion agreed with the defect. It passed a full review round and a mutation
run, because the bound CONSTANT was mutated and correctly killed while nothing mutated where
the counter lived. **For a bound, the fixture must hold MORE items than the bound**, or the
right rule and the wrong one agree by arithmetic.

1. **List the candidate rules, as a list, in writing.** The rule under test plus every
   wrong implementation a reader could reasonably expect. Four is common. Two belong on
   every list: **the input is ignored** (the save, POST or argument never reaches the
   code), and **something already there passes the check** (a seed row, a default, a
   neighbour that matches the filter).
2. **Choose ONE fixture that answers differently under every one of them.** If you cannot,
   the fixture is wrong, not the assertion. This is the step that gets skipped.
3. **Put that list in the test's own docstring**, with the answer each rule would give, so
   a later reader can re-run the check by reading.
4. **Mutate to each wrong rule in turn**, not only to "the rule deleted", and confirm the
   named test goes red for each. Keep a control mutation that no test claims; if it dies
   too, the fixture is over-constrained and the kills mean less than they look.

That is four cheap steps and it closed a shape three prose rules had failed to close.
**Derive the mutation set from the DIFF, not from the fixes you remember making.**
Read the guard-shaped lines the branch actually added, BEFORE choosing a set:

```bash
git diff -U0 --no-color "$BASE"...HEAD -- '*.py' '*.js' '*.ts' '*.sh' \
  | grep -E '^\+[[:space:]]*(if |elif |assert |raise |return (None|False|0|\[\]|""|\x27\x27))'
```

Check that the diff itself is not empty before trusting an empty result, because "no
guards" and "nothing was read" print the same thing. A set assembled from memory
covers the lines you were already thinking about, which are the least likely to be
wrong. On the PR that produced this rule an author-chosen set of six missed five real
guards, and one of the five was that PR's Critical. If a listed line has no test that
can fail, say so in the verdict rather than dropping it.

**Then run it AGAIN against the final diff, because an early enumeration goes stale.**
Enumerate at the start so the list shapes your set rather than arriving after it, and
enumerate again at the end, because everything you wrote in between is un-enumerated.
On a real project an early run against a partial diff checked 5 guard-shaped lines
while the FINAL diff carried 18. Treat "mutations enumerated from the diff" as a claim
with a SHA attached, and state in the verdict which diff the set was enumerated against.

**The mutation harness itself has to be trustworthy, or every row it prints is
decoration.** Three requirements, all cheap, none optional. Purge `__pycache__`
before each run: CPython invalidates bytecode on (mtime, size) at one-second
granularity, so a same-second mutate/run/restore can leave the OLD bytecode active,
and it lies in both directions (a mutant "survives", or a restore silently does not
take). Require a 0-fail BASELINE before mutating, because a suite that was already
red makes every later result unreadable. Require a 0-fail run AFTER restoring, which
is what proves you gave the code back. Then check the kill condition itself: assert
the run went RED and that the FAILING line is the intended one. Matching only the
test's label is vacuous when the harness prints that label on pass and fail alike.
Restore from an in-memory string; if the harness keeps a dict of saved file bytes
instead, key it by FULL PATH, since a basename key silently collides same-named files
at different depths and the restore then corrupts a source file. Nothing but the
post-restore run can see that one.

**Read every changed test line in the WEAKENING direction.** List the assertions
REMOVED from test files in the diff. A loosened assertion changes
neither the suite count nor the reviewed-as-new surface, so nothing else catches it,
and it is green by construction. When one appears, break what the ORIGINAL assertion
protected, not a nearby site.
