"""Flow — the plugin layer.

An ability contributes exactly one declarative artefact: `abilities/<name>/flow.yaml`.
The engine reads it and knows nothing else about that ability. Step ids, phase names,
gate policies, the actions a guard protects, and the model-facing directives are ALL
data in that file. None of them appear in engine code — see `tests/test_engine_purity.py`,
which fails the build if any leak in.

That test is the whole point. A prior system in this space accumulated 575 references
to 102 step ids spread across ~50 functions, which is what made its engine
inextricable from its first ability. Domain vocabulary in code is a one-way door;
the purity test is the door-stop.

VALIDATION IS FAIL-CLOSED. A spec that violates any load-time invariant raises and
the CLI exits 2 without touching the store. A related system once let the sum of its
per-section quotas drift above the global cap; the renderer then fail-closed at
USE time, so the defect surfaced as "the step cannot run at all" long after the edit
that caused it. Checking at load time turns that into an immediate, legible error.
"""
from __future__ import annotations

import functools
import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import yaml

ENGINE_DIR = Path(__file__).resolve().parent
REPO_ROOT = ENGINE_DIR.parent

# WHERE FLOWS LIVE, AND WHY IT IS NOT A CONSTANT
#
# The engine ships a sibling `abilities/` directory, and for anyone developing the engine
# that is the whole story. A consumer, though, installs the engine and keeps its own flows
# in its own tree. With a frozen constant its only two options are to fork the engine or to
# commit its own domain into the engine's repository — and the second one is exactly how a
# generic base fuses to its first consumer, which is what the purity guards exist to stop.
# A relocatable root is therefore not a convenience; it is the same property those guards
# assert, enforced at the filesystem instead of in the source.
#
# `HARNESS_ABILITIES_PATH` is an `os.pathsep`-separated list, like PATH. Setting it
# REPLACES the default rather than adding to it: an implicit union means a consumer can
# never obtain a clean set, and something appearing from a root nobody named is worse than
# having to name both roots.
ABILITIES_ENV = "HARNESS_ABILITIES_PATH"
ABILITIES_DIR_DEFAULT = REPO_ROOT / "abilities"


def abilities_roots() -> list[Path]:
    """The roots to search, resolved at CALL time.

    Not at import time: a long-lived process — or any caller that sets the variable after
    importing this module — would otherwise be frozen to whatever the value was when the
    module first loaded, and that failure is silent. It reads as "that root holds nothing"
    rather than "you configured it too late", so nobody looks at the variable.
    """
    raw = os.environ.get(ABILITIES_ENV, "").strip()
    if not raw:
        return [ABILITIES_DIR_DEFAULT]
    roots = [Path(p).expanduser() for p in raw.split(os.pathsep) if p.strip()]
    if not roots:
        raise FlowError(f"{ABILITIES_ENV} is set but names no directory: {raw!r}")
    return roots


def _installed() -> dict[str, Path]:
    """Map name -> directory, scanning every root in order.

    Two failure modes are refused loudly rather than absorbed:

    * A root named in the environment that is not a directory. A mistyped path would
      otherwise report "nothing installed", which reads as an empty install rather than a
      bad configuration — so the variable is the last place anyone would look. The DEFAULT
      root is allowed to be missing, because an engine-only checkout legitimately has none.
    * The same name present in two roots. First-wins would let a root nobody is looking at
      shadow the one being edited, and "which of the two actually ran" stops being
      answerable from the spec — the engine refuses to pick instead.
    """
    configured = bool(os.environ.get(ABILITIES_ENV, "").strip())
    found: dict[str, Path] = {}
    for root in abilities_roots():
        if not root.is_dir():
            if configured:
                raise FlowError(
                    f"{ABILITIES_ENV} names '{root}', which is not a directory.\n"
                    f"  Fix the variable, or unset it to fall back to"
                    f" {ABILITIES_DIR_DEFAULT}."
                )
            continue
        for p in sorted(root.iterdir()):
            if not p.is_dir() or not (p / "flow.yaml").is_file():
                continue
            if p.name in found:
                raise FlowError(
                    f"'{p.name}' is installed twice:\n"
                    f"  {found[p.name]}\n"
                    f"  {p}\n"
                    f"  Remove one, or narrow {ABILITIES_ENV} — the engine will not pick"
                    f" for you."
                )
            found[p.name] = p
    return found

# The spec format, as MAJOR.MINOR.
#
# MAJOR is a readability claim: a different major means the structure changed, and an older
# engine cannot read it — not because it refuses to try, but because it does not know what
# moved. MINOR means keys were ADDED. The structure is still readable, so a newer minor is
# accepted and read; what it costs is that keys added after this engine are unknown here, and
# the refusal for those says so instead of looking like a typo.
#
# There is deliberately no reader for a future MAJOR. Accepting one and reading it as this
# major would silently misinterpret it, and a claim that cannot be falsified is exactly what
# this engine refuses everywhere else. When a major 2 actually exists, supporting it is a
# reader plus a fixture per version — mechanical, and guarded.
SPEC_MAJOR = 2
SPEC_MINOR = 0


def spec_format() -> str:
    return f"{SPEC_MAJOR}.{SPEC_MINOR}"


# What changed between two MAJORs, keyed (from, to). Only filled for gaps this engine can
# describe — which is the ones it introduced. A refusal that can name the change turns a wall
# into a migration; one that cannot should stay silent rather than guess.
_MIGRATIONS = {
    (1, 2): (
        "  WHAT CHANGED IN 2.0: registrations are namespaced per ability, so a reference to\n"
        "  ANOTHER ability's predicate/provider/operator must be written with its owner and\n"
        "  declared — `providers: [<owner>.<name>]` plus `requires: [<owner>]`. Your own names\n"
        "  and the engine's stay bare and need no change. In 1.x a bare name reached any\n"
        "  ability loaded in the same process, which is why the format had to break.\n"
    ),
}


def _parse_spec_version(value, path: Path) -> tuple[int, int]:
    """Read `version:` as (major, minor). `1` means 1.0.

    A FLOAT is refused, and that is not pedantry: YAML reads `1.10` and `1.1` as the same
    float, so two different minors would collide into one and the engine would read the wrong
    one without noticing. The same coercion trap already has a guard for keys (`on:`/`yes:`
    becoming booleans); this is the value side of it. Write a minor as a quoted string.
    """
    if isinstance(value, bool):
        raise FlowError(f"{path}: 'version' is a boolean — YAML coerced it. Quote it.")
    if isinstance(value, int):
        return value, 0
    if isinstance(value, float):
        raise FlowError(
            f"{path}: 'version' is a float ({value!r}). YAML reads 1.10 and 1.1 as the SAME\n"
            f"  float, so two different minors would collide and the wrong one would be read\n"
            f"  silently. Quote it: version: \"{value}\""
        )
    if isinstance(value, str):
        text = value.strip()
        parts = text.split(".")
        if len(parts) <= 2 and all(x.isdigit() for x in parts) and parts[0]:
            return int(parts[0]), int(parts[1]) if len(parts) == 2 else 0
    raise FlowError(
        f"{path}: 'version' must be an integer major, or a quoted \"MAJOR.MINOR\"; "
        f"got {value!r}"
    )


def _capability_gap(raw: dict) -> str:
    """When the format is unreadable, say whether the MACHINERY is also missing.

    The two are different problems with different answers, and the version number alone cannot
    tell them apart. A spec whose `uses:` this engine fully implements is a pure format gap —
    everything it needs exists here, the writing does not. One naming an absent mechanism needs
    more than a newer reader. Saying which is the difference between a useful refusal and a
    number.
    """
    declared = raw.get("uses")
    if not isinstance(declared, list):
        return ("  It declares no capability list, so nothing can be said about which mechanisms\n"
                "  it needs — only that the structure is unreadable here.")
    names = [str(x).strip() for x in declared]
    missing = [n for n in names if n not in ENGINE_CAPABILITIES]
    if not missing:
        have = ", ".join(names) if names else "none"
        return (f"  Its `uses:` names only mechanisms this engine HAS ({have}), so this is a\n"
                f"  format gap and not a machinery gap: a newer engine can read it unchanged.")
    return (f"  Its `uses:` also names mechanisms this engine does NOT implement: "
            f"{', '.join(sorted(missing))}.\n"
            f"  So a newer reader alone would not be enough — the flow needs machinery that is\n"
            f"  absent here.")

# A gate policy says what authorises passing a step.
#   none          — no gate; closing the step is enough
#   affirm        — a human must affirm, proven by an out-of-band signal
#   preauth:<key> — a config key may stand in for the human, if it is truthy
GATE_NONE = "none"
# WHAT AN ABILITY IS FOR. `production` is the default and the only routable role; `fixture`
# marks a flow that exists to exercise the ENGINE, not to carry work.
#
# WHY THIS IS A DECLARED ROLE AND NOT A COMMENT. Two of the flows here are the cheapest
# fixtures the mechanism tests have — five and ten steps against a hundred-and-one — so every
# gate, witness, preauth, guard, scope-ambiguity and close-run test drives one of them. That
# made them look like unfinished abilities in every report: their criteria are weak because a
# fixture does not need strong criteria, and one of them keeps a deliberate `attest` step to
# demonstrate the honest floor. Reporting that as a shortfall for several rounds was a category
# error — a strength bar meant for transcribed real flows applied to test scaffolding.
#
# AND WHY THE LABEL CANNOT BUY A FREE PASS. A role that merely silences a report is an escape
# hatch: any ability could claim it. So the label is tied to something structural and checked
# at load — a fixture must be UNROUTABLE. It may not carry `when:` (the routing hint an agent
# reads), and no production ability may `requires:` one. Conversely `production` is the default
# and it MUST carry routing. The two constraints point in opposite directions, so neither role
# is the cheap one to claim.
ROLE_PRODUCTION = "production"
ROLE_FIXTURE = "fixture"
ROLES = (ROLE_PRODUCTION, ROLE_FIXTURE)

