"""Worktree isolation facts — what is TRUE of the directory a run works in.

Borrowed, not run. This ability exists to own one question — "is this work really isolated, and
does its worktree still exist" — so that any ship-like flow can ask it with `requires: [worktree]`
instead of copying a git-and-filesystem probe it would then have to keep correct.

READ-ONLY BY CONSTRUCTION. Nothing here provisions or tears down. Provisioning creates branches,
checkouts and a copied build skeleton; teardown runs guarded deletes. The agent runs the tool in
`tools/`, and this code independently asks the world what happened — which is the only arrangement
in which "I isolated the work" can be contradicted.

WHY IT IS A SEPARATE ABILITY. The knowledge here is about git and directories, not about shipping:
it is the part that must stay correct when the lifecycle around it changes, and the part that has
to be portable when the whole tree moves to another machine. The lifecycle STEPS that consult it
(declare isolation, hand back the tree, tear it down) stay with the flow that owns the lifecycle —
they are four points in a ship, not a lifecycle of their own.
"""
import re
import subprocess
from pathlib import Path

from engine import facts, operators


@facts.provider("worktree_state",
                requires=({"cmd": "git"},),
                schema={
    # What the PLAN asked for, read from the plan doc this run recorded. Without it, "I am
    # deliberately working in the shared tree" would be the one claim nothing could contradict.
    "isolation_declared": operators.T_BOOL,
    "plan_doc_readable": operators.T_BOOL,
    # WHICH directory the answers below are about. Under isolation the run does not work in
    # its scope — it works in a worktree whose path is unknowable until the provisioning tool has
    # run, so the run records it and this provider reads it. Reported as a fact rather than
    # assumed, because "no row, so I looked at the scope" and "a row that equals the scope" must
    # not be the same answer.
    "worktree_path_recorded": operators.T_BOOL,
    # What is actually TRUE of the directory this run WORKS IN. A linked worktree's `.git`
    # is a file pointing at the owning repo; a source checkout's is a directory.
    "in_linked_worktree": operators.T_BOOL,
    "on_isolation_branch": operators.T_BOOL,
    "worktree_dirty": operators.T_BOOL,
    # Whether THIS run's own worktree still exists — asked of the path the run RECORDED, so
    # teardown is observed directly and another session's worktree says nothing about this one.
    # It used to be matched by `run_id[:8]` against `refs/heads/shipcheck/*`, which quietly
    # required this engine's run id and the other engine's session UUID to share a prefix; the
    # implementation below no longer does that, and this comment said otherwise until now.
    "own_worktree_exists": operators.T_BOOL,
})
def _worktree_state(ctx: dict) -> dict:
    """Whether this run is really isolated, derived with read-only git — never by provisioning.

    THIS IS THE SHAPE FOR AN EFFECT. Provisioning a worktree creates branches, checkouts and a
    copied build skeleton; tearing one down runs guarded deletes. The engine performs none of
    it. The agent runs the tool, and the engine independently asks the world what is true —
    which is the only arrangement in which "I isolated the work" can be contradicted.

    The reference implementation states the failure mode this closes, and it is worth quoting
    because it is why a criterion here is not decoration: provisioning "FAILS HARD on error
    rather than falling back to the shared tree. A silent fallback would hand back exactly the
    shared-working-tree behaviour the caller asked to be isolated FROM, while reporting
    success." A recorded claim of isolation with no check is that same fallback, one layer up.
    """
    from engine import store
    empty = {"isolation_declared": False, "plan_doc_readable": False,
             "worktree_path_recorded": False,
             "in_linked_worktree": False, "on_isolation_branch": False,
             "worktree_dirty": False, "own_worktree_exists": False}
    run_id = str(ctx.get("run_id") or "")
    conn = store.connect(read_only=True)
    try:
        docs = store.find_evidence(conn, run_id, None, "plan_doc")
        # Run-wide (scope=None) on purpose: the path is recorded by whichever step provisioned the
        # worktree, and this provider must not care which one that was.
        wt_rows = store.find_evidence(conn, run_id, None, "worktree_path")
    finally:
        conn.close()
    got = dict(empty)

    # What was asked for. The plan doc path comes from the run's own evidence, so the answer
    # stays derived from what the run said rather than from a second place that could disagree.
    for row in reversed(docs):
        cand = Path(str(row["value"]).strip()).expanduser()
        if cand.is_file():
            try:
                head = cand.read_text(encoding="utf-8", errors="replace")[:4000]
            except OSError:
                break
            got["plan_doc_readable"] = True
            m = re.search(r"^worktree_isolation:\s*(\S+)", head, re.M)
            got["isolation_declared"] = bool(m) and m.group(1).lower() in ("true", "yes", "on")
            break

    # What is actually true — OF THE DIRECTORY THIS RUN WORKS IN.
    root = Path(str(ctx.get("scope") or ".")).expanduser()
    recorded: Path | None = None
    for row in reversed(wt_rows):
        got["worktree_path_recorded"] = True
        cand = Path(str(row["value"]).strip()).expanduser()
        recorded = cand
        if cand.is_dir():
            root = cand
        break
    if not root.is_dir():
        return got
    dotgit = root / ".git"
    got["in_linked_worktree"] = dotgit.is_file()

    def git(*args: str) -> str:
        try:
            r = subprocess.run(["git", "-C", str(root), *args],
                               capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.SubprocessError):
            return ""
        return r.stdout if r.returncode == 0 else ""

    branch = git("rev-parse", "--abbrev-ref", "HEAD").strip()
    got["on_isolation_branch"] = branch.startswith("shipcheck/")
    got["worktree_dirty"] = bool(git("status", "--porcelain").strip())
    # "Does THIS run's own worktree still exist" — asked of the path the run recorded, so teardown
    # is observed directly. The previous form matched `refs/heads/shipcheck/*` against `run_id[:8]`,
    # which required this engine's run id and the other engine's session UUID to share a prefix;
    # that held by arrangement, and nothing would have reported it if it stopped holding.
    got["own_worktree_exists"] = bool(
        recorded is not None and recorded.is_dir() and (recorded / ".git").is_file())
    return got
