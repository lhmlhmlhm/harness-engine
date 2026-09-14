"""Prose — resolving a step's pointer into actual text, and refusing when it rots.

WHY POINTERS AND NOT INLINE TEXT

An ability's procedural detail is large (thousands of lines in a mature flow) and is
organised by TOPIC, not by step: one document deliberately serves several steps. Folding
it into the spec would bloat the spec and tear those documents apart. So the spec carries
pointers and this module resolves them.

WHY FOUR TIERS

The three kinds of text a step needs have different sizes, different lifecycles, and —
decisively — different moments at which they are needed:

  title      one line          always visible
  directive  a few lines       printed on every `next`; enough to act without opening anything
  guide      a document        the procedural contract; read WHEN doing the step
  topics     documents         judgment/caveats; read only WHEN IN TROUBLE

Collapsing these into one field is how a context window dies: the caveats you need once
a quarter get loaded before every step. The tiers exist so the terse thing is the default
and the large thing is on request.

WHY THE ENGINE VALIDATES THE POINTERS

Prose rot is silent — a heading gets reworded, a file moves, and the reference quietly
resolves to nothing while the spec still claims it is there. This engine's whole premise
is turning silent degradation into an exit code, so a declared-but-unresolvable pointer
is fatal at load time, exactly like a guard aimed at an ungated step.

WHAT IS NOT FATAL: having no pointer at all. In the transcribed real flow only 75 of 111
steps have a heading of their own; the rest inherit their stage's document. Demanding one
per step would force 36 duplicates or 36 dangling references — so absence is fine, and
only a broken claim is an error.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


class ProseError(ValueError):
    """A declared prose pointer does not resolve."""


@dataclass(frozen=True)
class Resolved:
    """A guide pointer resolved to text."""
    path: Path
    anchor: str | None
    level: str          # step | stage | phase
    text: str
    whole_file: bool    # True when no anchor matched and the file was taken entire


def split_pointer(pointer: str) -> tuple[str, str | None]:
    """`file.md#anchor` → (`file.md`, `anchor`); `file.md` → (`file.md`, None)."""
    if "#" in pointer:
        rel, _, anchor = pointer.partition("#")
        return rel, anchor or None
    return pointer, None


def guide_path(flow, pointer: str) -> Path:
    rel, _ = split_pointer(pointer)
    if flow.prose_root is None:
        raise ProseError(
            f"guide '{pointer}' is declared but the spec has no prose.root, so there is "
            f"nowhere to resolve it from.\n"
            f"  Add:  prose:\\n          root: prose/"
        )
    return flow.prose_root / rel


def _anchor_regex(flow, token: str) -> re.Pattern:
    """Build the heading matcher from the ability's own declared pattern.

    The pattern lives in the ability's spec precisely so the engine never has to know
    what a step id looks like — knowing that is what fuses an engine to its first
    consumer. The engine only substitutes and searches.
    """
    pat = flow.anchor_pattern.replace("{step_id}", re.escape(token))
    try:
        return re.compile(pat, re.MULTILINE)
    except re.error as exc:
        raise ProseError(
            f"prose.anchor_pattern is not a valid regex once '{token}' is substituted: {exc}"
        ) from None


def _heading_level(line: str) -> int:
    m = re.match(r"^(#{1,6})\s", line)
    return len(m.group(1)) if m else 0


def extract_section(text: str, rx: re.Pattern) -> str | None:
    """Return the matched heading plus everything until the next same-or-shallower one."""
    m = rx.search(text)
    if m is None:
        return None
    lines = text.splitlines()
    start = text[: m.start()].count("\n")
    depth = _heading_level(lines[start])
    out = [lines[start]]
    for line in lines[start + 1:]:
        lvl = _heading_level(line)
        if lvl and depth and lvl <= depth:
            break
        out.append(line)
    return "\n".join(out).rstrip() + "\n"


def resolve_guide(flow, step_id: str) -> Resolved | None:
    """Resolve a step's guide, or None when the step declares none at any level."""
    pointer, level = flow.guide_for(step_id)
    if pointer is None:
        return None
    path = guide_path(flow, pointer)
    if not path.is_file():
        raise ProseError(f"guide '{pointer}' (level: {level}) → {path} does not exist")
    text = path.read_text(encoding="utf-8")
    _, anchor = split_pointer(pointer)

    if anchor is not None:
        # An EXPLICIT anchor is a promise, so failing to find it is an error.
        section = extract_section(text, _anchor_regex(flow, anchor))
        if section is None:
            raise ProseError(
                f"guide '{pointer}' names anchor '{anchor}' but no heading in {path.name} "
                f"matches the ability's anchor_pattern for it.\n"
                f"  pattern: {flow.anchor_pattern}"
            )
        return Resolved(path, anchor, level, section, whole_file=False)

    # No explicit anchor: TRY the step's own id, and fall back to the whole document.
    # The fallback is deliberate — see the module docstring on 75-of-111.
    section = extract_section(text, _anchor_regex(flow, step_id))
    if section is not None:
        return Resolved(path, step_id, level, section, whole_file=False)
    return Resolved(path, None, level, text, whole_file=True)