GATE_AFFIRM = "affirm"
GATE_PREAUTH_PREFIX = "preauth:"


from . import conditions as conditionsmod
from . import registry
from . import trust
from .conditions import ConditionError as ConditionErrorAlias


class FlowError(ValueError):
    """A flow spec is malformed. Always fatal — never degrade into a partial run."""


@dataclass(frozen=True)
class Step:
    id: str
    phase: str
    title: str
    deps: tuple[str, ...]
    gate: str
    completion: dict
    directive: str
    autonomy: str
    # Third structural level. A real flow groups steps inside a phase (24 such groups in
    # the transcribed one), and its goal predicates filter on that grouping — so it is
    # load-bearing structure, not decoration. Optional: a flat ability just omits it.
    stage: str | None = None
    # A step that may legitimately never run. Without this, a flow containing an
    # optional branch can never be closed: `close-run` would demand every step, and the
    # transcribed 111-step flow has a 16-step branch that only runs on findings.
    optional: bool = False
    # Pointer to the procedural detail for this step, as `<file>` or `<file>#<anchor>`.
    # NOT inlined: a real flow's prose runs to thousands of lines organised by topic, and
    # folding that into the spec would both bloat it and tear apart documents that
    # deliberately serve several steps at once.
    guide: str | None = None
    # Named judgment topics this step is prone to. Read on demand, never up front —
    # loading every caveat before every step is how a context window dies.
    topics: tuple[str, ...] = ()
    # What this step HANDS BACK when it closes: a path the driver recorded as evidence, read by
    # the engine and returned. NOT a criterion — see engine/outputs.py for why a channel whose
    # content comes from the driver must never be able to satisfy one.
    output: dict | None = None
    # WHO PRODUCES THIS STEP'S EFFECT, when the effect is one the engine refuses to perform.
    # A pointer and nothing else: the tool's own header is the single source of truth for how to
    # call it, and a copy of its argument shape in here would drift the first time a flag moved.
    # Absent producers are reported by `validate`, never fatal at load — see this module's header.
    produced_by: str | None = None
    # A step that may legitimately run more than once — a fix/re-check cycle.
    repeatable: bool = False
    # How many attempts the budget allows. 0 = unlimited.
    budget: int = 0
    # An exhausted budget REFUSES, unconditionally. This used to be a declared choice between
    # refusing and escalating to a human gate, on the reasoning that the right answer depends on
    # whether exceeding a budget means "this is broken" or "a person should look".
    #
    # In practice nothing ever chose to escalate — eight declarations across the installed flows,
    # every one of them restating the default. Deleting the unused branch would have left a key
    # with a single legal value, and a key that can only say one thing says nothing. So the key
    # went too. Escalation comes back the way anything does: a value, its branch, and a flow that
    # selects it.
    # Which VARIANTS this step belongs to. Empty = the shared core, applicable under every
    # variant. A step listed here is NOT APPLICABLE under any other variant — distinct from
    # `optional` (may be skipped by a decision) and from a skip (a decision was recorded).
    # Without this, a flow with pluggable execution modes has to either duplicate its shared
    # core once per mode, or mark the mode-specific steps optional — and optional cannot
    # prevent the WRONG mode's steps from running and closing cleanly, which is the whole
    # reason the distinction is worth a primitive.
    variants: tuple[str, ...] = ()
    # Forbid the recorded downgrade on THIS gate. Some transitions are irreversible
    # enough that "no witness available" must stop the run rather than annotate it —
    # the real flow marks exactly two of its gates this way.
    strict_witness: bool = False

    @property
    def evidence_scope(self) -> str | None:
        """Which step's evidence a completion predicate reads — this one."""
        return self.id

    @property
    def gate_kind(self) -> str:
        if self.gate == GATE_NONE:
            return GATE_NONE
        if self.gate == GATE_AFFIRM:
            return GATE_AFFIRM
        return GATE_PREAUTH_PREFIX.rstrip(":")

    @property
    def preauth_key(self) -> str | None:
        if self.gate.startswith(GATE_PREAUTH_PREFIX):
            return self.gate[len(GATE_PREAUTH_PREFIX):]
        return None


@dataclass(frozen=True)
class GoalSubject:
    """What a phase GOAL is checked against.

    A goal is not a step, but it is checked by the SAME predicate registry — a layer's
    acceptance criterion and a step's completion criterion are the same question asked at a
    different scope. One registry is what keeps the two vocabularies from drifting apart: the
    reference system kept them separate and ended up implementing one invariant twice, in two
    places, reading the same fields.

    `evidence_scope` is None, so evidence lookups span the whole run. A criterion like "the run
    holds a terminal-clean status" must not care WHICH step recorded it; pinning it to one step
    would make it satisfiable by recording the value against the wrong step.
    """
    phase: str
    id: str            # synthetic (e.g. "@verification") — messages only, never a step id
    stage: None = None
    optional: bool = False

    @property
    def evidence_scope(self) -> None:
        return None


