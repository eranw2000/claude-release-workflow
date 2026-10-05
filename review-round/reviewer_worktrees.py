#!/usr/bin/env python3
"""Give each file-changing reviewer its own copy of the CODE repository, and
prove afterwards that the real checkout was not touched.

Why not the Agent tool's `isolation: "worktree"`: it copies the repository the
SESSION was launched in. A session launched from a project notes folder under
~/.claude gets a private copy of ~/.claude, while the code under review stays
shared, and every reviewer still reports that isolation was applied (measured
2026-08-25 on a real project). So the copies are made here, in the repository the
diff actually lives in.

    setup --repo <code repo> --agents code-reviewer,pr-validator [--base <dir>]
          [--with <ignored path>]...
        Records the real checkout (every changed, untracked and ignored file,
        with a content hash and its staged blob, submodules included), then
        makes one detached worktree per agent and carries the uncommitted
        changes and untracked files into each, submodules included. Each
        `--with` path (an IGNORED path the suite needs: a build output, a data
        folder) is copied into every copy as well, following a symlink, cloned
        copy-on-write on macOS, so a reviewer's write cannot reach the original.
        Prints:
            <agent> <worktree path>
            pythonpath <agent> <value>   put this in PYTHONPATH, so the copy's
                                         code wins over an editable install
                                         that points at the real checkout
            interpreter <path>           only when the repo has a .venv
            not-carried <path> ...       ignored top-level paths the copies
                                         lack; any the suite reads belongs in
                                         a `--with`
            state <path>
        The state file is written before the first copy and after every copy,
        so a setup that fails halfway can still be cleaned up with `check`.

    check --state <base>/state.json
        Reports every file in the real checkout whose status, content or
        staged content changed since setup (a reviewer's leftover). Ignored
        DIRECTORIES (.venv, node_modules) are watched for appearing or
        vanishing only, not for changes inside. Then removes the copies, but
        only paths inside the state file's own folder that git lists as a
        detached worktree of the recorded repository AND whose git admin
        folder holds the random token setup wrote there.
        Then lists anything else left in the state file's folder (a reviewer
        that wrote OUTSIDE its copy) as STRAY lines.
        Exit 0 clean, 1 leftovers found, 4 the checkout is clean but strays
        were written beside the copies, 3 clean but a copy could not be
        removed, 2 usage or git error. It never reverts or deletes anything: a
        leftover or a stray is reported, not discarded.
"""

import argparse
import hashlib
import json
import os
import hmac
import pathlib
import secrets
import shutil
import subprocess
import sys
import tempfile

BIG_FILE = 64 * 1024 * 1024  # above this, compare size and mtime instead of hashing

# A random token written into each copy's OWN git admin folder (`.git/worktrees/<name>/`)
# the moment the copy exists, and kept in the state file. `check` removes a copy only when
# that file holds the same token, so an edited or stale state file cannot point the removal
# at the real checkout or at a worktree some other run made: neither carries this token.
TOKEN_FILE = "review-round-token"


def git(repo, *args, stdin=None):
    r = subprocess.run(["git", "-C", str(repo), *args], input=stdin,
                       capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.decode(errors='replace').strip()}")
    return r.stdout


def zsplit(out):
    return [p for p in out.decode(errors="surrogateescape").split("\0") if p]


def file_hash(path):
    p = pathlib.Path(path)
    if p.is_symlink():
        return "link:" + os.readlink(p)
    if not p.exists():
        return "absent"
    if p.is_dir():
        return "dir"
    st = p.stat()
    if st.st_size > BIG_FILE:
        return f"size:{st.st_size} mtime:{st.st_mtime_ns}"
    return hashlib.sha256(p.read_bytes()).hexdigest()


def index_blobs(repo):
    """{path: (mode, blob)} from the index."""
    blobs = {}
    for e in zsplit(git(repo, "ls-files", "-s", "-z")):
        meta, path = e.split("\t", 1)
        mode, blob, _stage = meta.split(" ")
        blobs[path] = (mode, blob)
    return blobs


def submodules(repo, blobs=None):
    """Checked-out submodule paths (gitlinks whose folder holds a .git)."""
    blobs = index_blobs(repo) if blobs is None else blobs
    return [p for p, (mode, _) in blobs.items()
            if mode == "160000" and (pathlib.Path(repo) / p / ".git").exists()]


