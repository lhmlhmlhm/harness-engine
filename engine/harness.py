"""harness — the CLI. This is the engine's entire surface.

EXIT CODES ARE THE PRODUCT. An ability declares a flow; what it gets back is a set of
commands that refuse, with a number a tool can branch on:

    0  ok / allowed
    1  usage or environment error
    2  the flow spec itself is invalid (fail-closed; the store is never touched)
    3  REFUSED — a rule was not satisfied (deps open, predicate false, gate unproven)
    4  BLOCKED — a guarded action was attempted without its gate

3 and 4 are split because they have different audiences. 3 answers the ability's own
driver ("you cannot close this step yet"); 4 answers an external tool hook ("do not let
this command run"). A hook only needs to branch on 4, so it never has to parse text.

Nothing in this file names a step, a phase, an action, or an ability. Grep it and see —
`tests/test_engine_purity.py` does exactly that, and fails if the grep finds anything.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
import uuid
from pathlib import Path

from . import flow as flowmod
from . import outputs
from . import policy
from . import trust
from . import brief as briefmod
from . import conditions, facts, hooks as hookmod, predicates, proof, prose, store

OK, USAGE, BAD_SPEC, REFUSED, BLOCKED, INTERNAL = 0, 1, 2, 3, 4, 5

# The one table. Published in `adapter-contract` and used to name a code in a JSON refusal,
# so those two cannot come to disagree about what a 3 means.
_EXIT_NAMES = {OK: "OK", USAGE: "USAGE", BAD_SPEC: "BAD_SPEC",
               REFUSED: "REFUSED", BLOCKED: "BLOCKED", INTERNAL: "INTERNAL"}

# Named rather than inlined: `brief` reports it, and a literal in two places is a
# second source that drifts.
ACTOR_ENV = "HARNESS_ACTOR"


# What the process has said on stderr, and whether stdout already carries an answer.
# Module state, reset by main() — see _answer_in_json_too for why it has to exist at all.
_SAID: list[str] = []
_ANSWERED = False


def _err(msg: str) -> None:
    text = msg.rstrip()
    sys.stderr.write(text + "\n")
    # Kept, not just printed, so a refusal can be answered in the format the caller asked
    # for WITHOUT each refusal site remembering to do it. There are 26 of them across the
    # write commands, plus three shared helpers that refuse on their behalf — a rule that
    # says "every new refusal must also emit JSON" is a rule that gets forgotten once and
    # then reads, to a JSON caller, as an empty answer.
    _SAID.append(text)


# ------------------------------------------------------------------ helpers

def _rowdicts(rows) -> list[dict]:
    return [{k: r[k] for k in r.keys()} for r in rows]


def _add_json(p) -> None:
    """Declare --json once. It was the same sentence pasted at eight call sites."""
    p.add_argument("--json", action="store_true",
                   help="emit the same answer as JSON — one data structure, two renderings")


def _sayer(args):
    """`print`, or a no-op when the caller asked for JSON.

    Prose and JSON share stdout, so a command that prints both hands back something no parser
    can read. Returning the function rather than checking a flag at each print keeps the
    decision in one place per command instead of one place per line.
    """
    if getattr(args, "json", False):
        return lambda *a, **k: None
    return print


def _emit(args, data: dict, render) -> int:
    """One data structure, two renderings — so the two cannot disagree.

    Every read command builds its answer as DATA first and then either dumps it or prints it.
    Producing the machine form separately would be a second implementation of the same query,
    and the two would drift the moment one of them gained a field.

    Why a machine form is needed at all: the exit codes are the contract, but until now the
    DETAIL was human prose only. Anything that is not a shell — a second runtime's adapter, a
    dashboard, a monitor — had to regex formatted text to learn what step was next or what was
    owed. `next` in particular exists to state a step's requirements AS DATA, and it was
    stating them as columns.
    """
    global _ANSWERED
    if getattr(args, "json", False):
        fn = getattr(args, "fn", None)
        # Named here, once, and derived from the handler — so every envelope (answer and
        # refusal alike) says which command produced it without ten literals to keep in step.
        if "command" not in data and fn is not None:
            data = {"command": fn.__name__[4:].replace("_", "-"), **data}
        print(json.dumps(data, indent=2, ensure_ascii=False, default=str))
        _ANSWERED = True
        return OK
    render(data)
    return OK



def _load_flow_or_exit(ability: str) -> flowmod.Flow:
    try:
        return flowmod.load(ability)
    except flowmod.FlowError as exc:
        _err(f"⛔ invalid flow spec\n{exc}")
        raise SystemExit(BAD_SPEC)


def _run_or_exit(conn, run_id: str, *, closed_ok: bool = False):
    """Fetch a run, and refuse a WRITE to one that has already ended.

    THE ENGINE HAD NO NOTION OF "THIS RUN IS FINISHED, STOP WRITING TO IT." Every write command
    accepted a closed run: evidence appended rows to a finished ledger, gate recorded on it,
    close-step re-closed steps, and close-run REWROTE the result — `done` became `abort` with a
    success message, no violation, and a fresh end timestamp. `history` then showed the rewrite
    as if it had always been the outcome. A record that can be edited afterwards, silently, is
    not a record.

    THE DEFAULT IS "MUST BE OPEN", AND THE DIRECTION IS THE POINT. Opt-in would mean the next
    write command that forgets to ask silently appends to a finished run — the exact hole being
    closed. This way round, a READ command that forgets refuses on a closed run, which its own
    test catches on the first run. Loud where it is wrong beats silent where it is wrong.

    Retrying an interrupted close is NOT blocked: `close-run` releases leases and then closes, so
    a process dying between the two leaves the run OPEN, and the retry passes this check.
    """
    row = store.get_run(conn, run_id)
    if row is None:
        _err(f"⛔ no run {run_id!r}. list them with: harness status")
        raise SystemExit(USAGE)
    if not closed_ok and row["status"] != "open":
        _err(f"⛔ run {run_id!r} has ended: {row['status']}"
             + (f" → {row['result']}" if row["result"] else "")
             + (f" at {row['closed_at']}" if row["closed_at"] else "") + ".\n"
             f"    NOTHING WAS CHANGED. A finished run does not accept writes — appending to it,\n"
             f"    or closing it again with a different result, would rewrite what happened with\n"
             f"    no trace that it had been rewritten.\n"
             f"    Read it with:  harness status --run {run_id}\n"
             f"    Its rows can be removed, and that removal is recorded: harness purge-run")
        raise SystemExit(USAGE)
    return row


def _flow_for_run(conn, row) -> flowmod.Flow:
    f = _load_flow_or_exit(row["ability"])
    if f.digest != row["flow_digest"]:
        # Not fatal: editing a flow mid-run is normal during development. But it must
        # be visible, because a step that vanished from the spec silently changes what
        # "complete" means for a run already in flight.
        _err(
            f"⚠️  flow '{row['ability']}' changed since this run opened "
            f"({row['flow_digest']} → {f.digest}). Step semantics may have moved."
        )
    return f


# ------------------------------------------------------------------ commands

def cmd_init(args) -> int:
    path, applied = store.init()
    conn = store.connect()
    try:
        tables = sorted(store.core_tables(conn))
    finally:
        conn.close()
    # Refresh the machine-local contract here, because `init` is the one command every setup
    # path already runs and is safe to re-run. A generated document nobody regenerates is a
    # hand-written one with extra steps — and a stale one misinstructs an agent silently, which
    # is the failure the generation was introduced to remove.
    try:
        bp = store.brief_path()
        bp.parent.mkdir(parents=True, exist_ok=True)
        bp.write_text(_render_brief(portable=False), encoding="utf-8")
        wrote_brief: str | None = str(bp)
    except Exception as exc:  # noqa — a document must not stop the store from being usable
        _err(f"⚠️  could not refresh the driving contract: {exc}")
        wrote_brief = None
    print(f"✅ store ready: {path}")
    print(f"   schema {store.SCHEMA_VERSION}"
          + (f" — migrated: {', '.join(str(v) for v in applied)}" if applied else ""))
    print(f"   tables ({len(tables)}): {', '.join(tables)}")
    abilities = flowmod.available_abilities()
    print(f"   abilities ({len(abilities)}): {', '.join(abilities) or '(none)'}")
    if wrote_brief:
        print(f"   driving contract: {wrote_brief}")
    return OK


def _code_fields(ability: str) -> dict:
    """Whether an ability ships code and whether it is trusted — computed WITHOUT loading it.

    Reported on the invalid branch too, because "invalid" and "not approved" are different
    answers and the second one has a fix the first does not.
    """
    mod = flowmod.ability_dir(ability) / "providers.py"
    if not mod.is_file():
        return {"executes_code": False, "trust": None}
    st, _d = trust.state(ability, mod)
    return {"executes_code": True, "trust": st}


def _code_files() -> list[tuple[str, Path]]:
    """Installed abilities that ship code, WITHOUT loading any of them.

    Deliberately does not go through `flow.load`: the whole point is to describe a file you
    have not agreed to run yet, and loading it is running it.
    """
    out = []
    for name in flowmod.available_abilities():
        mod = flowmod.ability_dir(name) / "providers.py"
        if mod.is_file():
            out.append((name, mod))
    return out


def cmd_trust(args) -> int:
    """Review and approve the extension files this installation would import."""
    if args.forget:
        if trust.forget(args.forget):
            print(f"forgotten: {args.forget}   (it will ask again before importing)")
            return OK
        _err(f"no approval recorded for '{args.forget}'")
        return USAGE
    if args.ability:
        pairs = dict(_code_files())
        mod = pairs.get(args.ability)
        if mod is None:
            _err(f"ability '{args.ability}' ships no providers.py — nothing to approve.\n"
                 f"  with code: {', '.join(sorted(pairs)) or '(none)'}")
            return USAGE
        if trust.is_internal(mod):
            _err(f"{mod}\n"
                 f"  is inside this engine's own tree, so it is not pinned and cannot be\n"
                 f"  approved: anyone able to edit it can edit the engine, and a record the\n"
                 f"  engine keeps cannot outlast an editor of the engine.")
            return USAGE
        dg = trust.approve(args.ability, mod)
        print(f"approved {args.ability}\n  {mod}\n  {dg}\n  recorded in {trust.trust_path()}")
        return OK

    rows = []
    for name, mod in _code_files():
        st, detail = trust.state(name, mod)
        rows.append({"ability": name, "path": str(mod), "state": st,
                     "digest": detail["digest"], "approved": detail.get("approved"),
                     "approved_at": detail.get("at") or None,
                     "imports": trust.declared_imports(mod)})
    if args.json:
        return _emit(args, {"record": str(trust.trust_path()),
                            "engine_tree": str(trust.ENGINE_TREE),
                            "extensions": rows}, lambda _d: None)
    if not rows:
        print("(no installed ability ships code — every flow here is purely declarative)")
        return OK
    MARK = {trust.STATE_INTERNAL: "—", trust.STATE_APPROVED: "✅",
            trust.STATE_UNKNOWN: "⚠️ ", trust.STATE_CHANGED: "⛔"}
    print(f"record {trust.trust_path()}")
    print(f"engine {trust.ENGINE_TREE}")
    for r in rows:
        print(f"{MARK[r['state']]} {r['ability']:<16} {r['state']:<9} {r['digest'][:23]}…")
        print(f"     {r['path']}")
        print(f"     imports (advisory): {', '.join(r['imports']) or '(none)'}")
        if r["state"] == trust.STATE_CHANGED:
            print(f"     approved earlier:   {r['approved'][:23]}…  — REVIEW THE DIFF")
    n = sum(1 for r in rows if r["state"] in (trust.STATE_UNKNOWN, trust.STATE_CHANGED))
    if n:
        print(f"\n{n} file(s) will refuse to load. Approve one with: "
              f"harness trust <ability>")
    print("\nApproval records that you accepted these bytes. It is NOT a sandbox: an approved\n"
          "file runs with everything this engine can reach. Files inside the engine's own tree\n"
          "are not pinned — see `harness trust --json` for which tree that is.")
    return OK


def cmd_abilities(args) -> int:
    names = flowmod.available_abilities()
    if args.json:
        out: list[dict] = []
        for name in names:
            try:
                f = flowmod.load(name)
            except flowmod.FlowError as exc:
                out.append({"ability": name, "valid": False,
                            "error": str(exc).splitlines()[0], **_code_fields(name)})
                continue
            out.append({
                "ability": name, "valid": True, "title": f.title, "role": f.role,
                "routable": f.role == flowmod.ROLE_PRODUCTION, "when": f.when,
                "scope_kind": f.scope_kind, "scope_match": f.scope_match,
                "phases": list(f.phases), "steps": len(f.steps),
                "required_steps": len(f.required_steps()),
                "optional_steps": sum(1 for st in f.steps.values() if st.optional),
                "gated_steps": sum(1 for st in f.steps.values()
                                   if st.gate != flowmod.GATE_NONE),
                "guards": sorted(f.guards),
                # WHICH guards a runtime hook can actually fire. An action with no match rules is
                # legal and deliberate — it declares a guard only an explicit caller can consult —
                # but it is invisible to the interception path, which is the one that does not
                # need the driver's cooperation. Reported so that difference is not something you
                # have to read the spec to find.
                "guard_reach": {a: ("hook" if f.guard_matches.get(a) else "ask_only")
                                for a in sorted(f.guards)},
                "variants": list(f.variants),
                "requires": list(f.requires),
                # So a driver knows the vocabulary BEFORE it closes a run, not only from the
                # refusal it gets for guessing. Null means the flow never said, and then any
                # string is still accepted.
                "results": ({"values": list(f.result_spec["values"]),
                             "default": f.result_spec["default"]} if f.result_spec else None),
                "facts_providers": list(f.facts_providers),
                "facts": sorted(f.facts_schema),
                "hooks": len(f.hooks), "uses": list(flowmod.capabilities_used(f)),
                **_code_fields(name),
            })
        return _emit(args, {"roots": [str(r) for r in flowmod.abilities_roots()],
                            "abilities": out}, lambda _d: None)
    if not names:
        roots = ", ".join(str(r) for r in flowmod.abilities_roots())
        print(f"(none installed under: {roots})")
        return OK
    fixtures = []
    for name in names:
        try:
            f = flowmod.load(name)
        except flowmod.FlowError as exc:
            print(f"❌ {name}: INVALID — {str(exc).splitlines()[0]}")
            continue
        if f.role == flowmod.ROLE_FIXTURE:
            # Kept OUT of the roster an agent reads to route. An entry that cannot be reached
            # is noise at best and a mis-route at worst; a trailing line keeps it discoverable
            # by a human without offering it as a choice.
            fixtures.append(name)
            continue
        gated = sum(1 for s in f.steps.values() if s.gate != flowmod.GATE_NONE)
        checked = sum(1 for s in f.steps.values()
                      if s.completion.get("type") != "attest")
        print(f"✅ {name}  ({f.title})")
        stages = sum(len(f.phase_stages.get(p, ())) for p in f.phases)
        opt = sum(1 for s in f.steps.values() if s.optional)
        print(f"     scope={f.scope_kind}  phases={len(f.phases)}  stages={stages}"
              f"  steps={len(f.steps)} ({len(f.required_steps())} required, {opt} optional)")
        ask_only = [a for a in sorted(f.guards) if not f.guard_matches.get(a)]
        print(f"     gated={gated}  machine-checked={checked}  guards={len(f.guards)}"
              + (f" ({len(ask_only)} ask-only: {', '.join(ask_only)})" if ask_only else "")
              + f"  exclusive-groups={len(f.exclusive_groups)}")
        conditional = sum(1 for h in f.hooks if h.when is not None)
        obliged = sum(1 for h in f.hooks if h.obligation)
        if f.when:
            for line in f.when.splitlines():
                if line.strip():
                    print(f"     ↳ {line.strip()}")
        print(f"     facts={'+'.join(f.facts_providers)}({len(f.facts_schema)} declared)"
              f"  hooks={len(f.hooks)} ({conditional} conditional, {obliged} obligation)")
    if fixtures:
        print(f"\n   fixtures (not routable): {', '.join(fixtures)}")
        print("   — engine test scaffolding; each flow.yaml header says why it is kept")
    return OK

def cmd_validate(args) -> int:
    """Validate one or all flow specs. Intended for CI on an ability's own repo."""
    names = [args.ability] if args.ability else flowmod.available_abilities()
    bad = 0
    for name in names:
        try:
            f = flowmod.load(name)
        except flowmod.FlowError as exc:
            _err(f"❌ {name}\n{exc}")
            bad += 1
            continue
        errs, warns = prose.validate_all(f)
        for wmsg in warns:
            _err(f"⚠️  {name}: {wmsg}")
        if errs:
            for e in errs:
                _err(f"❌ {name}: {e}")
            bad += 1
            continue
        cov = prose.coverage(f)
        print(f"✅ {name}: {len(f.steps)} steps, {len(f.phases)} phases, "
              f"{len(f.guards)} guard(s); order ok")
        used = flowmod.capabilities_used(f)
        print(f"   uses ({len(used)}): {', '.join(used)}")
        # Report criterion STRENGTH, not just "not attest". A flow can be 100% non-attest and
        # still be almost entirely self-reported; printing "machine-checked: N/N" would be
        # true and would mean far less than it sounds.
        import collections as _c
        tiers = _c.Counter(predicates.strength(st.completion) for st in f.steps.values())
        NAMES = {3: "derived", 2: "value-checked", 1: "record-exists", 0: "UNCHECKED"}
        if f.role == flowmod.ROLE_FIXTURE:
            # NOT REPORTED, and the omission is the point. A fixture's criteria are weak
            # because a fixture does not need strong criteria — it needs to be small, stable
            # and to reach a mechanism. Printing the tiers here invited reading a deliberate
            # `attest` floor as an unfinished ability, which is exactly what happened for
            # several rounds before this role existed.
            print(f"   role: {f.role} — engine test scaffolding, not routable; "
                  f"criteria strength not reported")
        else:
            print("   criteria: " + " · ".join(
                f"{NAMES[t]}×{tiers.get(t, 0)}" for t in (3, 2, 1, 0)))
        weak = [st.id for st in f.steps.values() if predicates.strength(st.completion) == 0]
        if weak and f.role != flowmod.ROLE_FIXTURE:
            print(f"   ⚠️  {len(weak)} step(s) check nothing: {', '.join(weak[:10])}")
        # COMPLETENESS, alongside strength. They are different failures and one hides the
        # other: a step that produces three artifacts and pins the strongest ONE scores well on
        # strength while two thirds of its output goes unchecked. Counting pinned artifacts is
        # what makes that visible — and what makes dropping one show up as a number moving.
        pinned = [len([r for r in predicates.requirements(st.completion)
                       if r["what"] == "evidence"]) for st in f.steps.values()]
        multi = sum(1 for n in pinned if n >= 2)
        if f.role != flowmod.ROLE_FIXTURE:
            print(f"   artifacts: {sum(pinned)} pinned across {len(pinned)} steps"
                  f" ({multi} step(s) pin ≥2)")
        # CAPABILITIES, probed here rather than left to be discovered mid-run. "Can this
        # machine run this flow" is a question an author asks BEFORE opening a run, and until
        # now the only way to answer it was to walk the flow until something failed — by which
        # point the failure looks like the flow's, not the environment's.
        declared = [(pn, d) for pn in f.facts_providers
                    for d in facts.capabilities(pn)]
        if declared:
            absent = {pn: facts.probe_capabilities(pn) for pn in f.facts_providers}
            gone = [(pn, m) for pn, ms in absent.items() for m in ms]
            line = f"   capabilities: {len(declared)} declared, {len(gone)} absent"
            print(line if not gone else line + " — " + "; ".join(
                f"{pn} needs {k} {a!r}" for pn, (k, a) in gone))
            if gone:
                print("   ⚠️  steps whose criteria or hooks read those facts will REFUSE, "
                      "not pass")
        no_goal = [x for x in f.phases if x not in f.phase_goals]
        gs = "/".join(f"{k}:{NAMES[predicates.strength(v)]}"
                      for k, v in f.phase_goals.items())
        print(f"   goals: {len(f.phase_goals)}/{len(f.phases)} phases"
              + (f" — MISSING on {', '.join(no_goal)}" if no_goal else "")
              + (f"  [{gs}]" if gs else ""))
        print(f"   prose: directive {cov['directive']}/{cov['steps']}"
              f" · guide {cov['guide']}/{cov['steps']}"
              f" (own section {cov['own_section']})"
              f" · topics cited {cov['topics_cited']}")
    return BAD_SPEC if bad else OK