@dataclass(frozen=True)
class Flow:
    ability: str
    title: str
    scope_kind: str
    phases: tuple[str, ...]
    phase_titles: dict
    phase_stages: dict  # phase id -> tuple of declared stage ids
    steps: dict  # id -> Step, in declaration order
    guards: dict  # action name -> step id whose gate protects it
    # action name -> tuple of {tool, field|None, regex} — how a RUNTIME recognises the action
    # in a tool call it is about to make. Data, not code: the engine compiles and applies the
    # pattern and never learns what any of them mean.
    guard_matches: dict
    # How an open run's scope_key is compared to a runtime's working location.
    scope_match: str
    config_defaults: dict
    # Mutually exclusive groups: closing one member satisfies the group and auto-skips
    # its siblings. A real flow has Y/N close branches that are two steps, exactly one
    # of which ever runs.
    exclusive_groups: tuple[tuple[str, ...], ...]
    # One line: WHEN a caller should reach for this ability. Lives here rather than in a
    # separate routing document because a hand-kept list of "which ability for what" drifts
    # from the abilities the moment one is added — and a routing table that is quietly wrong
    # sends work to the wrong lifecycle, which is worse than having no table.
    when: str
    role: str
    # Abilities whose extension registrations this one relies on. Declared so a shared source
    # of truth stays singular without an ability secretly depending on load order.
    requires: tuple[str, ...]
    # Declared variants: {"values": (...), "default": str, "fact": str | None}. Empty when the
    # ability has one shape.
    variant_spec: dict
    # How this flow may END. Empty means it never said, and then the engine keeps its own
    # fallback — the state every flow was in: `--result` took any string at all, so `done` /
    # `completed` and `abort` / `aborted` coexisted as four words for two things.
    result_spec: dict
    facts_providers: tuple[str, ...]
    facts_owner: dict   # fact name -> the provider that supplies it
    facts_schema: dict
    hooks: tuple
    prose_root: Path | None
    topics_dir: str
    anchor_pattern: str
    # How a prose BODY spells an inline reference to a topic. Declared rather than known, for the
    # same reason `anchor_pattern` is: the engine must not own one project's punctuation. Must
    # capture a group named `name`.
    topic_ref_pattern: str
    phase_goals: dict    # phase id -> completion spec that ACCEPTS the phase (may be absent)
    phase_guides: dict   # phase id -> guide pointer
    stage_guides: dict   # (phase id, stage id) -> guide pointer
    digest: str
    source: Path
    order: tuple[str, ...] = field(default=())

    def step(self, step_id: str) -> Step:
        try:
            return self.steps[step_id]
        except KeyError:
            raise FlowError(
                f"ability '{self.ability}' declares no step '{step_id}'. "
                f"known: {', '.join(self.steps)}"
            ) from None

    def steps_in_phase(self, phase: str) -> list[Step]:
        return [s for s in self.steps.values() if s.phase == phase]

    def steps_in_stage(self, phase: str, stage: str | None) -> list[Step]:
        return [s for s in self.steps.values() if s.phase == phase and s.stage == stage]

    def stages_of(self, phase: str) -> list[str | None]:
        """Declared stage order, plus None if any step in the phase is unstaged."""
        out: list[str | None] = list(self.phase_stages.get(phase, ()))
        if any(s.stage is None for s in self.steps_in_phase(phase)):
            out.append(None)
        return out

    def guide_for(self, step_id: str) -> tuple[str | None, str]:
        """Resolve a step's guide pointer, nearest-wins: step → stage → phase.

        Returns (pointer, level). Three levels exist because prose is NOT 1:1 with
        steps: in the transcribed real flow only 75 of 111 steps have a heading of
        their own — the rest are covered by a heading spanning their stage. Demanding a
        per-step pointer would force either 36 duplicates or 36 dangling references.
        """
        s = self.steps[step_id]
        if s.guide:
            return s.guide, "step"
        if s.stage is not None:
            g = self.stage_guides.get((s.phase, s.stage))
            if g:
                return g, "stage"
        g = self.phase_guides.get(s.phase)
        if g:
            return g, "phase"
        return None, "none"

    def topic_path(self, name: str) -> Path | None:
        if self.prose_root is None:
            return None
        return self.prose_root / self.topics_dir / f"{name}.md"

    def all_cited_topics(self) -> set[str]:
        out: set[str] = set()
        for s in self.steps.values():
            out.update(s.topics)
        return out

    def group_of(self, step_id: str) -> tuple[str, ...] | None:
        for grp in self.exclusive_groups:
            if step_id in grp:
                return grp
        return None

    @property
    def variants(self) -> tuple[str, ...]:
        return tuple(self.variant_spec.get("values", ()))

    @property
    def default_variant(self) -> str | None:
        return self.variant_spec.get("default")

    @staticmethod
    def _payload_names(scope_key: str, payload: dict | None) -> bool:
        """Does the CALL ITSELF name this scope?

        No early-out for an empty payload: `{}` serialises to a string the scope key cannot
        appear in, so the search already answers "not mine". A guard clause there looked
        defensive and could not change any outcome — the same inert-but-declared shape this
        engine exists to remove.

        Whole-token, so a scope of `CR-123` does not claim `CR-1234`. `/` is not a token
        character, so a path scope of `/a/b` DOES name `/a/b/c` — which is what makes this
        usable for a location scope whose action carries its own target.
        """
        import json as _json
        hay = _json.dumps(payload or {}, ensure_ascii=False, sort_keys=True)
        return re.search(rf"(?<![A-Za-z0-9_-]){re.escape(str(scope_key))}"
                         rf"(?![A-Za-z0-9_-])", hay) is not None

    def scope_covers(self, scope_key: str, where: str, payload: dict | None = None) -> bool:
        """Does a run scoped to `scope_key` own an action happening at `where`?

        `in_payload` exists because a working directory answers nothing for a scope that is not
        a location. A run about one review, or one sprint, is not tied to a directory — and
        comparing its scope key to a cwd simply never matches, so the guard silently never
        fires. The reliable answer for those is the one this store's own design already argues
        for: **the scope key the action carries with it**. A call that names the review it is
        commenting on identifies its owner unambiguously; a call that names none is not that
        run's business.

        `exact` is the default because equality is the only comparison that is right for every
        kind of scope. `path_prefix` exists for scopes that ARE locations, and it compares by
        path component so that a scope of `/a/b` does not claim `/a/bc`.

        A false positive here is worse than a miss, and not symmetrically: being told to satisfy
        a gate that belongs to somebody else's work leaves forging that gate as the only way
        forward. That is why widening this is a per-ability declaration and never a default.
        """
        if self.scope_match == "in_payload":
            return self._payload_names(scope_key, payload)
        if self.scope_match == "exact":
            return scope_key == where
        # A LOCATION SCOPE WHOSE ACTION NAMES ITS OWN TARGET. `path_prefix` alone asks only
        # where the CALLER is, and a tool call can act on a directory that is not there —
        # `git -C <path> commit` was measured going through untouched while the same commit
        # made from inside the scope was refused. Widening it is a per-ability DECLARATION and
        # never a default, for the reason stated above: a scope that is merely MENTIONED (a
        # commit message quoting a path) matches too, and being told to satisfy a gate that
        # belongs to somebody else's work leaves forging that gate as the way forward.
        #
        # The payload side compares the scope key LITERALLY — it does not resolve symlinks the
        # way the two directories below are resolved, because the payload is prose and the
        # paths in it are not addressable without guessing which substrings are paths. A
        # symlinked spelling in a command therefore MISSES rather than blocks, which is the
        # safe direction for the asymmetry above.
        if (self.scope_match == "path_prefix_or_payload"
                and self._payload_names(scope_key, payload)):
            return True
        # BOTH SIDES ARE RESOLVED, and skipping that is a silent miss rather than a loud one.
        # A runtime reports where it is by asking the OS, which answers with symlinks already
        # followed; a scope was recorded as whatever the opener typed. On a machine where a
        # common parent is a symlink, the two spellings of one directory never compare equal —
        # so the guard simply does not fire, and "not guarded" looks exactly like "nothing to
        # guard". Observed on a platform whose temp directory is symlinked by default.
        # Normalising at COMPARISON time, not at record time, keeps what `status` shows equal
        # to what the opener wrote.
        import os
        try:
            a = os.path.realpath(os.path.expanduser(str(scope_key)))
            b = os.path.realpath(os.path.expanduser(str(where)))
        except (TypeError, ValueError, OSError):
            return scope_key == where
        pa, pb = PurePosixPath(a), PurePosixPath(b)
        return pb == pa or pa in pb.parents

    def applicable(self, step_id: str, variant: str | None) -> bool:
        """Does this step exist AT ALL for a run following `variant`?

        Not the same question as "should it run": an inapplicable step is not part of the
        flow this run follows, so it is never owed, never skippable, and entering it is an
        error rather than a choice.
        """
        vs = self.steps[step_id].variants
        if not vs:
            return True
        if variant is None:
            return True   # single-shape run reading a multi-variant spec: nothing is filtered
        return variant in vs

    def required_steps(self, variant: str | None = None) -> list[str]:
        """Steps a run must account for: applicable, non-optional, one per exclusive group."""
        seen_groups: set[tuple[str, ...]] = set()
        out = []
        for sid in self.order:
            s = self.steps[sid]
            if not self.applicable(sid, variant):
                continue
            if s.optional:
                continue
            grp = self.group_of(sid)
            if grp is not None:
                if grp in seen_groups:
                    continue
                seen_groups.add(grp)
            out.append(sid)
        return out


# Keys the engine understands. Anything else in a step is a typo or a construct the
# engine cannot express — both must be loud.
#
# WHY STRICT: a silently-ignored key is how a spec ends up meaning something other than
# it reads. `gaet: affirm` would produce an UNGATED step while the yaml appears to
# declare a gate — the same "looks protective, is inert" failure the guard validation
# already refuses. Related systems in this space have the matching bug in both
# directions: one accepts a patch aimed at a non-existent id with only a warning, and
# another appends a duplicate registration without a uniqueness check. Cheap to
# prevent here, expensive to debug later.
STEP_KEYS = {"variants", "id", "phase", "stage", "title", "deps", "gate", "completion", "directive",
             "produced_by",
             "output",
             "autonomy", "optional", "strict_witness", "guide", "topics",
             "repeatable", "budget"}
TOP_KEYS = {"role", "when", "uses", "scope_match", "requires", "variants", "version", "ability", "title", "scope_kind", "phases", "steps", "guards", "results",
            "config", "exclusive_groups", "prose", "facts", "hooks"}
PHASE_KEYS = {"id", "title", "stages", "guide", "goal"}
STAGE_KEYS = {"id", "guide"}
PROSE_KEYS = {"root", "topics_dir", "anchor_pattern", "topic_ref_pattern"}
# Fields that address a READER and constrain nothing. Stripped before the digest is taken, so an
# open run is not told "step semantics may have moved" because a sentence was reworded. Everything
# not named here IS hashed — the list says what to EXCLUDE on purpose: an include-list would stop
# covering any key added later, which is exactly how a real change slips past a check that looks
# like it is watching.
DIGEST_PROSE_FIELDS = {"title", "when", "directive"}
FACTS_KEYS = {"provider", "providers"}
VARIANT_KEYS = {"values", "default", "fact"}
# HOW A RUN MAY END. Same shape as `variants` on purpose — values plus an explicit default —
# because the alternative was making a list's ORDER semantic, and order that carries meaning is
# a rule nobody can see in the yaml.
RESULT_KEYS = {"values", "default"}
GUARD_KEYS = {"step", "matches"}
GUARD_MATCH_KEYS = {"tool", "field", "pattern"}
# What a step may declare about the content it HANDS BACK. No `from_path`: a flow's own files
# already have a channel (the prose tier, reached by pointer), and this exists for content that
# does not exist until the run does.
OUTPUT_KEYS = {"from_evidence", "select", "max_bytes"}
SELECT_KEYS = {"anchor", "lines", "regex"}
SCOPE_MATCH_MODES = ("exact", "path_prefix", "in_payload",
                     "path_prefix_or_payload")


def _require_plain_id(value, where: str, path: Path) -> str:
    """An id must be written as a plain string in the YAML.

    WHY: YAML 1.1 (what PyYAML implements) coerces bare `YES`/`NO`/`ON`/`OFF`/`Y`/`N`
    to booleans and `07` to an int. `str()` would then happily turn `True` back into
    the string "True" — so a step written `- id: NO` would silently become a step
    called "True", and every reference to `NO` elsewhere in the file would fail to
    resolve. That is exactly the silent-meaning-change this loader exists to prevent,
    so it is fatal rather than coerced. Found by writing a Y/N branch by hand.
    """
    if not isinstance(value, str):
        raise FlowError(
            f"{path}: {where} has id {value!r} of type {type(value).__name__}, not a string.\n"
            f"  YAML coerces bare YES/NO/ON/OFF/TRUE/FALSE to booleans and 07 to an int.\n"
            f"  Quote it — e.g. id: \"NO\" — so the id is what you wrote."
        )
    if not value.strip():
        raise FlowError(f"{path}: {where} has an empty id")
    return value


