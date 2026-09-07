"""Predicates — the completion-check extension point.

A step is not "done" because an agent said so. It is done when a registered predicate
returns true against the store. The flow spec picks the predicate by name; this module
is the only place their logic lives.

This is the seam that decides how much of a flow is machine-checked. A prior system in
this space registered completion predicates for 11 of its ~100 steps and let the rest
fall back to prose instructions — and its own notes record the result: steps that
"every agent 'did'" while silently finding nothing and falling through to a default.
So the registry is deliberately small and each entry is cheap to satisfy honestly,
making it reasonable for an ability to declare one on EVERY step.

Contract for a predicate:
    check(conn, run_id, step, spec) -> (ok: bool, why: str)
`why` is shown to the caller on refusal and must name what is missing, not just say no.

Adding one is additive — no engine surgery, no ability knowledge. Keep them
domain-free: a predicate may know about "evidence of kind K exists", never about
what K means to an ability.
"""
from __future__ import annotations

from . import registry

from typing import Callable

from . import conditions, store

_REGISTRY: dict[str, dict] = {}
# Who claimed each name, so a collision can name BOTH sides rather than only the loser.
_OWNERS: dict[str, str] = {}


def predicate(name: str, *, requires: tuple[str, ...] = (),
              needs_facts: bool = False) -> Callable:
    """Register a completion predicate under `name`.

    `requires` names the spec keys the flow must supply, checked at flow-load time so
    a typo in an ability's yaml is a startup error rather than a mid-run surprise.

    `needs_facts` marks a predicate that must consult DERIVED facts, not only what was
    recorded. Declared rather than assumed, for two reasons: the caller passes the fact
    resolver only to predicates that asked for it, so gathering stays on-demand (a
    provider is a subprocess; running every one to evaluate a criterion that never looks
    at facts would make reading a step's status expensive), and flow-load can insist that
    an ability using such a predicate actually declares a provider for the fact.
    """
    def deco(fn: Callable) -> Callable:
        key = registry.claim("completion predicate", name, _REGISTRY, _OWNERS, fn)
        _REGISTRY[key] = {"fn": fn, "requires": requires, "needs_facts": needs_facts}
        return fn
    return deco


def resolve(ref: str, *, asking: str | None, requires: tuple[str, ...] = ()) -> str:
    """A spec's completion type -> a registry key. Raises registry.ResolveError."""
    return registry.resolve("completion predicate", ref, _REGISTRY,
                            asking=asking, requires=requires)


def visible(owner: str | None) -> list[str]:
    return registry.visible(_REGISTRY, owner)


def needs_facts(name: str) -> bool:
    entry = _REGISTRY.get(name)
    return bool(entry and entry["needs_facts"])


def is_registered(name: str) -> bool:
    return name in _REGISTRY


def registered() -> list[str]:
    return sorted(_REGISTRY)


def validate_spec(name: str, spec: dict, where: str) -> None:
    entry = _REGISTRY[name]
    for key in entry["requires"]:
        if key not in spec:
            raise ValueError(
                f"{where}: completion type '{name}' requires key '{key}'; "
                f"got keys {sorted(spec)}"
            )
    # Recurse into a composite's branches. Without this a typo inside a nested check would
    # survive load time and only surface as a mid-run failure — and the whole point of
    # validating specs up front is that an ability's mistakes are startup errors.
    for i, sub in enumerate(spec.get("checks") or []):
        if not isinstance(sub, dict) or "type" not in sub:
            raise ValueError(f"{where}: checks[{i}] is not a completion spec (needs 'type')")
        sub_name = str(sub["type"])
        if sub_name not in _REGISTRY:
            raise ValueError(
                f"{where}: checks[{i}] wants type '{sub_name}', which the engine does not "
                f"implement. registered: {', '.join(registered())}"
            )
        if sub_name == "all_checks":
            raise ValueError(
                f"{where}: checks[{i}] nests another 'all_checks'. Flatten it — a conjunction "
                f"of conjunctions reads as depth where there is none."
            )
        validate_spec(sub_name, sub, f"{where} checks[{i}]")


