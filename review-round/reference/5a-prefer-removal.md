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

**When a fix ADDS an early exit at an entry point (a raise, a refusal, a return at the top of a
command, a route or a handler), list every EXISTING handler below it on the same path before
committing.** Grep the function it guards and that function's callees for `except`, `raise` and
any record or log write, and name each one the new exit now runs before. For each, say whether
the trigger the new exit catches still reaches it, and run one test of it THROUGH the entry point
with that trigger present: a test that calls the inner function directly passes with the handler
dead. A handler the new exit makes unreachable is kept reachable (record first, then raise) or
named as dead in the commit message, never left silent.

**When a fix changes WHAT a guard trusts, list every PRODUCER of the trusted value too.** A
fix that switches the evidence a check accepts ("only a state written as a two-letter code"),
or drops one copy of a repeated value, is safe only if nothing else can produce that value.
Grep for every writer of the field, column or cell the guard now reads (code that builds the
text, a merge that combines two rows, a fallback that copies one), and every entry of the
input list that can carry the same value. Done when the fix commit message names each
producer and says why it cannot forge the value, or the fix covers it.

**When a fix REMOVES or REPLACES a call (a normalizer, a conversion, a filter, a check), list
every OTHER reader of what that call produced before committing.** Grep the function for each
use of the value after the line you changed, and the callers for each use of its return value.
Each reader the finding is not about keeps the input it had: give it its own copy (the old
call, kept for it) rather than the new raw value.

**When a fix ADDS a filter, a mask or a check at one place a value lands, list every OTHER
reader of the value's SOURCE before committing.** The source is the file, field or column the
value comes from: grep its name (`summary.txt`, `run.message`), not the sink the finding named.
Each reader that shows, logs or keeps the value gets the same filter, or a written reason why it
does not need it. Put that reader list in the commit's Paths. Done when every reader of the
source is in the list.

**When a fix changes a SHARED DEFAULT or a TABLE ROW (a fallback, a catch-all branch, a default
value, one entry of a lookup table several callers read), list every INPUT that reaches it and
every READER of what it returns, before committing.** The finding names one input; the default
also decides every other input no specific row matches, and each reader may act on the answer
differently. Take the inputs from the source of truth, not from memory: the vendor's documented
error list, the enum, the fixture corpus. Run the old and the new code over all of them and print
each input whose answer changed. Every changed answer is either the fix or a new bug: give a
known input that should not move its own row. Then grep each caller of the changed function for
what it does with the answer. Done when the commit's Paths list the inputs that moved and each
reader of the answer, with why each moved answer is intended.

**When a fix would REPLACE A LIBRARY with your own code** (an HTTP client, a parser, a date or
encoding routine), keep the library and put the new check around it. A library carries
behaviour nobody listed (cookies across redirects, charset parsing, host normalisation,
bounded decompression), and a rewrite drops every item on that unwritten list at once. Done
when the library still does the work and the fix adds only the check. Where the finding
genuinely cannot be closed around the library, name each behaviour of the old call that your
code must keep, and test against the library itself as the oracle (step 6).

**When any fix means reading text (shell, SQL, a config grammar, or plain prose such as a sentence splitter, even for display only), allow the smallest shape and refuse the rest.** Before writing it, name the shapes real use needs, count them on real past input, and refuse everything else. Do not track state across the text: scopes, quotes, which branch ran. Counting quote marks to decide whether a quote is open IS tracking quote state: a parity count lets a word-end apostrophe in, and the safe rule is "any quote mark before the cut means no cut". Each review then finds the next piece of grammar, in the lines you just wrote. The tell is a new counter, a second pass over the tokens, or a list of keywords. When a review finds holes in such a reader, ask first what can be deleted.

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