def _reject_unknown(mapping: dict, allowed: set, where: str, path: Path, *,
                    newer_by: tuple[int, int] | None = None) -> None:
    # A non-string KEY means YAML coerced it — `on:` / `no:` / `yes:` are booleans in
    # YAML 1.1, so a key spelled that way silently becomes True/False and stops matching
    # anything. Caught separately because the coerced value would otherwise blow up
    # formatting the error message, turning a legible refusal into a traceback.
    coerced = [k for k in mapping if not isinstance(k, str)]
    if coerced:
        raise FlowError(
            f"{path}: {where} has non-string key(s): "
            + ", ".join(f"{k!r} ({type(k).__name__})" for k in coerced)
            + "\n  YAML 1.1 turns bare on/off/yes/no/true/false into booleans. Quote the "
              "key, or use a name that is not reserved."
        )
    unknown = sorted(set(mapping) - allowed)
    if not unknown:
        return
    if newer_by:
        # A version gap and a typo used to produce the same message, so whoever hit it was told
        # to check their spelling when the truth was that their spec is newer than this engine.
        raise FlowError(
            f"{path}: {where} has key(s) this engine does not know: {', '.join(unknown)}\n"
            f"  This spec declares format {newer_by[0]}.{newer_by[1]} and this engine reads "
            f"{spec_format()} — same MAJOR, so the structure IS readable, but those keys were\n"
            f"  added after this engine.\n"
            f"  supported here: {', '.join(sorted(allowed))}\n"
            f"  Still fatal, and not because of the version: a key that is silently dropped\n"
            f"  reads as though it took effect, and one of them may be the constraint the flow\n"
            f"  is relying on. Upgrade the engine, or write the flow at {spec_format()}."
        )
    raise FlowError(
        f"{path}: {where} has unsupported key(s): {', '.join(unknown)}\n"
        f"  supported: {', '.join(sorted(allowed))}\n"
        f"  A typo here would silently change what this spec means, so it is fatal.\n"
        f"  If you need a construct the engine lacks, that is an engine gap — put it\n"
        f"  in a comment and raise it, rather than a key that does nothing."
    )


def available_abilities() -> list[str]:
    return sorted(_installed())


def ability_dir(ability: str) -> Path:
    """Where `ability` lives — or where it WOULD live if it is not installed.

    The fallback matters for error messages: a caller that cannot find a spec should be
    able to print the path it looked at, and printing nothing at all makes a typo'd name
    indistinguishable from a misconfigured root.
    """
    found = _installed().get(ability)
    return found if found is not None else abilities_roots()[0] / ability


def spec_path(ability: str) -> Path:
    return ability_dir(ability) / "flow.yaml"


_EXTENSIONS_LOADED: set[str] = set()


def load_extensions(ability: str) -> None:
    """Import an ability's own `providers.py`, if it has one.

    THE ESCAPE HATCH, AND WHY IT IS A PYTHON MODULE RATHER THAN SPEC SYNTAX

    An ability that needs a comparison or a source of truth the engine does not ship
    registers one. That registration is CODE — reviewed as code, in a file whose whole
    purpose is visible — and never an expression embedded in the spec. The distinction is
    the one this project keeps making: extend the registry, do not put code in data.

    THE COST, STATED PLAINLY: validating such an ability now imports its python. An
    ability WITHOUT a providers.py is still purely declarative and validatable without
    executing anything of its own; one WITH it asks for the same trust as any dependency.
    That is a real reduction in the "hand me an ability and I can check it" property, and
    it is the reason the hatch is a separate file rather than a field in the yaml — you
    can see at a glance which abilities take it.

    Import failure is fatal: a registry that half-loaded would make the spec's references
    resolve or not depending on import order.
    """
    if ability in _EXTENSIONS_LOADED:
        return
    mod_path = ability_dir(ability) / "providers.py"
    if not mod_path.is_file():
        # Nothing to trust: an ability with no providers.py is purely declarative, and that
        # difference is exactly what the separate file is for.
        _EXTENSIONS_LOADED.add(ability)
        return
    # Checked BEFORE the load marker is set. Marking first would mean a refused import leaves
    # the ability recorded as loaded, and a second attempt in the same process would skip the
    # check — a guard that stops firing after it fires once.
    try:
        trust.check(ability, mod_path)
    except trust.TrustError as exc:
        raise FlowError(str(exc)) from None
    _EXTENSIONS_LOADED.add(ability)
    import importlib.util
    spec = importlib.util.spec_from_file_location(f"_ability_{ability}_providers", mod_path)
    if spec is None or spec.loader is None:
        raise FlowError(f"{mod_path}: cannot be loaded as a python module")
    module = importlib.util.module_from_spec(spec)
    # Everything this file registers is owned by this ability. Ambient rather than an argument
    # on every decorator, because registration happens AT IMPORT — the window is exactly this
    # call, and an author repeating their own ability name in each decorator could get it wrong.
    registry.loading(ability)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise FlowError(
            f"{mod_path}: failed to import — {type(exc).__name__}: {exc}\n"
            f"  An ability's registry extensions must import cleanly, or its spec's "
            f"references to them would resolve unpredictably."
        ) from None
    finally:
        registry.loading(None)


def load(ability: str) -> Flow:
    path = spec_path(ability)
    if not path.is_file():
        known = available_abilities()
        raise FlowError(
            f"no flow spec for ability '{ability}' at {path}\n"
            f"  available: {', '.join(known) if known else '(none)'}"
        )
    load_extensions(ability)
    raw_text = path.read_text(encoding="utf-8")
    try:
        raw = yaml.safe_load(raw_text) or {}
    except yaml.YAMLError as exc:
        raise FlowError(f"{path}: not valid YAML — {exc}") from None
    digest = _digest(raw)
    return _build(ability, raw, digest, path)


def _require(raw: dict, key: str, path: Path, kind=None):
    if key not in raw:
        raise FlowError(f"{path}: missing required key '{key}'")
    val = raw[key]
    if kind is not None and not isinstance(val, kind):
        raise FlowError(
            f"{path}: '{key}' must be {kind.__name__}, got {type(val).__name__}"
        )
    return val


def _resolve_completion(spec: dict, where: str, ability: str,
                        requires: tuple[str, ...], predicates) -> str:
    """Resolve a completion spec's `type` to a registry key, IN PLACE, and recurse.

    Rewriting rather than returning a parallel structure: the spec dict is what `check()` is
    handed at runtime, so if the resolved key lived anywhere else the runtime would need to
    know who owns what — and every place that had to know would be a place it could be wrong.

    Recursion into `checks` is not optional. A composite's branches are completion specs like
    any other, and a borrowed predicate nested one level down would otherwise resolve against
    nobody's namespace.
    """
    kind = predicates.resolve(str(spec.get("type")), asking=ability, requires=requires)
    spec["type"] = kind
    for i, sub in enumerate(spec.get("checks") or []):
        if isinstance(sub, dict) and "type" in sub:
            _resolve_completion(sub, f"{where} checks[{i}]", ability, requires, predicates)
    return kind


def _digest(raw) -> str:
    """A fingerprint of what this spec ENFORCES.

    Taken over the parsed structure with `DIGEST_PROSE_FIELDS` removed at every depth, so that
    reformatting, a comment, or a reworded directive does not tell an open run its criteria moved —
    while a changed `completion`, `deps`, `gate`, `guards` or goal still does.

    Keys are sorted so an author reordering a mapping does not read as a change either. Reordering a
    LIST is preserved, because `steps:` order is the declared order and `exclusive_groups` entries
    are sets whose membership matters.
    """
    def strip(node):
        if isinstance(node, dict):
            # Sorted and compared as STRINGS, and serialised with `default=str`, because this runs
            # BEFORE the spec is validated and therefore must never raise on a malformed one. A
            # reserved YAML key is the concrete case: `on:` parses to the boolean True, and sorting
            # a mapping that mixes True with str keys is a TypeError — which would surface as a
            # crash (exit 5) where the author needs a legible refusal (exit 2). There is a test
            # pinning exactly that, and it caught this.
            return {str(k): strip(v) for k, v in sorted(node.items(), key=lambda kv: str(kv[0]))
                    if str(k) not in DIGEST_PROSE_FIELDS}
        if isinstance(node, list):
            return [strip(x) for x in node]
        return node

    canon = json.dumps(strip(raw), sort_keys=False, ensure_ascii=False,
                       separators=(",", ":"), default=str)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]


