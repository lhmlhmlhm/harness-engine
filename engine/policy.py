"""Policy — where a flow is MANDATORY on this machine.

WHAT THIS ADDRESSES

The guard enforces the gates INSIDE a run. It cannot enforce that a run exists: with nothing
open there is no scope to compare an action against, so `guard-tool` allows — and the engine
ships that admission as one of its own judgment rules, because prose asking an agent to open a
run does not bind it.

This supplies the one thing that was missing. An entry says "in THIS scope, THAT flow is
mandatory", which gives the guard a scope to compare against when no run has claimed one. The
comparison is the same function a run uses; only the scope key comes from a declaration instead
of from a row.

WHAT IT PREVENTS, AND WHAT IT DOES NOT

  * It prevents OMISSION. "The driver forgot to open a run" stops being invisible and becomes a
    refusal. That is the common case.
  * It does NOT prevent SUBVERSION. Anything with a shell can edit this file. That is the rare
    case, and the difference still matters: an omission leaves no trace, while deleting an entry
    is an overt change to a file its owner can diff and keep in version control.

A fail-closed mechanism its subject can switch off would be theatre if it were sold as
enforcement. Sold as what it is — the difference between forgetting and deciding — it is worth
having.

WHY A FILE AND NOT A TABLE

This is POLICY, not ledger. It has to be readable when the store is not, it should be editable
and reviewable by hand, and it must not need a migration to change shape. Same reasoning, and
same directory, as the record of approved extension code.

WHY AN ENTRY DOES NOT STORE THE SCOPE KIND

The flow declares its own scope kind, so storing a second copy would be a copy that can age —
and an entry whose kind disagreed with the flow's would match nothing, forever, in silence. It
is derived at the point of use from the flow that is being loaded anyway.
"""
from __future__ import annotations

import os
from pathlib import Path

from . import store

POLICY_FILE = "required-flows"
POLICY_ENV = "HARNESS_REQUIRED_FLOWS"


def path() -> Path:
    override = os.environ.get(POLICY_ENV, "").strip()
    return Path(override).expanduser() if override else store.state_dir() / POLICY_FILE


def read() -> tuple[dict, ...]:
    """Declared requirements. A missing or unreadable file means NOTHING is required.

    Absence is the empty policy on purpose: this file's whole job is to turn allowing into
    refusing, so a file that cannot be read must leave behaviour exactly as it was rather than
    start refusing on a guess.
    """
    try:
        text = path().read_text(encoding="utf-8")
    except OSError:
        return ()
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("\t") if p.strip() != ""]
        if len(parts) < 2:
            continue
        out.append({"ability": parts[0], "scope_key": parts[1],
                    "strict": len(parts) > 2 and parts[2].lower() == "strict"})
    return tuple(out)


def _write(entries) -> None:
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# harness-engine — where a flow is MANDATORY on this machine.",
        "# One line per requirement: <ability>\\t<scope key>\\t[strict]",
        "#",
        "# With no entries the guard behaves exactly as it did before this file existed: it",
        "# enforces the gates inside an open run and allows everything when nothing is open.",
        "#",
        "# `strict` changes only what happens when the named flow cannot be READ (its spec is",
        "# invalid, or its extension code is not approved here): by default the guard says so",
        "# loudly and allows, because one unreadable file must not stop all work in a scope.",
        "# With `strict` it refuses instead — for a scope where you would rather stop than",
        "# proceed unchecked.",
        "#",
        "# Edit or delete freely: it is your policy, and the engine only reads it.",
    ]
    for e in sorted(entries, key=lambda x: (x["ability"], x["scope_key"])):
        row = [e["ability"], e["scope_key"]]
        if e.get("strict"):
            row.append("strict")
        lines.append("\t".join(row))
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")


def add(ability: str, scope_key: str, *, strict: bool = False) -> bool:
    """Record a requirement. False if an identical one was already there."""
    entries = [e for e in read()]
    for e in entries:
        if e["ability"] == ability and e["scope_key"] == scope_key:
            if e["strict"] == strict:
                return False
            e["strict"] = strict
            _write(entries)
            return True
    entries.append({"ability": ability, "scope_key": scope_key, "strict": strict})
    _write(entries)
    return True


def remove(ability: str, scope_key: str) -> bool:
    entries = [e for e in read()
               if not (e["ability"] == ability and e["scope_key"] == scope_key)]
    if len(entries) == len(read()):
        return False
    _write(entries)
    return True