def check(conn, run_id: str, step, spec: dict, facts_fn=None) -> tuple[bool, str]:
    """Evaluate a completion spec. `facts_fn` is a zero-arg callable returning derived facts.

    Lazy on purpose: it is only ever CALLED by a predicate that declared `needs_facts`, so
    reading the status of a step whose criterion never looks at the world costs no
    subprocesses. A predicate that needs facts and is handed no resolver refuses — see
    `_claim_corroborated`; treating the absence of a resolver as "no contradiction found"
    is the exact shape of hole this predicate exists to close.
    """
    entry = _REGISTRY.get(str(spec.get("type")))
    if entry is None:  # unreachable: flow.load validates the type up front
        return False, f"no predicate for completion type {spec.get('type')!r}"
    if entry["needs_facts"]:
        return entry["fn"](conn, run_id, step, spec, facts_fn)
    return entry["fn"](conn, run_id, step, spec)


# ------------------------------------------------------------------ predicates

@predicate("attest")
def _attest(conn, run_id, step, spec) -> tuple[bool, str]:
    """Weakest form: the caller asserts completion and the assertion is logged.

    Honest about what it is — an attribution, not a check. Use it only for steps whose
    output has no machine-visible trace. If an ability's whole flow is `attest`, the
    engine is a ledger, not a guard, and its yaml should say so out loud.
    """
    return True, "attested"


@predicate("evidence", requires=("kind",))
def _evidence(conn, run_id, step, spec) -> tuple[bool, str]:
    """Done when at least one evidence row of `kind` exists for this step.

    Optional `min_count` (default 1) and `match` (substring the value must contain).
    """
    kind = str(spec["kind"])
    want = int(spec.get("min_count", 1))
    match = spec.get("match")
    rows = store.find_evidence(conn, run_id, step.evidence_scope, kind)
    if match is not None:
        rows = [r for r in rows if r["value"] and str(match) in r["value"]]
    if len(rows) >= want:
        return True, f"{len(rows)} evidence row(s) of kind '{kind}'"
    suffix = f" containing {match!r}" if match is not None else ""
    where = "anywhere in the run" if step.evidence_scope is None else f"on '{step.id}'"
    return False, (
        f"needs {want} evidence row(s) of kind '{kind}'{suffix} {where}, found {len(rows)}\n"
        f"    record one with: harness evidence --run {run_id} --step <step> "
        f"--kind {kind} --value <...>"
    )


@predicate("gate_recorded")
def _gate_recorded(conn, run_id, step, spec) -> tuple[bool, str]:
    """Done when this step's own gate has been answered affirmatively.

    Lets an ability express "this step IS its gate" without the engine hardcoding
    which steps those are.
    """
    row = store.get_gate(conn, run_id, step.id)
    if row is None:
        return False, (
            f"gate for '{step.id}' not recorded\n"
            f"    record it with: harness gate --run {run_id} --step {step.id} "
            f"--decision affirm --evidence <...>"
        )
    if row["decision"] == "decline":
        return False, f"gate for '{step.id}' was declined — the step did not pass"
    return True, f"gate recorded ({row['decision']})"


