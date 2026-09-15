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

TWO PLACES A REQUIREMENT CAN COME FROM, AND WHY THE SECOND ONE EXISTS

The record below is machine-local and its entries carry ABSOLUTE paths, which is the right shape
for a scope that is not a directory — a review, a sprint — and the wrong shape for everything
else. It cannot travel: another machine spells the same checkout differently, a colleague's clone
is somewhere else, and a renamed parent directory turns an entry into one that matches nothing,
forever, in silence.

So a DIRECTORY may declare it too, by holding a `.harness-required` file naming the abilities that
are mandatory for work beneath it. That file has no paths in it at all — the scope IS the directory
holding it — so it means the same thing on every machine, and committing it to a repository makes
the requirement travel with the code and show up in review when it changes.

An earlier round rejected this with "a repository is writable, so an agent could edit it". That
reasoning was wrong, and worth recording as wrong: the record below is writable by exactly the same
agent. Writability never distinguished the two, because this mechanism does not claim to prevent
subversion in either place. What actually differs is the opposite of the original fear — a deleted
line in a committed file is visible in a diff, and a deleted line here is visible to nobody.

NEAREST DECLARATION WINS, and an empty marker is therefore a deliberate local exemption. A parent
directory's requirement is the machine owner's; a repository's own marker is its authors'. When
both speak, the more specific one is the one that knows what it is talking about.

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

# The in-tree form. One name, so a repository that commits one and a machine that drops one in a
# parent directory are using the same mechanism rather than two that can diverge.
MARKER = ".harness-required"

# A declaration is a handful of lines. Reading is capped so that a marker which is a symlink to
# something endless cannot hang the guard that runs before every matching tool call.
MARKER_MAX_BYTES = 65536


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
                    "strict": len(parts) > 2 and parts[2].lower() == "strict",
                    # Where it came from, because after directory discovery "what is required
                    # here" has two possible answers and a reader has to be able to tell which.
                    "source": str(path())})
    return tuple(out)


def discover(start: str) -> tuple[tuple[dict, ...], tuple[str, ...]]:
    """Requirements declared by the directory tree containing `start`.

    Returns (entries, problems). Problems are returned rather than printed because this is a
    library and the CLI owns stderr — but they are returned rather than swallowed because a
    marker that EXISTS and cannot be read is not the same as no marker, and those two must not
    look alike. Entries come back in the shape `read()` produces, so nothing downstream needs to
    know which source it is looking at.

    THE WALK STARTS AT `start` AND GOES UP, AND STOPS AT THE FIRST MARKER. It does not begin from
    a path named inside the tool call, and that bound is deliberate: locating a marker from the
    payload would need a rule for which substrings are paths, which is guesswork this engine
    declines elsewhere for the same reason. The consequence is worth stating rather than
    discovering — a call issued from OUTSIDE the tree, naming a target inside it, finds no marker.
    The machine-local record does cover that case, because its entry names the directory outright.
    """
    problems: list[str] = []
    try:
        here = Path(start).expanduser().resolve()
    except (OSError, ValueError, TypeError):
        return (), ()
    for d in (here, *here.parents):
        f = d / MARKER
        try:
            if not f.is_file():
                continue
            with f.open("r", encoding="utf-8", errors="replace") as fh:
                text = fh.read(MARKER_MAX_BYTES)
        except OSError as exc:
            problems.append(f"{f}: cannot be read ({type(exc).__name__}), so what it declares "
                            f"could not be applied")
            return (), tuple(problems)
        out = []
        for lineno, raw in enumerate(text.splitlines(), 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            extra = [p for p in parts[1:] if p.lower() != "strict"]
            if extra:
                # The machine-local format written into a marker by mistake. Taking parts[0] and
                # ignoring the rest would silently apply a requirement to a directory the writer
                # did not name — so the line is reported and skipped instead.
                problems.append(f"{f}:{lineno}: a marker's scope is the directory holding it, so "
                                f"{' '.join(extra)!r} on this line names nothing. Line skipped.")
                continue
            out.append({"ability": parts[0], "scope_key": str(d),
                        "strict": any(p.lower() == "strict" for p in parts[1:]),
                        "source": str(f)})
        # Nearest wins, INCLUDING when it declares nothing: an empty marker is how a directory
        # says "not here" about a requirement its parent declared, and that has to be sayable
        # locally or the only way to opt out is editing somebody else's committed file.
        return tuple(out), tuple(problems)
    return (), tuple(problems)


def _render_entry(e) -> str:
    row = [e["ability"], e["scope_key"]]
    if e.get("strict"):
        row.append("strict")
    return "\t".join(row)


HEADER = (
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
)


def _write(entries) -> None:
    """Rewrite the record IN PLACE, keeping every comment and blank line already in the file.

    It used to render the file from `entries` plus a fixed header, which deleted any comment its
    reader had written — in a file whose own header says "edit or delete freely". A note saying WHY
    a scope is mandatory is exactly what someone writes there, and losing it on the next `--add`
    teaches people not to touch the file they were told is theirs. Nothing reported the loss,
    because `read()` never looked at comments in the first place.

    A surviving entry keeps its ORIGINAL text, so a key typed as `~/x` is not silently rewritten to
    an absolute path, and a comment sitting above an entry stays above it. New entries are appended.
    A removed entry takes only its own line: a comment that explained it is left where it is rather
    than guessed at, because deciding that a comment "belonged to" a line is exactly the guess that
    would delete the wrong one.

    Two deliberate consequences. Entries are no longer sorted — position now carries meaning that
    sorting would break — so the file's order is the order things were declared. And the header is
    only written for a file that does not exist yet: re-adding it to a file where someone deleted it
    would be this function overruling the same edit it now exists to preserve.
    """
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    wanted = {(e["ability"], e["scope_key"]): e for e in entries}

    try:
        existing = p.read_text(encoding="utf-8").splitlines()
    except OSError:
        existing = None

    if existing is None:
        lines = [*HEADER, *(_render_entry(e) for e in entries)]
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return

    lines: list[str] = []
    seen: set = set()
    for line in existing:
        bare = line.strip()
        if not bare or bare.startswith("#"):
            lines.append(line)
            continue
        parts = [x.strip() for x in bare.split("\t") if x.strip() != ""]
        if len(parts) < 2:
            # Not an entry as `read()` parses it, so it is not this function's to interpret —
            # and certainly not its to delete.
            lines.append(line)
            continue
        key = (parts[0], parts[1])
        if key not in wanted or key in seen:
            continue                      # removed, or a duplicate of a line already kept
        seen.add(key)
        e = wanted[key]
        was_strict = len(parts) > 2 and parts[2].lower() == "strict"
        # Only touch the line when the flag actually changed; otherwise the author's own spelling
        # (tilde form, spacing) survives.
        lines.append(line if was_strict == bool(e.get("strict")) else _render_entry(e))

    for key, e in wanted.items():
        if key not in seen:
            lines.append(_render_entry(e))

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