def _caller_note() -> dict:
    """What the engine can honestly say about who ran this command. DIAGNOSTIC ONLY.

    Isolation is by scope and must stay that way. A guard answers "may this happen to THIS
    thing", which is a property of the thing and not of whoever is touching it. Separate two
    sessions by actor instead and two workers in one repository each enforce only their own
    gates while neither knows the other exists — the very question the guard is for stops
    having an answer. A test pins the negative: two runs with different actors in one scope
    still collide.

    So this is recorded to answer a HUMAN's "which worker owns this run", nothing else.

    `actor` is whatever the caller declares in HARNESS_ACTOR, opaque and never interpreted.
    The engine cannot discover a session identity: the only process it can see is its own
    short-lived invocation, so those numbers are recorded under names that say exactly that
    rather than being passed off as the session.
    """
    note: dict = {"cli_pid": os.getpid(), "cli_ppid": os.getppid()}
    actor = os.environ.get(ACTOR_ENV, "").strip()
    if actor:
        note["actor"] = actor
    return note


def _scope_taken(scope_kind: str, scope_key: str, rows) -> None:
    ids = ", ".join(r["run_id"] for r in rows)
    _err(
        f"⛔ scope {scope_kind}={scope_key!r} already has {len(rows)} open run(s): {ids}\n"
        f"  Two open runs in one scope make guards ambiguous.\n"
        f"  Close the other one, or pass --allow-concurrent to accept that\n"
        f"  guards in this scope will refuse to adjudicate."
    )


# Every way `guard` can answer. A closed set, because the whole value of this command is that
# its kinds of ALLOW are not interchangeable: "there is nothing here to guard" and "I could not
# see what to guard" are the two the engine has always had to keep apart, and a bare
# `allowed: true` erases exactly that distinction.
GUARD_VERDICTS: dict[str, str] = {
    "blocked":
        "a run guards this action and its gate is not recorded.",
    "no_run_in_scope":
        "no run is open in this scope. Nothing to guard — not the same as nothing guarding it.",
    "not_guarded":
        "run(s) are open here and none of their flows declares this action.",
    "gate_recorded":
        "a run guards this action and the gate it needs is already recorded.",
    "unadjudicated":
        "several runs share this scope with no usable delegation, so which one owns the action "
        "is unanswerable. Allowed, and recorded as a violation.",
    "store_unusable":
        "the store is absent or at a schema this engine will not read. CANNOT guard — and "
        "cannot record that it could not, because recording needs the store just refused.",
    "view_incomplete":
        "at least one open run's flow could not be READ, so its guards were not consulted. No "
        "readable run blocked; that is not the same as nothing blocking.",
}

UNADJUDICATED = "guard_unadjudicated"
LEASE_OUTSTANDING = "lease_outstanding"


def _validated_grantor(conn, f, args):
    """Check that a delegation may be recorded. Returns (row, None) or (None, exit_code).

    A lease is granted at the moment the delegate STARTS and never afterwards. Handing
    authority to a run that is already going would mean authority could be rearranged
    mid-flight, and then "who owned this action at the time" depends on when you ask — which
    is the property the lease exists to provide.
    """
    if not (args.leased_from and args.leased_at):
        _err("⛔ --leased-from and --leased-at go together: a delegation has to say "
             "which run delegated and from where in its flow.")
        return None, USAGE
    grantor = store.get_run(conn, args.leased_from)
    if grantor is None:
        _err(f"⛔ no run {args.leased_from!r} to delegate from")
        return None, USAGE
    if grantor["status"] != "open":
        _err(f"⛔ run {args.leased_from!r} is {grantor['status']}; a closed run has no "
             f"authority to hand over")
        return None, REFUSED
    if grantor["scope_kind"] != f.scope_kind or grantor["scope_key"] != args.scope:
        # Not a technicality. A lease exists to answer "who owns actions in THIS scope"; two
        # runs in different scopes are already unambiguous, so a lease between them would
        # record a relation no guard reads — and grant authority over a scope the grantor
        # never had.
        _err(f"⛔ {args.leased_from!r} holds {grantor['scope_kind']}="
             f"{grantor['scope_key']!r}, not {f.scope_kind}={args.scope!r}.\n"
             f"    A lease covers one scope. Different scopes need no lease — they are "
             f"already unambiguous.")
        return None, REFUSED
    try:
        gf = flowmod.load(grantor["ability"])
    except flowmod.FlowError as exc:
        _err(f"⛔ {exc}")
        return None, BAD_SPEC
    if args.leased_at not in gf.steps:
        _err(f"⛔ {args.leased_from!r} has no step {args.leased_at!r}")
        return None, USAGE
    if not store.step_events(conn, grantor["run_id"], args.leased_at):
        _err(f"⛔ {args.leased_from!r} has not reached step {args.leased_at!r}.\n"
             f"    A delegation is anchored to progress that happened, not to a step "
             f"someone intends to reach.")
        return None, REFUSED
    if store.outgoing_lease(conn, grantor["run_id"], f.scope_kind, args.scope) is not None:
        _err(f"⛔ {args.leased_from!r} has already delegated this scope.\n"
             f"    One outgoing lease per run per scope: two would put authority in two "
             f"places at once, which is not authority.")
        return None, REFUSED
    return grantor, None


def _record_unadjudicated(scope_kind: str, scope_key: str, action: str,
                          run_ids: list[str], reason: str) -> None:
    """Persist that enforcement was SKIPPED. Best-effort; never breaks the caller.

    Until now this only went to stderr, so "how often did the guarantee silently stop
    applying" was unanswerable after the fact — and an enforcement gap nobody can count is
    the one that lasts. A neighbouring system's 162 forced acknowledgements were only
    findable because they left rows.

    The guard's own connection stays READ-ONLY. This is the rare branch, and making the hot
    path writable to serve it would put a write lock in front of every tool call, so a
    second short-lived connection is opened here and only here.

    Deduped on the exact situation, because the guard runs on every tool use: an unresolved
    scope would otherwise write one row per call, and ten thousand identical rows say no more
    than one while burying everything else. A genuinely different situation — another action,
    another set of runs, another reason — has a different detail and gets its own row.

    run_id is NULL deliberately. Pinning this on one of the ambiguous runs is exactly the
    guess the resolution refuses to make; a fact about a scope belongs to no run.
    """
    detail = (f"{scope_kind}={scope_key} action={action} reason={reason} "
              f"runs={','.join(sorted(run_ids))}")
    try:
        conn = store.connect()
    except store.StoreUnusable:
        return
    try:
        seen = conn.execute(
            "SELECT 1 FROM violation WHERE code = ? AND detail = ? LIMIT 1",
            (UNADJUDICATED, detail),
        ).fetchone()
        if seen is None:
            store.record_violation(conn, None, None, UNADJUDICATED, detail,
                                   severity="breach")
    except Exception as exc:
        # Broad on purpose: a ledger write must never break the caller. The guard runs in
        # front of every matching tool call, so an exception here would stop the user's work
        # over an audit note. It is reported, not swallowed.
        _err(f"⚠️  could not record the unadjudicated guard: {exc}")
    finally:
        conn.close()


def cmd_open(args) -> int:
    say = _sayer(args)
    f = _load_flow_or_exit(args.ability)
    run_id = args.run or f"{args.ability}-{uuid.uuid4().hex[:12]}"
    conn = store.connect()
    try:
        if store.get_run(conn, run_id) is not None:
            _err(f"⛔ run {run_id!r} already exists")
            return USAGE
        existing = store.open_runs_in_scope(conn, f.scope_kind, args.scope)
        grantor = None
        if args.leased_from or args.leased_at:
            grantor, err = _validated_grantor(conn, f, args)
            if grantor is None:
                return err
        if existing and grantor is None and not args.allow_concurrent and not os.environ.get("HARNESS_ACTOR"):
            # Refuse rather than pick: two open runs in one scope is exactly the state in
            # which a guard cannot tell which run owns an action.
            #
            # This check is a FAST PATH, not the authority. It is here so the common refusal
            # comes back before the variant derivation below spends time in provider
            # subprocesses. The binding check runs inside the write transaction that inserts
            # the row — this one cannot bind, because between reading and inserting there is a
            # gap, and concurrent sessions raced through it.
            _scope_taken(f.scope_kind, args.scope, existing)
            return REFUSED
        # Resolve the variant ONCE, here, and record it. Explicit beats derived beats default:
        # a caller who knows must be able to say so, and a derivation must be overridable
        # because the thing it reads can be wrong in ways only a person can see.
        variant = None
        if f.variants:
            if args.variant:
                if args.variant not in f.variants:
                    _err(f"⛔ unknown variant {args.variant!r}.\n"
                         f"    declared: {', '.join(f.variants)}")
                    return USAGE
                variant = args.variant
                source = "explicit"
            elif f.variant_spec.get("fact"):
                key = f.variant_spec["fact"]
                try:
                    vals = facts.gather_all(f.facts_providers, {
                        "run_id": run_id, "scope": args.scope,
                        "scope_kind": f.scope_kind, "ability": args.ability})
                except facts.FactsError as exc:
                    _err(f"⛔ cannot resolve the variant: {exc}\n"
                         f"    Pass --variant explicitly, or fix the provider. Falling back to "
                         f"a default here would pin the wrong shape silently.")
                    return REFUSED
                raw_variant = vals.get(key, "")
                if isinstance(raw_variant, facts.Unavailable):
                    # Pinning the shape of a run on a fact this machine cannot produce would
                    # silently land the default, which is the failure this derivation already
                    # refuses elsewhere — an absent capability must not become a shape choice.
                    _err(f"⛔ cannot resolve the variant: {raw_variant.why()}\n"
                         f"    Pass --variant explicitly, or make the capability available.")
                    return REFUSED
                got = str(raw_variant)
                if got not in f.variants:
                    _err(f"⛔ fact {key!r} resolved to {got!r}, which is not a declared "
                         f"variant ({', '.join(f.variants)}).\n"
                         f"    Pass --variant explicitly if this is intentional.")
                    return REFUSED
                variant, source = got, f"derived from fact '{key}'"
            else:
                variant, source = f.default_variant, "default"
        lease = None if grantor is None else {
            "grantor_run_id": grantor["run_id"], "granted_at_step": args.leased_at}
        reason, blockers = store.claim_scope_and_open(
            conn,
            require_free_scope=(grantor is None and not args.allow_concurrent),
            lease=lease,
            run_id=run_id, ability=args.ability, flow_digest=f.digest,
            title=args.title, scope_kind=f.scope_kind, scope_key=args.scope,
            variant=variant,
            metadata={"allow_concurrent": bool(args.allow_concurrent), **_caller_note()},
        )
        if reason == "scope_taken":
            _scope_taken(f.scope_kind, args.scope, blockers)
            return REFUSED
        if reason == "grantor_gone":
            _err(f"⛔ {args.leased_from!r} closed while this run was being opened; a closed "
                 f"run has no authority to hand over.")
            return REFUSED
        if reason == "grantor_already_delegated":
            _err(f"⛔ {args.leased_from!r} delegated this scope while this run was being "
                 f"opened. One outgoing lease per run per scope.")
            return REFUSED
        if grantor is not None:
            say(f"   leased {f.scope_kind}={args.scope} from {grantor['run_id']}"
                f" at its step {args.leased_at}")
        n_out = None
        if variant:
            n_out = sum(1 for x in f.steps if not f.applicable(x, variant))
            say(f"   variant {variant}  ({source}) — {n_out} step(s) not applicable")
        first = f.order[0] if f.order else None
        if first:
            store.touch_run(conn, run_id, current_step=first)
    finally:
        conn.close()
    say(f"✅ opened {run_id}  ability={args.ability}  {f.scope_kind}={args.scope}")
    if f.order:
        say(f"   first step: {f.order[0]}  ({f.step(f.order[0]).title})")
    return _emit(args, {
        "run": run_id,
        "ability": args.ability,
        # THE FIELD THIS COMMAND MOST NEEDED A MACHINE FORM FOR. Omit --run and the engine
        # invents the id; until now the only way to learn it was to read the prose back.
        "run_id_generated": args.run is None,
        "scope_kind": f.scope_kind,
        "scope": args.scope,
        "title": args.title,
        # Which shape this run was pinned to AND on whose authority — a derived variant and
        # an explicit one are the same word with different standing.
        "variant": None if not variant else {
            "value": variant, "source": source, "steps_not_applicable": n_out},
        "lease": None if grantor is None else {
            "grantor_run": grantor["run_id"], "granted_at_step": args.leased_at},
        "first_step": None if not first else {"id": first, "title": f.step(first).title},
    }, lambda _d: None)