@predicate("all_of", requires=("steps",))
def _all_of(conn, run_id, step, spec) -> tuple[bool, str]:
    """Done when every named step is closed, and each `any_of` group has one closed.

    `steps`  — every one must be closed (may be empty when only groups matter)
    `any_of` — a list of groups; each group needs at least ONE closed member

    The groups exist because a real acceptance rule is rarely a flat conjunction: "the
    test step OR the exemption step" is one requirement satisfied two ways, and flattening
    it into `steps` would demand both — which no run can do, since taking one path
    precludes the other.
    """
    want = [str(s) for s in (spec["steps"] or [])]
    groups = [[str(x) for x in g] for g in (spec.get("any_of") or [])]
    done = store.closed_steps(conn, run_id)

    missing = [s for s in want if s not in done]
    unmet = [g for g in groups if not any(x in done for x in g)]
    if not missing and not unmet:
        parts = []
        if want:
            parts.append(f"{len(want)} required step(s) closed")
        if groups:
            parts.append(f"{len(groups)} alternative group(s) satisfied")
        return True, "; ".join(parts) or "nothing required"
    why = []
    if missing:
        why.append(f"still open: {', '.join(missing)}")
    for g in unmet:
        why.append(f"none of [{', '.join(g)}] is closed (one is enough)")
    return False, "; ".join(why)


@predicate("no_open_violations")
def _no_open_violations(conn, run_id, step, spec) -> tuple[bool, str]:
    """Done when the run carries no recorded violations.

    Intended for terminal steps: it stops a run from being closed as clean while its
    own ledger says otherwise.
    """
    # BREACHES only, by default. A refused attempt files a 'blocked' row — that is the engine
    # having worked, and counting it here would make a run uncloseable for having been stopped,
    # which rewards not attempting over complying. `include_blocked: true` demands a run with
    # no refusals at all, which is a much stronger and rarely-appropriate claim.
    want = None if spec.get("include_blocked") else "breach"
    rows = store.violations(conn, run_id, severity=want)
    label = "violation(s)" if want is None else "breach(es)"
    if not rows:
        blocked = len(store.violations(conn, run_id, severity="blocked"))
        note = f" ({blocked} refused attempt(s) on the ledger, which is not a breach)" \
               if blocked and want == "breach" else ""
        return True, f"no {label} recorded{note}"
    codes = ", ".join(sorted({r["code"] for r in rows}))
    return False, f"{len(rows)} {label} recorded ({codes})"


@predicate("evidence_equals", requires=("kind", "value"))
def _evidence_equals(conn, run_id, step, spec) -> tuple[bool, str]:
    """Done when the LATEST evidence row of `kind` equals `value` exactly.

    Distinct from `evidence`'s `match:` on purpose. `match` is a substring, and substrings
    lie: a check for "pass" is satisfied by "passing" and by "0 passed, 3 failed". Where a
    recorded value is an enum, exact comparison is the only honest test.
    """
    rows = store.find_evidence(conn, run_id, step.evidence_scope, str(spec["kind"]))
    if not rows:
        return False, f"no evidence of kind '{spec['kind']}' recorded"
    latest = rows[-1]["value"]
    want = spec["value"]
    if str(latest) == str(want):
        return True, f"{spec['kind']} == {want!r}"
    return False, (
        f"{spec['kind']} is {latest!r}, expected exactly {want!r}\n"
        f"    (a substring test would have accepted this — that is why this is an equality)"
    )


@predicate("evidence_in", requires=("kind", "values"))
def _evidence_in(conn, run_id, step, spec) -> tuple[bool, str]:
    """Done when the LATEST evidence row of `kind` is one of `values`."""
    allowed = [str(v) for v in (spec["values"] or [])]
    rows = store.find_evidence(conn, run_id, step.evidence_scope, str(spec["kind"]))
    if not rows:
        return False, f"no evidence of kind '{spec['kind']}' recorded"
    latest = str(rows[-1]["value"])
    if latest in allowed:
        return True, f"{spec['kind']} = {latest!r} ∈ {allowed}"
    return False, f"{spec['kind']} is {latest!r}, expected one of {allowed}"


