"""Registry — the shape all four extension points share, and who owns a name.

The engine has exactly four registries (completion predicates, fact providers, condition
operators, gate witnesses) and they are deliberately identical in form: a name, a callable, and a
refusal if the name is taken. What lives here is ownership and resolution, because those are the
parts that were being got wrong in four separate copies.

WHAT WAS WRONG: ONE FLAT NAMESPACE FOR EVERYONE

The registries used to be keyed by the bare name, process-globally. Two independent flows, from
two authors who have never met, each registering `review_comments` would break each other — and
load order is alphabetical, so which one failed was decided by spelling. Naming both sides in the
message made the diagnosis honest, but the collision itself remained: the base could not host two
flows that had independently picked one obvious word.

WHAT REPLACES IT

A registration is owned by the ability whose file made it, and the key is `<ability>.<name>`.
Engine registrations are owned by nobody and keep the bare name. So:

  * `alpha.open_findings` and `beta.open_findings` now coexist. The collision that could not be
    removed is removed, because it was never one name — it was two.
  * A collision is still possible INSIDE one ability, and that one is a real defect: one author,
    one file, one name twice. The message says so, instead of blaming a stranger.
  * ENGINE NAMES ARE RESERVED. An ability may not claim `all_of` or `static`. Shadowing would let
    a flow silently redefine what a word means for its own steps while the same word in the next
    flow still meant the engine's — the failure would be invisible in both specs.

HOW A SPEC REFERS TO ONE

A bare name means MINE, or the ENGINE's — those two are disjoint, so it is unambiguous:

    facts: {provider: open_findings}      # mine, or the engine's

Borrowing another ability's registration must SAY SO, and the ability must be declared in
`requires:`:

    requires: [alpha]
    facts: {providers: [alpha.open_findings]}

The qualified form is not decoration. Under the old flat namespace, a bare `open_findings`
resolved in a spec that never mentioned `alpha` — it worked because something else had happened
to load first, which is a dependency that works by luck. Writing the owner makes the dependency
visible at the point of use, and the engine can then check it against `requires:` instead of
hoping.
"""
from __future__ import annotations

import inspect
from pathlib import Path
from typing import Callable

SEP = "."

# The ability whose providers.py is being imported RIGHT NOW, or None for the engine's own
# registrations. Set by flow.load_extensions around the import and cleared after, so the window
# is exactly the import. Registration is a decorator that runs at import time — the same moment
# the file is being read — which is why an ambient owner is enough and no author has to repeat
# their own ability name in every decorator.
_LOADING: str | None = None


def loading(ability: str | None) -> None:
    global _LOADING
    _LOADING = ability


def current_owner() -> str | None:
    return _LOADING


def qualify(owner: str | None, name: str) -> str:
    """The registry key. Engine registrations keep the bare name."""
    return name if owner is None else f"{owner}{SEP}{name}"


def split(ref: str) -> tuple[str | None, str]:
    """A reference as (ability or None, name). `a.b` → ('a', 'b'); `b` → (None, 'b')."""
    if SEP in ref:
        head, _, tail = ref.partition(SEP)
        return head, tail
    return None, ref


def visible(taken: dict, owner: str | None) -> list[str]:
    """The names a spec owned by `owner` may write bare — the engine's plus its own.

    Deliberately NOT everything registered. Listing a stranger's names as "registered" is what
    invited a spec to reference one, and the reference then worked or not depending on whether
    that stranger's flow happened to be loaded in the same process.
    """
    out = [k for k in taken if SEP not in k]
    if owner is not None:
        pre = f"{owner}{SEP}"
        out += [k[len(pre):] for k in taken if k.startswith(pre)]
    return sorted(out)


def _where(fn: Callable) -> str:
    """The file that registered this name, for the collision message.

    Best-effort on purpose: a callable built at runtime has no source file, and failing to
    register something because its origin could not be described would be absurd.
    """
    try:
        return str(Path(inspect.getfile(fn)).resolve())
    except (TypeError, OSError):
        return "<unknown origin>"


