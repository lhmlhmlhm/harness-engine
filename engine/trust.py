"""Trust — an extension file is CODE, and this records that you accepted these bytes.

THE PROBLEM, STATED WITHOUT SOFTENING

A flow may ship a `providers.py`, and loading that flow IMPORTS it. So merely READING a spec
— validating it, asking what step is next — executes code from whoever wrote the flow, in the
engine's process, with the engine's privileges. That is not a defect to be patched; it is the
price of the extension seam, and the seam is what keeps domain knowledge out of the base.

WHAT THIS MODULE DOES NOT DO, SO IT CANNOT BE MISREAD AS PROTECTION

  * It is NOT a sandbox. There is no sandbox here, and an in-process one written in this
    language would be a claim that cannot be kept — the escapes are numerous and well known,
    so shipping one would trade a visible risk for an invisible one.
  * Approving is NOT a safety verdict. It records a decision you made about a specific file's
    contents. An approved file runs with everything the engine can reach.
  * The import list it reports is ADVISORY. It is read from the syntax tree, so a file that
    assembles an import name at runtime does not appear in it. It is there to inform a human
    review, never to decide anything.

WHAT IT DOES DO, WHICH IS FALSIFIABLE

It pins content. An extension from outside this engine's own tree must be approved once, by
digest, and a later change to that file asks again instead of running. So "someone updated an
ability I installed and its code changed" stops being silent — which is the one thing in this
whole area that a record kept by the engine can actually establish.

WHY AN ABILITY INSIDE THE ENGINE'S OWN TREE IS NOT PINNED

Pinning it would be theatre. Anyone who can write `<tree>/abilities/x/providers.py` can write
`<tree>/engine/facts.py`, and no record the engine keeps survives an editor of the engine. A
trust boundary exists exactly where the code comes from somewhere the engine's own code does
not — so that is where the check is, and nowhere else. Installed as a package, the engine's
tree holds no abilities at all, and every flow is therefore external and pinned.
"""
from __future__ import annotations

import ast
import hashlib
import os
import time
from pathlib import Path

from . import store

# The installation tree that contains this file's package. Everything under it shares one write
# domain with the engine's own sources — see the module docstring.
ENGINE_TREE = Path(__file__).resolve().parent.parent

TRUST_FILE = "trusted-extensions"
DISABLE_ENV = "HARNESS_TRUST_FILE"

STATE_INTERNAL = "internal"      # same tree as the engine; not pinned, by design
STATE_APPROVED = "approved"
STATE_UNKNOWN = "unknown"
STATE_CHANGED = "changed"


class TrustError(RuntimeError):
    """An extension file may not be imported. Carries the whole explanation."""


def is_internal(path: Path) -> bool:
    """Is this file inside the engine's own installation tree?"""
    try:
        path.resolve().relative_to(ENGINE_TREE)
        return True
    except ValueError:
        return False


def digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def declared_imports(path: Path) -> list[str]:
    """Top-level module names this file imports, read from the syntax tree.

    ADVISORY. A file that builds a name at runtime will not appear here, and this function
    makes no attempt to find one — a partial detector presented as a check is worse than an
    honest list, because the reader stops looking.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, OSError):
        return []
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    return sorted(names)


def trust_path() -> Path:
    """Where approvals are recorded. A FILE, not a table.

    Flow loading never touches the store — an invalid spec exits without opening it — and a
    check that runs during loading must hold to the same rule, or reading a spec would start
    requiring an initialised database.
    """
    override = os.environ.get(DISABLE_ENV, "").strip()
    return Path(override).expanduser() if override else store.state_dir() / TRUST_FILE


def read() -> dict[str, dict]:
    """ability -> {digest, path, at}. A missing or unreadable file means nothing is approved."""
    p = trust_path()
    out: dict[str, dict] = {}
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        dg, ability, where = parts[0], parts[1], parts[2]
        out[ability] = {"digest": dg, "path": where,
                        "at": parts[3] if len(parts) > 3 else ""}
    return out


def _write(records: dict[str, dict]) -> None:
    p = trust_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# harness-engine — extension files you approved on THIS machine.",
        "# One line per ability: <digest>\\t<ability>\\t<path>\\t<approved at>",
        "# Approving is not a safety verdict; it records that you accepted these bytes, so a",
        "# later change to the file asks again instead of running. Edit or delete freely — it",
        "# is your record, and the engine only ever reads it.",
    ]
    for ability in sorted(records):
        r = records[ability]
        lines.append(f"{r['digest']}\t{ability}\t{r['path']}\t{r.get('at', '')}")
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")


def approve(ability: str, path: Path) -> str:
    dg = digest(path)
    records = read()
    records[ability] = {"digest": dg, "path": str(path.resolve()),
                        "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    _write(records)
    return dg


def forget(ability: str) -> bool:
    records = read()
    if ability not in records:
        return False
    del records[ability]
    _write(records)
    return True


def state(ability: str, path: Path) -> tuple[str, dict]:
    """Classify without raising, for reporting. Returns (state, detail)."""
    if is_internal(path):
        return STATE_INTERNAL, {"digest": digest(path)}
    dg = digest(path)
    rec = read().get(ability)
    if rec is None:
        return STATE_UNKNOWN, {"digest": dg}
    if rec["digest"] != dg:
        return STATE_CHANGED, {"digest": dg, "approved": rec["digest"], "at": rec.get("at", "")}
    return STATE_APPROVED, {"digest": dg, "at": rec.get("at", "")}


_NOT_A_SANDBOX = (
    "  WHAT APPROVAL IS AND IS NOT. It records that you accepted THESE BYTES, so a later\n"
    "  change asks again instead of running. It is NOT a sandbox and NOT a safety verdict:\n"
    "  an approved file runs with everything this engine can reach. The import list above is\n"
    "  read from the syntax tree — a file that assembles an import name at runtime will not\n"
    "  appear in it, and nothing here tries to find one."
)


def check(ability: str, path: Path) -> None:
    """Raise TrustError unless this file may be imported."""
    st, detail = state(ability, path)
    if st in (STATE_INTERNAL, STATE_APPROVED):
        return
    imports = ", ".join(declared_imports(path)) or "(none)"
    if st == STATE_CHANGED:
        raise TrustError(
            f"{path}\n"
            f"  changed after you approved it. That is the case this record exists for.\n"
            f"  approved{' ' + detail['at'] if detail.get('at') else ''}: {detail['approved']}\n"
            f"  on disk now:            {detail['digest']}\n"
            f"  imports (advisory):     {imports}\n"
            f"  Loading this flow IMPORTS the file, so reading its spec would run the change.\n"
            f"  Review the diff, then:  harness trust {ability}\n"
            f"  Or drop the record and stop loading it: harness trust --forget {ability}\n"
            + _NOT_A_SANDBOX
        )
    raise TrustError(
        f"{path}\n"
        f"  is code from outside this engine's own tree and has not been approved here.\n"
        f"  digest:             {detail['digest']}\n"
        f"  imports (advisory): {imports}\n"
        f"  Loading this flow IMPORTS the file, so reading its spec would run it. An ability\n"
        f"  with no providers.py is purely declarative and needs no approval at all.\n"
        f"  Review it, then:    harness trust {ability}\n"
        + _NOT_A_SANDBOX
    )