@predicate("evidence_all_in", requires=("kind", "values"))
def _evidence_all_in(conn, run_id, step, spec) -> tuple[bool, str]:
    """Done when EVERY recorded row of `kind` carries one of `values` — not just the latest.

    Distinct from `evidence_in`, and the difference is the whole point. A criterion of the
    form "every one of them has a verdict" is universal, and checking only the most recent
    row satisfies it with one good entry sitting on top of any number of blank or garbage
    ones. That is the same shape of hole as counting rows without looking at them.

    Optional `min_count` (default 1): a universal claim over an EMPTY set is vacuously true,
    so a step that must have looked at something needs a floor as well. Without it, recording
    nothing would pass a check that reads as "all of them are fine".
    """
    allowed = [str(v) for v in (spec["values"] or [])]
    want = int(spec.get("min_count", 1))
    rows = store.find_evidence(conn, run_id, step.evidence_scope, str(spec["kind"]))
    if len(rows) < want:
        return False, (
            f"needs at least {want} row(s) of kind '{spec['kind']}', found {len(rows)}\n"
            f"    (a universal check over nothing would otherwise pass while nothing was done)"
        )
    bad = [(i, str(r["value"])) for i, r in enumerate(rows, 1)
           if str(r["value"]) not in allowed]
    if not bad:
        return True, f"all {len(rows)} '{spec['kind']}' row(s) carry a verdict in {allowed}"
    shown = "; ".join(f"row {i} is {v!r}" for i, v in bad[:4])
    return False, (
        f"{len(bad)} of {len(rows)} '{spec['kind']}' row(s) carry no legal verdict "
        f"(expected one of {allowed}): {shown}{' …' if len(bad) > 4 else ''}"
    )


@predicate("claim_corroborated",
           requires=("kind", "claims", "disproved_when"), needs_facts=True)
def _claim_corroborated(conn, run_id, step, spec, facts_fn=None) -> tuple[bool, str]:
    """Done when a recorded claim that NOTHING was found survives an independent probe.

    WHAT THIS CLOSES. Every other predicate here reads only what was recorded, so the whole
    set shares one blind spot: a value asserting absence — "no dependencies", "clean", "all
    of these were noise" — is accepted on its own word. That is the single most attractive
    thing to record, because it is the value that requires no further work, and it is
    indistinguishable in the log from the same value honestly earned. A neighbouring system
    lost this exact way: a rule accepted "the tool was not available" as evidence that there
    was nothing to report, a mechanism unrelated to the flow made that failure the normal
    outcome, and one hundred and thirty-six acknowledgements were granted for work nobody did
    — each one individually plausible, and none of them checkable after the fact.

    So the flow may declare, for a claim of absence, WHICH derived fact would contradict it.
    The engine then asks the world before believing the record.

    THE CLAIM IS THE LATEST ROW — the run's current answer — and the universal reading was
    tried first and rejected. "Every row is inside `claims`" sounds stricter, and for a field
    whose rows are ONE verdict re-decided it is the opposite: a run that recorded `warn` and
    then `clean` has one row outside, so a universal check reads that as "something was found,
    nothing to corroborate" and waves through the very `clean` it was built to interrogate.
    Latest-row cannot produce that miss. It CAN over-fire on a field where each row is its own
    per-item verdict (many comments, most of them noise) — that shape is not this predicate's
    and belongs to `evidence_all_in` plus a hook on the count. An empty set is refused rather
    than passed: a claim over nothing would otherwise be the cheapest clean bill available.

    UNVERIFIABLE IS A REFUSAL, not a pass. If the fact cannot be obtained, the outcome is
    the same as a contradiction. This is the asymmetry the whole predicate is for: "I looked
    and there was nothing" and "nothing could look" produce identical records, so the one
    that cannot be corroborated must not be the one that costs less. The exits are to fix
    the provider, or to stop claiming absence — not to record the claim again.

    Generic by construction: the engine compares recorded strings against `claims` and hands
    the fact to the shared condition evaluator. It learns nothing about what any of them mean.
    """
    kind = str(spec["kind"])
    claims = [str(v) for v in (spec["claims"] or [])]
    cond = spec["disproved_when"]
    rows = store.find_evidence(conn, run_id, step.evidence_scope, kind)
    if not rows:
        return False, (
            f"no '{kind}' recorded — there is no claim to corroborate\n"
            f"    (an absence claim over an empty set would otherwise be the cheapest pass)"
        )
    latest = str(rows[-1]["value"])
    if latest not in claims:
        return True, (
            f"'{kind}' is {latest!r}, not a claim of absence ({claims}) — "
            f"nothing to corroborate"
        )
    if facts_fn is None:
        return False, (
            f"'{kind}' is {latest!r}, a claim of absence, but no fact resolver was "
            f"supplied — "
            f"the claim cannot be corroborated.\n"
            f"    This is a wiring fault, not a clean result. It is reported as a refusal "
            f"because the alternative is to pass an unchecked claim."
        )
    try:
        # The gather AND the evaluation are inside one try: a provider that errors and a
        # capability this machine lacks are the same thing from here — the claim cannot be
        # corroborated — and giving them separate handling would invite one of them to grow a
        # softer outcome than the other.
        values = facts_fn()
        contradicted = conditions.evaluate(cond, values)
    except Exception as exc:  # noqa — any failure to consult the world is unverifiable
        return False, (
            f"'{kind}' is {latest!r} (a claim of absence) and the fact that would "
            f"corroborate it is unavailable: {exc}\n"
            f"    Unverifiable is refused, not passed — otherwise 'nothing could check' "
            f"becomes the cheapest way to record 'nothing was there'.\n"
            f"    Fix the provider or the capability, or stop claiming absence and record "
            f"what was found."
        )
    if contradicted:
        lines = conditions.explain(cond, values)
        detail = "\n".join("      " + ln for ln in lines)
        return False, (
            f"'{kind}' is {latest!r} (a claim of absence), but an independent fact says "
            f"otherwise:\n{detail}\n"
            f"    The record and the world disagree. Deal with what was found, or explain "
            f"why it does not hold here — do NOT re-record the claim to make this pass."
        )
    return True, (
        f"'{kind}' is {latest!r} (a claim of absence), corroborated — the independent "
        f"fact does not contradict it"
    )