def cmd_status(args) -> int:
    conn = store.connect(read_only=True)
    try:
        if args.run:
            row = _run_or_exit(conn, args.run, closed_ok=True)
            f = _flow_for_run(conn, row)
            done = store.closed_steps(conn, row["run_id"])
            owed_ids = [x for x in f.required_steps(row["variant"]) if x not in done]
            nxt_ids = [x for x in f.order
                       if x not in done and f.applicable(x, row["variant"])]
            if args.json:
                return _emit(args, {
                    "run": {k: row[k] for k in row.keys() if k != "metadata_json"},
                    "actor": json.loads(row["metadata_json"] or "{}").get("actor"),
                    "ability": row["ability"], "title": f.title,
                    "closed_steps": sorted(done), "step_count": len(f.steps),
                    "owed": owed_ids, "closeable": not owed_ids,
                    "next": nxt_ids[0] if nxt_ids else None,
                    "phases": [{"phase": ph, "title": f.phase_titles[ph],
                                "steps": [{"step": st.id, "closed": st.id in done,
                                           "optional": st.optional, "stage": st.stage}
                                          for st in f.steps_in_phase(ph)]}
                               for ph in f.phases],
                    "violations": _rowdicts(store.violations(conn, row["run_id"])),
                }, lambda _d: None)
            print(f"run     {row['run_id']}")
            print(f"ability {row['ability']}  ({f.title})")
            print(f"scope   {row['scope_kind']}={row['scope_key']}")
            print(f"status  {row['status']}" + (f" → {row['result']}" if row["result"] else ""))
            opt = sum(1 for s in f.steps.values() if s.optional)
            print(f"steps   {len(done)}/{len(f.steps)} accounted for"
                  f"  ({len(f.required_steps())} required, {opt} optional)")
            for phase in f.phases:
                stages = f.stages_of(phase)
                if stages == [None]:
                    steps = f.steps_in_phase(phase)
                    marks = "".join(_mark(s, done) for s in steps)
                    print(f"  {marks:<14} {phase} ({f.phase_titles[phase]})")
                else:
                    print(f"  {phase} ({f.phase_titles[phase]})")
                    for stage in stages:
                        steps = f.steps_in_stage(phase, stage)
                        if not steps:
                            continue
                        marks = "".join(_mark(s, done) for s in steps)
                        print(f"    {marks:<14} {stage or '(unstaged)'}")
            print(f"owed    {len(owed_ids)} required step(s) remain" if owed_ids
                  else "owed    nothing — closeable")
            if nxt_ids:
                print(f"next    {nxt_ids[0]}  ({f.step(nxt_ids[0]).title})")
            v = store.violations(conn, row["run_id"])
            if v:
                print(f"⚠️  {len(v)} violation(s): "
                      f"{', '.join(sorted({r['code'] for r in v}))}")
            return OK
        # Read the table rather than the open-runs view: the view does not carry metadata, and
        # widening it would not reach a store that already exists — `init()` is IF NOT EXISTS
        # throughout and there is no migration step, so an added view column would appear on
        # new machines and silently not on developed-in-place ones.
        rows = conn.execute(
            "SELECT * FROM run WHERE status = 'open' ORDER BY updated_at DESC").fetchall()
        # Delegations, so three concurrent runs do not read as three unrelated peers.
        grants: dict[str, str] = {}
        holds: dict[str, str] = {}
        for lz in conn.execute(
                "SELECT grantor_run_id g, holder_run_id h FROM scope_lease"
                " WHERE released_at IS NULL"):
            grants[lz["g"]] = lz["h"]
            holds[lz["h"]] = lz["g"]
        scopes: dict[tuple[str, str], set[str]] = {}
        for r in rows:
            scopes.setdefault((r["scope_kind"], r["scope_key"]), set()).add(r["run_id"])
        shared = {k: v for k, v in scopes.items() if len(v) > 1}
        if args.json:
            verdicts = []
            for (kind, key), ids in sorted(shared.items()):
                chain, why = store.resolve_scope_chain(conn, kind, key, ids)
                verdicts.append({"scope_kind": kind, "scope_key": key,
                                 "runs": sorted(ids), "adjudicable": why is None,
                                 "chain": chain, "reason": why})
            return _emit(args, {
                "open_runs": [{**{k: r[k] for k in r.keys() if k != "metadata_json"},
                               "actor": json.loads(r["metadata_json"] or "{}").get("actor"),
                               "leased_from": holds.get(r["run_id"]),
                               "delegated_to": grants.get(r["run_id"])}
                              for r in rows],
                "shared_scopes": verdicts,
            }, lambda _d: None)
        if not rows:
            print("(no open runs)")
            return OK
        for r in rows:
            who = json.loads(r["metadata_json"] or "{}").get("actor")
            rel = ""
            if r["run_id"] in holds:
                rel += f"  ← leased from {holds[r['run_id']]}"
            if r["run_id"] in grants:
                rel += f"  → delegated to {grants[r['run_id']]}"
            print(f"{r['run_id']}  {r['ability']:<14} "
                  f"{r['scope_kind']}={r['scope_key']:<28} step={r['current_step']}"
                  + (f"  actor={who}" if who else "") + rel)
        # Whether each shared scope is adjudicable AT ALL. This is the question a human
        # actually has on seeing two runs in one scope, and until now it could only be
        # discovered by triggering a guard and reading the warning it printed.
        if shared:
            print()
            for (kind, key), ids in sorted(shared.items()):
                chain, why = store.resolve_scope_chain(conn, kind, key, ids)
                if why is None:
                    print(f"{kind}={key}: {len(ids)} runs, {' → '.join(chain)}"
                          f"  — guards adjudicate")
                else:
                    print(f"{kind}={key}: {len(ids)} runs, no usable delegation ({why})"
                          f"  — ⚠️  guards will NOT adjudicate here")
        return OK
    finally:
        conn.close()


def cmd_next(args) -> int:
    conn = store.connect(read_only=True)
    try:
        row = _run_or_exit(conn, args.run, closed_ok=True)
        f = _flow_for_run(conn, row)
        done = store.closed_steps(conn, row["run_id"])
        pending = [sid for sid in f.order
                   if sid not in done and f.applicable(sid, row["variant"])]
        if not pending:
            return _emit(args, {"run": row["run_id"], "step": None, "remaining": [],
                                "all_closed": True},
                         lambda _d: print("✅ all steps closed"))
        sid = pending[0]
        s = f.step(sid)
        blocked = [d for d in s.deps if d not in done]
        try:
            guide = prose.resolve_guide(f, s.id)
        except prose.ProseError as exc:
            _err(f"  ⚠️  guide unresolvable: {exc}")
            guide = None
        if args.json:
            # `requirements()` is already a machine-readable statement of what the step asks
            # for — it was being rendered into columns. This is the same list, unflattened.
            return _emit(args, {
                "run": args.run, "step": s.id, "title": s.title,
                "phase": s.phase, "phase_title": f.phase_titles[s.phase], "stage": s.stage,
                "autonomy": s.autonomy, "gate": s.gate,
                "strict_witness": s.strict_witness,
                "completion": s.completion.get("type"),
                "requirements": predicates.requirements(s.completion),
                "repeatable": s.repeatable, "budget": s.budget,
                "optional": s.optional,
                "exclusive_group": list(f.group_of(s.id) or ()),
                "blocked_on": blocked,
                "directive": s.directive,
                "guide": None if guide is None else {
                    "file": str(guide.path), "level": guide.level,
                    "whole_file": guide.whole_file, "anchor": guide.anchor},
                "topics": list(s.topics),
                "remaining": pending,
            }, lambda _d: None)
        print(f"▶ {s.id}  {s.title}")
        print(f"  phase      {s.phase} ({f.phase_titles[s.phase]})")
        print(f"  autonomy   {s.autonomy}")
        if s.stage:
            print(f"  stage      {s.stage}")
        print(f"  gate       {s.gate}"
              + ("  (strict witness — no downgrade)" if s.strict_witness else ""))
        print(f"  completion {s.completion.get('type')}")
        for req in predicates.requirements(s.completion):
            if req["what"] == "evidence":
                bits = [f"kind '{req['kind']}'"]
                if req.get("min_count"): bits.append(f"x{req['min_count']}")
                if "must_equal" in req: bits.append(f"== {req['must_equal']!r}")
                if req.get("must_be_one_of"):
                    bits.append(("every row ∈ " if req.get("every_row") else "∈ ")
                                + str(req["must_be_one_of"]))
                if req.get("note"): bits.append(f"({req['note']})")
                print(f"    · record evidence {' '.join(bits)}")
            elif req["what"] == "gate":
                print(f"    · {req['detail']}")
            else:
                print(f"    · {req['detail']}")
        if s.repeatable:
            used = store.step_attempts(conn, args.run, s.id) if False else None
            print(f"  ↻ repeatable"
                  + (f", budget {s.budget} (refused once used up)"
                     if s.budget else " (no budget)"))
        if s.optional:
            print(f"  ⏭  OPTIONAL — may be skipped: "
                  f"harness skip --run {args.run} --step {s.id} --reason <why>")
        grp = f.group_of(s.id)
        if grp:
            print(f"  ⚡ one of: {', '.join(grp)} — closing one skips the rest")
        if blocked:
            print(f"  ⛔ blocked on: {', '.join(blocked)}")
        if s.directive:
            print("  ── directive ──")
            for line in s.directive.splitlines():
                print(f"  {line}")
        # Deliberately the POINTER, not the text. Dumping a document on every `next`
        # would drown the driver and defeat the point of tiering it.
        if guide is not None:
            where = "whole file" if guide.whole_file else f"section '{guide.anchor}'"
            print(f"  ── guide ({guide.level}-level, {where}) ──")
            print(f"  {guide.path.name}   →  harness show --run {args.run} --step {s.id}")
        if s.topics:
            print(f"  ── topics (read only if you hit trouble) ──")
            for name in s.topics:
                print(f"  {name}   →  harness show --run {args.run} --step {s.id} "
                      f"--topic {name}")
        return OK
    finally:
        conn.close()


def cmd_enter(args) -> int:
    conn = store.connect()
    try:
        row = _run_or_exit(conn, args.run)
        f = _flow_for_run(conn, row)
        s = f.step(args.step)
        if not f.applicable(s.id, row["variant"]):
            # Not a choice to refuse — under this variant the step is not part of the flow.
            # Allowing it (even with a warning) is what makes a mode-specific step runnable in
            # the wrong mode, close cleanly, and read afterwards exactly like a correct run.
            _err(f"⛔ REFUSED: '{s.id}' is not part of this flow.\n"
                 f"    This run's variant is {row['variant']!r}; '{s.id}' belongs to "
                 f"{', '.join(s.variants)} only.\n"
                 f"    Not skippable either — a skip records a decision, and there is no "
                 f"decision to record about a step that does not apply.")
            return REFUSED
        done = store.closed_steps(conn, row["run_id"])
        missing = [d for d in s.deps if d not in done]
        if missing and not args.force_deps:
            store.log_step(conn, row["run_id"], s.id, "refused",
                           f"deps open: {','.join(missing)}")
            _err(f"⛔ REFUSED: '{s.id}' depends on unclosed step(s): {', '.join(missing)}")
            return REFUSED
        attempts = store.step_attempts(conn, row["run_id"], s.id)
        if attempts and not s.repeatable:
            _err(
                f"⛔ REFUSED: '{s.id}' has already been entered and is not repeatable.\n"
                f"    Declare `repeatable: true` in the flow if this step really may run again."
            )
            return REFUSED
        if s.budget and attempts >= s.budget:
            store.record_violation(conn, row["run_id"], s.id, "budget_exhausted",
                                   f"{attempts} attempt(s), budget {s.budget}",
                                   severity="blocked")
            _err(
                f"⛔ REFUSED: '{s.id}' has used its budget of {s.budget} attempt(s).\n"
                f"    Repeating past the budget is not a matter of trying harder. Change\n"
                f"    approach, or raise the budget in the spec if {s.budget} was simply the\n"
                f"    wrong number."
            )
            return REFUSED
        prior = store.closed_steps(conn, row["run_id"])
        entered_before = {r["step_id"] for r in conn.execute(
            "SELECT DISTINCT step_id FROM step_log WHERE run_id = ? AND event = 'entered'",
            (row["run_id"],)).fetchall()}
        store.log_step(conn, row["run_id"], s.id, "entered",
                       "forced past open deps" if (missing and args.force_deps) else None)
        store.touch_run(conn, row["run_id"], current_step=s.id)
        quiet = bool(getattr(args, "json", False))
        _sayer(args)(f"▶ entered {s.id} ({s.title})")
        # phase_start fires when the phase's FIRST step is entered — i.e. no step of this
        # phase had been entered or closed before now.
        phase_ids = {x.id for x in f.steps_in_phase(s.phase)}
        phase_started = not (phase_ids & (prior | entered_before))
        rc, fired = OK, []
        if phase_started:
            rc, fired = _fire(conn, f, row, "phase_start", s.phase, quiet=quiet)
        _emit(args, {
            "run": row["run_id"], "step": s.id, "title": s.title, "phase": s.phase,
            "entered": True,
            "attempts_before": attempts,
            "forced_past_deps": bool(missing and args.force_deps),
            # Which deps were left open, not just that some were. A forced entry is a
            # breach worth naming precisely.
            "deps_forced": missing if (missing and args.force_deps) else [],
            # The TRIGGER firing and a HOOK matching are different events: a phase can start
            # with no hook attached, and reporting only `fired_hooks` would make the two
            # indistinguishable.
            "phase_start_fired": phase_started,
            "fired_hooks": fired,
            "hooks_blocked": rc != OK,
        }, lambda _d: None)
        return rc
    finally:
        conn.close()


def cmd_close_step(args) -> int:
    conn = store.connect()
    try:
        row = _run_or_exit(conn, args.run)
        f = _flow_for_run(conn, row)
        s = f.step(args.step)

        done = store.closed_steps(conn, row["run_id"])
        missing = [d for d in s.deps if d not in done]
        if missing:
            store.log_step(conn, row["run_id"], s.id, "refused",
                           f"deps open: {','.join(missing)}")
            _err(f"⛔ REFUSED: '{s.id}' depends on unclosed step(s): {', '.join(missing)}")
            if getattr(args, "json", False):
                _emit(args, {"run": row["run_id"], "step": s.id, "closed": False,
                             "refused_because": "deps_open", "deps_open": missing},
                      lambda _d: None)
            return REFUSED

        ok, why = predicates.check(conn, row["run_id"], s, s.completion,
                                   _facts_resolver(f, row, s.completion))
        if not ok:
            store.log_step(conn, row["run_id"], s.id, "refused", why.splitlines()[0])
            _err(f"⛔ REFUSED: '{s.id}' is not complete.\n    {why}")
            if getattr(args, "json", False):
                # `why` rides as prose on purpose. It is a sentence naming what is missing and
                # the command that supplies it; re-encoding it as fields would be a second copy
                # of the same message, free to drift from the one stderr prints.
                _emit(args, {"run": row["run_id"], "step": s.id, "closed": False,
                             "refused_because": "criterion_unmet", "why": why,
                             "required": predicates.requirements(s.completion)},
                      lambda _d: None)
            return REFUSED

        store.log_step(conn, row["run_id"], s.id, "closed", why)

        # An exclusive group resolves the moment one branch closes: its siblings are
        # recorded as skipped so nothing downstream waits on a branch that will never run.
        siblings_skipped = []
        grp = f.group_of(s.id)
        if grp:
            for sib in grp:
                if sib != s.id and sib not in done:
                    store.log_step(conn, row["run_id"], sib, "skipped",
                                   f"exclusive group resolved by {s.id}")
                    siblings_skipped.append(sib)
        done = done | {s.id} | set(siblings_skipped)

        remaining = [sid for sid in f.order if sid not in done]
        store.touch_run(conn, row["run_id"],
                        current_step=remaining[0] if remaining else None)
        quiet = bool(getattr(args, "json", False))
        if not quiet:
            print(f"✅ closed {s.id} ({s.title}) — {why.splitlines()[0]}")

        # WHAT THIS STEP HANDS BACK. Read from a row the driver recorded AT THIS STEP — not
        # run-wide: the payload is "what this step produced", and reaching into another step's
        # rows would make it depend on unrelated history. Read BEFORE the hooks fire, so a hook
        # cannot change what is reported as the step's own product.
        out_payload = None
        if s.output:
            recorded = [r["value"] for r in
                        store.find_evidence(conn, row["run_id"], s.id, s.output["from_evidence"])]
            out_payload = outputs.deliver(s.output, recorded=recorded)
            # The digest and the source, never the content: "what was handed back" stays
            # answerable while the ledger stays a ledger. Same shape as the extension pinning.
            store.log_step(conn, row["run_id"], s.id, "output", json.dumps(
                {k: v for k, v in out_payload.items() if k != "content"}, ensure_ascii=False))
            if not quiet:
                print("   " + outputs.summarise(out_payload))

        rc_hooks, fired = _fire(conn, f, row, "step_close", s.id, quiet=quiet)
        # phase_end fires once every REQUIRED step of the phase is accounted for.
        req_in_phase = [x for x in f.required_steps(row["variant"])
                        if f.step(x).phase == s.phase]
        if req_in_phase and all(x in done for x in req_in_phase):
            rc2, fired2 = _fire(conn, f, row, "phase_end", s.phase, quiet=quiet)
            rc_hooks = rc_hooks or rc2
            fired = fired + fired2
        if quiet:
            _emit(args, {
                "run": row["run_id"], "step": s.id, "closed": True,
                "satisfied_by": predicates.requirements(s.completion),
                "why": why,
                "skipped_siblings": siblings_skipped,
                "next": remaining[0] if remaining else None,
                "remaining": remaining,
                "fired_hooks": fired,
                "output": out_payload,
                # A fail_closed hook command that failed does not un-close the step; it changes
                # the exit code. Reported so a caller need not infer it from that code.
                "hooks_blocked": rc_hooks != OK,
            }, lambda _d: None)
            return rc_hooks
        if siblings_skipped:
            print(f"   ↳ exclusive group resolved; skipped {', '.join(siblings_skipped)}")
        if remaining:
            print(f"   next: {remaining[0]} ({f.step(remaining[0]).title})")
        return rc_hooks
    finally:
        conn.close()


