**Two things go in the code-reviewer prompt whenever the diff touches them, because
neither is visible to any hook.** First, the three stop conditions: authentication or
authorization switched off, certificate checking switched off, a security check switched
off. All three are REMOVALS, so tell the agent to read the old side of the diff. Second,
the attack test: a change touching authentication or input handling needs a test that
tries to break it, and asserts the refusal rather than the absence of a crash. Both rules
are written out in `~/.claude/agents/code-reviewer.md`.

Write a per-agent prompt that NAMES the 2-3 riskiest spots in this diff and the
specific claims each agent should verify by execution. Point code-reviewer at the
paths you are least sure of; point deploy-guard at the config/prose/topology angle;
tell pr-validator which added tests must be proven to have run. A generic "review
this diff" prompt is a protocol violation: the 2026-07-17 Criticals were caught
because the prompts aimed the reviewers at the weak spots.

**Every prompt must ALSO tell the agent not to stop at the named spots**, and to
audit the new TESTS as adversarially as the source. Aiming is a floor, not a
ceiling: on 2026-07-27 four spots were named, one was refuted by execution, and the
Critical was none of them. It surfaced only because the prompt added "do not stop at
these" and "audit the test file as adversarially as the source". A prompt that names
three spots and says nothing else quietly caps the review at three spots.

**Every prompt asks: "Where did each new test's input come
from?"** A bug fix's first failing test must use the original input that showed the bug,
and a new feature needs at least one test on a real sample. A fixture written in the same
sitting as the code passes on exactly the cases its author already understood. Ask the
reviewer to name each test whose input has no stated source.
For a bug somebody REPORTED, the original input is their words AND what they saw: the page,
the count, the message. Paste the report verbatim into the brief, never only a triage's
restatement of it, and ask the reviewer which code path produces what they saw. Done when the
fixed path is the one that gives their count.

**Every prompt also asks: "What change is MISSING?"** For each
new parameter, field or flag the diff adds or a test passes in, does every production
caller actually send it? For each prompt, handler, schema or renderer the diff edits, how
many siblings does it have, and how many changed? For each value whose meaning the diff
changes (a limit, deadline, threshold or unit computed a new way), how many of its readers
outside the edited function use the new meaning? A diff cannot show a change that is not
in it, so ask the reviewer to search outside the diff and report "changed N of M".

**Every prompt carries the GOAL and asks for the real-use trigger.**
Paste the goal of this work and its definition of done into each prompt, and ask the
reviewer to open every failure scenario with how the case happens in real use: who does
what, with which input, in which setup. Where it cannot name one, it writes "no real-use
trigger found" and still reports the finding. Step 3a tags each finding from that line plus
its own search; a missing line is a reason to look, never proof that the case is rare. Do
not ask reviewers to leave rare cases out: a
rare security or data-loss case still blocks, and step 3a is where that is decided.

**A prompt for the review of a FIX commit covers that commit, not the whole branch again.**
Its scope is `git diff <the head the full round reviewed>..<the fix head>`, in place of step
1's branch diff, and the verdict binds to the fix head. It leaves the PLAN CHECK block out
(step 2a), because the full round already checked the plan
and a fix diff delivers only its fix. It asks two questions: did the fix close the finding at every site of its kind (the path
list step 5a puts in the fix commit message), and did it break something that worked. A new
finding in the fix's own lines is reported with its real-use line like any other, and step
3a recommends deferring it when it is `RARE`, the goal still holds, and it is outside the
protected class.
A fix that missed a sibling path did NOT close its
finding, so that is never a rare new case.

**Never ask a reviewer to walk a list. Ask it to sample the SITUATIONS.** Write the ask as
a grouping instruction rather than a count: "group the guard-shaped lines by the situation
each represents, check one of each, and report the groups plus how many instances you did
not individually check." The reviewer decides the grouping; you decide that grouping
happens. Applies to every enumerable thing you might hand a reviewer: files, call sites,
endpoints, fixtures, migrations.

**A cap you set silently is worse than a large ask**, so whatever you request, also require
the reviewer to state what it left unchecked. This does not soften step 4: a Critical is
still reproduced in full, and the reviewer still reads the whole diff.