@predicate("fields_agree", requires=("kind_a", "kind_b"))
def _fields_agree(conn, run_id, step, spec) -> tuple[bool, str]:
    """Done when two recorded values AGREE, and optionally the agreed value is acceptable.

    Optional `value_in`: a list the agreed value must belong to.
    Optional `absent_ok`: when both kinds are missing, treat as not-applicable and pass.

    WHY THIS SHAPE EXISTS. A real invariant in this space reads: "the revision whose checks
    were verified must BE the latest revision pushed, and its status must be terminal-clean".
    Neither existence nor counting expresses that — it is an equality between two moving
    values plus a set membership, with a not-applicable branch when there is nothing to
    compare. It is also the one predicate its own system implemented twice, in two places,
    reading the same fields; having it once, declaratively, is the point.

    Generic by construction: the engine compares two recorded values and knows nothing
    about what either means.
    """
    a = store.find_evidence(conn, run_id, step.evidence_scope, str(spec["kind_a"]))
    b = store.find_evidence(conn, run_id, step.evidence_scope, str(spec["kind_b"]))
    if not a and not b:
        if spec.get("absent_ok"):
            return True, f"neither '{spec['kind_a']}' nor '{spec['kind_b']}' recorded — N/A"
        return False, f"neither '{spec['kind_a']}' nor '{spec['kind_b']}' recorded"
    if not a or not b:
        missing = spec["kind_a"] if not a else spec["kind_b"]
        return False, (
            f"only one side recorded — '{missing}' is missing.\n"
            f"    A one-sided comparison is how 'verified' drifts away from 'latest'."
        )
    va, vb = str(a[-1]["value"]), str(b[-1]["value"])
    if va != vb:
        return False, (
            f"'{spec['kind_a']}' is {va!r} but '{spec['kind_b']}' is {vb!r} — they must agree"
        )
    allowed = spec.get("value_in")
    if allowed is not None:
        allowed = [str(v) for v in allowed]
        if va not in allowed:
            return False, f"both sides say {va!r}, which is not one of {allowed}"
        return True, f"both sides agree on {va!r} ∈ {allowed}"
    return True, f"both sides agree on {va!r}"