def _build(ability: str, raw: dict, digest: str, path: Path) -> Flow:
    # THE VERSION IS READ FIRST, before any key is judged.
    #
    # It used to be checked after the unknown-key rejection, and the consequence was a message
    # that lied by omission: a spec from a newer format carrying a new key was refused as
    # "unsupported key(s)" with advice about typos, and its version was never mentioned. Whoever
    # hit it was told to check their spelling. Order alone fixes that.
    major, minor = _parse_spec_version(_require(raw, "version", path), path)
    if major != SPEC_MAJOR:
        raise FlowError(
            f"{path}: spec format {major}.{minor} cannot be read; this engine reads "
            f"{spec_format()}.\n"
            f"  A different MAJOR means the structure changed, and this engine does not know\n"
            f"  what moved — reading it anyway would misinterpret it silently.\n"
            + (_MIGRATIONS.get((major, SPEC_MAJOR), ""))
            + _capability_gap(raw)
        )
    # A newer MINOR is readable: the structure is unchanged and only keys were added. Carried
    # into every key check so an unknown key can be named as a version gap rather than a typo.
    newer_by = (major, minor) if minor > SPEC_MINOR else None
    reject = functools.partial(_reject_unknown, newer_by=newer_by)

    reject(raw, TOP_KEYS, "the spec root", path)

    role = str(raw.get("role", ROLE_PRODUCTION)).strip() or ROLE_PRODUCTION
    if role not in ROLES:
        raise FlowError(
            f"{path}: role {role!r} is not one of {', '.join(ROLES)}.\n"
            f"  '{ROLE_FIXTURE}' means this flow exists to exercise the engine and is not "
            f"routable; anything else carrying work is '{ROLE_PRODUCTION}'."
        )
    when_text = str(raw.get("when", "")).strip()
    if role == ROLE_FIXTURE and when_text:
        raise FlowError(
            f"{path}: a '{ROLE_FIXTURE}' declares `when:`, which is the hint an agent reads to "
            f"REACH for an ability.\n"
            f"  A fixture is excluded from the criteria-strength report, so it must not also be "
            f"reachable — otherwise the label is a way to carry real work with weak criteria.\n"
            f"  Move the text to a comment, or drop `role: {ROLE_FIXTURE}`."
        )
    if role == ROLE_PRODUCTION and len(when_text) < 20:
        raise FlowError(
            f"{path}: no usable `when:` (needs at least 20 characters saying when to reach for "
            f"this ability).\n"
            f"  Routing has to be derivable from the abilities themselves; a hand-kept table "
            f"elsewhere is stale the moment one lands and reads authoritative while wrong.\n"
            f"  If this flow exists to exercise the engine rather than carry work, declare "
            f"`role: {ROLE_FIXTURE}` instead."
        )
    declared = _require(raw, "ability", path, str)
    if declared != ability:
        # A mismatch means the folder and the spec disagree about identity. Guessing
        # which one is authoritative would silently attach runs to the wrong ability.
        raise FlowError(
            f"{path}: spec says ability '{declared}' but it lives in folder '{ability}'"
        )

    scope_kind = _require(raw, "scope_kind", path, str)
    if not scope_kind.strip():
        raise FlowError(f"{path}: 'scope_kind' must not be empty — a run with no scope "
                        f"dimension cannot be guarded")

    # Variants must be parsed BEFORE steps, so a step naming an undeclared variant is a
    # load-time error rather than a step that silently belongs to nothing.
    var_raw = raw.get("variants") or {}
    if not isinstance(var_raw, dict):
        raise FlowError(f"{path}: 'variants' must be a mapping")
    variant_spec: dict = {}
    if var_raw:
        reject(var_raw, VARIANT_KEYS, "the 'variants' block", path)
        vals = var_raw.get("values")
        if not isinstance(vals, list) or len(vals) < 2:
            raise FlowError(
                f"{path}: variants.values must list at least TWO values — one variant is not "
                f"a variant, it is the flow"
            )
        values = tuple(_require_plain_id(v, "variants.values entry", path) for v in vals)
        if len(set(values)) != len(values):
            raise FlowError(f"{path}: variants.values repeats a value: {values}")
        default = var_raw.get("default")
        if default is None:
            raise FlowError(
                f"{path}: variants needs a 'default' — a run that resolves to nothing would "
                f"have no shape at all, and picking one implicitly hides which was picked"
            )
        default = str(default)
        if default not in values:
            raise FlowError(f"{path}: variants.default {default!r} is not in values {values}")
        variant_spec = {"values": values, "default": default,
                        "fact": str(var_raw["fact"]) if var_raw.get("fact") else None}

    res_raw = raw.get("results") or {}
    if not isinstance(res_raw, dict):
        raise FlowError(
            f"{path}: 'results' must be a mapping with 'values' and 'default' — a bare list "
            f"would make its ORDER decide the default, which is a rule nobody can see")
    result_spec: dict = {}
    if res_raw:
        reject(res_raw, RESULT_KEYS, "the 'results' block", path)
        rvals = res_raw.get("values")
        if not isinstance(rvals, list) or not rvals:
            raise FlowError(f"{path}: results.values must be a non-empty list")
        rvalues = tuple(_require_plain_id(v, "results.values entry", path) for v in rvals)
        if len(set(rvalues)) != len(rvalues):
            raise FlowError(f"{path}: results.values repeats a value: {rvalues}")
        rdefault = res_raw.get("default")
        if rdefault is None:
            raise FlowError(
                f"{path}: results needs a 'default' — a run closed without naming an outcome has "
                f"to record SOMETHING, and letting the engine pick would put a word this flow "
                f"never chose into its own history")
        rdefault = str(rdefault)
        if rdefault not in rvalues:
            raise FlowError(f"{path}: results.default {rdefault!r} is not in values {rvalues}")
        result_spec = {"values": rvalues, "default": rdefault}

    phases_raw = _require(raw, "phases", path, list)
    if not phases_raw:
        raise FlowError(f"{path}: 'phases' must declare at least one phase")
    phases: list[str] = []
    phase_titles: dict = {}
    phase_stages: dict = {}
    phase_guides: dict = {}
    phase_goals: dict = {}
    stage_guides: dict = {}
    for i, p in enumerate(phases_raw):
        if not isinstance(p, dict) or "id" not in p:
            raise FlowError(f"{path}: phases[{i}] must be a mapping with an 'id'")
        reject(p, PHASE_KEYS, f"phases[{i}]", path)
        pid = _require_plain_id(p["id"], f"phases[{i}]", path)
        if pid in phase_titles:
            raise FlowError(f"{path}: duplicate phase id '{pid}'")
        phases.append(pid)
        phase_titles[pid] = str(p.get("title", pid))
        if p.get("guide"):
            phase_guides[pid] = str(p["guide"])
        if p.get("goal") is not None:
            g = p["goal"]
            if not isinstance(g, dict) or "type" not in g:
                raise FlowError(
                    f"{path}: phase '{pid}' has a 'goal' without a 'type'. A goal is a "
                    f"completion spec — the same vocabulary a step's `completion` uses."
                )
            phase_goals[pid] = g
        stages_raw = p.get("stages") or []
        if not isinstance(stages_raw, list):
            raise FlowError(f"{path}: phase '{pid}' has 'stages' that is not a list")
        seen: list[str] = []
        for st in stages_raw:
            # A stage may be a bare id, or a mapping that also carries a guide pointer.
            # Both forms are supported so an existing flat spec keeps working.
            if isinstance(st, dict):
                reject(st, STAGE_KEYS, f"phase '{pid}' stage entry", path)
                sid_ = _require_plain_id(st.get("id"), f"phase '{pid}' stage entry", path)
                if st.get("guide"):
                    stage_guides[(pid, sid_)] = str(st["guide"])
            else:
                sid_ = _require_plain_id(st, f"phase '{pid}' stage entry", path)
            if sid_ in seen:
                raise FlowError(f"{path}: phase '{pid}' declares stage '{sid_}' twice")
            seen.append(sid_)
        phase_stages[pid] = tuple(seen)

    steps_raw = _require(raw, "steps", path, list)
    if not steps_raw:
        raise FlowError(f"{path}: 'steps' must declare at least one step")

    steps: dict = {}
    for i, s in enumerate(steps_raw):
        if not isinstance(s, dict) or "id" not in s:
            raise FlowError(f"{path}: steps[{i}] must be a mapping with an 'id'")
        reject(s, STEP_KEYS, f"step '{s['id']}'", path)
        sid = _require_plain_id(s["id"], f"steps[{i}]", path)
        if sid in steps:
            raise FlowError(f"{path}: duplicate step id '{sid}'")
        phase = str(s.get("phase", ""))
        if phase not in phase_titles:
            raise FlowError(
                f"{path}: step '{sid}' is in phase '{phase}', which is not declared"
            )
        gate = str(s.get("gate", GATE_NONE))
        if gate not in (GATE_NONE, GATE_AFFIRM) and not gate.startswith(GATE_PREAUTH_PREFIX):
            raise FlowError(
                f"{path}: step '{sid}' has gate '{gate}'; expected '{GATE_NONE}', "
                f"'{GATE_AFFIRM}', or '{GATE_PREAUTH_PREFIX}<config-key>'"
            )
        v_raw = s.get("variants") or []
        if not isinstance(v_raw, list):
            raise FlowError(f"{path}: step '{sid}' has 'variants' that is not a list")
        step_variants = tuple(str(x) for x in v_raw)
        if step_variants and not variant_spec:
            raise FlowError(
                f"{path}: step '{sid}' names variants {list(step_variants)} but the spec "
                f"declares no 'variants' block"
            )
        for v in step_variants:
            if v not in variant_spec["values"]:
                raise FlowError(
                    f"{path}: step '{sid}' names variant {v!r}, which is not declared.\n"
                    f"  declared: {', '.join(variant_spec['values'])}"
                )
        if variant_spec and len(step_variants) == len(variant_spec["values"]):
            raise FlowError(
                f"{path}: step '{sid}' lists EVERY variant — that is the shared core, so omit "
                f"'variants' instead. Listing them all means the list stops being a filter and "
                f"silently stops protecting anything when a variant is added."
            )
        completion = s.get("completion") or {"type": "attest"}
        if not isinstance(completion, dict) or "type" not in completion:
            raise FlowError(
                f"{path}: step '{sid}' has a 'completion' without a 'type'"
            )
        stage = s.get("stage")
        if stage is not None:
            stage = str(stage)
            declared = phase_stages.get(phase, ())
            if stage not in declared:
                # A typo'd stage would silently create a phantom grouping, and the
                # grouping is what goal predicates filter on. Declared-or-fatal.
                raise FlowError(
                    f"{path}: step '{sid}' is in stage '{stage}', which phase '{phase}' "
                    f"does not declare. declared: {', '.join(declared) or '(none)'}"
                )
        repeatable = bool(s.get("repeatable", False))
        budget = int(s.get("budget", 0) or 0)
        if budget and not repeatable:
            raise FlowError(
                f"{path}: step '{sid}' sets a budget but is not repeatable — a step that "
                f"runs once has nothing to budget"
            )
        if budget < 0:
            raise FlowError(f"{path}: step '{sid}' has a negative budget")
        optional = bool(s.get("optional", False))
        strict = bool(s.get("strict_witness", False))
        if strict and gate == GATE_NONE:
            raise FlowError(
                f"{path}: step '{sid}' sets strict_witness but has gate '{GATE_NONE}' — "
                f"there is no gate for it to constrain"
            )
        guide = s.get("guide")
        guide = str(guide) if guide else None
        out_raw = s.get("output")
        output = None
        if out_raw is not None:
            if not isinstance(out_raw, dict):
                raise FlowError(f"{path}: step '{sid}' has 'output' that is not a mapping")
            reject(out_raw, OUTPUT_KEYS, f"step '{sid}' output", path)
            kind = str(out_raw.get("from_evidence") or "").strip()
            if not kind:
                raise FlowError(
                    f"{path}: step '{sid}' output needs 'from_evidence' — the path it reads comes "
                    f"from a row the driver RECORDED.\n"
                    f"  A path written into the spec would be wrong on the second machine, and "
                    f"nothing in the ledger would say what was read.")
            sel = out_raw.get("select") or {}
            if not isinstance(sel, dict):
                raise FlowError(f"{path}: step '{sid}' output 'select' is not a mapping")
            reject(sel, SELECT_KEYS, f"step '{sid}' output select", path)
            if len(sel) > 1:
                raise FlowError(
                    f"{path}: step '{sid}' output selects by {', '.join(sorted(sel))} at once. "
                    f"Pick one — two selectors have no defined order, so which part was returned "
                    f"would depend on the engine rather than on the spec.")
            if "lines" in sel:
                m = re.fullmatch(r"(\d+)-(\d+)", str(sel["lines"]).strip())
                if m is None:
                    raise FlowError(
                        f"{path}: step '{sid}' output select.lines must be 'N-M' (1-based, "
                        f"inclusive); got {sel['lines']!r}")
                lo, hi = int(m.group(1)), int(m.group(2))
                if lo < 1 or hi < lo:
                    raise FlowError(
                        f"{path}: step '{sid}' output select.lines {sel['lines']!r} is not a "
                        f"range: it must start at 1 or later and not end before it starts")
            if "regex" in sel:
                try:
                    re.compile(str(sel["regex"]))
                except re.error as exc:
                    raise FlowError(f"{path}: step '{sid}' output select.regex is not a valid "
                                    f"regex: {exc}") from None
            cap = out_raw.get("max_bytes")
            if cap is not None and (not isinstance(cap, int) or isinstance(cap, bool) or cap < 1):
                raise FlowError(
                    f"{path}: step '{sid}' output max_bytes must be a positive integer; "
                    f"got {cap!r}. A cap of zero would deliver nothing while reporting success.")
            output = {"from_evidence": kind, "select": dict(sel)}
            if cap is not None:
                output["max_bytes"] = int(cap)

        prod_raw = s.get("produced_by")
        produced_by = None
        if prod_raw is not None:
            if not isinstance(prod_raw, str) or not prod_raw.strip():
                raise FlowError(
                    f"{path}: step '{sid}' has 'produced_by' that is not a non-empty string.\n"
                    f"  It names the tool that produces this step's effect — one path, nothing "
                    f"else. How to call it belongs in that tool's own header, not here."
                )
            produced_by = prod_raw.strip()

        topics_raw = s.get("topics") or []
        if not isinstance(topics_raw, list):
            raise FlowError(f"{path}: step '{sid}' has 'topics' that is not a list")
        topics = tuple(str(x) for x in topics_raw)
        if len(set(topics)) != len(topics):
            raise FlowError(f"{path}: step '{sid}' cites the same topic twice")
        deps = tuple(str(d) for d in (s.get("deps") or []))
        steps[sid] = Step(
            id=sid,
            phase=phase,
            title=str(s.get("title", sid)),
            deps=deps,
            gate=gate,
            completion=completion,
            variants=step_variants,
            directive=str(s.get("directive", "")).rstrip(),
            autonomy=str(s.get("autonomy", "agent")),
            stage=stage,
            repeatable=repeatable,
            budget=budget,
            guide=guide,
            topics=topics,
            output=output,
            produced_by=produced_by,
            optional=optional,
            strict_witness=strict,
        )

    # deps must exist, and must not cycle.
    for s in steps.values():
        for d in s.deps:
            if d not in steps:
                raise FlowError(
                    f"{path}: step '{s.id}' depends on '{d}', which is not declared"
                )
    # A dependency that does not EXIST under a variant where the depender does is a flow that
    # variant can never finish. Same class of defect as a guard pointing at a step with no gate:
    # it validates, it reads as a constraint, and it is unsatisfiable — except this one only
    # surfaces for whoever happens to run that variant.
    if variant_spec:
        for s in steps.values():
            for dep in s.deps:
                d = steps[dep]
                if not d.variants:
                    continue
                blockers = [v for v in variant_spec["values"]
                            if (not s.variants or v in s.variants) and v not in d.variants]
                if blockers:
                    raise FlowError(
                        f"{path}: step '{s.id}' depends on '{dep}', which does not exist under "
                        f"variant(s): {', '.join(blockers)}.\n"
                        f"  Under those variants '{s.id}' could never be reached, so the run "
                        f"could never close.\n"
                        f"  Either give '{s.id}' the same variant restriction, or move the "
                        f"dependency to a step in the shared core."
                    )

    order = _topo_order(steps, path)

    # `requires` is read BEFORE any reference is resolved, because it is what makes a borrowed
    # registration both loaded and legal. Read after the references, the engine would have to
    # either resolve against everything in the process — the load-order lottery namespacing
    # exists to end — or resolve twice.
    req_raw = raw.get("requires") or []
    if not isinstance(req_raw, list):
        raise FlowError(f"{path}: 'requires' must be a list of ability names")
    requires = tuple(str(x) for x in req_raw)
    for dep_ability in requires:
        if dep_ability == ability:
            raise FlowError(f"{path}: ability '{ability}' requires itself")
        if not spec_path(dep_ability).is_file():
            raise FlowError(
                f"{path}: requires ability '{dep_ability}', which is not installed.\n"
                f"  available: {', '.join(available_abilities())}"
            )
        load_extensions(dep_ability)

    # Completion predicates must be registered. Imported here to keep the module
    # import graph one-directional (predicates never import flow).
    from . import predicates
    for s in steps.values():
        try:
            kind = _resolve_completion(s.completion, f"{path}: step '{s.id}'",
                                       ability, requires, predicates)
        except registry.ResolveError as exc:
            raise FlowError(f"{path}: step '{s.id}': {exc}") from None
        try:
            predicates.validate_spec(kind, s.completion, f"{path}: step '{s.id}'")
        except ValueError as exc:
            # A malformed spec is a SPEC error (exit 2), not an internal fault. Letting the
            # ValueError escape would report the ability's mistake as the engine crashing.
            raise FlowError(str(exc)) from None
    for pid, g in phase_goals.items():
        try:
            kind = _resolve_completion(g, f"{path}: phase '{pid}' goal",
                                       ability, requires, predicates)
        except registry.ResolveError as exc:
            raise FlowError(f"{path}: phase '{pid}' goal: {exc}") from None
        try:
            predicates.validate_spec(kind, g, f"{path}: phase '{pid}' goal")
        except ValueError as exc:
            raise FlowError(str(exc)) from None
        if kind == "attest":
            # An `attest` goal accepts a layer on the agent's word alone. A per-step attest is
            # a defensible floor; a whole LAYER accepted that way is a rubber stamp on
            # everything inside it, and it reads in the log exactly like a checked one.
            raise FlowError(
                f"{path}: phase '{pid}' goal is 'attest' — a layer cannot be accepted on an "
                f"assertion. Use a criterion derived from the store (phase_steps_closed, "
                f"all_of, evidence_in, fields_agree, no_open_violations, phases_summarized)."
            )

    guards_raw = raw.get("guards") or {}
    if not isinstance(guards_raw, dict):
        raise FlowError(f"{path}: 'guards' must be a mapping of action -> step id")
    guards: dict = {}
    guard_matches: dict = {}
    for action, spec in guards_raw.items():
        # Two forms. `action: STEP` is the original and stays valid — it declares a guard that
        # only an explicit caller can consult. The mapping form adds `matches`, which is what
        # lets a runtime recognise the action in a tool call BEFORE making it, without the
        # engine learning what the tool call means.
        if isinstance(spec, dict):
            reject(spec, GUARD_KEYS, f"guard '{action}'", path)
            sid = str(spec.get("step") or "")
            rules = spec.get("matches") or []
            if not isinstance(rules, list):
                raise FlowError(f"{path}: guard '{action}' has 'matches' that is not a list")
            compiled = []
            for j, rule in enumerate(rules):
                if not isinstance(rule, dict):
                    raise FlowError(
                        f"{path}: guard '{action}' matches[{j}] must be a mapping "
                        f"with 'tool' and 'pattern'")
                reject(rule, GUARD_MATCH_KEYS, f"guard '{action}' matches[{j}]", path)
                tool = str(rule.get("tool") or "").strip()
                pat = str(rule.get("pattern") or "")
                if not tool:
                    raise FlowError(
                        f"{path}: guard '{action}' matches[{j}] has no 'tool' — a rule that "
                        f"matches every tool would fire on unrelated calls")
                if not pat:
                    raise FlowError(
                        f"{path}: guard '{action}' matches[{j}] has no 'pattern' — a rule with "
                        f"no pattern would block every call to that tool")
                try:
                    rx = re.compile(pat)
                except re.error as exc:
                    raise FlowError(
                        f"{path}: guard '{action}' matches[{j}] pattern is not a valid "
                        f"regex: {exc}") from None
                compiled.append({"tool": tool,
                                 "field": str(rule["field"]) if rule.get("field") else None,
                                 "regex": rx, "pattern": pat})
            if compiled:
                guard_matches[str(action)] = tuple(compiled)
        else:
            sid = str(spec)
        if sid not in steps:
            raise FlowError(
                f"{path}: guard '{action}' points at step '{sid}', which is not declared"
            )
        if steps[sid].gate == GATE_NONE:
            # A guard whose step has no gate can never refuse anything. Silently
            # allowing it would ship a guard that looks protective and is inert.
            raise FlowError(
                f"{path}: guard '{action}' points at step '{sid}', whose gate is "
                f"'{GATE_NONE}' — such a guard could never refuse and is a false promise"
            )
        guards[str(action)] = sid

    groups_raw = raw.get("exclusive_groups") or []
    if not isinstance(groups_raw, list):
        raise FlowError(f"{path}: 'exclusive_groups' must be a list of lists")
    groups: list[tuple[str, ...]] = []
    member_of: dict = {}
    for i, grp in enumerate(groups_raw):
        if not isinstance(grp, list) or len(grp) < 2:
            raise FlowError(
                f"{path}: exclusive_groups[{i}] must list at least 2 step ids "
                f"(a group of one excludes nothing)"
            )
        members = tuple(str(g) for g in grp)
        for mid in members:
            if mid not in steps:
                raise FlowError(
                    f"{path}: exclusive_groups[{i}] names step '{mid}', which is not declared"
                )
            if mid in member_of:
                raise FlowError(
                    f"{path}: step '{mid}' is in two exclusive groups; which one wins "
                    f"would be arbitrary"
                )
            member_of[mid] = members
        # A dependency on one specific member is a contradiction: that member may be the
        # branch that never runs, so the dependent could be permanently unreachable.
        for s in steps.values():
            if s.id in members:
                continue
            hit = [d for d in s.deps if d in members]
            if len(hit) == 1:
                raise FlowError(
                    f"{path}: step '{s.id}' depends on '{hit[0]}', which is one branch of "
                    f"an exclusive group — if the other branch is taken, '{s.id}' can never "
                    f"run. Depend on a step after the branches rejoin instead."
                )
        groups.append(members)

    prose_raw = raw.get("prose") or {}
    if not isinstance(prose_raw, dict):
        raise FlowError(f"{path}: 'prose' must be a mapping")
    reject(prose_raw, PROSE_KEYS, "the 'prose' block", path)
    prose_root = None
    if prose_raw.get("root"):
        prose_root = (path.parent / str(prose_raw["root"])).resolve()
        if not prose_root.is_dir():
            raise FlowError(
                f"{path}: prose.root '{prose_raw['root']}' is not a directory "
                f"(resolved to {prose_root})"
            )
        # Refuse to reach outside the ability. An ability must be a movable unit: if
        # `cp -r abilities/<name>` elsewhere does not still work, abilities are not
        # really pluggable. The same reasoning that keeps the engine's store out of a
        # consumer's tree keeps an ability's prose inside its own.
        if not str(prose_root).startswith(str(path.parent.resolve())):
            raise FlowError(
                f"{path}: prose.root escapes the ability directory. Prose must live "
                f"inside abilities/{ability}/ so the ability stays self-contained."
            )
    topics_dir = str(prose_raw.get("topics_dir", "topics"))
    anchor_pattern = str(prose_raw.get("anchor_pattern", r"^#{1,6}\s.*\b{step_id}\b"))
    topic_ref_pattern = str(prose_raw.get("topic_ref_pattern",
                                         r"topic\s+`(?P<name>[A-Za-z0-9][\w-]*)`"))
    if "(?P<name>" not in topic_ref_pattern:
        raise FlowError(
            f"{path}: prose.topic_ref_pattern must capture a group named 'name' — that group is "
            f"what names the topic being pointed at.\n"
            f"  got: {topic_ref_pattern}"
        )
    try:
        re.compile(topic_ref_pattern)
    except re.error as exc:
        raise FlowError(f"{path}: prose.topic_ref_pattern is not a valid regex: {exc}") from exc
    if "{step_id}" not in anchor_pattern:
        raise FlowError(
            f"{path}: prose.anchor_pattern must contain the placeholder '{{step_id}}' — "
            f"without it every step would resolve to the same section"
        )

    # Facts + hooks. Parsed after steps/phases so selectors can be checked against them.
    from . import facts as factsmod
    from . import hooks as hooksmod

    # Extensions from ANOTHER ability, declared. Two abilities legitimately sharing one source
    # of truth is the case this exists for: the reference system has two skills reading ONE
    # registry, and the whole value of that registry is being singular. Copying it per ability
    # would reintroduce exactly the drift it was built to remove, and an IMPLICIT cross-ability
    # import would make a spec's references resolve or not depending on which ability happened
    # to load first. So it is declared — visible, checked, and it states what to bring along
    # when the ability moves.
    facts_raw = raw.get("facts") or {}
    if not isinstance(facts_raw, dict):
        raise FlowError(f"{path}: 'facts' must be a mapping")
    reject(facts_raw, FACTS_KEYS, "the 'facts' block", path)
    # One provider (`provider: x`) or several (`providers: [x, y]`). Several is the normal
    # case for a mature flow: its facts come from genuinely different sources — what changed,
    # what an analysis computed, what an external system reports — and forcing them through one
    # provider would either build a god-provider or leave the others unreachable.
    if "provider" in facts_raw and "providers" in facts_raw:
        raise FlowError(f"{path}: facts declares both 'provider' and 'providers' — pick one")
    if "providers" in facts_raw:
        plist = facts_raw["providers"]
        if not isinstance(plist, list) or not plist:
            raise FlowError(f"{path}: facts.providers must be a non-empty list")
        facts_providers = tuple(str(x) for x in plist)
    else:
        facts_providers = (str(facts_raw.get("provider", "static")),)
    if len(set(facts_providers)) != len(facts_providers):
        raise FlowError(f"{path}: facts.providers lists a provider twice: {facts_providers}")
    facts_schema: dict = {}
    owner: dict = {}
    resolved_providers = []
    for ref in facts_providers:
        try:
            resolved_providers.append(factsmod.resolve(ref, asking=ability, requires=requires))
        except registry.ResolveError as exc:
            raise FlowError(
                f"{path}: facts provider '{ref}': {exc}\n"
                f"  A provider is python in the engine's registry, not code in this file."
            ) from None
    facts_providers = tuple(resolved_providers)
    for pn in facts_providers:
        for k, t in factsmod.schema_of(pn).items():
            if k in owner:
                raise FlowError(
                    f"{path}: fact '{k}' is declared by both '{owner[k]}' and '{pn}'.\n"
                    f"  A condition names a fact, not a provider, so a collision has no "
                    f"defined winner — rename one of them."
                )
            owner[k] = pn
            facts_schema[k] = t
    # ENGINE-OWNED FACT. A condition needs to be able to ask "which shape is this run",
    # and no provider can answer that — the variant lives in the run row, not in the world.
    # Without it, an ability whose variant is NOT derivable from its scope has no honest way
    # to condition on the variant at all, and the tempting substitute is a provider fact that
    # happens to be named similarly and is quietly wrong. (Observed: a hook reading a derived
    # 'executor' fact reported the DEFAULT while the run was on the other variant, because the
    # derivation had been fed an input it was never meant to read.)
    if variant_spec:
        if "run_variant" in owner:
            raise FlowError(
                f"{path}: provider '{owner['run_variant']}' declares a fact named "
                f"'run_variant', which the engine owns. Rename it — a condition asking for "
                f"the run's shape must get the engine's answer, not a provider's."
            )
        facts_schema["run_variant"] = "str"

    # A completion criterion that consults a DERIVED fact is validated against the same
    # schema, with the same validator, as a hook condition. One vocabulary: a criterion and a
    # hook asking "is there a blocking finding" must not be able to mean different things, and
    # a fact renamed in a provider has to break BOTH at load time or it breaks one at runtime.
    def _check_fact_conditions(spec: dict, where: str) -> None:
        t = str(spec.get("type"))
        if t == "all_checks":
            for i, sub in enumerate(spec.get("checks") or []):
                if isinstance(sub, dict):
                    _check_fact_conditions(sub, f"{where} check[{i}]")
            return
        if t != "claim_corroborated":
            return
        claims = spec.get("claims")
        if not isinstance(claims, list) or not claims:
            raise FlowError(
                f"{path}: {where}: claim_corroborated needs a non-empty 'claims' list — "
                f"which recorded values assert that nothing was found."
            )
        try:
            conditionsmod.validate(spec.get("disproved_when"), facts_schema,
                                   f"{path}: {where} disproved_when",
                                   asking=ability, requires=requires)
        except ConditionErrorAlias as exc:
            raise FlowError(str(exc)) from None
        # NO SEPARATE "references at least one fact" CHECK. It was written and then removed:
        # `conditions.validate` already refuses every tree that could reference none — a leaf
        # must carry `fact`, and a combinator must be non-empty — so the extra clause could not
        # change any outcome. A guard that cannot fire is the thing this engine spends its
        # refusals on elsewhere; keeping one here because it reads carefully would be worse
        # than not having it, since it would look like the unfalsifiable case was covered by
        # something of its own.

    for s_ in steps.values():
        _check_fact_conditions(s_.completion, f"step '{s_.id}'")
    for pid, g in phase_goals.items():
        _check_fact_conditions(g, f"phase '{pid}' goal")

    hooks_raw = raw.get("hooks") or []
    try:
        hook_list = hooksmod.parse(hooks_raw, steps, tuple(phases), facts_schema,
                                   reject, path,
                                   asking=ability, requires=requires)
    except (hooksmod.HookError, ConditionErrorAlias) as exc:
        raise FlowError(str(exc)) from None

    config_defaults = raw.get("config") or {}
    if not isinstance(config_defaults, dict):
        raise FlowError(f"{path}: 'config' must be a mapping")

    scope_match = str(raw.get("scope_match", "exact"))
    if scope_match not in SCOPE_MATCH_MODES:
        raise FlowError(
            f"{path}: scope_match must be one of {', '.join(SCOPE_MATCH_MODES)}, "
            f"got {scope_match!r}")

    built = Flow(
        when=str(raw.get("when", "")).strip(),
        role=role,
        requires=requires,
        ability=ability,
        title=str(raw.get("title", ability)),
        scope_kind=scope_kind,
        phases=tuple(phases),
        phase_titles=phase_titles,
        phase_stages=phase_stages,
        steps=steps,
        guards=guards,
        guard_matches=guard_matches,
        scope_match=scope_match,
        config_defaults=config_defaults,
        exclusive_groups=tuple(groups),
        variant_spec=variant_spec,
        result_spec=result_spec,
        facts_providers=facts_providers,
        facts_owner=owner,
        facts_schema=facts_schema,
        hooks=hook_list,
        prose_root=prose_root,
        topics_dir=topics_dir,
        anchor_pattern=anchor_pattern,
        topic_ref_pattern=topic_ref_pattern,
        phase_goals=phase_goals,
        phase_guides=phase_guides,
        stage_guides=stage_guides,
        digest=digest,
        source=path,
        order=order,
    )
    _check_uses(built, raw.get("uses"), path)
    return built


