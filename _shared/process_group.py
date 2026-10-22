"""Owned process-group identity and a bounded stop. Standard library only.

    ident = identify(pid)            # {"pid", "pgid", "start"}, taken at launch
    result = stop_group(ident, grace=10, leader=popen_or_None)

IDENTITY. A process is named by its pid AND its exact start time as `ps -o
lstart` prints it (whole seconds). Before any signal:
  - leader alive with the recorded start time and still in the recorded group:
    it is ours;
  - leader alive with any other start time, or in another group: the number was
    reused, so REFUSE and send nothing;
  - leader gone: REFUSE. The group may have emptied and its number been
    reused by an unrelated session whose leader has also exited, so nothing
    proves the members are ours. A caller that
    passes its own unreaped leader never reaches this branch: a zombie leader
    still shows its start time.
A start time that cannot be read for a live leader is also a refusal: identity
not proved means nothing is signalled. The
caller's OWN process group is always refused: a child that failed to get its
own group would otherwise hand us our own group id, and stopping it would kill
the caller.

STOP. Descendants of the leader and their parent links are listed BEFORE the
TERM, so one that moved itself into another group can be recorded as escaped.
Escaped processes are never signalled. Then TERM to the group, up to `grace`
seconds, KILL, up to `kill_wait` seconds, and confirm the group is empty. When
the caller passes the leader's Popen it is reaped along the way, because on
macOS a group holding only an unreaped zombie answers killpg(pgid, 0) with
PermissionError, which means "present".

LIMITS. Whole-second start times leave a narrow window for a pid reused within
the same second. A descendant that calls setsid() leaves the group and is only
reported, never stopped.
"""

import os
import signal
import subprocess
import time

PS = "/bin/ps"
POLL = 0.05


class PsUnavailable(Exception):
    """`ps` could not be run, so nothing about a process can be proved."""


def proc_start(pid):
    """The process's start time as `ps -o lstart` prints it, or None when ps
    ran and printed nothing. Raises PsUnavailable when ps itself failed, so a
    broken ps can never read as "that process is gone"."""
    try:
        out = subprocess.run([PS, "-o", "lstart=", "-p", str(int(pid))],
                             capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise PsUnavailable(f"{PS} failed ({exc.__class__.__name__})")
    out = " ".join(out.split())
    return out or None


def pid_exists(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def identify(pid):
    """The identity recorded when a process is started."""
    try:
        pgid = os.getpgid(pid)
    except OSError:
        pgid = None
    try:
        start = proc_start(pid)
    except PsUnavailable:
        start = None
    return {"pid": pid, "pgid": pgid, "start": start}


def group_present(pgid):
    """True while the process group has any member, a zombie included."""
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # macOS: an unreaped zombie is still a member (gate M4).
        return True
    return True


def process_table():
    """{pid: (ppid, pgid, command)} for every process, or {} if ps fails."""
    try:
        out = subprocess.run([PS, "-axo", "pid=,ppid=,pgid=,command="],
                             capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    table = {}
    for line in out.splitlines():
        parts = line.split(None, 3)
        if len(parts) >= 3 and all(p.isdigit() for p in parts[:3]):
            table[int(parts[0])] = (int(parts[1]), int(parts[2]),
                                    parts[3] if len(parts) > 3 else "")
    return table


def descendants(pid, table):
    """Every process whose parent chain reaches `pid`, as a list of dicts."""
    children = {}
    for child, (ppid, _pgid, _cmd) in table.items():
        children.setdefault(ppid, []).append(child)
    found, todo = [], list(children.get(pid, []))
    while todo:
        child = todo.pop()
        ppid, pgid, cmd = table[child]
        found.append({"pid": child, "ppid": ppid, "pgid": pgid, "command": cmd[:200]})
        todo.extend(children.get(child, []))
    return found


def _reap(leader):
    if leader is not None:
        try:
            leader.poll()
        except OSError:
            pass


def _wait_empty(pgid, seconds, leader):
    end = time.monotonic() + seconds
    while True:
        _reap(leader)
        if not group_present(pgid):
            return True
        if time.monotonic() >= end:
            return False
        time.sleep(POLL)


def check_identity(ident):
    """(ok, reason). ok False means refuse and send nothing."""
    pid, pgid, start = ident.get("pid"), ident.get("pgid"), ident.get("start")
    if not (isinstance(pid, int) and isinstance(pgid, int) and pid > 0 and pgid > 0):
        return False, "identity incomplete: pid and pgid must be recorded"
    if pgid == os.getpgrp():
        return False, f"group {pgid} is the caller's own group"
    if not start:
        return False, f"no start time was recorded for pid {pid}"
    try:
        now = proc_start(pid)
    except PsUnavailable as exc:
        return False, f"cannot read the start time of pid {pid}: {exc}"
    if now is None:
        if pid_exists(pid):
            return False, f"pid {pid} exists but shows no start time"
        return False, f"leader {pid} is gone, so group {pgid} cannot be proved ours"
    if now != start:
        return False, f"pid {pid} was reused: started {now!r}, recorded {start!r}"
    try:
        if os.getpgid(pid) != pgid:
            return False, f"pid {pid} is no longer in group {pgid}"
    except ProcessLookupError:
        return False, f"leader {pid} is gone, so group {pgid} cannot be proved ours"
    except OSError as exc:
        return False, f"cannot read the group of pid {pid} ({exc.__class__.__name__})"
    return True, "leader alive, identity matches"


def stop_group(ident, grace=10.0, leader=None, kill_wait=5.0):
    """Stop the owned group per the module docstring. Returns a dict:
    outcome "stopped" | "refused" | "members-remain", plus reason,
    term_sent, kill_sent, escaped (list), and members_before (count)."""
    result = {"outcome": None, "reason": "", "term_sent": False, "kill_sent": False,
              "escaped": [], "members_before": 0}
    ok, reason = check_identity(ident)
    if not ok:
        result.update(outcome="refused", reason=reason)
        return result
    pgid = ident["pgid"]
    _reap(leader)
    table = process_table()
    result["members_before"] = sum(1 for _pp, g, _c in table.values() if g == pgid)
    result["escaped"] = [d for d in descendants(ident["pid"], table) if d["pgid"] != pgid]
    if not group_present(pgid):
        result.update(outcome="stopped", reason="group already empty")
        return result
    try:
        os.killpg(pgid, signal.SIGTERM)
        result["term_sent"] = True
    except ProcessLookupError:
        pass
    except PermissionError:
        pass  # only a zombie left; reaping below settles it
    if _wait_empty(pgid, grace, leader):
        result.update(outcome="stopped", reason="left after TERM")
        return result
    try:
        os.killpg(pgid, signal.SIGKILL)
        result["kill_sent"] = True
    except (ProcessLookupError, PermissionError):
        pass
    if _wait_empty(pgid, kill_wait, leader):
        result.update(outcome="stopped", reason="left after KILL")
    else:
        result.update(outcome="members-remain",
                      reason=f"group {pgid} still has members after KILL")
    return result
