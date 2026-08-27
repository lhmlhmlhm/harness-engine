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

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import yaml

ENGINE_DIR = Path(__file__).resolve().parent
REPO_ROOT = ENGINE_DIR.parent
ABILITIES_DIR = REPO_ROOT / "abilities"

SPEC_VERSION = 1

# A gate policy says what authorises passing a step.
#   none          — no gate; closing the step is enough
#   affirm        — a human must affirm, proven by an out-of-band signal
#   preauth:<key> — a config key may stand in for the human, if it is truthy
GATE_NONE = "none"
GATE_AFFIRM = "affirm"
GATE_PREAUTH_PREFIX = "preauth:"


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
    # A step that may legitimately run more than once — a fix/re-check cycle.
    repeatable: bool = False
    # How many attempts the budget allows. 0 = unlimited.
    budget: int = 0
    # WHAT HAPPENS WHEN THE BUDGET RUNS OUT — declared by the flow, never decided by the
    # engine. Introducing a counter forces this question ("refuse? escalate? carry on?")
    # and answering it in the engine would bake one ability's answer into everyone's:
    #   refuse   — the step cannot be entered again (exit 3); the run is stuck by design
    #   escalate — further attempts require a human gate on this step
    # Both are legitimate; which is right depends on whether exceeding the budget means
    # "this is broken" or "a person should look".
    on_exhausted: str = "refuse"
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
    # Abilities whose extension registrations this one relies on. Declared so a shared source
    # of truth stays singular without an ability secretly depending on load order.
    requires: tuple[str, ...]
    # Declared variants: {"values": (...), "default": str, "fact": str | None}. Empty when the
    # ability has one shape.
    variant_spec: dict
    facts_providers: tuple[str, ...]
    facts_owner: dict   # fact name -> the provider that supplies it
    facts_schema: dict
    hooks: tuple
    prose_root: Path | None
    topics_dir: str
    anchor_pattern: str
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
            # No early-out for an empty payload: `{}` serialises to a string the scope key
            # cannot appear in, so the search below already answers "not mine". A guard clause
            # there looked defensive and could not change any outcome — which is the same
            # inert-but-declared shape this engine exists to remove, so it is not written.
            import json as _json
            hay = _json.dumps(payload or {}, ensure_ascii=False, sort_keys=True)
            # Whole-token, so a scope of `CR-123` does not claim `CR-1234`.
            return re.search(rf"(?<![A-Za-z0-9_-]){re.escape(str(scope_key))}"
                             rf"(?![A-Za-z0-9_-])", hay) is not None
        if self.scope_match == "exact":
            return scope_key == where
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
             "autonomy", "optional", "strict_witness", "guide", "topics",
             "repeatable", "budget", "on_exhausted"}
TOP_KEYS = {"when", "scope_match", "requires", "variants", "version", "ability", "title", "scope_kind", "phases", "steps", "guards",
            "config", "exclusive_groups", "prose", "facts", "hooks"}
PHASE_KEYS = {"id", "title", "stages", "guide", "goal"}
STAGE_KEYS = {"id", "guide"}
PROSE_KEYS = {"root", "topics_dir", "anchor_pattern"}
FACTS_KEYS = {"provider", "providers"}
VARIANT_KEYS = {"values", "default", "fact"}
GUARD_KEYS = {"step", "matches"}
GUARD_MATCH_KEYS = {"tool", "field", "pattern"}
SCOPE_MATCH_MODES = ("exact", "path_prefix", "in_payload")


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


def _reject_unknown(mapping: dict, allowed: set, where: str, path: Path) -> None:
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
    if unknown:
        raise FlowError(
            f"{path}: {where} has unsupported key(s): {', '.join(unknown)}\n"
            f"  supported: {', '.join(sorted(allowed))}\n"
            f"  A typo here would silently change what this spec means, so it is fatal.\n"
            f"  If you need a construct the engine lacks, that is an engine gap — put it\n"
            f"  in a comment and raise it, rather than a key that does nothing."
        )


def available_abilities() -> list[str]:
    if not ABILITIES_DIR.is_dir():
        return []
    return sorted(
        p.name for p in ABILITIES_DIR.iterdir()
        if p.is_dir() and (p / "flow.yaml").is_file()
    )


def spec_path(ability: str) -> Path:
    return ABILITIES_DIR / ability / "flow.yaml"


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
    mod_path = ABILITIES_DIR / ability / "providers.py"
    _EXTENSIONS_LOADED.add(ability)
    if not mod_path.is_file():
        return
    import importlib.util
    spec = importlib.util.spec_from_file_location(f"_ability_{ability}_providers", mod_path)
    if spec is None or spec.loader is None:
        raise FlowError(f"{mod_path}: cannot be loaded as a python module")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise FlowError(
            f"{mod_path}: failed to import — {type(exc).__name__}: {exc}\n"
            f"  An ability's registry extensions must import cleanly, or its spec's "
            f"references to them would resolve unpredictably."
        ) from None


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
    digest = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()[:16]
    try:
        raw = yaml.safe_load(raw_text) or {}
    except yaml.YAMLError as exc:
        raise FlowError(f"{path}: not valid YAML — {exc}") from None
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