def cmd_gate(args) -> int:
    conn = store.connect()
    try:
        row = _run_or_exit(conn, args.run)
        f = _flow_for_run(conn, row)
        s = f.step(args.step)

        if s.gate == flowmod.GATE_NONE:
            _err(f"⛔ step '{s.id}' declares no gate — nothing to record")
            return USAGE

        if args.decision == "preauth":
            key = s.preauth_key
            if key is None:
                store.record_violation(conn, row["run_id"], s.id, "unauthorised_preauth",
                                       f"preauth used on gate '{s.gate}'",
                                       severity="blocked")
                _err(
                    f"⛔ REFUSED: '{s.id}' has gate '{s.gate}', which requires a human.\n"
                    f"    'preauth' is only accepted on a '{flowmod.GATE_PREAUTH_PREFIX}<key>' gate.\n"
                    f"    Recorded as a violation."
                )
                return REFUSED
            meta = json.loads(row["metadata_json"] or "{}")
            cfg = {**f.config_defaults, **(meta.get("config") or {})}
            if not cfg.get(key):
                store.record_violation(conn, row["run_id"], s.id, "unauthorised_preauth",
                                       f"config key '{key}' is not enabled",
                                       severity="blocked")
                _err(
                    f"⛔ REFUSED: '{s.id}' allows pre-authorisation via config key "
                    f"'{key}', but it is not enabled.\n"
                    f"    Enable it with: harness config --run {row['run_id']} "
                    f"--set {key}=true\n"
                    f"    Do NOT infer pre-authorisation from the run's progress.\n"
                    f"    Recorded as a violation."
                )
                return REFUSED
            proof_doc = {"witness": "config", "witnessed": True, "config_key": key}
        else:
            cursor = _witness_cursor(conn, row["run_id"])
            try:
                proof_doc = proof.vouch(cursor=cursor)
                if s.strict_witness and not proof_doc.get("witnessed"):
                    # This gate is marked as one where an annotated downgrade is not an
                    # acceptable outcome. Refuse instead of recording a flagged pass.
                    raise proof.NoWitness(
                        f"step '{s.id}' is marked strict_witness: an unwitnessed gate is "
                        f"refused outright rather than recorded with a flag.\n"
                        f"  Get a real human affirmation on a channel the witness can see."
                    )
            except proof.NoWitness as exc:
                store.record_violation(conn, row["run_id"], s.id, "gate_refused_unwitnessed",
                                       str(exc), severity="blocked")
                _err(f"⛔ REFUSED: cannot record an affirm gate for '{s.id}'.\n    {exc}")
                return REFUSED

        store.record_gate(conn, row["run_id"], s.id, args.decision, args.evidence, proof_doc)
        if not proof_doc.get("witnessed"):
            # An unwitnessed gate goes on the violation ledger too, not just into the
            # audit view. Without this a run could satisfy a `no_open_violations`
            # predicate while holding a gate nobody vouched for — the degradation would
            # launder itself into a clean run, which is precisely the silent kind.
            store.record_violation(
                conn, row["run_id"], s.id, "unwitnessed_gate",
                f"recorded via witness '{proof_doc.get('witness')}' with no independent proof",
            )
        unwitnessed = not proof_doc.get("witnessed")
        flag = "  ⚠️ UNWITNESSED (recorded as a violation)" if unwitnessed else ""
        quiet = bool(getattr(args, "json", False))
        _sayer(args)(f"✅ gate {s.id} = {args.decision}"
                     f"  (witness={proof_doc.get('witness')}){flag}")
        rc, fired = OK, []
        if args.decision != "decline":
            rc, fired = _fire(conn, f, row, "gate_recorded", s.id, quiet=quiet)
        _emit(args, {
            "run": row["run_id"], "step": s.id, "decision": args.decision,
            "witness": proof_doc.get("witness"),
            # RECORDED IS NOT THE SAME AS VOUCHED FOR. The default witness degrades rather
            # than refusing, so a caller reading only "gate recorded" would not learn that
            # this one passed with nobody attesting to it.
            "witnessed": not unwitnessed,
            "violation_recorded": unwitnessed,
            "fired_hooks": fired,
            "hooks_blocked": rc != OK,
        }, lambda _d: None)
        return rc
    finally:
        conn.close()


def _facts_resolver(f, row, spec: dict):
    """A lazy resolver for a completion criterion that consults derived facts.

    Narrowed to the providers that OWN the facts the spec actually references, the same way
    hook evaluation narrows — a criterion asking one question must not drag every provider's
    subprocess along with it. Returns None when the spec references no fact, so nothing is
    gathered and `predicates.check` never calls out.
    """
    from . import conditions

    needed: set = set()

    def walk(sp) -> None:
        if not isinstance(sp, dict):
            return
        if str(sp.get("type")) == "all_checks":
            for sub in sp.get("checks") or []:
                walk(sub)
            return
        if "disproved_when" in sp:
            needed.update(conditions.facts_referenced(sp["disproved_when"]))

    walk(spec)
    if not needed:
        return None

    def resolve() -> dict:
        use = tuple(pn for pn in f.facts_providers
                    if any(f.facts_owner.get(k) == pn for k in needed))
        values = facts.gather_all(use, {
            "run_id": row["run_id"], "scope": row["scope_key"],
            "scope_kind": row["scope_kind"], "ability": row["ability"],
        }) if use else {}
        values["run_variant"] = row["variant"] or ""
        return values

    return resolve


def _fire(conn, f, row, on: str, selector: str, *, quiet: bool = False) -> tuple[int, list[dict]]:
    """Fire the hooks on one boundary. Returns (code, what fired) — BOTH, deliberately.

    The code is non-OK only when a fail_closed command failed; everything else is reported and
    allowed through. What changed here is the second half: a hook firing, and above all an
    OBLIGATION being raised, used to be announced in prose and nowhere else. `obligations` could
    list it afterwards, but the moment it was created was unreadable by anything that is not a
    human — so a driver either parsed the print or missed the debt it had just been handed.
    """
    def say(line: str) -> None:
        if not quiet:
            print(line)

    try:
        fired = hookmod.fire(conn, f, row, on, selector)
    except facts.FactsError as exc:
        # A provider that ERRORS must be loud: treating it as "no facts" would turn every
        # condition false and mute every hook, which is the exact failure this design
        # exists to prevent.
        _err(f"⛔ facts unavailable for {on} '{selector}': {exc}")
        return REFUSED, [{"boundary": on, "selector": selector, "facts_error": str(exc)}]
    blocked = OK
    data: list[dict] = []
    for fr in fired:
        h = fr.hook
        entry: dict = {"boundary": on, "selector": selector, "hook": h.id,
                       "matched": fr.matched, "mode": h.mode,
                       "obligation": h.obligation or None}
        if not fr.matched:
            entry["trace"] = list(fr.trace)
            data.append(entry)
            say(f"  ⃝ hook {h.id}: condition not met")
            for line in fr.trace:
                say(f"      {line}")
            continue
        tag = " [obligation]" if h.obligation else ""
        say(f"  🔔 hook {h.id}{tag}")
        if h.mode == hookmod.MODE_CONTRACT:
            entry["rendered"] = fr.rendered
            entry["obligation_created"] = bool(fr.obligation_created)
            for line in fr.rendered.splitlines():
                say(f"      {line}")
            if fr.obligation_created:
                entry["discharge_with"] = (f"harness discharge --run {row['run_id']} "
                                           f"--hook {h.id} --evidence <what you did>")
                say(f"      ↳ discharge with: harness discharge --run {row['run_id']} "
                    f"--hook {h.id} --evidence \"<what you did>\"")
        else:
            ok = fr.command_rc == 0
            entry.update({"command_exit": fr.command_rc, "fail_closed": bool(h.fail_closed),
                          "command_out": fr.command_out or ""})
            say(f"      command exit {fr.command_rc}"
                + ("" if ok else ("  ⛔ fail_closed" if h.fail_closed else "  ⚠️ warning only")))
            if fr.command_out:
                for line in fr.command_out.splitlines()[:8]:
                    say(f"      | {line}")
            if not ok and h.fail_closed:
                blocked = REFUSED
        data.append(entry)
    return blocked, data


def _mark(step, done: set) -> str:
    """● closed/skipped · ◌ optional & untouched · ○ still owed."""
    if step.id in done:
        return "●"
    return "◌" if step.optional else "○"