@predicate("evidence_matches_variant", requires=("kind",))
def _evidence_matches_variant(conn, run_id, step, spec) -> tuple[bool, str]:
    """Done when the LATEST recorded value of `kind` equals the variant this run was opened as.

    WHY A RUN NEEDS THIS AT ALL. The variant is pinned when the run opens, because a flow whose
    shape changes mid-run cannot be analysed. But the truth it encodes is sometimes only
    DISCOVERED later — a mode that depends on a value the flow itself collects cannot be derived
    before the flow has run. Those two facts collide, and the collision is silent: whoever opened
    the run guessed (or a derivation read the wrong input and answered with its default), the
    wrong shape got pinned, and every step of the right shape then reports as "not part of this
    flow" — which reads like a correct refusal.

    So the flow declares the reconciliation: at the step where the real value becomes known,
    record it and require agreement. A mismatch is caught at the first moment it CAN be caught,
    and the remedy is to re-open with the right variant rather than to walk a flow shaped wrong.

    Generic: the engine compares a recorded string to the run's own variant and knows what
    neither means.
    """
    row = store.get_run(conn, run_id)
    pinned = row["variant"]
    kind = str(spec["kind"])
    rows = store.find_evidence(conn, run_id, step.evidence_scope, kind)
    if not rows:
        return False, f"no evidence of kind '{kind}' recorded — nothing to reconcile"
    got = str(rows[-1]["value"])
    if pinned is None:
        return False, (
            f"this run has no variant, but '{kind}' was recorded as {got!r}.\n"
            f"    A reconciliation check on a single-shape ability is a spec error."
        )
    if got == pinned:
        return True, f"recorded {kind}={got!r} agrees with the run's variant"
    return False, (
        f"'{kind}' is {got!r} but this run was opened as variant {pinned!r}.\n"
        f"    The run is walking the wrong shape. Close it and re-open with "
        f"--variant {got}; do NOT continue, and do not 'fix' the recorded value to match."
    )


@predicate("phases_summarized")
def _phases_summarized(conn, run_id, step, spec) -> tuple[bool, str]:
    """Done when every phase the run has REACHED also has a summary row.

    Cross-phase derivation, which is why it cannot be an `all_of`: the set of phases owed
    is not fixed in the spec, it is discovered from what the run actually did. A run that
    skipped an optional phase entirely owes no summary for it.

    Optional `exclude`: phase ids never owed (typically the phase this step lives in, which
    is still in progress).
    """
    from . import flow as flowmod
    row = store.get_run(conn, run_id)
    f = flowmod.load(row["ability"])
    done = store.closed_steps(conn, run_id)
    exclude = {str(x) for x in (spec.get("exclude") or [])} | {step.phase}

    reached = {s.phase for s in f.steps.values() if s.id in done} - exclude
    have = store.summarized_phases(conn, run_id)
    missing = sorted(reached - have)
    if not missing:
        return True, f"{len(reached)} reached phase(s) all summarized"
    return False, (
        f"reached but not summarized: {', '.join(missing)}\n"
        f"    record each with: harness summarize --run {run_id} --phase <id>"
    )


