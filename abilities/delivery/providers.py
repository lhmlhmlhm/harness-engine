"""Ability-supplied fact providers for `delivery`.

WHY THIS FILE EXISTS AT ALL — IT IS A CORRECTION.

`git_tree` used to be a BUILT-IN provider, defined in `engine/facts.py`, shelling out to a
specific version-control tool. Its own comment gave the reason: "what did I touch" is the most
common thing a condition asks, so it shipped as a convenience.

That was domain knowledge living in the base, and it was the base's own seam that it bypassed.
The engine already has a mechanism for a provider that needs an external tool — a provider
declares `requires={"cmd": ...}` and the engine reports the capability as absent rather than
letting a missing tool look like an empty answer. Shipping the tool-dependent provider inside
the engine skipped the very thing that mechanism is for.

It also survived the purity guard, which is the part worth remembering. The guard exempts
`facts.py` from the no-fact-names rule, for the sound reason that declaring a schema is a
provider's whole job — but the exemption silently covered more than that, and a tool name is not
a fact name. Two guards now close it: the engine's built-in providers may not shell out at all,
and the tool's name is banned in the sharp forms.

Nothing about the provider changed. It moved.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from engine import facts, operators  # noqa: E402


@facts.provider("git_tree", requires=({"cmd": "git"},), schema={
    "changed_files": operators.T_LIST,
    "vcs_branch": operators.T_STR,
    "dirty": operators.T_BOOL,
    "change_count": operators.T_INT,
})
def _git_tree(ctx: dict) -> dict:
    """Uncommitted paths in the run's scope, treated as a directory.

    `requires` is declared now that this lives where it belongs: with the tool absent, the engine
    marks every fact from this provider unavailable and a condition touching one REFUSES, instead
    of reading "no changes" off a machine that simply has no such command. That distinction —
    "nothing changed" versus "I could not look" — is the whole reason the capability layer exists,
    and as a built-in this provider had no way to state it.

    Degrading to empty when the scope is not a repository is different and stays: absence of
    changes is a real, correct answer there, whereas a provider that ERRORS must raise.
    """
    root = Path(str(ctx.get("scope") or ".")).expanduser()
    empty = {"changed_files": [], "vcs_branch": "", "dirty": False, "change_count": 0}
    if not root.is_dir():
        return dict(empty)

    def run(*args: str) -> str:
        try:
            r = subprocess.run(
                ["git", "-C", str(root), *args],
                capture_output=True, text=True, timeout=15,
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        return r.stdout if r.returncode == 0 else ""

    if run("rev-parse", "--is-inside-work-tree").strip() != "true":
        return dict(empty)

    paths: list[str] = []
    for line in run("status", "--porcelain").splitlines():
        if len(line) > 3:
            p = line[3:].strip()
            if " -> " in p:      # a rename reports old -> new; the new path is the subject
                p = p.split(" -> ", 1)[1]
            paths.append(p.strip('"'))
    return {
        "changed_files": sorted(set(paths)),
        "vcs_branch": run("rev-parse", "--abbrev-ref", "HEAD").strip(),
        "dirty": bool(paths),
        "change_count": len(set(paths)),
    }
