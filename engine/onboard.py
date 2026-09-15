"""What an agent is allowed to reach for, declared as data and CHECKED.

WHY THIS EXISTS AS A CHECKED FILE RATHER THAN A DOCUMENT. An agent needs two things before it can
drive this engine: how to drive it at all, and which of the installed flows are ITS business. The
first is generated (`harness brief`) and therefore cannot drift. The second used to be a hand-written
document, and the drift was measured: the document opened by warning that a hand-written list of
"what is installed and when to use it" expires the moment another one is installed — and then, three
sections later, contained exactly such a list.

So the split here is by WHO KNOWS:

    the engine knows        what is installed, each one's `when:`, each one's role, its dependencies
                            → already generated, never written down twice
    only the owner knows    which subset THIS agent may reach for, which of them hand off to each
                            other across separate runs, and which pairs are easy to confuse
                            → this file

The second list is small, it is specific to one person's setup, and none of it is derivable. That is
the test for whether something belongs here: **if the engine could derive it, writing it down is a
copy that will rot.**

WHY IT IS VALIDATED, and not merely a documented convention. A schema nothing checks costs a reader
exactly what a prose document costs: they must guess what the keys mean and they never find out
whether they got it right. The check is what converts a convention into a contract — every name is
resolved against what is actually installed, and the one failure mode this file has, quietly not
mentioning something that IS installed, is reported rather than left to be discovered by an agent
that never reaches for it.
"""
from __future__ import annotations

import os
from pathlib import Path

import yaml

from engine import flow as flowmod

# A SIBLING OF AN ABILITIES ROOT, so there is nothing to configure. Whoever has a set of abilities at
# `<somewhere>/abilities` has their agent definitions at `<somewhere>/agents`, and both roots move
# together when the directory is copied to another machine. An absolute path in a config file is the
# thing that does not survive that copy.
AGENTS_DIR_NAME = "agents"
# Overridable for the reason every location in this engine is: a test must be able to point it at a
# scratch directory rather than at whatever the developer happens to have installed.
AGENTS_ENV = "HARNESS_AGENTS_PATH"

SUFFIXES = (".yaml", ".yml")

# Closed key sets, refused rather than ignored — same posture as a flow spec, and for the same
# reason: a key that is silently dropped reads as though it took effect.
TOP_KEYS = {"agent", "title", "abilities", "chains", "confusable", "tools"}
CHAIN_KEYS = {"id", "hops", "carried_by", "note"}
CONFUSABLE_KEYS = {"pair", "distinction", "cost"}
TOOL_KEYS = {"name", "reach", "note"}


class OnboardError(Exception):
    """A definition that cannot be read at all, as opposed to one that reads badly."""


def agents_roots() -> list[Path]:
    """Where to look, resolved at CALL time rather than at import.

    Same rule as the abilities roots, for the same measured reason: a caller that sets the variable
    after importing this module would otherwise be frozen to the value at import, and that failure
    is silent — it reads as "that directory holds nothing" rather than "you configured it too late".
    """
    raw = os.environ.get(AGENTS_ENV, "").strip()
    if raw:
        roots = [Path(p).expanduser() for p in raw.split(os.pathsep) if p.strip()]
        if not roots:
            raise OnboardError(f"{AGENTS_ENV} is set but names no directory: {raw!r}")
        return roots
    # Derived from the abilities roots, deduplicated in order. Deriving rather than defaulting to one
    # fixed place is what makes a second root (somebody's own set beside the engine's) work without
    # naming it twice.
    out: list[Path] = []
    for root in flowmod.abilities_roots():
        candidate = root.parent / AGENTS_DIR_NAME
        if candidate not in out:
            out.append(candidate)
    return out


def installed() -> dict[str, Path]:
    """Map agent name -> definition file, scanning every root in order.

    A name present in two roots is REFUSED rather than resolved first-wins. First-wins lets a root
    nobody is looking at shadow the one being edited, and "which of the two is in force" stops being
    answerable from the files — which is the question this whole file exists to answer.
    """
    found: dict[str, Path] = {}
    for root in agents_roots():
        if not root.is_dir():
            continue
        for path in sorted(root.iterdir()):
            if path.suffix not in SUFFIXES or not path.is_file():
                continue
            name = path.stem
            if name in found:
                raise OnboardError(
                    f"two roots define the agent '{name}':\n"
                    f"  {found[name]}\n  {path}\n"
                    f"  The engine will not pick one — remove one, or narrow {AGENTS_ENV}.")
            found[name] = path
    return found