@predicate("phase_steps_closed")
def _phase_steps_closed(conn, run_id, step, spec) -> tuple[bool, str]:
    """Done when every REQUIRED step of the subject's phase is closed (or legitimately skipped).

    The commonest layer acceptance criterion, and the reason it is a predicate rather than an
    `all_of` with a hand-written list: the list would have to be maintained in two places, and
    a step added to the phase later would silently escape the criterion. Derived from the spec,
    so it cannot go stale.

    Optional `allow_optional_open`: by default an optional step that was neither closed nor
    explicitly skipped counts as outstanding, because "nobody decided about it" and "it was
    decided not to run it" are different states and only the second is a finished phase.
    """
    from . import flow as flowmod
    row = store.get_run(conn, run_id)
    f = flowmod.load(row["ability"])
    done = store.closed_steps(conn, run_id)
    skipped = {r["step_id"] for r in conn.execute(
        "SELECT DISTINCT step_id FROM step_log WHERE run_id = ? AND event = 'skipped'",
        (run_id,)).fetchall()}

    outstanding = []
    for st in f.steps_in_phase(step.phase):
        if not f.applicable(st.id, row["variant"]):
            continue   # not part of the flow this run follows — never owed
        if st.id in done or st.id in skipped:
            continue
        if st.optional and spec.get("allow_optional_open"):
            continue
        outstanding.append(st.id)
    if not outstanding:
        n = len([x for x in f.steps_in_phase(step.phase) if x.id in done])
        return True, f"phase '{step.phase}': {n} step(s) closed, none outstanding"
    return False, (
        f"phase '{step.phase}' has {len(outstanding)} step(s) neither closed nor skipped: "
        f"{', '.join(outstanding[:8])}{' …' if len(outstanding) > 8 else ''}"
    )


@predicate("all_checks", requires=("checks",), needs_facts=True)
def _all_checks(conn, run_id, step, spec, facts_fn=None) -> tuple[bool, str]:
    """Done when EVERY nested check passes. A conjunction of the other predicates.

    WHY THIS IS NECESSARY, not sugar. Without it a step can carry exactly one criterion,
    and a step that produces three things then gets its strongest ONE pinned while the rest
    go unchecked — which reads in the log exactly like a fully checked step. Four independent
    readings of one real flow hit this separately, on different steps: three artifacts
    from one publish action, a confirmation that must ALSO have persisted its choices, a
    pre-exit invariant that is four conditions, and a layer criterion that is "the identity is
    pinned AND the state is terminal-clean". Each had to drop what would not fit.

    Deliberately AND-only. An `any_of` at this level would let a step be satisfied by its
    weakest alternative, and the interesting mistake is not "no criterion" but "one criterion
    standing in for several". Alternatives that genuinely exist belong in `all_of`'s groups,
    where they are about which STEPS ran, not about how little was checked.

    Nesting is allowed; recursion is bounded by the spec's own depth. Every failing branch is
    reported, not just the first — being told one of four problems is how a fix cycle turns
    into four fix cycles.
    """
    checks = spec["checks"]
    if not isinstance(checks, list) or not checks:
        return False, "all_checks needs a non-empty 'checks' list"
    oks, whys = [], []
    for i, sub in enumerate(checks):
        if not isinstance(sub, dict) or "type" not in sub:
            return False, f"all_checks[{i}] is not a completion spec (needs a 'type')"
        # The resolver is forwarded, not dropped. A conjunction is the normal home for a
        # fact-consulting check (a value AND its corroboration), and a parent that swallowed
        # the resolver would make that nested check refuse for a wiring reason while reading
        # as a real contradiction.
        ok, why = check(conn, run_id, step, sub, facts_fn)
        oks.append(ok)
        whys.append(("✔" if ok else "✘") + f" {sub['type']}: {why.splitlines()[0]}")
    if all(oks):
        return True, f"all {len(checks)} checks passed — " + "; ".join(
            w[2:] for w in whys)
    n = sum(1 for o in oks if not o)
    return False, f"{n} of {len(checks)} checks failed:\n      " + "\n      ".join(whys)


