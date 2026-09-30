**Text or code a fix ADDS is a first draft, and it gets the scrutiny a first draft gets.**
When a finding can be closed by deleting or narrowing (drop the exception, drop the "or"
alternative, tighten the condition), do that rather than writing something new. When the
fix has to add a list, an exception, a new alternative, a new equivalence ("X counts as
Y") or a new feature, name each added piece in the fix commit message and in the prompt
for the fix-commit review, as "new text, attack
this first".

Measured twice on one day, 2026-09-18, on `secure-dev-guardrails`. PR #7: the round-1 fix
added private-registry awareness and that addition carried 4 of round 2's findings,
including printing index URLs with their logins. PR #9: the fix closed 10 of 13 wording
findings, and all 4 new Majors sat in text it had added (a closed list of "acts" that
missed restarting a service, a test-system exception, "a limit" accepted as the check,
a version pin called an integrity check). Every fix that deleted or narrowed held.

**When a finding names a MECHANISM, the fix closes the mechanism, not the line.** A
mechanism is how a bad result reaches its consumer: "an error lets the caller walk on", "a
cache in another process holds the old value". Before committing, list every path by which
the changed function can reach that consumer: each return, an exception from EVERY call the
fix adds, and every write on each of those paths. Put the list in the fix commit message.