def _reject_unknown(got, allowed: set, where: str, path: Path) -> list[str]:
    if not isinstance(got, dict):
        return [f"{path}: {where} is {type(got).__name__}, not a mapping"]
    unknown = sorted(set(map(str, got)) - allowed)
    if not unknown:
        return []
    return [f"{path}: {where} has unsupported key(s): {', '.join(unknown)}\n"
            f"    supported: {', '.join(sorted(allowed))}\n"
            f"    A typo here would silently narrow what this agent may reach for, so it is "
            f"fatal rather than ignored."]


def check(path: Path) -> tuple[dict, list[str], list[str]]:
    """Read one definition and say what is wrong with it.

    Returns `(parsed, problems, unmentioned)`. `problems` are reasons the file is wrong. `unmentioned`
    is separate and NOT a problem: an agent legitimately reaches for a subset. It is reported anyway
    because it is this file's only silent failure mode — an installed flow nobody bound to an agent
    is a flow that never gets driven, and nothing else in the system would ever say so.
    """
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise OnboardError(f"{path}: cannot be read: {exc}") from None
    if raw is None:
        raise OnboardError(f"{path}: is empty")
    problems = _reject_unknown(raw, TOP_KEYS, "the definition", path)
    if problems:
        return ({}, problems, [])

    name = str(raw.get("agent") or "").strip()
    if not name:
        problems.append(f"{path}: names no `agent`")
    elif name != path.stem:
        # A file named one thing and declaring another is a rename that was done in one place. The
        # engine finds definitions BY FILENAME, so the declared name is the one that would be wrong
        # everywhere it is quoted, while the file kept working.
        problems.append(
            f"{path}: declares `agent: {name}` but the file is named '{path.stem}'. The engine "
            f"finds a definition by its filename, so these must agree.")

    names = raw.get("abilities")
    if not isinstance(names, list) or not names:
        problems.append(
            f"{path}: `abilities` must be a non-empty list — an agent bound to nothing has no "
            f"reason to exist, and an empty list reads as 'everything' to a human.")
        names = []
    names = [str(x).strip() for x in names]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        problems.append(f"{path}: `abilities` lists the same name twice: {', '.join(dupes)}")

    try:
        available = set(flowmod.available_abilities())
    except flowmod.FlowError as exc:
        raise OnboardError(f"the installed set cannot be read, so nothing here can be "
                           f"resolved against it: {exc}") from None

    bound_production: set[str] = set()
    for n in names:
        if n not in available:
            problems.append(
                f"{path}: binds '{n}', which is not installed.\n"
                f"    installed: {', '.join(sorted(available)) or '(none)'}")
            continue
        try:
            f = flowmod.load(n)
        except flowmod.FlowError as exc:
            problems.append(f"{path}: binds '{n}', whose spec does not load: {exc}")
            continue
        if f.role in flowmod.ROLES_UNROUTABLE:
            problems.append(
                f"{path}: binds '{n}', whose role is '{f.role}' — that role is never routed to, so "
                f"an agent cannot reach for it. It is reached THROUGH another ability, or not at all.")
            continue
        bound_production.add(n)

    problems += _check_chains(raw, path, set(names))
    problems += _check_confusable(raw, path, set(names))
    problems += _check_tools(raw, path)

    unmentioned = []
    for n in sorted(available):
        if n in names:
            continue
        try:
            if flowmod.load(n).role not in flowmod.ROLES_UNROUTABLE:
                unmentioned.append(n)
        except flowmod.FlowError:
            continue
    return (raw, problems, unmentioned)


