**Text or code a fix ADDS is a first draft, and it gets the scrutiny a first draft gets.**
When a finding can be closed by deleting or narrowing (drop the exception, drop the "or"
alternative, tighten the condition), do that rather than writing something new. When the
fix has to add a list, an exception, a new alternative, a new equivalence ("X counts as
Y") or a new feature, name each added piece in the fix commit message and in the prompt
for the fix-commit review, as "new text, attack
this first".

**"Prefer removal" covers only what YOU added. It never deletes a piece the user picked.** When
the user chose the fix ("read only regular files, bounded read"), every part of that choice
stays, even when breaking one part turns no test red because another part covers the same
case. That is a gap in the TESTS, not a reason to delete: write a test that isolates the
part, by faking the one input only it judges (report the handle as a FIFO, return a short
read), and confirm breaking it now goes red.

**When the finding is a fail-closed check REFUSING something legitimate, the fix makes it
ACCEPT more, and that is the riskiest add there is.** Before writing it, name each piece of
evidence the new pass trusts and ask who ELSE can hold it, by reading its producer, never by
reasoning from this one case. A value the batch merely carries (an old id, a name, a hash, a
path) proves nothing unless nothing else can hold that value now. If you cannot show that,
leave the refusal in place: a stuck change costs a retry, a wrong pass destroys data.

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

**When the picked findings cluster in ONE flow, write its rules before fixing any of them.**
The tell is several findings in the same function or path, above all one that deletes, sends
or pays (three or more is a cluster). Fixing them one by one patches each case and leaves the
next one open. So first group them by the invariant each breaks ("the copy that stays is proven
right before the delete", "done needs a full list, never an absence", "the last Pause check
sits right before the first irreversible step"), write each invariant as ONE check at ONE place
in the code, and only then close the findings through those checks. Put the invariants in the
fix commit message and in the fix-commit review's prompt, so the reviewer attacks the rules
rather than the patches.

**When a finding names a CLASS rather than a line** ("an offer can act after something
invalidates it during the await"), the reviewer's example is ONE member, and the fix is not
done until every OTHER site of the same shape is found BY A COMMAND, never from memory: grep
the call that opens the window (`grep -n 'await _helper_' app/module.py`), not the helper the
example happened to use. Put the command, the list it printed and its count in the fix commit
message, and say for each site whether it is fixed or why not. A class with two spellings (a
shared helper, and a site that calls the lower function itself) is where the second is missed.