def _witness_cursor(conn, run_id: str) -> dict | None:
    """The high-water mark of every witness proof recorded on this run.

    NOT "the proof of the most recent gate", which is what this used to be and which had a
    hole wide enough to drive through:

      * a gate authorised some other way — by config pre-authorisation, or by the recorded
        downgrade — carries no turn count, so reading only the LATEST proof yielded no
        cursor at all and the check silently skipped. One intervening preauth gate was
        therefore enough to let the NEXT affirm gate claim a human reply that had already
        been spent. Observed directly: two gates both recording `human_turns: 1` against a
        transcript that only ever had one.
      * `recorded_at` is second-granularity, so "the latest gate" was a non-deterministic
        tie-break whenever two gates landed in the same second — the check would fire or
        not depending on which row the sort happened to return.

    Taking the MAXIMUM over the whole run fixes both at once: it is monotonic, so nothing
    can lower it; it ignores gates that carry no count instead of being reset by them; and
    it needs no ordering, so ties cannot change the answer.
    """
    rows = conn.execute(
        "SELECT proof_json FROM gate WHERE run_id = ?", (run_id,)
    ).fetchall()
    best: dict | None = None
    high = -1
    for r in rows:
        try:
            p = json.loads(r["proof_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        n = p.get("human_turns")
        if isinstance(n, int) and n > high:
            high, best = n, p
    return best


def cmd_guard(args) -> int:
    """The external-tool hook. Answers ONE question: may this action proceed?

    Exit 4 = block. Everything else allows, because a guard that misfires on an
    unrelated action is worse than one that misses: being told to satisfy a gate that
    does not belong to your work leaves forging that gate as the only way forward.
    """
    def answer(verdict: str, **extra) -> int:
        """One place builds the answer, so the prose and the payload cannot disagree."""
        assert verdict in GUARD_VERDICTS, verdict
        _emit(args, {"action": args.action, "scope_kind": args.scope_kind, "scope": args.scope,
                     "allowed": verdict != "blocked", "verdict": verdict,
                     "why": GUARD_VERDICTS[verdict], **extra}, lambda _d: None)
        return BLOCKED if verdict == "blocked" else OK

    try:
        conn = store.connect(read_only=True)
    except store.StoreUnusable as exc:
        # Allow, but say it out loud. A guard runs in front of every matching tool call, so a
        # store that is absent — or at a schema this engine will not read — must not brick the
        # machine. It also cannot be RECORDED, because recording needs the very store just
        # refused; that asymmetry is why this one prints instead of leaving a row.
        _err(f"⚠️  guard not enforced: {str(exc).splitlines()[0]}")
        return answer("store_unusable", detail=str(exc).splitlines()[0])
    try:
        candidates = store.open_runs_in_scope(conn, args.scope_kind, args.scope)
        if not candidates:
            return answer("no_run_in_scope", runs=[])
        by_id = {r["run_id"]: r for r in candidates}
        if len(candidates) == 1:
            chain = [candidates[0]["run_id"]]
        else:
            # More than one run shares this scope. A DECLARED delegation makes "who owns this
            # action" answerable; nothing else does. Where it is answerable, adjudicate —
            # where it is not, keep allowing, but leave a row saying so.
            chain, why = store.resolve_scope_chain(
                conn, args.scope_kind, args.scope, set(by_id))
            if why is not None:
                _record_unadjudicated(args.scope_kind, args.scope, args.action,
                                      list(by_id), why)
                _err(
                    f"⚠️  guard NOT enforced for action '{args.action}': scope "
                    f"{args.scope_kind}={args.scope!r} has {len(candidates)} open runs "
                    f"({', '.join(by_id)}) and no usable delegation ({why}).\n"
                    f"    Allowing rather than guessing which one owns this action.\n"
                    f"    Recorded as '{UNADJUDICATED}'. To make this adjudicable, start the "
                    f"inner run with --leased-from/--leased-at."
                )
                return answer("unadjudicated", runs=sorted(by_id),
                              violation_recorded=True, detail=why)
        # EVERY run in the chain that guards this action must have its gate — not only the
        # holder. Otherwise delegating becomes the way around a gate: the outer run says "no
        # such action here until a human affirms", hands the scope to a flow that guards
        # nothing, and the action goes through. The outer gate is not an unrelated run's gate:
        # that run authorised the delegation, over this very scope.
        # THE WHOLE CHAIN IS EXAMINED BEFORE ANYTHING IS DECIDED. Returning at the first block
        # was cheaper and made the answer lie: `unreadable` would come back EMPTY because the
        # links past the block were never looked at, and an empty list reads as "everything was
        # readable". A payload may not report on what it did not check. Chains are one or two
        # links, so the cost is nil.
        unreadable: list[dict] = []
        gated: list[dict] = []
        blocks: list[dict] = []
        for i, run_id in enumerate(chain):
            row = by_id[run_id]
            try:
                f = flowmod.load(row["ability"])
            except flowmod.FlowError as exc:
                # THE SAME HOLE THE HOOK SIDE HAD, on the side a caller ASKS. Skipping in
                # silence made "this run declares no guard for the action" and "I could not
                # read the flow that might" produce the identical answer — and once this
                # command answers as data, the first of those is a claim the engine would be
                # stating without being able to back it.
                #
                # Three causes, all of them soundless until now: the ability is not on this
                # process's search path, its spec is invalid, or its extension code is not
                # approved here so loading refuses.
                unreadable.append({
                    "run": run_id, "ability": row["ability"],
                    "why": " — ".join(x.strip() for x in str(exc).split("\n")[:2] if x.strip()),
                })
                continue
            step_id = f.guards.get(args.action)
            if step_id is None:
                continue  # this ability does not guard this action
            g = store.get_gate(conn, run_id, step_id)
            if g is not None and g["decision"] in ("affirm", "preauth"):
                gated.append({"run": run_id, "step": step_id, "decision": g["decision"]})
                continue
            blocks.append({"run": run_id, "step": step_id, "title": f.step(step_id).title,
                            "link": i + 1, "of": len(chain)})

        if unreadable:
            _err(f"⚠️  guard: {len(unreadable)} run(s) in this scope whose flow it CANNOT "
                 f"READ.\n"
                 f"    Their guards were not consulted — that is \"the guard could not be "
                 f"looked up\",\n"
                 f"    not \"there is no guard\".")
            for u in unreadable:
                _err(f"      {u['run']} ({u['ability']}): {u['why']}")
            _err("    Fix whichever applies:\n"
                 "      · not visible here     → HARNESS_ABILITIES_PATH must cover the runs\n"
                 "      · spec invalid         → harness validate <ability>\n"
                 "      · extension unapproved → harness trust <ability>")

        if blocks:
            b = blocks[0]
            where = "" if b["of"] == 1 else f" (link {b['link']} of {b['of']} in this scope)"
            _err(
                f"⛔ BLOCKED: action '{args.action}' requires gate '{b['step']}' "
                f"({b['title']}) on run {b['run']}{where}.\n"
                f"    Do not proceed. Get a real human affirmation, then record it:\n"
                f"      harness gate --run {b['run']} --step {b['step']} "
                f"--decision affirm --evidence \"<what they said>\"\n"
                f"    Recording it without a witnessed human turn is refused."
            )
            # A block stands whatever else could not be read: more information cannot turn a
            # refusal into an allow. Reported WITH the partial view rather than instead of it.
            return answer("blocked", blocked_by=b, unreadable=unreadable)
        if unreadable:
            return answer("view_incomplete", unreadable=unreadable, gate_recorded=gated)
        if gated:
            return answer("gate_recorded", gate_recorded=gated)
        return answer("not_guarded", runs=sorted(by_id))
    finally:
        conn.close()


def cmd_show(args) -> int:
    """Print the prose behind a step: its guide section, or one of its topics.

    Separate from `next` on purpose. `next` answers "what now" in a few lines; this
    answers "give me the detail", and only when asked.
    """
    conn = store.connect(read_only=True)
    try:
        row = _run_or_exit(conn, args.run, closed_ok=True)
        f = _flow_for_run(conn, row)
        s = f.step(args.step)
    finally:
        conn.close()

    if args.topic:
        if args.topic not in s.topics:
            _err(
                f"⛔ step '{s.id}' does not cite topic '{args.topic}'.\n"
                f"    it cites: {', '.join(s.topics) or '(none)'}"
            )
            return USAGE
        try:
            path = prose.resolve_topic(f, args.topic)
        except prose.ProseError as exc:
            _err(f"⛔ {exc}")
            return BAD_SPEC
        print(f"# topic: {args.topic}   ({path})\n")
        print(path.read_text(encoding="utf-8").rstrip())
        return OK

    try:
        r = prose.resolve_guide(f, s.id)
    except prose.ProseError as exc:
        _err(f"⛔ {exc}")
        return BAD_SPEC
    if r is None:
        print(f"(step '{s.id}' declares no guide at step, stage, or phase level)")
        if s.directive:
            print()
            print(s.directive)
        return OK
    scope = "whole file" if r.whole_file else f"section '{r.anchor}'"
    print(f"# guide for {s.id} — {r.level}-level pointer, {scope}   ({r.path})\n")
    print(r.text.rstrip())
    return OK


def _check_goal(conn, row, f, phase: str):
    """Evaluate a phase's goal. Returns (applicable, ok, why)."""
    spec = f.phase_goals.get(phase)
    if spec is None:
        return False, True, "no goal declared for this phase"
    subject = flowmod.GoalSubject(phase=phase, id=f"@{phase}")
    ok, why = predicates.check(conn, row["run_id"], subject, spec,
                               _facts_resolver(f, row, spec))
    return True, ok, why


def cmd_assert_goal(args) -> int:
    """Ask whether a phase MEETS its acceptance criterion. Read-only.

    Separate from `summarize` so it can be asked at any time — the answer is the same
    question `summarize` refuses on, and being able to ask it early is what lets a caller
    find out what a layer still owes before trying to close it out.
    """
    conn = store.connect()
    try:
        row = _run_or_exit(conn, args.run, closed_ok=True)
        f = _flow_for_run(conn, row)
        if args.phase not in f.phases:
            _err(f"⛔ ability '{row['ability']}' declares no phase '{args.phase}'.\n"
                 f"    phases: {', '.join(f.phases)}")
            return USAGE
        applicable, ok, why = _check_goal(conn, row, f, args.phase)
        say = _sayer(args)
        if not applicable:
            say(f"⃝  phase '{args.phase}' declares no goal — nothing to assert")
            # `declared: false` with `met: null`. A phase with no criterion and a phase whose
            # criterion holds both exit 0, and only this tells them apart — reporting `met:
            # true` for the first would be the answer inventing a check nobody wrote.
            return _emit(args, {"run": row["run_id"], "phase": args.phase,
                                "declared": False, "met": None, "why": None},
                         lambda _d: None)
        if ok:
            say(f"✅ phase '{args.phase}' goal met: {why}")
            return _emit(args, {"run": row["run_id"], "phase": args.phase,
                                "declared": True, "met": True, "why": why},
                         lambda _d: None)
        _err(f"⛔ REFUSED: phase '{args.phase}' goal NOT met.\n    {why}")
        # Emitted here rather than left to the backstop: `met: false` is a fact a caller can
        # branch on, and the backstop can only offer the sentence.
        _emit(args, {"run": row["run_id"], "phase": args.phase,
                     "declared": True, "met": False, "why": why}, lambda _d: None)
        return REFUSED
    finally:
        conn.close()


def cmd_summarize(args) -> int:
    """Record a phase's rollup. Explicit on purpose — see the table's comment.

    Deriving the step lists is trivial; the value is the attestation that somebody wrapped
    the phase up. A phase whose steps all closed and which nobody summarised is a phase
    nobody looked back at.
    """
    conn = store.connect()
    try:
        row = _run_or_exit(conn, args.run)
        f = _flow_for_run(conn, row)
        if args.phase not in f.phases:
            _err(f"⛔ ability '{row['ability']}' declares no phase '{args.phase}'.\n"
                 f"    phases: {', '.join(f.phases)}")
            return USAGE
        # THE CHOKEPOINT. A phase is wrapped up only if it MEETS its acceptance criterion.
        # Wiring the goal here rather than adding a separate mandatory command is what makes
        # it unskippable: `close-run` demands summaries (phases_summarized), a summary demands
        # the goal, so the criterion sits on the path to closing the run instead of beside it.
        applicable, ok, why = _check_goal(conn, row, f, args.phase)
        if applicable and not ok:
            store.record_violation(conn, row["run_id"], None, "phase_goal_unmet",
                                   f"{args.phase}: {why.splitlines()[0]}",
                                   severity="blocked")
            conn.commit()
            _err(f"⛔ REFUSED: cannot summarize phase '{args.phase}' — its goal is not met.\n"
                 f"    {why}\n"
                 f"    (asked any time with: harness assert-goal --run {args.run} "
                 f"--phase {args.phase})")
            return REFUSED

        done = store.closed_steps(conn, row["run_id"])
        in_phase = [s.id for s in f.steps_in_phase(args.phase)]
        skipped = {r["step_id"] for r in conn.execute(
            "SELECT DISTINCT step_id FROM step_log WHERE run_id = ? AND event = 'skipped'",
            (row["run_id"],)).fetchall()}
        closed = [x for x in in_phase if x in done and x not in skipped]
        skips = [x for x in in_phase if x in skipped]

        metrics = {}
        for pair in args.metric or []:
            if "=" not in pair:
                _err(f"⛔ --metric expects key=value, got {pair!r}")
                return USAGE
            k, v = pair.split("=", 1)
            metrics[k.strip()] = _coerce(v.strip())

        store.add_phase_summary(
            conn, row["run_id"], args.phase,
            closed=",".join(closed), skipped=",".join(skips),
            duration_s=args.duration, metrics_json=json.dumps(metrics, ensure_ascii=False),
            note=args.note,
        )
        _sayer(args)(
            f"✅ summarized phase '{args.phase}': {len(closed)} closed, {len(skips)} skipped"
            + (f", {len(metrics)} metric(s)" if metrics else ""))
        return _emit(args, {
            "run": row["run_id"], "phase": args.phase,
            "closed": closed, "skipped": skips,
            "metrics": metrics, "duration_s": args.duration, "note": args.note,
            # A phase with no declared goal and a phase whose goal was checked and met both
            # summarise successfully; only this distinguishes them.
            "goal": {"declared": applicable, "met": (ok if applicable else None)},
        }, lambda _d: None)
    finally:
        conn.close()


def cmd_skip(args) -> int:
    """Record that a step legitimately will not run.

    Only allowed for a step the flow declares `optional`. A mandatory step has no
    skip: if it could be skipped it was not mandatory, and letting the caller decide
    at runtime would make the flow mean whatever the caller wanted that day.
    """
    conn = store.connect()
    try:
        row = _run_or_exit(conn, args.run)
        f = _flow_for_run(conn, row)
        s = f.step(args.step)
        if not f.applicable(s.id, row["variant"]):
            _err(
                f"⛔ '{s.id}' is not part of this flow (variant {row['variant']!r}) — there is "
                f"nothing to skip.\n"
                f"    It is already not owed; recording a skip would log a decision nobody made."
            )
            return REFUSED
        if not s.optional:
            _err(
                f"⛔ REFUSED: '{s.id}' is not declared optional, so it cannot be skipped.\n"
                f"    Either close it, or declare `optional: true` in the flow spec if it "
                f"really may never run."
            )
            return REFUSED
        store.log_step(conn, row["run_id"], s.id, "skipped", args.reason)
        _sayer(args)(f"⏭  skipped {s.id} ({s.title})"
                     + (f" — {args.reason}" if args.reason else ""))
        return _emit(args, {"run": row["run_id"], "step": s.id, "title": s.title,
                            "skipped": True, "reason": args.reason}, lambda _d: None)
    finally:
        conn.close()


def cmd_purge_run(args) -> int:
    """Remove a CLOSED run's rows. For test residue, not for tidying an inconvenient record.

    Restricted and recorded — see store.purge_run for why both.
    """
    conn = store.connect()
    try:
        try:
            counts = store.purge_run(conn, args.run, args.reason)
        except KeyError:
            _err(f"⛔ no run {args.run!r}")
            return USAGE
        except ValueError as exc:
            _err(f"⛔ REFUSED: {exc}.\n"
                 f"    Only a closed run may be purged — an open run's violations are live "
                 f"evidence.")
            return REFUSED
        total = sum(counts.values())
        print(f"✅ purged run {args.run}  ({total} row(s): "
              + ", ".join(f"{k}={v}" for k, v in sorted(counts.items()) if v) + ")")
        print(f"   recorded in purge_log — the removal itself leaves a trace")
        return OK
    finally:
        conn.close()


def cmd_guard_tool(args) -> int:
    """The RUNTIME hook. Answers: may this tool call proceed?

    Different question from `guard`, and the difference is who knows what. `guard` is for a
    caller that already knows the semantic action and the scope. A runtime hook knows neither:
    it holds a tool name, that tool's arguments, and a working directory. Turning those into an
    action name is domain knowledge — which command constitutes a commit, which field carries a
    close — so the ability DECLARES it (`guards.<action>.matches`) and the engine only compiles
    the pattern and applies it. The engine never learns what any of them mean.

    Same refusal posture as `guard`, for the same reason: ambiguity ALLOWS, loudly. A guard that
    misfires on somebody else's work leaves forging its gate as the only way past it.

    Exit 4 = block. Everything else allows.
    """
    try:
        payload = json.loads(args.input_json or "{}")
    except json.JSONDecodeError:
        # A runtime that changed its payload shape must not brick every tool call.
        _err("⚠️  guard-tool: --input-json is not JSON; allowing")
        return OK
    if not isinstance(payload, dict):
        payload = {"value": payload}

    try:
        conn = store.connect(read_only=True)
    except store.StoreUnusable:
        return OK
    try:
        hits = []
        unreadable: list[tuple[str, str, str]] = []
        for row in store.all_open_runs(conn):
            try:
                f = flowmod.load(row["ability"])
            except flowmod.FlowError as exc:
                # A run whose flow cannot be READ is not a run without guards. Skipping it
                # in silence makes "nothing to guard" and "I cannot see what to guard" look
                # identical — the same distinction this integration already draws for the
                # state directory, for the same reason.
                #
                # Three ways it happens, and every one of them used to be soundless: the
                # ability is not on this process's search path, its spec is invalid, or its
                # extension code is not approved here so loading refuses. The last arrived
                # WITH the trust check — editing an approved providers.py turned that run's
                # guards off and printed nothing, which is a safety feature handing out a
                # bypass.
                #
                # NOT filtered by scope, because deciding whether this run's scope covers the
                # call needs the flow that just failed to load. Reporting a run that turns out
                # to be irrelevant is noise; staying quiet about one that was relevant is the
                # failure this exists to remove.
                why = " — ".join(x.strip() for x in str(exc).split("\n")[:2] if x.strip())
                unreadable.append((row["run_id"], row["ability"], why))
                continue
            if not f.scope_covers(row["scope_key"], args.cwd, payload):
                continue
            for action, rules in f.guard_matches.items():
                for rule in rules:
                    if rule["tool"] != args.tool:
                        continue
                    if rule["field"]:
                        hay = str(payload.get(rule["field"], ""))
                    else:
                        hay = json.dumps(payload, ensure_ascii=False, sort_keys=True)
                    if rule["regex"].search(hay):
                        hits.append((row, f, action, f.guards[action], rule["pattern"]))
                        break

        if unreadable:
            _err(f"⚠️  guard-tool: {len(unreadable)} open run(s) whose flow it CANNOT READ. "
                 f"Their\n"
                 f"    guards are not enforced for this call — that is \"the guard could not be\n"
                 f"    looked up\", not \"there is no guard\".")
            for run_id, ability, why in unreadable:
                _err(f"      {run_id} ({ability}): {why}")
            _err("    Allowing anyway: this hook runs before every matching tool call, so it\n"
                 "    must never brick normal work. Fix whichever applies:\n"
                 "      · not visible here     → HARNESS_ABILITIES_PATH must cover the runs\n"
                 "                               this hook is expected to guard\n"
                 "      · spec invalid         → harness validate <ability>\n"
                 "      · extension unapproved → harness trust <ability>  (a changed\n"
                 "                               providers.py re-asks, and until it is\n"
                 "                               answered that run's guards cannot be read)")
        if not hits:
            return _required_flow_missing(conn, args, payload)
        owners = {h[0]["run_id"] for h in hits}
        if len(owners) > 1:
            _err(f"⚠️  guard-tool NOT enforced: {len(owners)} open runs claim this call "
                 f"({', '.join(sorted(owners))}). Allowing rather than guessing which owns it.")
            return OK

        row, f, action, step_id, pattern = hits[0]
        g = store.get_gate(conn, row["run_id"], step_id)
        if g is not None and g["decision"] in ("affirm", "preauth"):
            return OK
        s = f.step(step_id)
        _err(
            f"⛔ BLOCKED: this looks like action '{action}' (matched {pattern!r}), which "
            f"requires gate '{step_id}' ({s.title}) on run {row['run_id']}.\n"
            f"    Do not proceed, and do not reword the command to get past this.\n"
            f"    Get a real human affirmation, then record it:\n"
            f"      harness gate --run {row['run_id']} --step {step_id} "
            f"--decision affirm --evidence \"<what they said>\""
        )
        return BLOCKED
    finally:
        conn.close()


def _required_flow_missing(conn, args, payload: dict) -> int:
    """THE SECOND QUESTION, asked only when no open run claimed this call.

    The first question is "does an open run guard this?" — it enforces the gates INSIDE a run and
    cannot enforce that a run exists, because with nothing open there is no scope to compare the
    action against. This asks the declared policy for that missing scope: is this a guarded action
    of a flow someone said is mandatory HERE?

    WHY THERE IS NO "BUT A RUN EXISTS" CHECK HERE. If a run of the named ability covered this call
    and the flow loads, the first question already hit — so it either allowed on a recorded gate
    or refused. Reaching this point with such a run is not possible in one process, and a check
    for it would be a guard that cannot fire. (Both questions load the same spec from the same
    disk in the same process, so one cannot succeed where the other failed.)

    An empty policy leaves behaviour byte-identical to behaviour before this existed — the loop
    below simply does not run. NO EARLY RETURN FOR IT: one was written and removed, because with
    nothing declared the loop is empty and the function already falls through to OK, so the
    branch could not change any outcome. A guard that cannot fire is what this engine spends its
    refusals on elsewhere.
    """
    declared, problems = policy.discover(args.cwd)
    for note in problems:
        # A marker that exists and cannot be applied is reported even though the call is allowed:
        # "nothing declared here" and "a declaration I could not use" must not look alike.
        _err(f"⚠️  {note}")
    for entry in (*policy.read(), *declared):
        try:
            f = flowmod.load(entry["ability"])
        except flowmod.FlowError as exc:
            first = str(exc).splitlines()[0]
            if entry["strict"]:
                _err(f"⛔ BLOCKED: '{entry['ability']}' is declared MANDATORY (strict) for "
                     f"scope '{entry['scope_key']}', and its flow cannot be read:\n"
                     f"    {first}\n"
                     f"    Strict means refuse rather than proceed unchecked. Fix the flow, or "
                     f"drop the entry:\n"
                     f"      harness require --remove {entry['ability']} "
                     f"--scope-key {entry['scope_key']}")
                return BLOCKED
            _err(f"⚠️  '{entry['ability']}' is declared mandatory for scope "
                 f"'{entry['scope_key']}' but its flow cannot be read, so whether this call "
                 f"needs a run\n    could not be decided: {first}\n"
                 f"    Allowing — one unreadable spec must not stop all work in a scope. Mark "
                 f"the entry `strict` to refuse instead.")
            continue
        if not f.scope_covers(entry["scope_key"], args.cwd, payload):
            continue
        for action, rules in f.guard_matches.items():
            for rule in rules:
                if rule["tool"] != args.tool:
                    continue
                hay = (str(payload.get(rule["field"], "")) if rule["field"]
                       else json.dumps(payload, ensure_ascii=False, sort_keys=True))
                if not rule["regex"].search(hay):
                    continue
                step_id = f.guards[action]
                _err(
                    f"⛔ BLOCKED: this looks like action '{action}' (matched "
                    f"{rule['pattern']!r}), and '{entry['ability']}' is declared MANDATORY for "
                    f"scope '{entry['scope_key']}' — but no run of it is open here.\n"
                    f"    A run is what makes any of this enforceable: without one, gate "
                    f"'{step_id}' and every other criterion is unrecorded and unchecked.\n"
                    f"    Open one first:\n"
                    f"      harness open {entry['ability']} --scope "
                    f"{entry['scope_key']} --run <id>\n"
                    f"    Do NOT reword the command to get past this. If this action genuinely "
                    f"does not belong to that flow, the requirement is what is wrong — say so "
                    f"to whoever owns it. It is declared in:\n"
                    f"      {entry['source']}\n"
                    # THE REMEDY DEPENDS ON THE SOURCE, and naming the wrong one is worse than
                    # naming none: `require --remove` edits the machine record, so offering it
                    # for a requirement declared by a directory would send the reader to change a
                    # file that does not contain it — and the requirement would survive, which
                    # reads as the command having silently failed.
                    + (f"    and comes out with:\n"
                       f"      harness require --remove {entry['ability']} "
                       f"--scope-key {entry['scope_key']}"
                       if entry["source"] == str(policy.path()) else
                       f"    and comes out by editing that file — it is not in the machine's "
                       f"record, so `harness require --remove` would not touch it.")
                )
                return BLOCKED
    return OK


def cmd_require(args) -> int:
    """Show, add or remove a declared requirement. Never for a model — see NOT_A_TOOL."""
    if args.add or args.remove:
        ability = args.add or args.remove
        if not args.scope_key:
            _err("--scope-key is required: a requirement without a scope would either mean "
                 "nowhere or everywhere, and neither is a policy.")
            return USAGE
        try:
            f = _load_flow_or_exit(ability)
        except SystemExit:
            raise
        if args.add:
            # The kind is NOT stored — it is the flow's, and a second copy could age. Reported
            # here so whoever writes the entry can see what the key will be compared as.
            changed = policy.add(ability, args.scope_key, strict=bool(args.strict))
            print(f"{'recorded' if changed else 'already recorded'}: {ability} is "
                  f"{'STRICTLY ' if args.strict else ''}mandatory for "
                  f"{f.scope_kind}={args.scope_key}")
            print(f"  matched as '{f.scope_match}' (the flow's own declaration)")
            print(f"  in {policy.path()}")
            return OK
        if policy.remove(ability, args.scope_key):
            print(f"removed: {ability} is no longer mandatory for {args.scope_key}")
            return OK
        _err(f"no requirement recorded for {ability} at {args.scope_key}")
        return USAGE

    # BOTH SOURCES, or this view answers a different question than the guard does. A directory
    # marker is discovered from where the asking happens, so this reports the requirements that
    # apply HERE — which is what "what is mandatory" means to whoever is standing somewhere.
    here = os.getcwd()
    found, problems = policy.discover(here)
    rows = []
    for entry in (*policy.read(), *found):
        try:
            f = _load_flow_or_exit(entry["ability"])
            kind, match, readable = f.scope_kind, f.scope_match, True
        except SystemExit:
            kind, match, readable = None, None, False
        rows.append({**entry, "scope_kind": kind, "scope_match": match, "readable": readable})
    if args.json:
        return _emit(args, {"record": str(policy.path()), "cwd": here,
                            "marker": policy.MARKER, "problems": list(problems),
                            "required": rows}, lambda _d: None)
    for note in problems:
        _err(f"⚠️  {note}")
    if not rows:
        print(f"(nothing is declared mandatory here)")
        print(f"  machine record : {policy.path()}")
        print(f"  directory      : no {policy.MARKER} at or above {here}")
        print("With nothing declared the guard enforces the gates inside an open run and allows")
        print("everything when nothing is open — exactly as it did before this existed.")
        return OK
    print(f"applying at {here}")
    for r in rows:
        mark = "⛔" if r["strict"] else "•"
        print(f"{mark} {r['ability']:<16} {r['scope_kind'] or '?'}={r['scope_key']}"
              + ("  (strict)" if r["strict"] else "")
              + ("" if r["readable"] else "  ⚠️  its flow cannot be read from here"))
        print(f"    from {r['source']}")
    print("\nThere is no recorded way to skip one of these yet: a one-off exception means "
          "removing\nthe declaration, which is a change to a file rather than a logged decision.")
    return OK


def cmd_discharge(args) -> int:
    conn = store.connect()
    try:
        row = _run_or_exit(conn, args.run)
        ob = store.get_obligation(conn, row["run_id"], args.hook)
        if ob is None:
            open_ids = [r["hook_id"] for r in store.open_obligations(conn, row["run_id"])]
            _err(
                f"⛔ no obligation '{args.hook}' on run {row['run_id']}.\n"
                f"    open: {', '.join(open_ids) or '(none)'}"
            )
            return USAGE
        say = _sayer(args)
        if ob["discharged_at"]:
            say(f"⃝ obligation {args.hook} was already discharged at {ob['discharged_at']}")
            # Not an error, and not a fresh discharge either. A caller that cannot tell the
            # two apart would count this call as having done something.
            return _emit(args, {
                "run": row["run_id"], "hook": args.hook, "discharged_now": False,
                "already_discharged_at": ob["discharged_at"],
                "open_remaining": [r["hook_id"] for r in
                                   store.open_obligations(conn, row["run_id"])],
            }, lambda _d: None)
        store.discharge_obligation(conn, row["run_id"], args.hook, args.evidence)
        left = [r["hook_id"] for r in store.open_obligations(conn, row["run_id"])]
        say(f"✅ discharged {args.hook}"
            + (f"  ({len(left)} obligation(s) still open)" if left else "  (none left)"))
        return _emit(args, {
            "run": row["run_id"], "hook": args.hook, "discharged_now": True,
            "already_discharged_at": None, "open_remaining": left,
        }, lambda _d: None)
    finally:
        conn.close()


def cmd_obligations(args) -> int:
    conn = store.connect(read_only=True)
    try:
        row = _run_or_exit(conn, args.run, closed_ok=True)
        rows = store.all_obligations(conn, row["run_id"])
        data = {"run": row["run_id"], "obligations": [
            {**d, "facts": json.loads(d.pop("facts_json") or "{}"),
             "open": d["discharged_at"] is None}
            for d in _rowdicts(rows)]}
        if args.json:
            return _emit(args, data, lambda _d: None)
        if not rows:
            print("(no obligations raised)")
            return OK
        for r in rows:
            state = "✅ discharged" if r["discharged_at"] else "⏳ OPEN"
            print(f"{state}  {r['hook_id']}   raised at {r['trigger_kind']} "
                  f"'{r['selector']}'  {r['raised_at']}")
            if r["evidence"]:
                print(f"    evidence: {r['evidence']}")
            if args.verbose:
                snap = json.loads(r["facts_json"] or "{}")
                if snap:
                    print(f"    facts at that moment: "
                          + ", ".join(f"{k}={v!r}" for k, v in sorted(snap.items())))
                for line in r["body"].splitlines():
                    print(f"    | {line}")
        return OK
    finally:
        conn.close()


def cmd_evidence(args) -> int:
    conn = store.connect()
    try:
        row = _run_or_exit(conn, args.run)
        f = _flow_for_run(conn, row)
        f.step(args.step)  # validate the id exists
        store.add_evidence(conn, row["run_id"], args.step, args.kind, args.value)
        _sayer(args)(f"✅ evidence recorded: {args.step} kind={args.kind}")
        return _emit(args, {
            "run": row["run_id"], "step": args.step, "kind": args.kind,
            # HOW MANY ROWS OF THIS KIND NOW EXIST AT THIS STEP. A `min_count` criterion is
            # counted in exactly these rows, so a driver working towards one had to either
            # keep its own tally or ask `next` again to find out where it stood.
            "rows_now": len(store.find_evidence(conn, row["run_id"], args.step, args.kind)),
        }, lambda _d: None)
    finally:
        conn.close()


def cmd_config(args) -> int:
    conn = store.connect()
    try:
        row = _run_or_exit(conn, args.run, closed_ok=not args.set)
        f = _flow_for_run(conn, row)
        meta = json.loads(row["metadata_json"] or "{}")
        cfg = meta.setdefault("config", {})
        for pair in args.set or []:
            if "=" not in pair:
                _err(f"⛔ --set expects key=value, got {pair!r}")
                return USAGE
            k, v = pair.split("=", 1)
            cfg[k.strip()] = _coerce(v.strip())
        if args.set:
            conn.execute("UPDATE run SET metadata_json = ?, updated_at = ? WHERE run_id = ?",
                         (json.dumps(meta, ensure_ascii=False), store.now_iso(), row["run_id"]))
            conn.commit()
        merged = {**f.config_defaults, **cfg}
        say = _sayer(args)
        for k in sorted(merged):
            src = "run" if k in cfg else "flow-default"
            say(f"{k} = {merged[k]!r}   [{src}]")
        return _emit(args, {
            "run": row["run_id"],
            # Set by THIS call, so a caller can confirm what it just changed rather than
            # diffing the whole merged view against what it remembers.
            "set": {k.split("=", 1)[0].strip(): _coerce(k.split("=", 1)[1].strip())
                    for k in (args.set or [])},
            # Value AND provenance per key: a run override and a flow default read the same
            # once merged, and only one of them is this run's own decision.
            "config": {k: {"value": merged[k], "source": "run" if k in cfg
                           else "flow-default"} for k in sorted(merged)},
        }, lambda _d: None)
    finally:
        conn.close()


def _coerce(v: str):
    low = v.lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    try:
        return int(v)
    except ValueError:
        return v


def cmd_leases(args) -> int:
    """Show who currently holds which scope, and where each delegation came from."""
    conn = store.connect(read_only=True)
    try:
        rows = conn.execute(
            "SELECT l.*, gr.ability AS grantor_ability, ho.ability AS holder_ability"
            " FROM scope_lease l"
            " JOIN run gr ON gr.run_id = l.grantor_run_id"
            " JOIN run ho ON ho.run_id = l.holder_run_id"
            " WHERE l.released_at IS NULL ORDER BY l.scope_kind, l.scope_key, l.id"
        ).fetchall()
        if args.json:
            return _emit(args, {"leases": _rowdicts(rows)}, lambda _d: None)
        if not rows:
            print("(no scope is delegated)")
            return OK
        seen_scope = None
        for r in rows:
            here = f"{r['scope_kind']}={r['scope_key']}"
            if here != seen_scope:
                print(here)
                seen_scope = here
            print(f"  {r['grantor_run_id']} ({r['grantor_ability']}) "
                  f"at step {r['granted_at_step']}"
                  f"  →  {r['holder_run_id']} ({r['holder_ability']})   {r['granted_at']}")
        return OK
    finally:
        conn.close()


def _subcommand_help() -> dict[str, str]:
    """Read the command surface off the parser rather than listing it again.

    Reaching into argparse internals is the price of having ONE list of commands. The
    alternative is a hand-kept second list, which is the exact failure `brief` exists to remove —
    so the private attribute is the lesser evil, and a test fails if this returns nothing.
    """
    parser = build_parser()
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return {ca.dest: (ca.help or "") for ca in action._choices_actions}
    return {}


# Commands a MODEL must not be handed as a callable tool, each with the reason. Data, so the
# reason ships with the refusal instead of living in someone's head — and so an adapter in
# any language gets the same list without re-deciding it.
NOT_A_TOOL: dict[str, str] = {
    "guard-tool":
        "invoked by a RUNTIME before a tool call it may block, never by the model. A model will "
        "not call a tool whose purpose is to stop it, so exposing this as one would replace the "
        "only mechanism that does not need the agent's cooperation with one that does.",
    "trust":
        "approves extension CODE to run with this engine's privileges. That is a decision for "
        "whoever owns the machine; a model able to approve its own code makes the content "
        "pinning decorative.",
    "init":
        "creates or migrates the store. A driver should find the store already there — letting "
        "the model create one hides a mis-pointed state directory behind an automatic repair, "
        "and a fresh empty store in the wrong place looks exactly like a clean slate.",
    "purge-run":
        "deletes a closed run's rows. Test residue cleanup, not driving.",
    "require":
        "declares where a flow is MANDATORY. The subject of a rule may not be the one who "
        "writes it — a driver able to declare its own obligations has none.",
}


# WHY THESE TWO TABLES EXIST AT ALL. `--json` is a promise that stdout carries the answer, and
# the failing path is where a caller most needs it. Every command therefore either has the flag
# or appears below WITH A REASON — because a command that simply never got one is
# indistinguishable, from the outside, from a command that was decided against. A test asserts
# the three sets partition the parser, so a new subcommand cannot join the silent side by
# omission: it will not be classified, and that fails.

PROSE_ONLY: dict[str, str] = {
    "adapter-contract":
        "emits JSON as its whole output already; a --json flag would be a second name for the "
        "only thing it does.",
    "brief":
        "the driving contract is prose FOR a driver to read. Fields describing it would be a "
        "summary of the document, standing next to the document.",
    "show":
        "prints a section of a guide verbatim. Wrapping prose in a field does not make it "
        "machine-readable, it makes it prose in a field.",
    "validate":
        "the exit code IS the answer and that is what a build step reads. A payload beside it "
        "invites parsing the payload instead, and then two things say whether a spec is valid.",
    "guard-tool":
        "its caller is a hook that decides on the exit code. The text is written into the "
        "agent's context to be READ, so structuring it would serve nobody in the path.",
    "init":
        "one-time setup whose output is a human confirmation of where the store landed.",
    "purge-run":
        "deletes a closed run's rows; the count is a human confirmation, not an answer.",
}

# Empty, and kept rather than deleted: the table is what made the two entries that used to be
# here visible as a GAP instead of as a decision, and the partition test needs somewhere to put
# the next one. A command with no machine form must land in a table, not in the silence.
NO_JSON_YET: dict[str, str] = {}


def _arg_shape(action) -> dict:
    """One argparse action as a language-neutral parameter description.

    Same trade as `_subcommand_help`: private attributes are the price of deriving the surface
    from the parser instead of restating it. A restated surface is the drift this removes.
    """
    flag = action.option_strings[0] if action.option_strings else None
    if isinstance(action, argparse._StoreTrueAction):
        kind = "boolean"
    elif action.nargs in ("*", "+"):
        kind = "array"
    else:
        kind = "string"
    required = bool(action.required) if action.option_strings else action.nargs != "?"
    return {"name": action.dest, "flag": flag, "kind": kind,
            "required": required, "help": action.help or "",
            "choices": sorted(str(c) for c in action.choices) if action.choices else None}


def _tool_surface() -> dict:
    """The command surface as DATA, derived from the parser, for adapters that expose tools.

    WHY THIS IS PUBLISHED RATHER THAN WRITTEN DOWN BY EACH ADAPTER

    An adapter that hand-writes its tool schemas has made a second copy of the command surface,
    and the first thing that copy does is age. This project already lost that bet twice: a
    hand-written driving document drifted from the engine, so it became generated; then the
    generated copy on one machine aged past the engine, one line at a time. A schema an adapter
    derives from here cannot describe a flag the parser does not have.

    The exclusions matter as much as the inclusions — see NOT_A_TOOL.
    """
    parser = build_parser()
    subs = None
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            subs = action
    if subs is None:                       # pragma: no cover - the parser always has one
        return {"tools": [], "not_a_tool": NOT_A_TOOL}
    helps = _subcommand_help()
    tools = []
    for name, sub in sorted(subs.choices.items()):
        if name in NOT_A_TOOL:
            continue
        args = [_arg_shape(a) for a in sub._actions
                if not isinstance(a, argparse._HelpAction) and a.dest != "fn"]
        tools.append({
            "command": name,
            # One naming rule, stated once here, so the adapter does not invent a second.
            "tool": "harness_" + name.replace("-", "_"),
            "help": helps.get(name, ""),
            "structured": any(a["flag"] == "--json" for a in args),
            "args": args,
        })
    return {
        "tools": tools,
        "not_a_tool": NOT_A_TOOL,
        # So an adapter can tell "no machine form by decision" from "no machine form yet"
        # without inferring it from the absence of a flag.
        "prose_only": PROSE_ONLY,
        "no_json_yet": NO_JSON_YET,
        # So an adapter needs no second copy of the one table that IS the contract.
        "exit_codes": {str(k): v for k, v in _EXIT_NAMES.items()},
        "result_shape": {
            "note": "An exit code is the PRODUCT, not a failure. A tool result must carry the "
                    "code as data and must not be flagged as an error for 3 or 4 — a runtime "
                    "that reports a refusal as a tool malfunction invites the model to retry "
                    "the call instead of reading what is missing.",
            "fields": ["exit", "meaning", "stdout", "stderr"],
        },
    }


PORTABLE_PATH = "<path-to-engine>/bin/harness"


def _invocation(portable: bool = False) -> str:
    """How the reader should spell the command.

    An installed copy has it on PATH; a source checkout does not, and telling an agent to type
    `harness` when nothing answers to that name is worse than a long path.

    `portable` exists because the copy CHECKED INTO this tree must not carry one machine's
    absolute path — that is machine-specific content in version control, and it would also make
    the drift test pass or fail depending on whose checkout ran it. A consumer regenerates
    without the flag to get a path that actually works for them.
    """
    if portable:
        return PORTABLE_PATH
    import shutil
    if shutil.which("harness"):
        return "harness"
    return str(Path(__file__).resolve().parents[1] / "bin" / "harness")


def _render_brief(portable: bool) -> str:
    """Gather every fact off the engine, then render. Shared by `brief` and `init`.

    One gatherer, because two would drift — and drift in a document about the engine is the
    defect this whole command exists to remove.
    """
    routable: list[tuple[str, str]] = []
    caps: set[str] = set()
    guard_tools: set[str] = set()
    example_step: str | None = None
    ask_only_guards: list = []
    widest = -1
    for name in flowmod.available_abilities():
        try:
            f = flowmod.load(name)
        except flowmod.FlowError:
            continue  # a broken spec must not stop the rest of the brief
        caps |= set(flowmod.capabilities_used(f))
        for rules in f.guard_matches.values():
            guard_tools |= {r["tool"] for r in rules}
        # Collected at the SAME level as the tool list above, and deliberately not below the
        # routable filter: the interception path iterates every open run whatever its role, so a
        # fixture's guarded actions are as real to a hook as a production flow's. Listing one
        # kind of declaration and hiding the other would make the tool list read as the complete
        # guarded surface, which is exactly what it is not.
        for action in sorted(f.guards):
            if not f.guard_matches.get(action):
                ask_only_guards.append((name, action))
        if f.role != flowmod.ROLE_PRODUCTION:
            continue
        routable.append((name, f.when.strip().splitlines()[0] if f.when else ""))
        if len(f.steps) > widest and f.order:
            widest, example_step = len(f.steps), f.order[0]

    from . import __version__
    return briefmod.render(
        invocation=_invocation(portable),
        version=__version__,
        portable=portable,
        write_hint="harness brief --write",
        exit_codes={"OK": OK, "USAGE": USAGE, "BAD_SPEC": BAD_SPEC,
                    "REFUSED": REFUSED, "BLOCKED": BLOCKED, "INTERNAL": INTERNAL},
        subcommands=_subcommand_help(),
        routable=sorted(routable),
        capabilities=caps,
        env=[
            (flowmod.ABILITIES_ENV,
             "where flows are searched for; os.pathsep-separated, REPLACES the default"),
            (store.STATE_ENV, "where the store lives"),
            (ACTOR_ENV, "who is driving — recorded for diagnosis, never used for isolation"),
        ],
        guard_tools=sorted(guard_tools),
        example_step=example_step,
        required=() if portable else policy.read(),
        ask_only_guards=tuple(ask_only_guards),
    )


def cmd_brief(args) -> int:
    """Print the driving contract, derived from THIS installation.

    `--write` puts it beside the store instead of on stdout, because an agent runtime loads a
    FILE as context — it cannot run a command to fill it. So the generated document has to be
    materialised somewhere machine-local, and the command prints where it went rather than
    leaving the caller to guess.
    """
    text = _render_brief(args.portable)
    if args.write:
        if args.portable:
            _err("⛔ --write and --portable name different audiences: the written copy is for "
                 "THIS machine and must carry a working path.")
            return USAGE
        path = store.brief_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        print(f"✅ wrote {path}")
        return OK
    sys.stdout.write(text)
    return OK


def cmd_adapter_contract(args) -> int:
    """Publish what a runtime adapter must satisfy, as data any language can consume.

    WHY DATA AND NOT A GENERATED ADAPTER. An adapter translates between this engine and one
    runtime's dialect, and those dialects are not in one language — one is a python hook reading
    stdin, another is a plugin in a typed runtime. Generating the code is not possible; agreeing
    on the CASES is, and it is the part that actually goes wrong.

    THE SPLIT IS DELIBERATE. `translation` and `resilience` are fully specifiable: they are about
    what the adapter does given an engine exit code or a broken input, and an author can check
    every one of them against a stub. `end_to_end` cannot be handed over complete — it needs a
    payload that matches a pattern this installation declares, and synthesising a string from an
    arbitrary regex is not something to pretend at. So the pattern is published and the
    construction is named as the author's job, rather than shipping a case that looks complete
    and silently is not.

    The cases here are load-bearing, not decorative: this engine's own adapter test iterates
    them. A case nobody runs is a claim, and this file is full of arguments against those.
    """
    translation = [
        {"engine_exit": BLOCKED, "expect": "block",
         "why": "the only verdict that becomes a block. Everything else allows."},
        {"engine_exit": OK, "expect": "allow", "why": "nothing to stop"},
        {"engine_exit": REFUSED, "expect": "allow",
         "why": "a refusal belongs to whoever ran the command, not to a tool call being made"},
        {"engine_exit": BAD_SPEC, "expect": "allow",
         "why": "a broken spec must not stop unrelated work in every session"},
        {"engine_exit": INTERNAL, "expect": "allow",
         "why": "a defect in the engine must never brick the runtime hosting it"},
    ]
    resilience = [
        {"condition": "stdin is not JSON", "expect": "allow",
         "engine_called": False,
         "why": "a runtime that changes its event shape must not lose every tool call"},
        {"condition": "event has no tool name", "expect": "allow", "engine_called": False,
         "why": "nothing to look up"},
        {"condition": "the engine binary is absent", "expect": "allow", "engine_called": False,
         "why": "SAY SO on stderr as well — 'cannot guard' and 'nothing to guard' must not "
                "look alike"},
        {"condition": "the engine call times out or cannot start", "expect": "allow",
         "engine_called": True, "why": "same rule: uncertainty allows"},
        {"condition": "the adapter itself raises", "expect": "allow", "engine_called": None,
         "why": "the fail-open rule outranks tidiness; catch and allow"},
    ]

    end_to_end = None
    for name in flowmod.available_abilities():
        try:
            f = flowmod.load(name)
        except flowmod.FlowError:
            continue
        for action, rules in sorted(f.guard_matches.items()):
            if not rules:
                continue
            end_to_end = {
                "ability": name,
                "action": action,
                "gate_step": f.guards.get(action),
                # The compiled regex is dropped: it is what the engine APPLIES, while `pattern`
                # already carries the source an adapter author has to read. Publishing a repr of
                # a compiled object would be unusable and also not serialisable.
                "matches": [{k: v for k, v in r.items() if k != "regex"} for r in rules],
                "setup": [
                    f"harness open {name} --scope <a path this flow's scope_match covers> "
                    f"--run <id>",
                ],
                "then": "feed your adapter an event naming the tool above, carrying a payload "
                        "whose named field matches the pattern. Expect a block. Record the "
                        "gate, feed the same event again, expect an allow.",
                "note": "the payload is yours to construct: a matching string cannot be derived "
                        "from a regex, and a case with an invented payload that happens not to "
                        "match would pass while proving nothing.",
            }
            break
        if end_to_end:
            break

    print(json.dumps({
        "io": {
            "stdin": "one JSON object; at least {tool_name: str, tool_input: object}",
            "engine_call": ["guard-tool", "--tool", "<tool_name>",
                            "--input-json", "<tool_input as JSON>", "--cwd", "<working dir>"],
            "adapter_exit": "whatever the host runtime reads as allow / block",
            "env": {flowmod.ABILITIES_ENV: "must match the value the runs were opened with",
                    store.STATE_ENV: "MUST match the value the runs were opened with"},
        },
        "iron_rule": "Only a positive, unambiguous BLOCKED from the engine becomes a block. "
                     "Every other outcome allows. This runs before every matching tool call in "
                     "every session, so a bug here must not stop normal work.",
        "translation": translation,
        "resilience": resilience,
        "surface": _tool_surface(),
        "end_to_end": end_to_end,
    }, indent=2, ensure_ascii=False))
    return OK


def cmd_history(args) -> int:
    """List runs that have ended. Read-only."""
    conn = store.connect(read_only=True)
    try:
        if args.actors:
            rows = store.actor_totals(conn)
            if args.json:
                return _emit(args, {"actors": _rowdicts(rows)}, lambda _d: None)
            if not rows:
                print("(no runs recorded)")
                return OK
            print(f"{'actor':<24} {'runs':>5} {'open':>5} {'violations':>11}")
            for r in rows:
                print(f"{r['actor']:<24} {r['runs']:>5} {r['open_runs']:>5} "
                      f"{r['violations']:>11}")
            return OK
        rows = store.closed_runs(conn, limit=args.limit, actor=args.actor,
                                 ability=args.ability)
        if args.json:
            return _emit(args, {"runs": _rowdicts(rows)}, lambda _d: None)
        if not rows:
            print("(no closed runs match)")
            return OK
        for r in rows:
            marks = []
            if r["violations"]:
                marks.append(f"⚠️ {r['violations']} violation(s)")
            if r["owed"]:
                marks.append(f"⚠️ {r['owed']} owed obligation(s)")
            if r["actor"]:
                marks.append(f"actor={r['actor']}")
            print(f"{r['run_id']}  {r['ability']:<14} {r['scope_kind']}={r['scope_key']}")
            print(f"    {r['result'] or '(no result)':<14} "
                  f"steps={r['closed_steps']} gates={r['gates']}  "
                  f"{r['opened_at']} → {r['closed_at']}"
                  + ("  " + " · ".join(marks) if marks else ""))
        return OK
    finally:
        conn.close()


def _share(n: int, population) -> str:
    """`n/population` as a percentage, or a dash when there is nothing to divide by.

    A population of zero is not zero percent: it means the records that would have formed the
    denominator are not there. Printing 0% would state something the ledger does not say.
    """
    if not population:
        return "—"
    return f"{round(100 * n / population)}%"


def cmd_audit_recurring(args) -> int:
    """The same subject as `audit`, grouped ACROSS runs instead of within one.

    `audit` answers "what went wrong in this run". This answers "what keeps going wrong here",
    which the ledger could always support and nothing ever asked it. Every row is printed with
    the population it came out of, and the ordering leads with the ABSOLUTE count: sorting by
    share alone puts a single 1-of-1 above a persistent 12-of-20, and the first is noise while
    the second is the finding.
    """
    conn = store.connect(read_only=True)
    try:
        viol = store.recurring_violations(conn)
        gates = store.recurring_unwitnessed_gates(conn)
        runs = conn.execute("SELECT COUNT(*) n FROM run").fetchone()["n"]
        purged = conn.execute("SELECT COUNT(*) n FROM purge_log").fetchone()["n"]
        if args.json:
            return _emit(args, {
                "population": {"runs_recorded": runs, "purge_events": purged},
                "recurring_violations": [
                    {**v, "share": _share(v["runs"], v["population"])} for v in viol],
                "recurring_unwitnessed_gates": [
                    {**g, "share": _share(g["unwitnessed"], g["gates"])} for g in gates],
            }, lambda _d: None)
        print(f"over {runs} run(s) recorded here"
              + (f"; {purged} purge event(s) — purged runs are NOT in this population"
                 if purged else ""))
        if not viol and not gates:
            print("\n(nothing recurs: no violations, and no gate recorded without a witness)")
            return OK
        if viol:
            print("\nViolations, across runs:")
            print(f"  {'ability':<16} {'step':<8} {'code':<26} {'runs':>5} {'of':>5} "
                  f"{'share':>6} {'rows':>5}")
            for v in viol:
                print(f"  {v['ability']:<16} {(v['step_id'] or '(run)'):<8} {v['code']:<26} "
                      f"{v['runs']:>5} {(v['population'] or 0):>5} "
                      f"{_share(v['runs'], v['population']):>6} {v['total']:>5}")
        if gates:
            print("\nGates recorded WITHOUT a witness, across runs:")
            print("  (the same events are above as violations — these are counted against the "
                  "GATES\n   recorded at the step, not the runs that reached it, which is the "
                  "sharper\n   denominator for a gate: a step can be reached often and gated "
                  "rarely.)")
            print(f"  {'ability':<16} {'step':<8} {'unwitnessed':>11} {'gates':>6} {'share':>6}")
            for g in gates:
                print(f"  {g['ability']:<16} {g['step_id']:<8} {g['unwitnessed']:>11} "
                      f"{g['gates']:>6} {_share(g['unwitnessed'], g['gates']):>6}")
        return OK
    finally:
        conn.close()


def cmd_audit(args) -> int:
    if args.recurring:
        return cmd_audit_recurring(args)
    conn = store.connect(read_only=True)
    try:
        rows = conn.execute(
            "SELECT g.run_id, r.ability, g.step_id, g.decision, g.proof_json, g.recorded_at"
            " FROM gate g JOIN run r ON r.run_id = g.run_id ORDER BY g.recorded_at DESC"
        ).fetchall()
        unwitnessed = []
        for r in rows:
            p = json.loads(r["proof_json"] or "{}")
            if not p.get("witnessed"):
                unwitnessed.append((r, p))
        v_pre = conn.execute(
            "SELECT v.run_id, v.step_id, v.code, COUNT(*) n,"
            " json_extract(r.metadata_json, '$.actor') AS actor"
            " FROM violation v LEFT JOIN run r ON r.run_id = v.run_id"
            " GROUP BY v.run_id, v.step_id, v.code ORDER BY n DESC"
        ).fetchall()
        if args.json:
            return _emit(args, {
                "gates": {"recorded": len(rows), "unwitnessed": len(unwitnessed)},
                "unwitnessed": [{**{k: r[k] for k in r.keys() if k != "proof_json"},
                                 "proof": pr} for r, pr in unwitnessed],
                "violations": _rowdicts(v_pre),
            }, lambda _d: None)
        print(f"gates recorded: {len(rows)}   unwitnessed: {len(unwitnessed)}")
        for r, p in unwitnessed:
            print(f"  ⚠️  {r['run_id']}  {r['step_id']}  {r['decision']}"
                  f"  witness={p.get('witness')}  {r['recorded_at']}")
        v = conn.execute(
            "SELECT v.run_id, v.step_id, v.code, COUNT(*) n,"
            " json_extract(r.metadata_json, '$.actor') AS actor"
            " FROM violation v LEFT JOIN run r ON r.run_id = v.run_id"
            " GROUP BY v.run_id, v.step_id, v.code ORDER BY n DESC"
        ).fetchall()
        if v:
            print(f"violations: {sum(r['n'] for r in v)}")
            for r in v:
                print(f"  {r['code']:<24} {r['run_id'] or '-'}  "
                      f"{r['step_id'] or '-'}  ×{r['n']}"
                      + (f"  actor={r['actor']}" if r["actor"] else ""))
        return OK
    finally:
        conn.close()


def cmd_close_run(args) -> int:
    conn = store.connect()
    try:
        row = _run_or_exit(conn, args.run)
        f = _flow_for_run(conn, row)
        spec = f.result_spec
        if spec:
            if args.result is None:
                result, result_source = spec["default"], "flow_default"
            elif args.result not in spec["values"]:
                _err(f"⛔ '{args.result}' is not one of the results "
                     f"'{row['ability']}' declares.\n"
                     f"    legal: {', '.join(spec['values'])}   (default {spec['default']})\n"
                     f"    A free-form outcome cannot be counted or compared: this flow's own\n"
                     f"    history would end up holding several words for one ending.")
                return USAGE
            else:
                result, result_source = args.result, "explicit"
        else:
            # The flow never said. Keep the engine's old fallback rather than inventing a
            # vocabulary on its behalf — and `harness abilities` reports which flows are in
            # this state, so the silence is visible.
            result = args.result if args.result is not None else "completed"
            # Which of the three produced this word, because they do not carry equal weight:
            # only `flow_default` was chosen by the flow's own author.
            result_source = "explicit" if args.result is not None else "engine_fallback"
        done = store.closed_steps(conn, row["run_id"])
        # Optional steps and untaken exclusive branches are not owed. Demanding them is
        # what made the transcribed 111-step flow impossible to close.
        missing = [sid for sid in f.required_steps(row["variant"]) if sid not in done]
        if missing and not args.force_steps:
            _err(
                f"⛔ REFUSED: {len(missing)} step(s) still open: "
                f"{', '.join(missing[:8])}{' …' if len(missing) > 8 else ''}\n"
                f"    Pass --force-steps to close anyway (recorded as a breach).\n"
                f"    That authorises leaving WORK undone — it does not also excuse an\n"
                f"    undischarged obligation, which needs --force-obligations."
            )
            return REFUSED
        owed = store.open_obligations(conn, row["run_id"])
        if owed and not args.force_obligations:
            _err(
                f"⛔ REFUSED: {len(owed)} obligation(s) not discharged:\n"
                + "\n".join(f"    {r['hook_id']}  (raised at {r['trigger_kind']} "
                             f"'{r['selector']}')" for r in owed)
                + f"\n    Discharge each with: harness discharge --run {row['run_id']} "
                  f"--hook <id> --evidence \"<what you did>\"\n"
                  f"    or inspect them:      harness obligations --run {row['run_id']}"
            )
            return REFUSED
        if owed:
            store.record_violation(
                conn, row["run_id"], None, "undischarged_obligations",
                f"{len(owed)} open: {','.join(r['hook_id'] for r in owed)}")
        if missing:
            store.record_violation(conn, row["run_id"], None, "forced_close",
                                   f"{len(missing)} step(s) open: {','.join(missing)}")
        # A lease is ended by the end of a run, in BOTH directions — a row naming a closed
        # run would keep resolving to something that can no longer act, and that answer looks
        # as authoritative as a live one.
        outgoing = conn.execute(
            "SELECT scope_kind, scope_key, holder_run_id, granted_at_step FROM scope_lease"
            " WHERE grantor_run_id = ? AND released_at IS NULL", (row["run_id"],)
        ).fetchall()
        for lease in outgoing:
            # Recorded, not refused. The engine's job is to notice that authority was handed
            # out and never came back; what to DO about it is the flow's own business, and a
            # hook can raise an obligation over it.
            store.record_violation(
                conn, row["run_id"], row["current_step"], LEASE_OUTSTANDING,
                f"delegated {lease['scope_kind']}={lease['scope_key']} to "
                f"{lease['holder_run_id']} at step {lease['granted_at_step']}, still held "
                f"when this run closed",
                severity="breach")
        freed = store.release_leases_touching(conn, row["run_id"], "run_closed")
        store.close_run(conn, row["run_id"], result)
        say = _sayer(args)
        say(f"✅ closed run {row['run_id']} → {result}")
        if freed["held"] or freed["granted"]:
            say(f"   leases released: {freed['held']} held, {freed['granted']} granted")
        if outgoing:
            say(f"   ⚠️  {len(outgoing)} delegation(s) were still outstanding "
                f"(recorded as '{LEASE_OUTSTANDING}')")
        return _emit(args, {
            "run": row["run_id"], "result": result, "result_source": result_source,
            "closed": True,
            # WHAT WAS WAIVED TO GET HERE. A clean close and a forced one both print a tick;
            # these are the fields that tell them apart without reading the violation ledger.
            "forced_steps": missing if missing else [],
            "forced_obligations": [r["hook_id"] for r in owed] if owed else [],
            "leases_released": {"held": freed["held"], "granted": freed["granted"]},
            "outstanding_delegations": [
                {"scope_kind": x["scope_kind"], "scope": x["scope_key"],
                 "holder_run": x["holder_run_id"], "granted_at_step": x["granted_at_step"]}
                for x in outgoing],
        }, lambda _d: None)
    finally:
        conn.close()


# ------------------------------------------------------------------ parser

class _Parser(argparse.ArgumentParser):
    """Argparse, with two defaults corrected because both broke this engine's own promises.

    NO PREFIX ABBREVIATION. Argparse accepts any unambiguous prefix, so retiring a vague flag
    name does not actually retire it: `--force` still resolved to `--force-deps` on the one
    subcommand that had a single match, while erroring on the one that had two. The old name
    therefore kept working in half the places and the retirement was cosmetic — and worse, it
    behaved differently per subcommand.

    USAGE ERRORS EXIT 1, NOT 2. Argparse exits 2 on a bad invocation, and 2 already means
    "the spec is invalid" here. An unknown flag reported as an invalid spec sends the reader
    looking in the wrong file — and "the exit code is the product" cannot be true while one
    code means two unrelated things.
    """
    def __init__(self, *a, **kw) -> None:
        # Forced here rather than passed at each construction site, because subparsers do NOT
        # inherit it — and a setting that holds on the top-level parser while silently lapsing
        # on every subcommand is worse than not setting it: the retired name then works in
        # exactly the places nobody checked.
        kw["allow_abbrev"] = False
        super().__init__(*a, **kw)

    # argparse names this method; the casing is not ours to choose.
    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        print(f"{self.prog}: error: {message}", file=sys.stderr)
        raise SystemExit(USAGE)


def build_parser() -> argparse.ArgumentParser:
    p = _Parser(
        prog="harness",
        description="harness-engine — declare a flow, get exit-code enforcement.",
    )
    sub = p.add_subparsers(dest="cmd", required=True, parser_class=_Parser)

    sub.add_parser("init", help="create the engine's store").set_defaults(fn=cmd_init)
    ab = sub.add_parser("abilities", help="list installed abilities")
    _add_json(ab)
    ab.set_defaults(fn=cmd_abilities)
    rq = sub.add_parser("require", help="show/declare where a flow is mandatory on this machine")
    rq.add_argument("--add", metavar="ABILITY", help="declare this ability mandatory")
    rq.add_argument("--remove", metavar="ABILITY", help="drop a declared requirement")
    rq.add_argument("--scope-key", help="the scope the requirement applies to")
    rq.add_argument("--strict", action="store_true",
                    help="refuse rather than allow when the flow cannot be read")
    _add_json(rq)
    rq.set_defaults(fn=cmd_require)
    tr = sub.add_parser("trust", help="review/approve extension files this install imports")
    tr.add_argument("ability", nargs="?", help="approve this ability's providers.py")
    tr.add_argument("--forget", metavar="ABILITY", help="drop a recorded approval")
    _add_json(tr)
    tr.set_defaults(fn=cmd_trust)
    lz = sub.add_parser("leases", help="show delegated scopes")
    _add_json(lz)
    lz.set_defaults(fn=cmd_leases)
    br = sub.add_parser("brief", help="print the driving contract for this installation")
    br.add_argument("--portable", action="store_true",
                    help="use a placeholder path instead of this machine's (for a checked-in copy)")
    br.add_argument("--write", action="store_true",
                    help="write it beside the store instead of to stdout, and print the path")
    br.set_defaults(fn=cmd_brief)
    hi = sub.add_parser("history", help="list runs that have ended")
    hi.add_argument("--limit", type=int, default=20)
    hi.add_argument("--actor", help="only runs driven by this actor")
    hi.add_argument("--ability")
    hi.add_argument("--actors", action="store_true",
                    help="per-actor rollup instead of a run list")
    _add_json(hi)
    hi.set_defaults(fn=cmd_history)

    sub.add_parser("adapter-contract",
                   help="print what a runtime adapter must satisfy (JSON)"
                   ).set_defaults(fn=cmd_adapter_contract)

    v = sub.add_parser("validate", help="validate flow spec(s); exit 2 if invalid")
    v.add_argument("ability", nargs="?")
    v.set_defaults(fn=cmd_validate)

    o = sub.add_parser("open", help="open a run of an ability's flow")
    o.add_argument("ability")
    o.add_argument("--scope", required=True, help="the scope key this run owns")
    o.add_argument("--run", help="explicit run id (default: generated)")
    o.add_argument("--title")
    o.add_argument("--allow-concurrent", action="store_true",
                   help="accept another open run in the same scope (guards go inert)")
    o.add_argument("--leased-from", metavar="RUN",
                   help="this run is delegated the scope by RUN (guards stay in force)")
    o.add_argument("--leased-at", metavar="STEP",
                   help="the step of --leased-from at which it delegated")
    o.add_argument("--variant", help="pick the flow variant explicitly (overrides derivation)")
    _add_json(o)
    o.set_defaults(fn=cmd_open)

    s = sub.add_parser("status", help="show a run, or all open runs")
    s.add_argument("--run")
    _add_json(s)
    s.set_defaults(fn=cmd_status)

    n = sub.add_parser("next", help="show the next step and its directive")
    n.add_argument("--run", required=True)
    _add_json(n)
    n.set_defaults(fn=cmd_next)

    e = sub.add_parser("enter", help="enter a step")
    e.add_argument("--run", required=True)
    e.add_argument("--step", required=True)
    e.add_argument("--force-deps", action="store_true",
                   help="enter despite unclosed dependencies (logged as forced)")
    _add_json(e)
    e.set_defaults(fn=cmd_enter)

    c = sub.add_parser("close-step", help="close a step (exit 3 if incomplete)")
    c.add_argument("--run", required=True)
    c.add_argument("--step", required=True)
    _add_json(c)
    c.set_defaults(fn=cmd_close_step)

    g = sub.add_parser("gate", help="record a gate decision (exit 3 if unproven)")
    g.add_argument("--run", required=True)
    g.add_argument("--step", required=True)
    g.add_argument("--decision", required=True, choices=["affirm", "decline", "preauth"])
    g.add_argument("--evidence")
    _add_json(g)
    g.set_defaults(fn=cmd_gate)

    gu = sub.add_parser("guard", help="may this action proceed? exit 4 = block")
    gu.add_argument("--action", required=True)
    gu.add_argument("--scope-kind", required=True)
    gu.add_argument("--scope", required=True)
    _add_json(gu)
    gu.set_defaults(fn=cmd_guard)

    pr = sub.add_parser("purge-run",
                        help="remove a CLOSED run's rows (test residue); recorded in purge_log")
    pr.add_argument("--run", required=True)
    pr.add_argument("--reason", required=True, help="why — written to purge_log")
    pr.set_defaults(fn=cmd_purge_run)

    gt = sub.add_parser("guard-tool",
                        help="may this TOOL CALL proceed? exit 4 = block (for runtime hooks)")
    gt.add_argument("--tool", required=True, help="the runtime's name for the tool")
    gt.add_argument("--input-json", required=True, help="the tool's arguments, as JSON")
    gt.add_argument("--cwd", required=True, help="where the call is happening")
    gt.set_defaults(fn=cmd_guard_tool)

    dc = sub.add_parser("discharge", help="mark a hook's obligation as fulfilled")
    dc.add_argument("--run", required=True)
    dc.add_argument("--hook", required=True)
    dc.add_argument("--evidence")
    _add_json(dc)
    dc.set_defaults(fn=cmd_discharge)

    ob = sub.add_parser("obligations", help="list this run's obligations")
    ob.add_argument("--run", required=True)
    ob.add_argument("-v", "--verbose", action="store_true")
    _add_json(ob)
    ob.set_defaults(fn=cmd_obligations)

    sw = sub.add_parser("show", help="print a step's guide section, or one of its topics")
    sw.add_argument("--run", required=True)
    sw.add_argument("--step", required=True)
    sw.add_argument("--topic")
    sw.set_defaults(fn=cmd_show)

    ag = sub.add_parser("assert-goal", help="check whether a phase meets its acceptance criterion")
    ag.add_argument("--run", required=True)
    ag.add_argument("--phase", required=True)
    _add_json(ag)
    ag.set_defaults(fn=cmd_assert_goal)

    sm = sub.add_parser("summarize", help="record a phase's rollup")
    sm.add_argument("--run", required=True)
    sm.add_argument("--phase", required=True)
    sm.add_argument("--metric", action="append", metavar="KEY=VALUE")
    sm.add_argument("--duration", type=int)
    sm.add_argument("--note")
    _add_json(sm)
    sm.set_defaults(fn=cmd_summarize)

    sk = sub.add_parser("skip", help="record that an OPTIONAL step will not run")
    sk.add_argument("--run", required=True)
    sk.add_argument("--step", required=True)
    sk.add_argument("--reason")
    _add_json(sk)
    sk.set_defaults(fn=cmd_skip)

    ev = sub.add_parser("evidence", help="record evidence backing a completion predicate")
    ev.add_argument("--run", required=True)
    ev.add_argument("--step", required=True)
    ev.add_argument("--kind", required=True)
    ev.add_argument("--value")
    _add_json(ev)
    ev.set_defaults(fn=cmd_evidence)

    cf = sub.add_parser("config", help="show or set this run's config")
    cf.add_argument("--run", required=True)
    cf.add_argument("--set", action="append", metavar="KEY=VALUE")
    _add_json(cf)
    cf.set_defaults(fn=cmd_config)

    ad = sub.add_parser("audit", help="list unwitnessed gates and violations")
    ad.add_argument("--recurring", action="store_true",
                    help="group ACROSS runs: what keeps going wrong here, with populations")
    _add_json(ad)
    ad.set_defaults(fn=cmd_audit)

    cr = sub.add_parser("close-run", help="close a run (exit 3 if steps remain)")
    cr.add_argument("--run", required=True)
    # No default here: the FLOW decides, when it has said. Baking one in was how a word the
    # flow never chose ("completed") ended up in its own history alongside three others.
    cr.add_argument("--result", default=None,
                    help="one of the flow's declared results; see `harness abilities --json`")
    # TWO flags, deliberately, and no combined one. They authorise different things:
    # leaving work undone, versus leaving a recorded commitment unmet. One switch made
    # whoever reached for it grant both — usually while meaning only the first — and the
    # familiar short name is what would keep doing that, so it is not kept as an alias.
    cr.add_argument("--force-steps", action="store_true",
                    help="close despite required steps still open (recorded as a breach)")
    cr.add_argument("--force-obligations", action="store_true",
                    help="close despite undischarged obligations (recorded as a breach)")
    _add_json(cr)
    cr.set_defaults(fn=cmd_close_run)

    return p


def _answer_in_json_too(args, rc: int) -> int:
    """A caller that asked for JSON is answered in JSON on the FAILING path too.

    WHY THIS IS A BACKSTOP AND NOT A RULE. Adding `--json` to a command means promising that
    stdout carries the answer. The failing path is where that promise matters most and where it
    is easiest to break: the write commands refuse from 26 sites, and three shared helpers
    (`_run_or_exit`, `_load_flow_or_exit`, `_scope_taken`) refuse on their behalf, so a per-site
    rule would have had to be remembered in places the command's own author does not edit. A
    caller then reads an empty stdout and concludes success, or falls back to parsing the prose
    the flag existed to avoid.

    So it hangs off `_err`, which is already the single funnel every refusal passes through to
    say anything at all. A refusal that says nothing produces no envelope — and there is nothing
    to report about it either.

    The SENTENCE rides as prose, deliberately. It names what is missing and the command that
    supplies it; re-encoding it into fields would be a second copy of the same message, free to
    drift from the one stderr prints. Sites with a machine-relevant DISTINCTION emit their own
    envelope with a `refused_because` token first, and this never fires for them.

    One case cannot be served: a malformed invocation is rejected by argparse before anything
    knows `--json` was on the command line. It stays prose, and that is honest — the request to
    be answered in JSON was itself part of what did not parse.
    """
    if args is None or _ANSWERED or not getattr(args, "json", False):
        return rc
    if rc == OK:
        # A COMMAND THAT TOOK --json AND SAID NOTHING IS A DEFECT HERE, NOT BAD INPUT.
        # The backstop below can speak for a refusal because `_err` collected the sentence;
        # there is no equivalent for a success — the answer simply was not built. Exit 5 exists
        # for exactly this: `next --json` on a finished run printed prose for as long as the
        # flag existed, because no path exercised it, and nothing anywhere said so.
        _err("⛔ INTERNAL: this command accepts --json but produced no machine answer on a\n"
             "    successful path. That is a defect in the engine: the flag promises stdout\n"
             "    carries the answer, and on this path it does not.")
        return INTERNAL
    fn = getattr(args, "fn", None)
    print(json.dumps({
        # Derived from the handler, not declared a second time next to the subparser.
        "command": (fn.__name__[4:].replace("_", "-") if fn is not None else None),
        "code": rc,
        "code_name": _EXIT_NAMES.get(rc, "?"),
        "why": "\n".join(_SAID),
    }, indent=2, ensure_ascii=False))
    return rc


def main(argv: list[str] | None = None) -> int:
    """Every exit from the CLI passes through here, INCLUDING an unexpected one.

    WHY THERE IS A CODE FOR "THIS IS OUR BUG". Python exits 1 on an uncaught exception, and 1
    is also this CLI's usage/environment error — so a crash was indistinguishable from a
    refusal for anything that reads the number, which is what a tool hook and every test here
    do. It was not theoretical: a mutation that removed the newer-store refusal fell through to
    a RuntimeError, and the test asserting `== USAGE` passed and called the crash a refusal.

    Fixing that by adding a message assertion to every affected test would have been fixing
    ~20 symptoms of one ambiguity. A distinct code removes it at the source: 1 now means the
    input or the environment, 5 means the engine.

    The traceback still goes to stderr — a bug that hides its own location is worse than one
    with an ugly exit — and `parse_args` is inside the try, so a defect in the parser itself
    cannot escape as a bare 1 either. `KeyboardInterrupt` is a BaseException and deliberately
    passes straight through: the user interrupting is not an engine fault.
    """
    global _ANSWERED
    _SAID.clear()
    _ANSWERED = False
    args = None
    try:
        args = build_parser().parse_args(argv)
        rc = args.fn(args)
    except store.StoreUnusable as exc:
        _err(f"⛔ {exc}")
        rc = USAGE
    except flowmod.FlowError as exc:
        # Handled here and not only per call site: discovering WHAT IS INSTALLED can now
        # fail — a misconfigured root, or one name present in two roots — and that happens
        # inside commands whose own try/except wraps only the LOADING of a single spec.
        # Without this, the honest refusal those checks raise reaches the user as a
        # traceback, which reads as an engine bug rather than as their configuration.
        _err(f"⛔ {exc}")
        rc = BAD_SPEC
    except SystemExit as exc:  # raised by the _or_exit helpers, and by argparse
        rc = int(exc.code or 0)
    except Exception:
        traceback.print_exc()
        _err("⛔ INTERNAL: the engine failed in a way it does not account for.\n"
             "    This is a defect here, not a problem with your input — the traceback above\n"
             "    is the whole report. Exit 5 is reserved for it so that nothing has to tell\n"
             "    a crash apart from a refusal by reading text.")
        rc = INTERNAL
    return _answer_in_json_too(args, rc)


if __name__ == "__main__":
    sys.exit(main())