def _check_chains(raw: dict, path: Path, bound: set) -> list[str]:
    """A chain is a hand-off ACROSS runs, which is the part the engine cannot see.

    `requires:` inside a spec is a load-time dependency the engine already resolves. A chain is
    different in kind: three separate runs, opened at different times, connected only by an artifact
    one of them produced. Nothing in the engine can derive that, and getting the ORDER wrong is how
    work gets recorded before it is planned.
    """
    out: list[str] = []
    chains = raw.get("chains") or []
    if not isinstance(chains, list):
        return [f"{path}: `chains` must be a list"]
    seen = set()
    for i, ch in enumerate(chains):
        out += _reject_unknown(ch, CHAIN_KEYS, f"chains[{i}]", path)
        if not isinstance(ch, dict):
            continue
        cid = str(ch.get("id") or "").strip()
        if not cid:
            out.append(f"{path}: chains[{i}] has no `id`")
        elif cid in seen:
            out.append(f"{path}: two chains share the id '{cid}'")
        else:
            seen.add(cid)
        hops = ch.get("hops")
        if not isinstance(hops, list) or len(hops) < 2:
            out.append(
                f"{path}: chains[{i}] ('{cid}') needs at least two `hops` — a single-ability chain "
                f"is just that ability, and writing it down adds a place to go stale.")
            continue
        for h in hops:
            if str(h).strip() not in bound:
                out.append(
                    f"{path}: chains[{i}] ('{cid}') hops through '{h}', which this agent does not "
                    f"bind. A chain the agent cannot walk is a promise it cannot keep.")
        if not str(ch.get("carried_by") or "").strip():
            out.append(
                f"{path}: chains[{i}] ('{cid}') does not say what is `carried_by` between hops. "
                f"Separate runs share no state, so the artifact that connects them IS the chain — "
                f"without it this is just an ordering somebody remembered.")
    return out


def _check_confusable(raw: dict, path: Path, bound: set) -> list[str]:
    """A pair worth recording is one where confusing them costs something ASYMMETRIC.

    Both halves are required, and the cost is the load-bearing one. "These two are different" is
    something a reader already suspects; what they cannot work out is which way round the mistake is
    expensive, and that is the only thing that changes a decision under time pressure.
    """
    out: list[str] = []
    pairs = raw.get("confusable") or []
    if not isinstance(pairs, list):
        return [f"{path}: `confusable` must be a list"]
    for i, rec in enumerate(pairs):
        out += _reject_unknown(rec, CONFUSABLE_KEYS, f"confusable[{i}]", path)
        if not isinstance(rec, dict):
            continue
        pair = rec.get("pair")
        if not isinstance(pair, list) or len(pair) != 2:
            out.append(f"{path}: confusable[{i}] `pair` must name exactly two")
            continue
        a, b = (str(x).strip() for x in pair)
        if a == b:
            out.append(f"{path}: confusable[{i}] pairs '{a}' with itself")
        for x in (a, b):
            if x not in bound:
                out.append(
                    f"{path}: confusable[{i}] names '{x}', which this agent does not bind — it "
                    f"cannot confuse two things when it can only reach one.")
        if not str(rec.get("distinction") or "").strip():
            out.append(f"{path}: confusable[{i}] ('{a}' / '{b}') states no `distinction`")
        if not str(rec.get("cost") or "").strip():
            out.append(
                f"{path}: confusable[{i}] ('{a}' / '{b}') states no `cost`. That is the half worth "
                f"writing: a reader can usually see that two things differ, but not which way round "
                f"the mistake is expensive — and that is what decides it under time pressure.")
    return out


def _check_tools(raw: dict, path: Path) -> list[str]:
    """Things that are NOT flows, and so have no `when:` for the engine to publish.

    They still have to be findable, which is the whole content of an entry here: a name, and how to
    reach it. A name with no route is a reminder that something exists, which is worse than nothing —
    it costs a search every time somebody reads it.
    """
    out: list[str] = []
    tools = raw.get("tools") or []
    if not isinstance(tools, list):
        return [f"{path}: `tools` must be a list"]
    seen = set()
    for i, t in enumerate(tools):
        out += _reject_unknown(t, TOOL_KEYS, f"tools[{i}]", path)
        if not isinstance(t, dict):
            continue
        name = str(t.get("name") or "").strip()
        if not name:
            out.append(f"{path}: tools[{i}] has no `name`")
        elif name in seen:
            out.append(f"{path}: two tools share the name '{name}'")
        else:
            seen.add(name)
        if not str(t.get("reach") or "").strip():
            out.append(
                f"{path}: tools[{i}] ('{name}') says nothing about how to `reach` it. A name with "
                f"no route costs a search every time it is read.")
    return out