def _build(ability: str, raw: dict, digest: str, path: Path) -> Flow:
    _reject_unknown(raw, TOP_KEYS, "the spec root", path)
    version = _require(raw, "version", path)
    if version != SPEC_VERSION:
        raise FlowError(
            f"{path}: spec version {version!r} unsupported (engine speaks {SPEC_VERSION})"
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
        _reject_unknown(var_raw, VARIANT_KEYS, "the 'variants' block", path)
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
        _reject_unknown(p, PHASE_KEYS, f"phases[{i}]", path)
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
                _reject_unknown(st, STAGE_KEYS, f"phase '{pid}' stage entry", path)
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
        _reject_unknown(s, STEP_KEYS, f"step '{s['id']}'", path)
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
        on_exhausted = str(s.get("on_exhausted", "refuse"))
        if on_exhausted not in ("refuse", "escalate"):
            raise FlowError(
                f"{path}: step '{sid}' has on_exhausted='{on_exhausted}'; "
                f"expected 'refuse' or 'escalate'"
            )
        if budget and not repeatable:
            raise FlowError(
                f"{path}: step '{sid}' sets a budget but is not repeatable — a step that "
                f"runs once has nothing to budget"
            )
        if budget < 0:
            raise FlowError(f"{path}: step '{sid}' has a negative budget")
        if on_exhausted == "escalate" and budget and s.get("gate", GATE_NONE) == GATE_NONE:
            # Escalation means "a human must approve further attempts", which needs
            # somewhere to record that approval.
            raise FlowError(
                f"{path}: step '{sid}' escalates on exhaustion but declares no gate — "
                f"there would be nowhere to record the human's decision"
            )
        optional = bool(s.get("optional", False))
        strict = bool(s.get("strict_witness", False))
        if strict and gate == GATE_NONE:
            raise FlowError(
                f"{path}: step '{sid}' sets strict_witness but has gate '{GATE_NONE}' — "
                f"there is no gate for it to constrain"
            )
        guide = s.get("guide")
        guide = str(guide) if guide else None
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
            on_exhausted=on_exhausted,
            guide=guide,
            topics=topics,
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

    # Completion predicates must be registered. Imported here to keep the module
    # import graph one-directional (predicates never import flow).
    from . import predicates
    for s in steps.values():
        kind = str(s.completion.get("type"))
        if not predicates.is_registered(kind):
            raise FlowError(
                f"{path}: step '{s.id}' wants completion type '{kind}', which the engine "
                f"does not implement. registered: {', '.join(predicates.registered())}"
            )
        try:
            predicates.validate_spec(kind, s.completion, f"{path}: step '{s.id}'")
        except ValueError as exc:
            # A malformed spec is a SPEC error (exit 2), not an internal fault. Letting the
            # ValueError escape would report the ability's mistake as the engine crashing.
            raise FlowError(str(exc)) from None
    for pid, g in phase_goals.items():
        kind = str(g.get("type"))
        if not predicates.is_registered(kind):
            raise FlowError(
                f"{path}: phase '{pid}' goal wants type '{kind}', which the engine does not "
                f"implement. registered: {', '.join(predicates.registered())}"
            )
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
            _reject_unknown(spec, GUARD_KEYS, f"guard '{action}'", path)
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
                _reject_unknown(rule, GUARD_MATCH_KEYS, f"guard '{action}' matches[{j}]", path)
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
    _reject_unknown(prose_raw, PROSE_KEYS, "the 'prose' block", path)
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
    req_raw = raw.get("requires") or []
    if not isinstance(req_raw, list):
        raise FlowError(f"{path}: 'requires' must be a list of ability names")
    requires = tuple(str(x) for x in req_raw)
    for dep_ability in requires:
        if dep_ability == ability:
            raise FlowError(f"{path}: ability '{ability}' requires itself")
        if not (ABILITIES_DIR / dep_ability / "flow.yaml").exists():
            raise FlowError(
                f"{path}: requires ability '{dep_ability}', which is not installed.\n"
                f"  available: {', '.join(available_abilities())}"
            )
        load_extensions(dep_ability)

    facts_raw = raw.get("facts") or {}
    if not isinstance(facts_raw, dict):
        raise FlowError(f"{path}: 'facts' must be a mapping")
    _reject_unknown(facts_raw, FACTS_KEYS, "the 'facts' block", path)
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
    for pn in facts_providers:
        if not factsmod.is_registered(pn):
            raise FlowError(
                f"{path}: facts provider '{pn}' is not registered.\n"
                f"  available: {', '.join(factsmod.registered())}\n"
                f"  A provider is python in the engine's registry, not code in this file."
            )
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

    hooks_raw = raw.get("hooks") or []
    try:
        hook_list = hooksmod.parse(hooks_raw, steps, tuple(phases), facts_schema,
                                   _reject_unknown, path)
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

    return Flow(
        when=str(raw.get("when", "")).strip(),
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
        facts_providers=facts_providers,
        facts_owner=owner,
        facts_schema=facts_schema,
        hooks=hook_list,
        prose_root=prose_root,
        topics_dir=topics_dir,
        anchor_pattern=anchor_pattern,
        phase_goals=phase_goals,
        phase_guides=phase_guides,
        stage_guides=stage_guides,
        digest=digest,
        source=path,
        order=order,
    )


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