# WHAT A FLOW DECLARES IT RELIES ON, AND WHY IT IS A CLAIM RATHER THAN A SWITCH
#
# `uses:` names the engine mechanisms a flow actually exercises. It is deliberately NOT a
# feature switch. A mechanism that can be turned off is not enforcement: the value of the step
# ledger, the violation record and the evidence rows is precisely that nothing can opt out of
# them, and a flow able to declare `state: off` would be issuing itself a free pass. This
# repository already settled the same question once, for `role:` — a label buys no exemption
# there, and the two constraints pull in opposite directions so that neither role is the cheap
# one. The secondary cost is arithmetic: N switches are 2**N configurations, and the tests
# cover one of them.
#
# So what it buys instead:
#   * a load-time cross-check in BOTH directions — declared but unused is the "announced and
#     never read" shape this codebase keeps deleting, and used but undeclared means the list is
#     not the surface it claims to be;
#   * a visible minimum for whoever writes the next flow: what must be provided, what can be
#     left out;
#   * a version contract that names what is missing. A flow declaring a mechanism this engine
#     does not implement is refused BY NAME, which is a far more useful answer than comparing
#     two version numbers — it says which capability is absent.
#
# Each entry must be DETECTABLE from the loaded flow. A capability the engine cannot see in a
# spec could be declared falsely and nothing would notice, and an undetectable entry is exactly
# the unfalsifiable claim the cross-check exists to prevent.
ENGINE_CAPABILITIES: dict = {
    "gates": lambda f: any(st.gate != GATE_NONE for st in f.steps.values()),
    "guards": lambda f: bool(f.guards),
    # NOT `facts_providers`: that tuple is never empty — a flow saying nothing still gets
    # the default provider, so testing it reported every flow as using facts, including
    # one with no facts block at all. The schema is what "this flow has facts to read"
    # actually means, and a detector that cannot tell DECLARED from DEFAULTED makes the
    # whole cross-check demand a declaration for something nobody opted into.
    "facts": lambda f: bool(f.facts_schema),
    # A flow whose steps point at the tools that produce their effects. Detected from the steps
    # rather than from a top-level block, because the pointer belongs to the step that owes the
    # effect — and `uses:` then makes the declaration bidirectional like every other capability.
    "producers": lambda f: any(st.produced_by for st in f.steps.values()),
    "variants": lambda f: bool(f.variant_spec),
    "hooks": lambda f: bool(f.hooks),
    "obligations": lambda f: any(h.obligation for h in f.hooks),
    "hook_commands": lambda f: any(h.mode == "command" for h in f.hooks),
    "prose": lambda f: f.prose_root is not None,
    "stages": lambda f: any(f.phase_stages.values()),
    "phase_goals": lambda f: bool(f.phase_goals),
    "exclusive_groups": lambda f: bool(f.exclusive_groups),
    "repeatable": lambda f: any(st.repeatable for st in f.steps.values()),
    "optional_steps": lambda f: any(st.optional for st in f.steps.values()),
    "ability_deps": lambda f: bool(f.requires),
    "outputs": lambda f: any(st.output for st in f.steps.values()),
    "results": lambda f: bool(f.result_spec),
}