def resolve_topic(flow, name: str) -> Path:
    path = flow.topic_path(name)
    if path is None:
        raise ProseError(
            f"topic '{name}' is cited but the spec has no prose.root to resolve it from"
        )
    if not path.is_file():
        raise ProseError(
            f"topic '{name}' → {path} does not exist "
            f"(topics resolve to <prose.root>/{flow.topics_dir}/<name>.md)"
        )
    return path


def validate_all(flow) -> tuple[list[str], list[str]]:
    """Check every declared pointer. Returns (errors, warnings).

    Errors are dangling claims — a pointer that says text exists where it does not.
    Warnings are orphans — prose nobody cites. Orphans are only a warning because
    freshly-written prose that is not wired up yet is a normal intermediate state,
    whereas a dangling claim is never correct.

    Checking BOTH directions is what makes "single source of truth" real. A related
    system keeps a keep-list naming two files that do not exist in its source; pack
    neither errors nor warns, so both entries are silent no-ops nobody has noticed.
    """
    errors: list[str] = []
    warnings: list[str] = []

    for sid in flow.steps:
        try:
            resolve_guide(flow, sid)
        except ProseError as exc:
            errors.append(f"step '{sid}': {exc}")
        for topic in flow.steps[sid].topics:
            try:
                resolve_topic(flow, topic)
            except ProseError as exc:
                errors.append(f"step '{sid}': {exc}")

    if flow.prose_root is not None:
        tdir = flow.prose_root / flow.topics_dir
        if tdir.is_dir():
            on_disk = {p.stem for p in tdir.glob("*.md")}
            orphans = sorted(on_disk - flow.all_cited_topics())
            for o in orphans:
                warnings.append(
                    f"topic '{o}' exists at {flow.topics_dir}/{o}.md but no step cites it"
                )
    return errors, warnings


def coverage(flow) -> dict:
    """How much of the flow actually has prose behind it, by tier."""
    total = len(flow.steps)
    directives = sum(1 for s in flow.steps.values() if s.directive)
    own_anchor = 0
    any_guide = 0
    for sid in flow.steps:
        try:
            r = resolve_guide(flow, sid)
        except ProseError:
            continue
        if r is None:
            continue
        any_guide += 1
        if not r.whole_file:
            own_anchor += 1
    return {
        "steps": total,
        "directive": directives,
        "guide": any_guide,
        "own_section": own_anchor,
        "topics_cited": len(flow.all_cited_topics()),
    }

def dangling_topics(flow) -> list:
    """Inline topic references in prose BODIES that resolve to no file.

    The declared side (`topics:` on a step) was already checked and was already clean. This reads the
    other side — the pointers written INTO the prose — because those are the ones a driver actually
    follows mid-step, and a pointer to nothing sends it looking elsewhere.

    Returns (path, lineno, name) for each dangling reference, in file order. Empty means every
    pointer resolves.
    """
    if flow.prose_root is None or not flow.prose_root.is_dir():
        return []
    pat = re.compile(flow.topic_ref_pattern)
    out = []
    for path in sorted(flow.prose_root.glob("*.md")):
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for lineno, line in enumerate(lines, 1):
            for m in pat.finditer(line):
                name = m.group("name")
                if not flow.topic_path(name).is_file():
                    out.append((path, lineno, name))
    return out
