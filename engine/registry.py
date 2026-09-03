"""Registry — the shape all four extension points share.

The engine has exactly four registries (completion predicates, fact providers, condition
operators, gate witnesses) and they are deliberately identical in form: a name, a callable, and a
refusal if the name is taken. What lives here is the refusal, because it is the one part that was
getting it wrong in four separate copies.

WHAT IT WAS GETTING WRONG

The registries are process-global. Two independent flows, from two authors who have never met,
each registering under the same name would break each other — and the old message named only the
SECOND one, so it read as a defect in whichever happened to load later. Load order is
alphabetical and neither author controls it, so the accusation was arbitrary as well as wrong.

Naming both sides does not remove the collision; nothing here can, short of namespacing
registrations per flow, which would change how a spec refers to them. It removes the wrong
diagnosis, which is the part that sends someone looking in the wrong file.
"""
from __future__ import annotations

import inspect
from pathlib import Path
from typing import Callable


def _where(fn: Callable) -> str:
    """The file that registered this name, for the collision message.

    Best-effort on purpose: a callable built at runtime has no source file, and failing to
    register something because its origin could not be described would be absurd.
    """
    try:
        return str(Path(inspect.getfile(fn)).resolve())
    except (TypeError, OSError):
        return "<unknown origin>"


def claim(kind: str, name: str, taken: dict, owners: dict, fn: Callable) -> None:
    """Refuse a second claim on `name`, naming BOTH sides. Record who claimed it.

    THE AUTHORITY IS `taken` — THE REGISTRY ITSELF — AND NOT THE OWNER MAP.
    A second dictionary that must be kept in sync with the first is a bug waiting to happen, and
    it happened immediately: cleanup code that removed a name from the registry left it in the
    owner map, and the next registration of that name was refused as a collision with nobody.
    So the question "is this name taken" is still answered where it always was, and the owner map
    is advisory. Out of sync, it degrades a message; it can no longer invent a conflict.
    """
    here = _where(fn)
    if name in taken:
        prior = owners.get(name, "<unrecorded origin>")
        raise RuntimeError(
            f"{kind} '{name}' is claimed twice:\n"
            f"  already: {prior}\n"
            f"  also:    {here}\n"
            f"  This is a NAME COLLISION between two independent extensions, not a defect in\n"
            f"  either of them. The registries are process-global, so whichever loads second\n"
            f"  fails — and load order is alphabetical, which neither author chose. Rename one,\n"
            f"  or keep the two out of the same process."
        )
    owners[name] = here