def capabilities_used(f: "Flow") -> tuple[str, ...]:
    """Which mechanisms this flow actually exercises, read off the flow itself."""
    return tuple(name for name, seen in ENGINE_CAPABILITIES.items() if seen(f))


def _check_uses(f: "Flow", declared_raw, path: Path) -> tuple[str, ...]:
    """Cross-check `uses:` against what the flow does. Both directions are fatal.

    Silence is tolerated only for a fixture: those exist to exercise the engine, so making each
    one enumerate the machinery it pokes is churn with no reader. A `production` flow is the
    thing a consumer picks up, so its surface has to be stated — the same reasoning that makes
    `when:` mandatory there. And a fixture that DOES declare the list is held to it, because a
    declaration left to rot is worse than none.
    """
    if declared_raw is None:
        declared: tuple[str, ...] = ()
        stated = False
    else:
        if not isinstance(declared_raw, list):
            raise FlowError(f"{path}: 'uses' must be a list of engine capability names")
        declared = tuple(str(x).strip() for x in declared_raw)
        stated = True
    unknown = [d for d in declared if d not in ENGINE_CAPABILITIES]
    if unknown:
        raise FlowError(
            f"{path}: declares capabilit{'y' if len(unknown) == 1 else 'ies'} "
            f"{', '.join(repr(u) for u in unknown)}, which this engine does not implement.\n"
            f"  known: {', '.join(ENGINE_CAPABILITIES)}\n"
            f"  Either the name is wrong, or this flow was written for a NEWER engine than "
            f"this one — that is what the list is for."
        )
    if f.role != ROLE_PRODUCTION and not stated:
        return declared
    used = set(capabilities_used(f))
    over = sorted(set(declared) - used)
    under = sorted(used - set(declared))
    if over:
        raise FlowError(
            f"{path}: declares {', '.join(repr(o) for o in over)} in 'uses', and nothing in "
            f"this flow exercises it.\n"
            f"  A declaration nothing reads is the shape that rots first. Remove it, or use it."
        )
    if under:
        raise FlowError(
            f"{path}: exercises {', '.join(repr(u) for u in under)} without declaring it in "
            f"'uses'.\n"
            f"  uses: [{', '.join(sorted(used))}]\n"
            f"  An incomplete list is worse than none: it reads as a complete surface."
        )
    return declared


def _topo_order(steps: dict, path: Path) -> tuple[str, ...]:
    """Kahn, with declaration order as the tie-break so a run is reproducible."""
    indegree = {sid: 0 for sid in steps}
    dependents: dict = {sid: [] for sid in steps}
    for s in steps.values():
        for d in s.deps:
            indegree[s.id] += 1
            dependents[d].append(s.id)

    ready = [sid for sid in steps if indegree[sid] == 0]
    out: list[str] = []
    while ready:
        ready.sort(key=lambda sid: list(steps).index(sid))
        sid = ready.pop(0)
        out.append(sid)
        for nxt in dependents[sid]:
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                ready.append(nxt)

    if len(out) != len(steps):
        stuck = sorted(set(steps) - set(out))
        raise FlowError(
            f"{path}: dependency cycle among steps {', '.join(stuck)}"
        )
    return tuple(out)