def snapshot(repo, prefix=""):
    """{path: description} for every changed, untracked or ignored file,
    recursing into checked-out submodules."""
    repo = pathlib.Path(repo)
    blobs = index_blobs(repo)
    snap = {}
    entries = zsplit(git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all"))
    i = 0
    while i < len(entries):
        e = entries[i]
        i += 1
        code, path = e[:2], e[3:]
        if "R" in code or "C" in code:  # the next entry is the old path, not a record
            i += 1
        idx = blobs.get(path, ("-", "-"))[1]
        snap[prefix + path] = f"{code} {file_hash(repo / path)} idx:{idx}"
    for path in zsplit(git(repo, "ls-files", "-z", "--others", "--ignored",
                           "--exclude-standard", "--directory")):
        if path.endswith("/"):
            snap[prefix + path] = "ignored-dir"
        else:
            snap[prefix + path] = "ignored " + file_hash(repo / path)
    for sub in submodules(repo, blobs):
        snap.update(snapshot(repo / sub, f"{prefix}{sub}/"))
    return snap


def admin_dir(worktree):
    """The worktree's own git admin folder, absolute."""
    return pathlib.Path(git(worktree, "rev-parse", "--path-format=absolute",
                            "--git-dir").decode().strip())


def made_by_this_setup(path, token):
    """True only when `path`'s git admin folder holds `token`. The caller has already
    checked that `path` is a detached worktree of the recorded repository."""
    if not token:
        raise RuntimeError("the state file carries no token; not a copy this setup made")
    mark = admin_dir(path) / TOKEN_FILE
    if not mark.is_file() or not hmac.compare_digest(mark.read_text(), token):
        raise RuntimeError(f"{path} does not carry this setup's token; not a copy it made")
    return True


def make_copy(repo, dest, rev, agent, state, save):
    """Worktree of `repo` at `rev` in `dest`, plus its uncommitted and untracked
    files, then the same for every checked-out submodule inside it."""
    repo = pathlib.Path(repo)
    git(repo, "worktree", "add", "--detach", str(dest), rev)
    (admin_dir(dest) / TOKEN_FILE).write_text(state["token"])
    state["copies"].append({"agent": agent, "repo": str(repo), "path": str(dest)})
    save()
    patch = git(repo, "diff", "HEAD", "--binary", "--ignore-submodules=all")
    if patch:
        git(dest, "apply", "--binary", "--whitespace=nowarn", stdin=patch)
    for rel in zsplit(git(repo, "ls-files", "--others", "--exclude-standard", "-z")):
        dst = dest / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(repo / rel, dst, follow_symlinks=False)
    for sub in submodules(repo):
        sub_head = git(repo / sub, "rev-parse", "HEAD").decode().strip()
        make_copy(repo / sub, dest / sub, sub_head, agent, state, save)


# Ignored entries never worth naming in the not-carried line: tooling caches and the
# interpreter, which a copy reaches through the printed `interpreter` line instead.
NOT_WORTH_NAMING = {".venv/", "venv/", "node_modules/", "__pycache__/", ".pytest_cache/",
                    ".mypy_cache/", ".ruff_cache/", ".idea/", ".DS_Store"}


def carried_paths(repo, names):
    """The `--with` paths, each checked to be an IGNORED path inside `repo` that exists.

    A tracked path is already in every copy, and a path outside the repository is not
    this round's to copy, so both are refused before any copy is made: `git
    check-ignore` answers non-zero for each, which is the one check that covers both."""
    out = []
    for name in names:
        rel = pathlib.PurePosixPath(name.strip().rstrip("/"))
        if not name.strip():
            raise RuntimeError("--with: empty path")
        if not os.path.lexists(repo / rel):
            raise RuntimeError(f"--with {name}: no such path in {repo}")
        ignored = subprocess.run(["git", "-C", str(repo), "check-ignore", "-q", str(rel)],
                                 capture_output=True).returncode == 0
        if not ignored:
            raise RuntimeError(f"--with {name}: not an ignored path (tracked or untracked "
                               "files are carried already)")
        out.append(str(rel))
    return out


def carry(repo, dest, rel):
    """A PRIVATE copy of the ignored path `rel` in the reviewer copy `dest`.

    A symlink is followed, so a folder reached through a link to another checkout
    arrives as a real folder and a reviewer's write cannot land in the original. On
    macOS `cp -c` clones (copy-on-write), so a large data folder costs no disk."""
    src, dst = (repo / rel).resolve(), dest / rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == "darwin" and subprocess.run(
            ["cp", "-cR", str(src), str(dst)], capture_output=True).returncode == 0:
        return
    if dst.exists() or dst.is_symlink():  # a failed clone may leave a partial copy
        shutil.rmtree(dst) if dst.is_dir() and not dst.is_symlink() else dst.unlink()
    if src.is_dir():
        shutil.copytree(src, dst, symlinks=True)
    else:
        shutil.copy2(src, dst)


def setup(args):
    repo = pathlib.Path(git(args.repo, "rev-parse", "--show-toplevel").decode().strip())
    agents = [a.strip() for a in args.agents.split(",") if a.strip()]
    if not agents:
        raise RuntimeError("no agents named")
    carried = carried_paths(repo, args.with_ or [])
    base = pathlib.Path(args.base or tempfile.mkdtemp(prefix="review-round-")).resolve()
    if base == repo or repo in base.parents:
        raise RuntimeError(f"--base {base} is inside the repository {repo}")
    base.mkdir(parents=True, exist_ok=True)
    state_path = base / "state.json"

    head = git(repo, "rev-parse", "HEAD").decode().strip()
    state = {"repo": str(repo), "head": head, "snapshot": snapshot(repo), "copies": [],
             "agents": agents, "token": secrets.token_hex(16)}

    def save():
        state_path.write_text(json.dumps(state, indent=2))

    save()
    try:
        for agent in agents:
            make_copy(repo, base / agent, head, agent, state, save)
            for rel in carried:
                carry(repo, base / agent, rel)
    except Exception:
        print(f"state {state_path}")
        print(f"setup failed partway; remove what it made with: check --state {state_path}",
              file=sys.stderr)
        raise

    venv_python = repo / ".venv" / "bin" / "python"
    for agent in agents:
        wt = base / agent
        print(f"{agent} {wt}")
        paths = ([str(wt / "src")] if (wt / "src").is_dir() else []) + [str(wt)]
        print(f"pythonpath {agent} {os.pathsep.join(paths)}")
    if venv_python.exists():
        print(f"interpreter {venv_python}")
    # The ignored top-level paths the copies do NOT have. A suite that needs one (a
    # build output, a data folder) fails in every copy for reasons the diff did not
        # cause, and certifies nothing.
    left_out = [p for p in zsplit(git(repo, "ls-files", "-z", "--others", "--ignored",
                                      "--exclude-standard", "--directory"))
                if "/" not in p.rstrip("/") and p not in NOT_WORTH_NAMING
                and p.rstrip("/") not in carried]
    if left_out:
        print("not-carried " + " ".join(left_out))
    print(f"state {state_path}")
    return 0


def detached_worktrees(repo):
    """Resolved paths git lists as DETACHED linked worktrees of `repo`."""
    found, cur = set(), None
    for line in git(repo, "worktree", "list", "--porcelain").decode().splitlines():
        if line.startswith("worktree "):
            cur = line[len("worktree "):]
        elif line == "detached" and cur:
            found.add(str(pathlib.Path(cur).resolve()))
    return found


def check(args):
    state_path = pathlib.Path(args.state).resolve()
    base = state_path.parent
    state = json.loads(state_path.read_text())
    repo = state["repo"]
    before, after = state["snapshot"], snapshot(repo)
    changed = sorted(p for p in set(before) | set(after) if before.get(p) != after.get(p))
    for p in changed:
        print(f"LEFTOVER {p}: before={before.get(p, 'clean')} after={after.get(p, 'clean')}")

    stuck = 0
    for copy in reversed(state["copies"]):  # submodule copies first
        path = pathlib.Path(copy["path"]).resolve()
        try:
            if base not in path.parents:
                raise RuntimeError(f"{path} is outside {base}; not a copy this setup made")
            if str(path) not in detached_worktrees(copy["repo"]):
                raise RuntimeError(f"{path} is not a detached worktree of {copy['repo']}")
            made_by_this_setup(path, state.get("token"))
            git(copy["repo"], "worktree", "remove", "--force", str(path))
        except RuntimeError as e:
            stuck += 1
            print(f"NOT REMOVED {copy['agent']}: {e}", file=sys.stderr)
    for r in {c["repo"] for c in state["copies"]}:
        git(r, "worktree", "prune")

    # Anything else in the round's own folder was written by a reviewer OUTSIDE its copy
        # (logs, scripts, a copied config file), which the checkout comparison above
        # cannot see. Named, never deleted.
    # An agent's own slot is never a stray, even when it is still there: a copy that could
    # not be removed (reported above) or one a failed setup half-made.
    left = {str(pathlib.Path(c["path"]).resolve()) for c in state["copies"]}
    slots = set(state.get("agents") or [c["agent"] for c in state["copies"]])
    strays = sorted(p for p in base.iterdir()
                    if p != state_path and p.name not in slots
                    and str(p.resolve()) not in left)
    for p in strays:
        print(f"STRAY {p}{'/' if p.is_dir() else ''}: written beside the copies, not removed")

    if changed:
        print(f"check: {len(changed)} file(s) in {repo} changed during the round")
        return 1
    if strays:
        print(f"check: {repo} unchanged, but {len(strays)} file(s) were written outside "
              f"the copies in {base}")
        return 4
    if stuck:
        print(f"check: {repo} unchanged, but {stuck} copy(ies) were not removed")
        return 3
    print(f"check: {repo} unchanged during the round")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("setup")
    s.add_argument("--repo", required=True)
    s.add_argument("--agents", required=True)
    s.add_argument("--base")
    s.add_argument("--with", dest="with_", action="append", metavar="PATH",
                   help="an ignored path the suite needs (repeatable)")
    c = sub.add_parser("check")
    c.add_argument("--state", required=True)
    args = ap.parse_args(argv)
    try:
        return setup(args) if args.cmd == "setup" else check(args)
    except (RuntimeError, OSError, ValueError, KeyError) as e:
        print(f"ERROR {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