def requirements(spec: dict) -> list[dict]:
    """What a completion spec DEMANDS, as data — so a caller can be told, not left to guess.

    Strengthening a criterion is only half the work: a step that refuses until three specific
    records exist, while announcing no more than its predicate's NAME, has moved the guesswork
    rather than removed it. The caller then either reads the engine's source or discovers the
    requirement one refusal at a time.

    Generic by construction: this walks the spec's own key vocabulary (`kind`, `values`,
    `steps`, `checks`, …), which is the engine's, so it needs no per-predicate knowledge and
    cannot fall behind a predicate it has not been taught about — the worst it can do for an
    unfamiliar shape is say less.
    """
    t = str(spec.get("type"))
    out: list[dict] = []
    if t == "all_checks":
        for sub in spec.get("checks") or []:
            out.extend(requirements(sub))
        return out
    if t == "gate_recorded":
        return [{"what": "gate", "detail": "this step's own gate, answered affirmatively"}]
    if t == "all_of":
        d = []
        if spec.get("steps"):
            d.append("closed: " + ", ".join(str(x) for x in spec["steps"]))
        for g in spec.get("any_of") or []:
            d.append("one of: " + ", ".join(str(x) for x in g))
        return [{"what": "steps", "detail": "; ".join(d)}]
    for key in ("kind", "kind_a", "kind_b"):
        if spec.get(key):
            item = {"what": "evidence", "kind": str(spec[key])}
            if spec.get("min_count"):
                item["min_count"] = int(spec["min_count"])
            # `match` is ENFORCED by the evidence predicate and was missing here, so the
            # machine-readable requirements understated the criterion: a driver reading them saw
            # "record a row of this kind" for a step that also demanded the value contain
            # something. Reported now, because a requirement list that omits a requirement is
            # worse than none — it is trusted.
            if spec.get("match"):
                item["match"] = str(spec["match"])
            if "value" in spec:
                item["must_equal"] = spec["value"]
            if "values" in spec:
                item["must_be_one_of"] = [str(v) for v in spec["values"]]
                if t == "evidence_all_in":
                    item["every_row"] = True
            if key in ("kind_a", "kind_b"):
                item["note"] = "must agree with the other side"
            out.append(item)
    if t == "claim_corroborated":
        return [{"what": "evidence", "kind": str(spec["kind"]),
                 "every_row": True,
                 "claims": [str(v) for v in spec["claims"]],
                 "note": f"a claim of absence ({[str(v) for v in spec['claims']]}) is only "
                         f"accepted if an independent fact does not contradict it"},
                {"what": "derived",
                 "detail": "an independently derived fact must not contradict the claim"}]
    if t == "evidence_matches_variant":
        return [{"what": "evidence", "kind": str(spec["kind"]),
                 "note": "must equal the run's pinned variant"}]
    if t in ("no_open_violations", "phases_summarized", "phase_steps_closed", "attest"):
        out.append({"what": "derived", "detail": {
            "no_open_violations": "the run carries no violations",
            "phases_summarized": "every reached phase has a summary",
            "phase_steps_closed": "every required step of this phase is closed or skipped",
            "attest": "nothing is checked — this records an assertion only",
        }[t]})
    return out


# Strength tiers, derived from `requirements` rather than a hand-kept table — a new predicate
# gets classified by what it DEMANDS, so the report cannot drift from the registry.
#   0  nothing is checked
#   1  a record must exist (self-reported, but auditable content)
#   2  a recorded value must match a declared expectation, or two records must agree
#   3  derived from engine state; an assertion cannot produce it
def strength(spec: dict) -> int:
    reqs = requirements(spec)
    if not reqs:
        return 0
    best = 0
    for r in reqs:
        if r["what"] in ("gate", "steps"):
            best = max(best, 3)
        elif r["what"] == "derived":
            best = max(best, 0 if str(spec.get("type")) == "attest" else 3)
        elif "must_equal" in r or r.get("must_be_one_of") or r.get("note"):
            best = max(best, 2)
        else:
            best = max(best, 1)
    return best
