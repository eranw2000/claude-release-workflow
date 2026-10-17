#!/usr/bin/env python3
"""Release a STACKED set of PRs one squash at a time without losing the review.

Why this exists: when /release squash-merges the base PR of a stack, the squash cuts
the commit link, so the next PR (built on the base's ORIGINAL commits) conflicts with
main on the base's own lines, and it still targets the base's branch.
The fix was done by hand: replay only the next PR's own commits onto the new main,
prove the code is byte-identical to what was reviewed, re-point a .gitleaksignore
fingerprint the rebase invalidated, re-post the verdict. This script is that procedure,
so it cannot be skipped or half done.

    restack.py preview --repo R --base B --head H
        Would merging H into B produce exactly H's tree?
        0 CLEAN (merged tree == H's tree), 1 CONFLICTS, 3 MERGES BUT DIFFERS
        (B holds something H lacks), 2 CANNOT TELL.

    restack.py replay --repo R --old-base OB --new-base NB --reviewed REV
        Run in R with the PR branch checked out at REV. Replays OB..HEAD onto NB and proves
        the result. Refuses (3) unless NB's tree equals OB's tree, because only then is the
        replay content-neutral and the old verdict still describes the code.
        0 TREE EQUAL to REV (or equal except re-pointed .gitleaksignore fingerprints, which
        are committed and named), 1 the rebase or the rescan failed, 3 the new base is not
        the old base's content, or the replayed tree differs from REV in code,
        2 CANNOT TELL (dirty checkout, unknown commit).

    restack.py merge-ours --repo R --old-base OB --new-base NB --reviewed REV
        For a stack that was MERGED UP rather than rebased (a lower branch's fixes merged
        up into each branch above, so replay's rebase drops the merge commit that resolved
        the conflicts and stops on the first commit).
        Run with the PR branch checked out at REV. Requires OB to be an ancestor of
        REV and NB's tree to equal OB's, so NB brings nothing REV lacks; then records NB as
        merged with `git merge -s ours`, which keeps REV's tree exactly. No commit is
        rewritten, so the push is a fast-forward.
        0 TREE EQUAL to REV and a preview into NB is CLEAN, 1 the merge failed, 3 OB is not
        in REV, NB is not OB's content, or the result is not REV's tree, 2 CANNOT TELL.

Python 3.9 compatible. Never pushes; the caller pushes (replay with --force-with-lease;
merge-ours needs no force).
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile

IGNORE = ".gitleaksignore"


def git(repo, *args, check=True):
    p = subprocess.run(["git", "-C", repo] + list(args), capture_output=True, text=True)
    if check and p.returncode != 0:
        raise RuntimeError("git %s failed: %s" % (" ".join(args), p.stderr.strip()))
    return p


def tree(repo, ref):
    return git(repo, "rev-parse", ref + "^{tree}").stdout.strip()


def preview(repo, base, head):
    p = git(repo, "merge-tree", "--write-tree", base, head, check=False)
    merged = (p.stdout.splitlines() or [""])[0].strip()
    if p.returncode == 1:
        print("CONFLICTS: merging %s into %s conflicts" % (head[:8], base[:8]))
        for line in p.stdout.splitlines():
            if line.startswith("CONFLICT"):
                print("  " + line)
        return 1
    if p.returncode != 0:
        print("CANNOT TELL: git merge-tree exited %d: %s" % (p.returncode, p.stderr.strip()))
        return 2
    if merged == tree(repo, head):
        print("CLEAN: the merge result is exactly %s's tree" % head[:8])
        return 0
    print("MERGES BUT DIFFERS: %s holds changes %s lacks; the reviewed head is not what "
          "would ship" % (base[:8], head[:8]))
    return 3


def repoint_fingerprints(lines, findings):
    """Re-point ignore entries whose file:rule:line matches a finding under a new commit.

    lines: the .gitleaksignore lines. findings: fingerprints `<sha>:<file>:<rule>:<line>`
    reported now. An entry is re-pointed only when its tail matches a finding EXACTLY and
    its sha differs, so an unrelated secret is never silenced. Returns (new_lines, moved).
    """
    tails = {}
    for f in findings:
        sha, _, tail = f.partition(":")
        if tail:
            tails.setdefault(tail, sha)
    out, moved = [], []
    for line in lines:
        s = line.strip()
        sha, _, tail = s.partition(":")
        if s and not s.startswith("#") and tail in tails and tails[tail] != sha:
            out.append(line.replace(sha, tails[tail], 1))
            moved.append("%s -> %s (%s)" % (sha[:8], tails[tail][:8], tail))
        else:
            out.append(line)
    return out, moved


def gitleaks_findings(repo, rng):
    if not shutil.which("gitleaks"):
        return None
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    try:
        subprocess.run(["gitleaks", "git", "--no-banner", "--log-opts=" + rng,
                        "--report-format", "json", "--report-path", path, repo],
                       capture_output=True, text=True)
        with open(path) as fh:
            data = fh.read().strip()
        return [f["Fingerprint"] for f in (json.loads(data) if data else [])]
    finally:
        os.unlink(path)


def replay(repo, old_base, new_base, reviewed):
    try:
        head = git(repo, "rev-parse", "HEAD").stdout.strip()
        rev = git(repo, "rev-parse", reviewed).stdout.strip()
        if git(repo, "status", "--porcelain", "--untracked-files=no").stdout.strip():
            print("CANNOT TELL: the checkout has uncommitted tracked changes")
            return 2
        if head != rev:
            print("CANNOT TELL: HEAD %s is not the reviewed head %s" % (head[:8], rev[:8]))
            return 2
        if tree(repo, new_base) != tree(repo, old_base):
            print("REFUSED: the new base %s does not hold the old base %s's code, so a "
                  "replay changes what was reviewed. Re-review instead." % (new_base[:8],
                                                                          old_base[:8]))
            return 3
    except RuntimeError as exc:
        print("CANNOT TELL: %s" % exc)
        return 2
    p = git(repo, "rebase", "--onto", new_base, old_base, check=False)
    if p.returncode != 0:
        git(repo, "rebase", "--abort", check=False)
        print("FAILED: the rebase stopped and was aborted: %s" % p.stderr.strip()[:300])
        return 1
    new_head = git(repo, "rev-parse", "HEAD").stdout.strip()
    note = ""
    ignore = os.path.join(repo, IGNORE)
    if os.path.exists(ignore):
        found = gitleaks_findings(repo, "%s..HEAD" % new_base)
        if found is None:
            print("NOTE: gitleaks is not installed; %s not re-checked" % IGNORE)
        elif found:
            with open(ignore) as fh:
                lines = fh.read().split("\n")
            new_lines, moved = repoint_fingerprints(lines, found)
            if moved:
                with open(ignore, "w") as fh:
                    fh.write("\n".join(new_lines))
                git(repo, "add", IGNORE)
                git(repo, "commit", "-q", "-m",
                    "gitleaks: re-point fingerprints after replaying onto %s" % new_base[:8])
                new_head = git(repo, "rev-parse", "HEAD").stdout.strip()
                note = " except %s: %s" % (IGNORE, "; ".join(moved))
            if gitleaks_findings(repo, "%s..HEAD" % new_base):
                print("FAILED: gitleaks still reports findings after re-pointing; inspect "
                      "them before pushing")
                return 1
    diff = git(repo, "diff", "--name-only", rev, new_head).stdout.split()
    if not diff or (note and diff == [IGNORE]):
        print("TREE EQUAL%s: %s (replayed) matches the reviewed %s" % (note, new_head,
                                                                       rev[:8]))
        return 0
    print("TREE DIFFERS: %s differs from the reviewed %s in: %s" % (new_head[:8], rev[:8],
                                                                    ", ".join(diff)))
    return 3


def merge_ours(repo, old_base, new_base, reviewed):
    try:
        head = git(repo, "rev-parse", "HEAD").stdout.strip()
        rev = git(repo, "rev-parse", reviewed).stdout.strip()
        if git(repo, "status", "--porcelain", "--untracked-files=no").stdout.strip():
            print("CANNOT TELL: the checkout has uncommitted tracked changes")
            return 2
        if head != rev:
            print("CANNOT TELL: HEAD %s is not the reviewed head %s" % (head[:8], rev[:8]))
            return 2
        if git(repo, "merge-base", "--is-ancestor", old_base, rev, check=False).returncode:
            print("REFUSED: the old base %s is not in the reviewed head %s, so the branch was "
                  "never merged up; use replay." % (old_base[:8], rev[:8]))
            return 3
        if tree(repo, new_base) != tree(repo, old_base):
            print("REFUSED: the new base %s does not hold exactly the old base %s's code, so "
                  "an ours-merge would hide what it adds. Re-review instead." % (new_base[:8],
                                                                                 old_base[:8]))
            return 3
    except RuntimeError as exc:
        print("CANNOT TELL: %s" % exc)
        return 2
    p = git(repo, "merge", "-q", "-s", "ours", "--no-edit", "-m",
            "Merge %s into the stacked branch; tree kept as reviewed at %s"
            % (new_base[:8], rev[:8]), new_base, check=False)
    if p.returncode != 0:
        git(repo, "merge", "--abort", check=False)
        print("FAILED: the ours-merge stopped and was aborted: %s" % p.stderr.strip()[:300])
        return 1
    new_head = git(repo, "rev-parse", "HEAD").stdout.strip()
    if tree(repo, new_head) != tree(repo, rev):
        print("TREE DIFFERS: %s is not the reviewed tree of %s" % (new_head[:8], rev[:8]))
        return 3
    if preview(repo, new_base, new_head) != 0:
        return 3
    print("TREE EQUAL: %s (ours-merge of %s) matches the reviewed %s" % (new_head,
                                                                      new_base[:8], rev[:8]))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("preview")
    p.add_argument("--repo", required=True)
    p.add_argument("--base", required=True)
    p.add_argument("--head", required=True)
    r = sub.add_parser("replay")
    r.add_argument("--repo", required=True)
    r.add_argument("--old-base", required=True)
    r.add_argument("--new-base", required=True)
    r.add_argument("--reviewed", required=True)
    m = sub.add_parser("merge-ours")
    m.add_argument("--repo", required=True)
    m.add_argument("--old-base", required=True)
    m.add_argument("--new-base", required=True)
    m.add_argument("--reviewed", required=True)
    a = ap.parse_args(argv)
    try:
        if a.cmd == "preview":
            return preview(a.repo, a.base, a.head)
        if a.cmd == "merge-ours":
            return merge_ours(a.repo, a.old_base, a.new_base, a.reviewed)
        return replay(a.repo, a.old_base, a.new_base, a.reviewed)
    except RuntimeError as exc:
        print("CANNOT TELL: %s" % exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