def claim(kind: str, name: str, taken: dict, owners: dict, fn: Callable) -> str:
    """Settle ownership of `name` and return the KEY to store it under.

    THE AUTHORITY IS `taken` — THE REGISTRY ITSELF — AND NOT THE OWNER MAP.
    A second dictionary that must be kept in sync with the first is a bug waiting to happen, and
    it happened immediately: cleanup code that removed a name from the registry left it in the
    owner map, and the next registration of that name was refused as a collision with nobody.
    So the question "is this key taken" is still answered where it always was, and the owner map
    is advisory. Out of sync, it degrades a message; it cannot invent a conflict.
    """
    if SEP in name:
        raise RuntimeError(
            f"{kind} name '{name}' contains '{SEP}', which separates an ability from a name.\n"
            f"  A registration names only itself; its owner comes from the file it lives in."
        )
    owner = current_owner()
    here = _where(fn)
    key = qualify(owner, name)

    # Engine names are reserved. Checked BEFORE the collision below, so an ability shadowing a
    # builtin is told that it is a reserved word rather than that it collided with itself.
    if owner is not None and name in taken:
        raise RuntimeError(
            f"{kind} '{name}' is a name the ENGINE already defines, and an ability may not\n"
            f"  redefine it:\n"
            f"  engine:  {owners.get(name, '<unrecorded origin>')}\n"
            f"  ability: {here}\n"
            f"  Shadowing it would make one word mean your version inside '{owner}' and the\n"
            f"  engine's version in every other flow, with nothing in either spec to show it.\n"
            f"  Pick a different name; yours does not have to avoid other ABILITIES, only the\n"
            f"  engine — '{owner}{SEP}{name}' would have been free if the engine did not hold it."
        )
    if key in taken:
        prior = owners.get(key, "<unrecorded origin>")
        if owner is None:
            raise RuntimeError(
                f"{kind} '{name}' is claimed twice by the ENGINE itself:\n"
                f"  already: {prior}\n"
                f"  also:    {here}\n"
                f"  Both are engine files, so this is a defect in the engine, not a collision\n"
                f"  between extensions."
            )
        raise RuntimeError(
            f"{kind} '{name}' is claimed twice by ability '{owner}':\n"
            f"  already: {prior}\n"
            f"  also:    {here}\n"
            f"  Registrations are namespaced per ability, so this is NOT a collision with some\n"
            f"  other flow — it is one ability registering one name twice. Rename one of them."
        )
    owners[key] = here
    return key


class ResolveError(RuntimeError):
    """A reference could not be resolved to exactly one registration."""


def resolve(kind: str, ref: str, taken: dict, *, asking: str | None,
            requires: tuple[str, ...] = ()) -> str:
    """Turn a spec's reference into a registry key, or say precisely why it cannot be.

    Resolution is deliberately NOT a search. Bare means mine-or-the-engine; anything else must be
    written with its owner. A resolver that fell back to "whoever else has this name" would
    reintroduce exactly the dependency-by-load-order that namespacing removes.
    """
    dep, name = split(ref)

    if dep is None:
        own = qualify(asking, name)
        if asking is not None and own in taken:
            return own
        if name in taken:
            return name
        # Someone else has it. Say who, because "not registered" would be false and would send
        # the author looking for a typo in a name that exists.
        holders = sorted(k.split(SEP)[0] for k in taken
                         if SEP in k and k.split(SEP, 1)[1] == name)
        if holders:
            raise ResolveError(
                f"{kind} '{name}' is not yours and not the engine's — it belongs to "
                f"{' and '.join(repr(h) for h in holders)}.\n"
                f"  Borrowing must be written with the owner, and the owner must be declared:\n"
                f"    requires: [{holders[0]}]\n"
                f"    ... {holders[0]}{SEP}{name}\n"
                f"  A bare name used to resolve to whichever flow happened to load first, which\n"
                f"  is a dependency that works by luck."
            )
        raise ResolveError(
            f"{kind} '{name}' is not registered.\n"
            f"  available here: {', '.join(visible(taken, asking)) or '(none)'}\n"
            f"  If it belongs to another ability, a bare name will never reach it: write\n"
            f"  '<ability>{SEP}{name}' and declare that ability in `requires:`. (Whether this\n"
            f"  refusal can NAME the owner depends on whether that ability is loaded in this\n"
            f"  process, so it does not always say — but the fix is the same either way.)"
        )

    # Qualified.
    if dep == asking:
        raise ResolveError(
            f"{kind} '{ref}' names this ability's own registration. Write it bare: '{name}'.\n"
            f"  The qualified form is for BORROWING, so keeping it for your own names would\n"
            f"  make an ability look like a dependency of itself."
        )
    if name in taken and dep not in requires:
        raise ResolveError(
            f"{kind} '{ref}' qualifies '{name}', which the ENGINE defines — it has no owning\n"
            f"  ability. Write it bare: '{name}'."
        )
    if dep not in requires:
        raise ResolveError(
            f"{kind} '{ref}' borrows from ability '{dep}', which this flow does not declare.\n"
            f"  Add it: requires: [{dep}]\n"
            f"  Naming the owner is not the same as declaring the dependency — the declaration\n"
            f"  is what makes the other ability's registrations load at all."
        )
    key = qualify(dep, name)
    if key not in taken:
        mine = sorted(k[len(dep) + 1:] for k in taken if k.startswith(f"{dep}{SEP}"))
        raise ResolveError(
            f"{kind} '{ref}': ability '{dep}' is declared, but registers no {kind} '{name}'.\n"
            f"  it registers: {', '.join(mine) or '(none)'}"
        )
    return key
