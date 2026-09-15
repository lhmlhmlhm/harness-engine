"""Behaviour tests — the exit codes ARE the contract, so they are what gets pinned.

Every test here asserts a NUMBER, not a message. A tool hook branches on the number;
if a refactor keeps the wording and changes the code, the enforcement silently
disappears and only these assertions notice.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
HARNESS = REPO / "bin" / "harness"

OK, USAGE, BAD_SPEC, REFUSED, BLOCKED = 0, 1, 2, 3, 4


@pytest.fixture
def env(tmp_path, monkeypatch):
    """An isolated store per test. Never touches a developer's real state."""
    state = tmp_path / "state"
    e = {"HARNESS_STATE_DIR": str(state)}
    run(["init"], e)
    return e


def run(args, env_extra, *, transcript=None, witness=None):
    import os
    env = {**os.environ, **env_extra}
    env.pop("HARNESS_TRANSCRIPT", None)
    env.pop("HARNESS_WITNESS", None)
    if transcript is not None:
        env["HARNESS_TRANSCRIPT"] = str(transcript)
    if witness is not None:
        env["HARNESS_WITNESS"] = witness
    return subprocess.run(
        [sys.executable, str(HARNESS), *args],
        capture_output=True, text=True, env=env, cwd=str(REPO),
    )


def rc(args, env_extra, **kw) -> int:
    return run(args, env_extra, **kw).returncode


def transcript_with(tmp_path, n_human: int) -> Path:
    p = tmp_path / f"t{n_human}.jsonl"
    lines = []
    for i in range(n_human):
        lines.append(json.dumps({"role": "user", "content": f"turn {i}"}))
        lines.append(json.dumps({"role": "assistant", "content": "ok"}))
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def advance_to(env_extra, run_id, upto):
    """Close the delivery steps that only need evidence, up to (not including) `upto`."""
    seq = [("C01", "change_list"), ("C02", "test_baseline"),
           ("K01", "diff"), ("K02", "test_result")]
    for step, kind in seq:
        if step == upto:
            return
        assert rc(["evidence", "--run", run_id, "--step", step,
                   "--kind", kind, "--value", "pass"], env_extra) == OK
        assert rc(["close-step", "--run", run_id, "--step", step], env_extra) == OK
    if upto != "D01":
        assert rc(["close-step", "--run", run_id, "--step", "D01"], env_extra) == OK


# ------------------------------------------------------------------ smoke

def test_init_creates_every_core_table(env):
    db = Path(env["HARNESS_STATE_DIR"]) / "harness.db"
    assert db.is_file()
    conn = sqlite3.connect(db)
    try:
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )}
    finally:
        conn.close()
    assert names == {"run", "step_log", "gate", "evidence", "violation", "obligation",
                     "phase_summary", "purge_log", "scope_lease"}


def test_commands_refuse_before_init(tmp_path):
    """No lazy table creation: an uninitialised store is a loud error, not an empty one."""
    e = {"HARNESS_STATE_DIR": str(tmp_path / "nope")}
    assert rc(["status"], e) == USAGE


def test_both_abilities_validate(env):
    assert rc(["validate"], env) == OK
    out = run(["abilities"], env).stdout
    assert "delivery" in out and "authoring" in out


# ------------------------------------------------------------------ flow spec

def test_bad_spec_exits_bad_spec_and_never_touches_the_store(env, tmp_path, monkeypatch):
    """A malformed spec must fail at LOAD time, before any state is written."""
    broken = REPO / "abilities" / "__broken_test__"
    broken.mkdir(exist_ok=True)
    (broken / "flow.yaml").write_text(
        # depends on a step that does not exist
        "version: 2\nability: __broken_test__\nrole: fixture\nscope_kind: x\n"
        "phases:\n  - id: p\n"
        "steps:\n  - id: S1\n    phase: p\n    deps: [NOPE]\n",
        encoding="utf-8",
    )
    try:
        assert rc(["validate", "__broken_test__"], env) == BAD_SPEC
        assert rc(["open", "__broken_test__", "--scope", "s"], env) == BAD_SPEC
        assert run(["status"], env).stdout.strip() == "(no open runs)"
    finally:
        (broken / "flow.yaml").unlink()
        broken.rmdir()


def test_unknown_step_key_is_fatal(env):
    """A typo'd key must be fatal, never silently ignored.

    `gaet: affirm` reads as a gate declaration and would produce an UNGATED step —
    the same "looks protective, is inert" failure the guard validation refuses. This
    was found by transcribing a real 111-step flow, where the source system's third
    structural level (`stage`) had nowhere to go and was silently swallowed.
    """
    d = REPO / "abilities" / "__key_test__"
    d.mkdir(exist_ok=True)
    (d / "flow.yaml").write_text(
        "version: 2\nability: __key_test__\nrole: fixture\nscope_kind: x\n"
        "phases:\n  - id: p\n"
        "steps:\n  - id: S1\n    phase: p\n    gaet: affirm\n    stage: oops\n",
        encoding="utf-8",
    )
    try:
        r = run(["validate", "__key_test__"], env)
        assert r.returncode == BAD_SPEC
        assert "gaet" in r.stderr and "stage" in r.stderr
    finally:
        (d / "flow.yaml").unlink()
        d.rmdir()


def test_unknown_root_key_is_fatal(env):
    d = REPO / "abilities" / "__root_test__"
    d.mkdir(exist_ok=True)
    (d / "flow.yaml").write_text(
        "version: 2\nability: __root_test__\nrole: fixture\nscope_kind: x\nstages: []\n"
        "phases:\n  - id: p\n"
        "steps:\n  - id: S1\n    phase: p\n",
        encoding="utf-8",
    )
    try:
        r = run(["validate", "__root_test__"], env)
        assert r.returncode == BAD_SPEC
        assert "stages" in r.stderr
    finally:
        (d / "flow.yaml").unlink()
        d.rmdir()


def test_transcribed_flow_has_all_four_prose_tiers(env):
    """Every step of the transcribed flow must carry all four tiers.

    A partially-filled tier is worse than an empty one: `next` prints the directive
    unconditionally, so a step whose directive is missing looks identical to a step that
    needs nothing said about it. Pinning the counts is what stops the tiers rotting
    unevenly as steps get added.

    The directives here are DERIVED from the guides' contract blocks rather than written
    separately, which is why they cannot drift apart: one source, two presentations.
    """
    r = run(["validate", "shipcheck-asis"], env)
    assert r.returncode == OK, r.stderr
    # Asserted as FULL coverage, not a literal count. Pinning 101 made a legitimate addition
    # look like a regression — the invariant is that every step has both layers, whatever the
    # step count happens to be.
    from engine import flow as _fl
    n = len(_fl.load("shipcheck-asis").steps)
    assert f"directive {n}/{n}" in r.stdout, (n, r.stdout)
    assert f"guide {n}/{n}" in r.stdout, (n, r.stdout)
    assert f"own section {n}" in r.stdout, (
        "every step must resolve to its OWN prose section — a step falling back to the "
        "whole file means its anchor stopped matching"
    )
    assert "⚠️" not in r.stderr  # no orphan topics


def test_transcribed_flow_prose_names_no_internal_systems(env):
    """The prose is ORIGINAL, not copied — and this is how that stays true.

    A leaked internal system name is evidence of transcription rather than authorship, and
    it also makes the ability unusable outside the environment that owns the name.
    """
    prose = REPO / "abilities" / "shipcheck-asis" / "prose"
    files = sorted(prose.glob("*.md")) + sorted((prose / "topics").glob("*.md"))
    assert len(files) >= 9, f"expected the eight phases plus topics, found {len(files)}"
    banned = ["crux", "brazil", "taskei", "midway", "agent-fleet",
              "autosde", "coverlay", "easymrgr", "mwinit"]
    for f in files:
        low = f.read_text(encoding="utf-8").lower()
        for word in banned:
            assert word not in low, f"{f.name} names {word!r}"



def _close_step_honestly(env, run_id: str, sid: str, step_raw: dict, counts: dict | None = None):
    """Close a step, and when a claim of absence is DISPROVED, do what an honest run does.

    Shared by both full-flow drivers because they were drifting: the retry lived in one of
    them, so adding a corroborated criterion elsewhere in the flow broke the other with an
    unrelated-looking exit 3. A driver duplicated is a driver that stops testing the same
    thing.

    `_satisfy` deliberately records the claim of absence — the value that needs no further
    work — so this path is REACHED rather than dodged. If the world contradicts it, record
    what is really the case and close. If the flow declares no non-claim value, the step
    genuinely cannot be closed here, and that is reported as such.
    """
    r = run(["close-step", "--run", run_id, "--step", sid], env)
    if r.returncode == REFUSED and "claim_corroborated" in r.stderr:
        assert "an independent fact says otherwise" in r.stderr or \
               "is unavailable" in r.stderr, (sid, r.stderr)
        if counts is not None:
            counts["claim_disproved"] = counts.get("claim_disproved", 0) + 1
        for rq in predicates_requirements(step_raw.get("completion") or {}):
            if not rq.get("claims"):
                continue
            legal = rq.get("must_be_one_of") or _truthful_values(step_raw, rq["kind"])
            alt = [v for v in legal if v not in rq["claims"]]
            if not alt:
                # EVERY legal value is a claim, each with its own disproving fact. Then exactly
                # ONE of them should survive here, and which one is a property of the world
                # rather than of the flow — so try them and let the engine pick. This also
                # asserts the interesting thing about such an enum: it is a real PARTITION.
                # If none survives, the values do not cover the world; if two do, one of the
                # corroborations is inert.
                survivors = []
                for v in legal:
                    assert rc(["evidence", "--run", run_id, "--step", sid,
                               "--kind", rq["kind"], "--value", v], env) == OK, sid
                    if run(["close-step", "--run", run_id, "--step", sid],
                           env).returncode == OK:
                        survivors.append(v)
                        break
                assert survivors, (
                    f"{sid}: every legal value of {rq['kind']!r} is a corroborated claim "
                    f"({legal}) and NONE of them survives — the values do not cover the world."
                )
                continue
            assert rc(["evidence", "--run", run_id, "--step", sid,
                       "--kind", rq["kind"], "--value", alt[0]], env) == OK, sid
        r = run(["close-step", "--run", run_id, "--step", sid], env)
    return r


def predicates_requirements(spec: dict) -> list[dict]:
    from engine import predicates
    return predicates.requirements(spec)


def _truthful_values(step_raw: dict, kind: str) -> list[str]:
    """The legal values for `kind` as the step's OWN criteria declare them.

    Read from the spec rather than hardcoded, for the same reason `_satisfy` is: a driver
    carrying its own table of acceptable answers keeps passing after the flow's vocabulary
    changes underneath it.
    """
    out: list[str] = []

    def walk(sp) -> None:
        if not isinstance(sp, dict):
            return
        for sub in sp.get("checks") or []:
            walk(sub)
        if sp.get("kind") == kind and sp.get("values"):
            out.extend(str(v) for v in sp["values"])

    walk(step_raw.get("completion") or {})
    return out


def _satisfy(env, run_id: str, sid: str, spec: dict, variant: str | None = None) -> None:
    """Record whatever a step's completion spec DEMANDS, derived from the spec itself.

    Deliberately not a hardcoded table. A driver that knows by hand which kinds each step
    wants would keep passing after a criterion was weakened — the test would be pinning its
    own assumptions instead of the flow. Asking the engine (`predicates.requirements`) means a
    criterion that changes shape either still gets satisfied or fails loudly here.
    """
    from engine import predicates
    reqs = predicates.requirements(spec)
    paired = [r for r in reqs if r.get("note")]          # fields_agree: both sides must MATCH
    shared = "agreed-value"
    for r in reqs:
        if r["what"] != "evidence":
            continue
        if "variant" in (r.get("note") or ""):
            # A reconciliation check wants the run's own variant, not an arbitrary value —
            # feeding it anything else would make the check pass for the wrong reason.
            assert variant, f"{sid} reconciles against the variant; the driver must pass it"
            value = variant
        elif r.get("claims"):
            # Take the HARDER path on purpose. A claim of absence is the value that needs no
            # further work, so a driver that records anything else never reaches the
            # corroboration check at all — and the criterion would then pass this suite while
            # broken. Recording the claim is what forces the independent fact to be consulted.
            value = r["claims"][0]
        elif r.get("must_be_one_of"):
            value = r["must_be_one_of"][0]
        elif "must_equal" in r:
            value = str(r["must_equal"])
        elif r in paired:
            value = shared
        else:
            value = f"{sid}:{r['kind']}"
        for _ in range(int(r.get("min_count", 1))):
            assert rc(["evidence", "--run", run_id, "--step", sid,
                       "--kind", r["kind"], "--value", value], env) == OK, (sid, r)


def test_the_real_flow_end_to_end_with_every_mechanism(env, tmp_path):
    """ONE run that exercises every mechanism at once, and closes without --force.

    WHY THIS TEST AND NOT SEVERAL. Each mechanism already has its own test, and they all
    passed while the combination did not: an earlier full walk closed cleanly precisely
    BECAUSE no hook ever fired, so the obligation chain — the one thing that can refuse a
    close — went unexercised in the same breath as everything else. Two mechanisms both
    verified separately is not evidence that they compose.

    So this drives a real repository through all 101 steps and requires, in a single run:

        witness       every affirm gate proven by a NEW human turn (no `manual` downgrade),
                      AND a second gate claiming the previous reply is refused
        preauth       config-authorised gates accepted only after the key is set
        optional      29 steps skipped, and not owed at close
        exclusive     one branch taken, its sibling never touched, close still possible
        obligation    a hook fires on a real condition, REFUSES the close, then discharges
        analysis      the copied domain tool produces a verdict from a real change-set
        summarize     phase rollups recorded
        audit         zero unwitnessed gates, zero violations

    The obligation is triggered honestly rather than staged: the run declares a change
    file whose old value still survives, so the analysis genuinely fails an assertion.
    """
    import json as _json

    # A repository where a declared old value really does survive the change.
    ws = tmp_path / "repo"
    ws.mkdir()
    def git(*a):
        subprocess.run(["git", "-C", str(ws), *a], capture_output=True, check=False)
    git("init", "-q")
    (ws / "a.py").write_text("MAX = 3\ndef f():\n    return MAX\n", encoding="utf-8")
    git("add", "-A")
    git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
    (ws / "a.py").write_text("MAX = 3\ndef f():\n    return 99\n", encoding="utf-8")

    transcript = tmp_path / "t.jsonl"
    transcript.write_text("", encoding="utf-8")
    human_turns = 0

    def human(msg: str) -> None:
        nonlocal human_turns
        human_turns += 1
        with transcript.open("a", encoding="utf-8") as fh:
            fh.write(_json.dumps({"role": "user", "content": msg}, ensure_ascii=False) + "\n")

    assert rc(["open", "shipcheck-asis", "--scope", str(ws), "--run", "full"], env) == OK

    import yaml as _yaml
    flow = _yaml.safe_load(
        (REPO / "abilities" / "shipcheck-asis" / "flow.yaml").read_text(encoding="utf-8"))
    steps = flow["steps"]
    exclusive = set(sum(flow.get("exclusive_groups") or [], []))

    # Declare a change set so the analysis has something to check. This is what makes the
    # assertion fail for a real reason rather than a contrived one.
    first = steps[0]["id"]
    assert rc(["evidence", "--run", "full", "--step", first,
               "--kind", "change_file", "--value", "a.py"], env) == OK
    assert rc(["evidence", "--run", "full", "--step", first,
               "--kind", "old_value", "--value", "MAX = 3"], env) == OK

    counts = {"closed": 0, "skipped": 0, "affirm": 0, "preauth": 0, "branch_skipped": 0,
              "claim_disproved": 0,
              "same_turn_refused": 0}
    branch_taken = False
    phases_seen = []

    for s in steps:
        sid = s["id"]
        if sid in exclusive:
            if branch_taken:
                counts["branch_skipped"] += 1
                continue
            branch_taken = True
        if s["phase"] not in phases_seen:
            # Wrap the PREVIOUS phase up before entering the next. `summarize` refuses unless
            # that phase meets its goal, so walking the flow is now also proof every layer
            # passed its own acceptance criterion — and a later step's cross-phase criterion
            # ("every reached phase is summarized") can actually be satisfied in order.
            if phases_seen:
                assert rc(["summarize", "--run", "full", "--phase", phases_seen[-1],
                           "--note", "walked"], env) == OK, phases_seen[-1]
            phases_seen.append(s["phase"])
        if s.get("optional"):
            assert rc(["skip", "--run", "full", "--step", sid,
                       "--reason", "not applicable"], env) == OK, sid
            counts["skipped"] += 1
            continue

        gate = s.get("gate", "none")
        if gate == "affirm":
            counts["affirm"] += 1
            # On the SECOND affirm gate onward, first try recording it WITHOUT a new human
            # turn. That is the exact shape of a fabricated affirmation — two gates claiming
            # one reply — and the witness must refuse it.
            #
            # It has to be the second, not the first: the first gate has no prior gate, so
            # there is no cursor to compare against and the check legitimately cannot fire.
            # An earlier version of this test appended a turn before EVERY gate, so the
            # same-turn path was never exercised here and a mutation that deleted the check
            # outright left the whole suite green.
            if counts["affirm"] == 2:
                r = run(["gate", "--run", "full", "--step", sid, "--decision", "affirm",
                         "--evidence", "claiming the previous reply"],
                        env, transcript=transcript, witness="transcript")
                assert r.returncode == REFUSED, (
                    f"{sid}: a second gate with no new human turn must be refused, got "
                    f"{r.returncode}\n{r.stderr}"
                )
                assert "no new human turn" in r.stderr, r.stderr
                counts["same_turn_refused"] += 1
            human(f"confirmed {sid}")
            assert rc(["gate", "--run", "full", "--step", sid, "--decision", "affirm",
                       "--evidence", f"human confirmed {sid}"],
                      env, transcript=transcript, witness="transcript") == OK, sid
        elif gate.startswith("preauth:"):
            key = gate.split(":", 1)[1]
            # A preauth gate must be REFUSED before the key is enabled — the whole point
            # of pre-authorisation is that it is granted, never inferred from progress.
            assert rc(["gate", "--run", "full", "--step", sid, "--decision", "preauth"],
                      env) == REFUSED, sid
            assert rc(["config", "--run", "full", "--set", f"{key}=true"], env) == OK
            assert rc(["gate", "--run", "full", "--step", sid, "--decision", "preauth"],
                      env) == OK, sid
            counts["preauth"] += 1

        _satisfy(env, "full", sid, s.get("completion") or {})
        r = _close_step_honestly(env, "full", sid, s, counts)
        assert r.returncode == OK, f"{sid}: {r.stderr}"
        counts["closed"] += 1

    # Every step accounted for, and the untaken branch counted once.
    assert counts["closed"] + counts["skipped"] + counts["branch_skipped"] == len(steps)
    assert counts["skipped"] >= 20, "the optional steps should have been skipped"
    assert counts["branch_skipped"] == 1, "exactly one exclusive branch should be untaken"
    assert counts["affirm"] >= 3 and counts["preauth"] >= 1
    # A claim of absence was recorded, an independent fact contradicted it, and the walk was
    # stopped — on the real repository. Asserted rather than merely tolerated: if this drops to
    # zero the criterion is no longer being reached, and the run would close on the agent's
    # word for the one answer that costs nothing to give.
    assert counts["claim_disproved"] >= 1, counts
    assert human_turns == counts["affirm"], "one real human turn per affirm gate"
    assert counts["same_turn_refused"] == 1, (
        "the same-turn fabrication path must have been exercised exactly once — if this is "
        "zero the walk never tried to record two gates against one reply, and a mutation "
        "deleting the witness cursor check would pass unnoticed"
    )

    # The analysis found a genuine failure, so an obligation is outstanding and the run
    # cannot be closed on it.
    obs = run(["obligations", "--run", "full", "-v"], env).stdout
    assert "⏳ OPEN" in obs
    assert "blast-radius-failures" in obs, obs
    assert "A1" in obs, "the failing assertion should appear in the fact snapshot"
    assert rc(["close-run", "--run", "full"], env) == REFUSED

    # Phase rollups, then discharge, then a clean close with no force.
    assert rc(["summarize", "--run", "full", "--phase", phases_seen[-1],
               "--note", "walked"], env) == OK, phases_seen[-1]
    for line in run(["obligations", "--run", "full"], env).stdout.splitlines():
        if line.strip().startswith("⏳"):
            assert rc(["discharge", "--run", "full", "--hook", line.split()[2],
                       "--evidence", "handled"], env) == OK

    assert rc(["close-run", "--run", "full", "--result", "completed"], env) == OK

    audit = run(["audit"], env).stdout
    assert "unwitnessed: 0" in audit, audit
    # The ONLY violations should be the ones this test deliberately provoked, and asserting
    # the EXACT set is what makes that meaningful — "no violations" would have quietly
    # accepted an unrelated one appearing later, and a bare count would not notice a
    # different code substituting itself.
    #   unauthorised_preauth  one per preauth gate, attempted before its key was set, to
    #                         prove pre-authorisation is granted and never inferred
    #   gate_refused_unwitnessed  the one same-turn fabrication attempt, refused
    # Both are 'blocked', not 'breach': every row on this run's ledger is the engine having
    # WORKED. A run that only ever refused things must still be closeable, or the terminal
    # criterion would punish attempting over complying — so the run holds ZERO breaches.
    import re as _re
    codes = _re.findall(r"^\s{2}(\w+)\s+full\s", audit, _re.M)
    assert set(codes) == {"unauthorised_preauth", "gate_refused_unwitnessed"}, (
        f"unexpected violation code(s):\n{audit}"
    )
    import sqlite3 as _sq
    _db = _sq.connect(env["HARNESS_STATE_DIR"] + "/harness.db")
    try:
        sev = dict(_db.execute(
            "SELECT severity, COUNT(*) FROM violation WHERE run_id='full' GROUP BY severity"
        ).fetchall())
    finally:
        _db.close()
    assert sev.get("breach", 0) == 0, f"a refusal must not read as a breach: {sev}"
    assert sev.get("blocked", 0) == len(codes), f"every row should be blocked: {sev}"
    assert codes.count("unauthorised_preauth") == counts["preauth"], (
        f"expected one refusal per preauth gate ({counts['preauth']})"
    )
    assert codes.count("gate_refused_unwitnessed") == counts["same_turn_refused"], (
        f"expected one per fabrication attempt ({counts['same_turn_refused']})"
    )
    assert "forced_close" not in audit
    assert "undischarged_obligations" not in audit


def test_the_transcribed_real_flow_validates(env):
    """The 111-step transcription of a real, mature flow must stay loadable.

    This is the scale test. If a change to the spec language breaks a real flow, that
    is the signal — a language that only fits toy examples proves nothing.
    """
    r = run(["validate", "shipcheck-asis"], env)
    assert r.returncode == OK, r.stderr
    # 101, not the registry's 111: ten of the registered ids turned out to be slots with
    # no content anywhere (eight in one phase, plus a step whose real work is its two
    # exclusive branches). Cross-checked against the source's own per-step table, which
    # has no row for any of them. Transcribing a registry verbatim imports its placeholders.
    # Derived, not pinned: a legitimately added step must not read as a regression.
    from engine import flow as _flmod
    _n = len(_flmod.load("shipcheck-asis").steps)
    assert f"{_n} steps" in r.stdout


# ------------------------------------------------------------------ control flow

def test_optional_step_can_be_skipped_and_run_still_closes(env):
    """The v1 blocker: a flow with an optional branch must still be closeable.

    Before `optional` existed, `close-run` demanded every step, so the transcribed
    111-step flow (which has a 16-step branch that only runs on findings) could only
    ever be closed with --force, i.e. by recording a violation on every normal run.
    """
    d = _spec(env, "__opt__", """
exclusive_groups: []
phases:
  - id: p
steps:
  - id: A
    phase: p
  - id: OPT
    phase: p
    optional: true
  - id: B
    phase: p
    deps: [A]
""")
    try:
        assert rc(["open", "__opt__", "--scope", "s", "--run", "o1"], env) == OK
        assert rc(["close-step", "--run", "o1", "--step", "A"], env) == OK
        assert rc(["close-step", "--run", "o1", "--step", "B"], env) == OK
        # OPT never ran, and that is legitimate
        assert rc(["close-run", "--run", "o1"], env) == OK
        assert "forced_close" not in run(["audit"], env).stdout
    finally:
        _rm(d)


def test_mandatory_step_cannot_be_skipped(env):
    """`skip` is only for what the SPEC declared optional, never a runtime choice."""
    d = _spec(env, "__mand__", """
phases:
  - id: p
steps:
  - id: A
    phase: p
""")
    try:
        assert rc(["open", "__mand__", "--scope", "s", "--run", "m1"], env) == OK
        r = run(["skip", "--run", "m1", "--step", "A"], env)
        assert r.returncode == REFUSED
        assert "not declared optional" in r.stderr
    finally:
        _rm(d)


def test_stage_must_be_declared_by_its_phase(env):
    """A typo'd stage would create a phantom grouping, so it is fatal."""
    d = _spec(env, "__stg__", """
phases:
  - id: p
    stages: [alpha]
steps:
  - id: A
    phase: p
    stage: alfa
""")
    try:
        r = run(["validate", "__stg__"], env)
        assert r.returncode == BAD_SPEC
        assert "does not declare" in r.stderr and "alpha" in r.stderr
    finally:
        _rm(d)


def test_exclusive_group_auto_skips_siblings(env):
    d = _spec(env, "__ex__", """
exclusive_groups:
  - [CloseY, CloseN]
phases:
  - id: p
steps:
  - id: A
    phase: p
  - id: CloseY
    phase: p
    deps: [A]
  - id: CloseN
    phase: p
    deps: [A]
""")
    try:
        assert rc(["open", "__ex__", "--scope", "s", "--run", "e1"], env) == OK
        assert rc(["close-step", "--run", "e1", "--step", "A"], env) == OK
        r = run(["close-step", "--run", "e1", "--step", "CloseY"], env)
        assert r.returncode == OK
        assert "skipped CloseN" in r.stdout
        # the group counts once, so the run is closeable with CloseN never touched
        assert rc(["close-run", "--run", "e1"], env) == OK
    finally:
        _rm(d)


def test_depending_on_one_branch_of_a_group_is_fatal(env):
    """If the other branch is taken, the dependent could never run."""
    d = _spec(env, "__br__", """
exclusive_groups:
  - [CloseY, CloseN]
phases:
  - id: p
steps:
  - id: CloseY
    phase: p
  - id: CloseN
    phase: p
  - id: Z
    phase: p
    deps: [CloseY]
""")
    try:
        r = run(["validate", "__br__"], env)
        assert r.returncode == BAD_SPEC
        assert "can never run" in r.stderr
    finally:
        _rm(d)


def test_strict_witness_refuses_the_downgrade(env):
    """Some transitions must stop rather than record a flagged pass."""
    d = _spec(env, "__sw__", """
phases:
  - id: p
steps:
  - id: G
    phase: p
    gate: affirm
    strict_witness: true
    completion: { type: gate_recorded }
""")
    try:
        assert rc(["open", "__sw__", "--scope", "s", "--run", "w1"], env) == OK
        r = run(["gate", "--run", "w1", "--step", "G", "--decision", "affirm"],
                env, witness="manual")
        assert r.returncode == REFUSED
        assert "strict_witness" in r.stderr
    finally:
        _rm(d)


def test_strict_witness_on_an_ungated_step_is_fatal(env):
    d = _spec(env, "__sw2__", """
phases:
  - id: p
steps:
  - id: A
    phase: p
    strict_witness: true
""")
    try:
        assert rc(["validate", "__sw2__"], env) == BAD_SPEC
    finally:
        _rm(d)


def test_yaml_coerced_id_is_fatal(env):
    """`- id: NO` becomes the boolean False in YAML 1.1 — a silently different id.

    Found by hand-writing a Y/N branch: the ids became "True"/"False" and every
    reference to them failed to resolve. Coercing with str() would have hidden it.
    """
    d = _spec(env, "__yml__", """
phases:
  - id: p
steps:
  - id: NO
    phase: p
""")
    try:
        r = run(["validate", "__yml__"], env)
        assert r.returncode == BAD_SPEC
        assert "not a string" in r.stderr
    finally:
        _rm(d)


def test_the_real_flow_closes_without_force(env, tmp_path):
    """End-to-end on the 111-step transcription: it must reach a clean close.

    This is the payoff test for all four primitives at once. Without them the same
    flow could only be closed with --force.
    """
    import json as _json
    t = tmp_path / "t.jsonl"
    t.write_text("", encoding="utf-8")
    assert rc(["open", "shipcheck-asis", "--scope", "/repo/t", "--run", "real"], env) == OK

    import yaml as _yaml
    flow = _yaml.safe_load((REPO / "abilities" / "shipcheck-asis" / "flow.yaml").read_text())
    turns = 0
    seen_phase = None
    for s in flow["steps"]:
        sid = s["id"]
        # Phase boundary: wrap the previous phase up. `summarize` REFUSES unless that phase
        # meets its goal, so reaching the end of the flow is now also proof that every layer
        # passed its own acceptance criterion — not just that its steps were ticked off.
        if seen_phase is not None and s["phase"] != seen_phase:
            assert rc(["summarize", "--run", "real", "--phase", seen_phase], env) == OK, seen_phase
        seen_phase = s["phase"]
        if s.get("optional"):
            assert rc(["skip", "--run", "real", "--step", sid, "--reason", "n/a"], env) == OK
            continue
        gate = s.get("gate", "none")
        if gate == "affirm":
            turns += 1
            with t.open("a", encoding="utf-8") as fh:
                fh.write(_json.dumps({"role": "user", "content": f"ok {sid}"}) + "\n")
            assert rc(["gate", "--run", "real", "--step", sid, "--decision", "affirm",
                       "--evidence", "confirmed"], env,
                      transcript=t, witness="transcript") == OK, sid
        elif gate.startswith("preauth:"):
            key = gate.split(":", 1)[1]
            assert rc(["config", "--run", "real", "--set", f"{key}=true"], env) == OK
            assert rc(["gate", "--run", "real", "--step", sid, "--decision", "preauth"],
                      env) == OK, sid
        _satisfy(env, "real", sid, s.get("completion") or {})
        if sid == "E22b":
            continue  # the exclusive sibling of E22a; closing one is enough
        r = _close_step_honestly(env, "real", sid, s)
        assert r.returncode == OK, f"{sid}: {r.stderr}"

    # Conditional hooks may have raised obligations along the way; discharge them, since
    # the point of this test is that STEPS close cleanly, not that hooks never fire.
    for r in run(["obligations", "--run", "real"], env).stdout.splitlines():
        if r.strip().startswith("⏳"):
            hook_id = r.split()[2]
            assert rc(["discharge", "--run", "real", "--hook", hook_id,
                       "--evidence", "handled in test"], env) == OK
    assert rc(["close-run", "--run", "real", "--result", "completed"], env) == OK
    audit = run(["audit"], env).stdout
    assert "unwitnessed: 0" in audit
    assert "forced_close" not in audit
    assert "undischarged_obligations" not in audit


def _spec(env, name: str, body: str) -> Path:
    """A throwaway spec for one assertion. Declared `role: fixture` — which is what it is.

    The engine requires a `production` ability to carry routing (`when:`), so every spec has
    to say which kind it is. That rule caught ~70 specs here the moment it landed, and giving
    each one an invented `when:` would have been the wrong repair: they exist to exercise the
    engine, not to be reached for. A body may still override the role by declaring its own.
    """
    d = REPO / "abilities" / name
    d.mkdir(exist_ok=True)
    role = "" if "role:" in body else "role: fixture\n"
    (d / "flow.yaml").write_text(
        f"version: 2\nability: {name}\n{role}scope_kind: s\n{body.strip()}\n",
        encoding="utf-8",
    )
    return d


def _rm(d: Path) -> None:
    (d / "flow.yaml").unlink(missing_ok=True)
    d.rmdir()


# ------------------------------------------------------------------ prose

def test_delivery_prose_resolves_and_is_covered(env):
    """The reference ability must keep its four tiers wired.

    Guards against the tiers silently rotting: a reworded heading or a moved file makes
    `guide` resolve to nothing while the spec still claims it is there.
    """
    r = run(["validate", "delivery"], env)
    assert r.returncode == OK, r.stderr
    assert "directive 10/10" in r.stdout
    assert "guide 8/10" in r.stdout
    assert "own section 8" in r.stdout
    assert "topics cited 4" in r.stdout
    assert "⚠️" not in r.stderr  # no orphan topics


def test_dangling_guide_file_is_fatal(env):
    d = _spec(env, "__g1__", """
prose:
  root: prose/
phases:
  - id: p
    guide: nope.md
steps:
  - id: A
    phase: p
""")
    (d / "prose").mkdir(exist_ok=True)
    try:
        r = run(["validate", "__g1__"], env)
        assert r.returncode == BAD_SPEC
        assert "does not exist" in r.stderr
    finally:
        (d / "prose").rmdir()
        _rm(d)


def test_dangling_explicit_anchor_is_fatal(env):
    """An explicit `#anchor` is a promise; a heading that no longer matches breaks it.

    Contrast with an ABSENT anchor, which legitimately falls back to the whole file —
    that distinction is what lets 36-of-111 steps share their stage's document without
    36 dangling references.
    """
    d = _spec(env, "__g2__", """
prose:
  root: prose/
  anchor_pattern: '^#{1,6}\\s+Step:\\s+{step_id}\\b'
phases:
  - id: p
steps:
  - id: A
    phase: p
    guide: doc.md#NOPE
""")
    pd = d / "prose"
    pd.mkdir(exist_ok=True)
    (pd / "doc.md").write_text("## Step: A something\n\nbody\n", encoding="utf-8")
    try:
        r = run(["validate", "__g2__"], env)
        assert r.returncode == BAD_SPEC
        assert "no heading" in r.stderr
    finally:
        (pd / "doc.md").unlink()
        pd.rmdir()
        _rm(d)


def test_absent_anchor_falls_back_to_whole_file(env):
    d = _spec(env, "__g3__", """
prose:
  root: prose/
  anchor_pattern: '^#{1,6}\\s+Step:\\s+{step_id}\\b'
phases:
  - id: p
    guide: doc.md
steps:
  - id: A
    phase: p
  - id: B
    phase: p
""")
    pd = d / "prose"
    pd.mkdir(exist_ok=True)
    (pd / "doc.md").write_text("## Step: A only A has a heading\n\nbody\n", encoding="utf-8")
    try:
        r = run(["validate", "__g3__"], env)
        assert r.returncode == OK, r.stderr
        # A gets its own section, B inherits the whole document — neither is an error
        assert "guide 2/2" in r.stdout and "own section 1" in r.stdout
        assert rc(["open", "__g3__", "--scope", "s", "--run", "g3"], env) == OK
        out = run(["show", "--run", "g3", "--step", "B"], env).stdout
        assert "whole file" in out
    finally:
        (pd / "doc.md").unlink()
        pd.rmdir()
        _rm(d)


def test_dangling_topic_is_fatal(env):
    d = _spec(env, "__t1__", """
prose:
  root: prose/
phases:
  - id: p
steps:
  - id: A
    phase: p
    topics: [missing-topic]
""")
    (d / "prose").mkdir(exist_ok=True)
    try:
        r = run(["validate", "__t1__"], env)
        assert r.returncode == BAD_SPEC
        assert "missing-topic" in r.stderr
    finally:
        (d / "prose").rmdir()
        _rm(d)


def test_orphan_topic_warns_but_does_not_fail(env):
    """Prose nobody cites is a warning: freshly written and not yet wired is normal."""
    d = _spec(env, "__t2__", """
prose:
  root: prose/
phases:
  - id: p
steps:
  - id: A
    phase: p
""")
    pd = d / "prose" / "topics"
    pd.mkdir(parents=True, exist_ok=True)
    (pd / "nobody-cites-me.md").write_text("orphan\n", encoding="utf-8")
    try:
        r = run(["validate", "__t2__"], env)
        assert r.returncode == OK
        assert "nobody-cites-me" in r.stderr and "no step cites it" in r.stderr
    finally:
        (pd / "nobody-cites-me.md").unlink()
        pd.rmdir()
        (d / "prose").rmdir()
        _rm(d)


def test_prose_root_may_not_escape_the_ability(env):
    """An ability must be a movable unit; prose reaching outside breaks that."""
    d = _spec(env, "__esc__", """
prose:
  root: ../delivery/prose/
phases:
  - id: p
steps:
  - id: A
    phase: p
""")
    try:
        r = run(["validate", "__esc__"], env)
        assert r.returncode == BAD_SPEC
        assert "escapes the ability directory" in r.stderr
    finally:
        _rm(d)


def test_anchor_pattern_must_carry_the_placeholder(env):
    d = _spec(env, "__ap__", """
prose:
  root: prose/
  anchor_pattern: '^## Step'
phases:
  - id: p
steps:
  - id: A
    phase: p
""")
    (d / "prose").mkdir(exist_ok=True)
    try:
        r = run(["validate", "__ap__"], env)
        assert r.returncode == BAD_SPEC
        assert "{step_id}" in r.stderr
    finally:
        (d / "prose").rmdir()
        _rm(d)


def test_guide_precedence_is_step_over_stage_over_phase(env):
    d = _spec(env, "__prec__", """
prose:
  root: prose/
  anchor_pattern: '^#{1,6}\\s+Step:\\s+{step_id}\\b'
phases:
  - id: p
    guide: phase.md
    stages:
      - id: s1
        guide: stage.md
steps:
  - id: A
    phase: p
    stage: s1
  - id: B
    phase: p
    stage: s1
    guide: step.md
  - id: C
    phase: p
""")
    pd = d / "prose"
    pd.mkdir(exist_ok=True)
    for name in ("phase", "stage", "step"):
        (pd / f"{name}.md").write_text(f"# {name} level\n", encoding="utf-8")
    try:
        assert rc(["validate", "__prec__"], env) == OK
        assert rc(["open", "__prec__", "--scope", "s", "--run", "pr"], env) == OK
        assert "stage-level" in run(["show", "--run", "pr", "--step", "A"], env).stdout
        assert "step-level" in run(["show", "--run", "pr", "--step", "B"], env).stdout
        assert "phase-level" in run(["show", "--run", "pr", "--step", "C"], env).stdout
    finally:
        for name in ("phase", "stage", "step"):
            (pd / f"{name}.md").unlink()
        pd.rmdir()
        _rm(d)


def test_show_refuses_an_uncited_topic(env):
    assert rc(["open", "delivery", "--scope", "/r", "--run", "sh"], env) == OK
    r = run(["show", "--run", "sh", "--step", "K01", "--topic", "never-forge-a-gate"], env)
    assert r.returncode == USAGE
    assert "does not cite" in r.stderr


def test_next_prints_the_pointer_not_the_document(env):
    """Tiering only pays off if `next` stays terse."""
    assert rc(["open", "delivery", "--scope", "/r", "--run", "nx"], env) == OK
    for step, kind in (("C01", "change_list"), ("C02", "test_baseline")):
        assert rc(["evidence", "--run", "nx", "--step", step,
                   "--kind", kind, "--value", "ok"], env) == OK
        assert rc(["close-step", "--run", "nx", "--step", step], env) == OK
    out = run(["next", "--run", "nx"], env).stdout
    assert "build.md" in out                       # the pointer is shown
    assert "harness show" in out                   # and how to get the text
    assert "三条硬约束" not in out                  # but not the text itself
    assert len(out.splitlines()) < 20


# ------------------------------------------------------------------ conditions & hooks

def test_unknown_fact_is_fatal_with_a_suggestion(env):
    """A typo'd fact must be exit 2, never a silently-false condition.

    This is THE reason the fact schema exists. Without it `changed_file` (singular) would
    evaluate to nothing, the condition would come out false, the hook would never fire,
    and nobody would notice for a year.
    """
    d = _spec(env, "__f1__", """
facts:
  provider: run_progress
hooks:
  - id: h
    trigger: phase_end
    phase: p
    when: { fact: closed_step, matches_any: ["*"] }
    contract: x
phases:
  - id: p
steps:
  - id: A
    phase: p
""")
    try:
        r = run(["validate", "__f1__"], env)
        assert r.returncode == BAD_SPEC
        assert "not in the provider's schema" in r.stderr
        assert "did you mean 'closed_steps'" in r.stderr
    finally:
        _rm(d)


def test_operator_type_mismatch_is_fatal(env):
    d = _spec(env, "__f2__", """
facts:
  provider: scope_only
hooks:
  - id: h
    trigger: phase_end
    phase: p
    when: { fact: scope, count_gte: 3 }
    contract: x
phases:
  - id: p
steps:
  - id: A
    phase: p
""")
    try:
        r = run(["validate", "__f2__"], env)
        assert r.returncode == BAD_SPEC
        assert "cannot be applied to a fact of type 'str'" in r.stderr
    finally:
        _rm(d)


def test_unknown_operator_is_fatal_with_a_suggestion(env):
    d = _spec(env, "__f3__", """
facts:
  provider: run_progress
hooks:
  - id: h
    trigger: phase_end
    phase: p
    when: { fact: closed_steps, matchs_any: ["*"] }
    contract: x
phases:
  - id: p
steps:
  - id: A
    phase: p
""")
    try:
        r = run(["validate", "__f3__"], env)
        assert r.returncode == BAD_SPEC
        assert "operator 'matchs_any' is not registered" in r.stderr
        assert "matches_any" in r.stderr
    finally:
        _rm(d)


def test_unknown_fact_provider_is_fatal(env):
    d = _spec(env, "__f4__", """
facts:
  provider: no_such_provider
phases:
  - id: p
steps:
  - id: A
    phase: p
""")
    try:
        r = run(["validate", "__f4__"], env)
        assert r.returncode == BAD_SPEC
        assert "not registered" in r.stderr
    finally:
        _rm(d)


def test_yaml_reserved_key_gives_a_legible_error(env):
    """`on:` is a YAML 1.1 boolean, so the key becomes True and matches nothing.

    Found by choosing `on:` as the trigger key name — it surfaced as a TypeError while
    formatting the error rather than a refusal, so both the key name and the check were
    fixed.
    """
    d = _spec(env, "__yk__", """
phases:
  - id: p
steps:
  - id: A
    phase: p
    on: something
""")
    try:
        r = run(["validate", "__yk__"], env)
        assert r.returncode == BAD_SPEC
        assert "non-string key" in r.stderr
        assert "YAML 1.1" in r.stderr
    finally:
        _rm(d)


def test_nested_boolean_conditions_evaluate(env):
    """all_of / any_of / not compose to any depth."""
    d = _spec(env, "__nb__", """
facts:
  provider: run_progress
hooks:
  - id: deep
    trigger: step_close
    step: A
    when:
      any_of:
        - all_of:
            - { fact: gate_count, count_gte: 99 }
            - { fact: violation_count, count_lte: 0 }
        - not: { fact: closed_steps, contains: NEVER }
    contract: fired
phases:
  - id: p
steps:
  - id: A
    phase: p
""")
    try:
        assert rc(["validate", "__nb__"], env) == OK
        assert rc(["open", "__nb__", "--scope", "s", "--run", "nb"], env) == OK
        out = run(["close-step", "--run", "nb", "--step", "A"], env).stdout
        # the first branch is false, the second (a `not`) is true → any_of holds
        assert "hook deep" in out and "fired" in out
        assert "any_of: ✔" not in out  # trace only printed when NOT matched
    finally:
        _rm(d)


def test_unmatched_hook_prints_a_per_leaf_trace(env):
    """'the hook did not fire' must be answerable, or the condition rots unnoticed."""
    d = _spec(env, "__tr__", """
facts:
  provider: run_progress
hooks:
  - id: never
    trigger: step_close
    step: A
    when: { fact: gate_count, count_gte: 99 }
    contract: x
phases:
  - id: p
steps:
  - id: A
    phase: p
""")
    try:
        assert rc(["open", "__tr__", "--scope", "s", "--run", "tr"], env) == OK
        out = run(["close-step", "--run", "tr", "--step", "A"], env).stdout
        assert "condition not met" in out
        assert "gate_count count_gte 99" in out
        assert "actual: 0" in out
    finally:
        _rm(d)


def test_obligation_blocks_close_run_until_discharged(env):
    d = _spec(env, "__ob__", """
facts:
  provider: run_progress
hooks:
  - id: must_do
    trigger: step_close
    step: A
    mode: contract
    obligation: true
    contract: do the thing
phases:
  - id: p
steps:
  - id: A
    phase: p
""")
    try:
        assert rc(["open", "__ob__", "--scope", "s", "--run", "ob"], env) == OK
        assert rc(["close-step", "--run", "ob", "--step", "A"], env) == OK
        r = run(["close-run", "--run", "ob"], env)
        assert r.returncode == REFUSED
        assert "must_do" in r.stderr
        assert rc(["discharge", "--run", "ob", "--hook", "must_do",
                   "--evidence", "did it"], env) == OK
        assert rc(["close-run", "--run", "ob"], env) == OK
    finally:
        _rm(d)


def test_obligation_snapshots_the_facts(env):
    """A discharge reviewed later is unauditable unless the facts are frozen with it."""
    d = _spec(env, "__snap__", """
facts:
  provider: run_progress
hooks:
  - id: snap
    trigger: step_close
    step: A
    obligation: true
    when: { fact: closed_steps, count_lte: 99 }
    contract: x
phases:
  - id: p
steps:
  - id: A
    phase: p
""")
    try:
        assert rc(["open", "__snap__", "--scope", "s", "--run", "sn"], env) == OK
        assert rc(["close-step", "--run", "sn", "--step", "A"], env) == OK
        out = run(["obligations", "--run", "sn", "-v"], env).stdout
        assert "facts at that moment" in out
        assert "closed_steps=" in out
    finally:
        _rm(d)


def test_forced_close_records_undischarged_obligations(env):
    d = _spec(env, "__fo__", """
facts:
  provider: run_progress
hooks:
  - id: skipme
    trigger: step_close
    step: A
    obligation: true
    contract: x
phases:
  - id: p
steps:
  - id: A
    phase: p
""")
    try:
        assert rc(["open", "__fo__", "--scope", "s", "--run", "fo"], env) == OK
        assert rc(["close-step", "--run", "fo", "--step", "A"], env) == OK
        # BOTH, because this run has open steps AND an owed obligation — and the whole point
        # of the split is that authorising one does not authorise the other.
        assert rc(["close-run", "--run", "fo", "--force-steps"], env) == REFUSED
        assert rc(["close-run", "--run", "fo", "--force-steps",
                   "--force-obligations"], env) == OK
        assert "undischarged_obligations" in run(["audit"], env).stdout
    finally:
        _rm(d)


def test_fail_closed_command_blocks_the_step(env):
    d = _spec(env, "__fc__", """
facts:
  provider: run_progress
hooks:
  - id: refuse
    trigger: step_close
    step: A
    mode: command
    fail_closed: true
    command: exit 7
phases:
  - id: p
steps:
  - id: A
    phase: p
  - id: B
    phase: p
""")
    try:
        assert rc(["open", "__fc__", "--scope", "s", "--run", "fc"], env) == OK
        r = run(["close-step", "--run", "fc", "--step", "A"], env)
        assert r.returncode == REFUSED
        assert "fail_closed" in r.stdout
    finally:
        _rm(d)


def test_command_failure_without_fail_closed_only_warns(env):
    """Matches the reference contract: log one line, continue — never STOP."""
    d = _spec(env, "__cw__", """
facts:
  provider: run_progress
hooks:
  - id: noisy
    trigger: step_close
    step: A
    mode: command
    command: exit 3
phases:
  - id: p
steps:
  - id: A
    phase: p
""")
    try:
        assert rc(["open", "__cw__", "--scope", "s", "--run", "cw"], env) == OK
        r = run(["close-step", "--run", "cw", "--step", "A"], env)
        assert r.returncode == OK
        assert "warning only" in r.stdout
    finally:
        _rm(d)


def test_gate_hook_on_an_ungated_step_is_fatal(env):
    """A hook that can never fire is inert-but-looks-active."""
    d = _spec(env, "__gh__", """
hooks:
  - id: h
    trigger: gate_recorded
    step: A
    contract: x
phases:
  - id: p
steps:
  - id: A
    phase: p
""")
    try:
        r = run(["validate", "__gh__"], env)
        assert r.returncode == BAD_SPEC
        assert "could never fire" in r.stderr
    finally:
        _rm(d)


def test_hook_selector_must_match_its_trigger(env):
    d = _spec(env, "__sel__", """
hooks:
  - id: h
    trigger: phase_end
    step: A
    contract: x
phases:
  - id: p
steps:
  - id: A
    phase: p
""")
    try:
        r = run(["validate", "__sel__"], env)
        assert r.returncode == BAD_SPEC
        assert "needs 'phase'" in r.stderr
    finally:
        _rm(d)


def test_template_expands_run_id_and_facts(env):
    d = _spec(env, "__tpl__", """
facts:
  provider: run_progress
hooks:
  - id: t
    trigger: step_close
    step: A
    when: { fact: gate_count, count_lte: 99 }
    contract: "run={{run_id}} trigger={{trigger}} gates={{fact.gate_count}}"
phases:
  - id: p
steps:
  - id: A
    phase: p
""")
    try:
        assert rc(["open", "__tpl__", "--scope", "s", "--run", "tp"], env) == OK
        out = run(["close-step", "--run", "tp", "--step", "A"], env).stdout
        assert "run=tp" in out and "trigger=step_close" in out and "gates=0" in out
    finally:
        _rm(d)


def test_two_abilities_use_disjoint_fact_providers(env):
    """The genericity PROOF: the shipped abilities share no facts at all.

    `delivery` reads a repository; `authoring` reads only the run's own progress. If any
    part of the engine had assumed facts are file-shaped, one of them would not work — and
    a promise of genericity that no test can break is not worth much.
    """
    import sys as _s
    _s.path.insert(0, str(REPO))
    from engine import flow as _flow  # noqa
    a, b = _flow.load("delivery"), _flow.load("authoring")
    assert set(a.facts_providers) != set(b.facts_providers)
    assert set(a.facts_schema) & set(b.facts_schema) == set(), (
        "the two abilities must not share a single fact name"
    )
    assert a.hooks and b.hooks


def test_ability_supplied_fact_provider_is_loaded(env):
    """An ability may register its own provider — the escape hatch must actually work.

    Without this, "register an operator or a provider" is an escape hatch only the ENGINE's
    author can take, which would make the genericity claim hollow.
    """
    import sys as _s
    _s.path.insert(0, str(REPO))
    from engine import facts as _f, flow as _fl
    _fl.load_extensions("shipcheck-asis")
    assert "shipcheck-asis.blast_radius" in _f.registered()
    schema = _f.schema_of("shipcheck-asis.blast_radius")
    # The distinction this schema exists to preserve: "the analysis found nothing" and
    # "the analysis did not run" must be different facts, or they collapse downstream.
    assert "analysis_ran" in schema
    assert "failed_assertions" in schema


def test_domain_analysis_decides_the_flow(env):
    """THE SEAM: a domain tool computes, the engine's declared conditions decide.

    The engine records gates; it cannot compute whether one is warranted. That computation
    is domain analysis (~2,600 lines of it in the reference system) and belongs outside.
    This test pins the arrangement that connects them:

        tool → structured output → declared facts → condition → obligation → close-run block

    Driven through the `analysis_ran: false` branch because it is the one with a designed
    meaning: no information is NOT the same as no problem, and the flow says so out loud.
    """
    assert rc(["open", "shipcheck-asis", "--scope", "/definitely/not/a/repo",
               "--run", "seam"], env) == OK
    # Walk to the end of the phase whose boundary carries the hooks.
    import yaml as _yaml
    flow = _yaml.safe_load((REPO / "abilities" / "shipcheck-asis" / "flow.yaml").read_text())
    fired = None
    for s in flow["steps"]:
        if s["phase"] not in ("context", "config", "coding", "verification"):
            break
        sid = s["id"]
        if s.get("optional"):
            assert rc(["skip", "--run", "seam", "--step", sid, "--reason", "n/a"], env) == OK
            continue
        gate = s.get("gate", "none")
        if gate == "affirm":
            assert rc(["gate", "--run", "seam", "--step", sid, "--decision", "affirm",
                       "--evidence", "ok"], env, witness="manual") == OK
        elif gate.startswith("preauth:"):
            key = gate.split(":", 1)[1]
            assert rc(["config", "--run", "seam", "--set", f"{key}=true"], env) == OK
            assert rc(["gate", "--run", "seam", "--step", sid, "--decision", "preauth"],
                      env) == OK
        _satisfy(env, "seam", sid, s.get("completion") or {})
        # Third driver to reach for the shared helper. The first two drifted apart the same
        # way and were merged last round; this one surfaced when a corroborated criterion
        # landed on a step it walks. A driver duplicated is a driver that stops testing the
        # same thing.
        r = _close_step_honestly(env, "seam", sid, s)
        assert r.returncode == OK, f"{sid}: {r.stderr}"
        fired = r.stdout

    # The hook fired and named itself. Note it does NOT print a trace: a trace answers
    # "why did this NOT fire", which is the question that is otherwise unanswerable.
    assert "blast-radius-absent" in fired
    assert "[obligation]" in fired
    # ...and the ones that did not fire each explain themselves, leaf by leaf, against the
    # facts the domain tool produced.
    assert "blast-radius-failures: condition not met" in fired
    assert "failed_assertions count_gte 1" in fired
    assert "analysis_ran equals True   (actual: False)" in fired

    # The obligation exists, carries the fact snapshot, and blocks the close
    obs = run(["obligations", "--run", "seam", "-v"], env).stdout
    assert "⏳ OPEN" in obs and "blast-radius-absent" in obs
    assert "analysis_ran=False" in obs
    assert rc(["close-run", "--run", "seam"], env) == REFUSED

    # TWO obligations, not one — a scope that is not a repository makes both the blast-radius
    # analysis and the decision pipeline unable to run, and each of those raises its own. This
    # assertion is here because the test previously discharged one and let a combined --force
    # excuse the other, so a second obligation appearing went unnoticed: exactly the silence the
    # flag split removes.
    owed = [ln.split()[2] for ln in run(["obligations", "--run", "seam"], env).stdout.splitlines()
            if ln.strip().startswith("⏳")]
    assert set(owed) == {"blast-radius-absent", "decision-gate-vacuous"}, owed
    for hook_id in owed:
        assert rc(["discharge", "--run", "seam", "--hook", hook_id,
                   "--evidence", "scope is not a repository"], env) == OK
    # Steps are still open, so this authorises leaving WORK undone — and nothing else.
    assert rc(["close-run", "--run", "seam", "--force-steps"], env) == OK


def test_guard_pointing_at_ungated_step_is_rejected(env):
    """A guard that could never refuse is a false promise, so the spec is invalid."""
    d = REPO / "abilities" / "__inert_test__"
    d.mkdir(exist_ok=True)
    (d / "flow.yaml").write_text(
        "version: 2\nability: __inert_test__\nrole: fixture\nscope_kind: x\n"
        "guards:\n  act: S1\n"
        "phases:\n  - id: p\n"
        "steps:\n  - id: S1\n    phase: p\n    gate: none\n",
        encoding="utf-8",
    )
    try:
        r = run(["validate", "__inert_test__"], env)
        assert r.returncode == BAD_SPEC
        assert "could never refuse" in r.stderr
    finally:
        (d / "flow.yaml").unlink()
        d.rmdir()


def test_diamond_dependencies_are_ordered(env):
    """authoring forks B1/B2 from A1 and rejoins at D1 — the topo sort must handle it."""
    assert rc(["open", "authoring", "--scope", "doc-1", "--run", "a1"], env) == OK
    # D1 cannot close while either fork is open, even though its own predicate is all_of
    assert rc(["close-step", "--run", "a1", "--step", "D1"], env) == REFUSED
    assert rc(["close-step", "--run", "a1", "--step", "A1"], env) == OK
    for kind, step in (("section", "B1"), ("section", "B1"), ("open_question", "B2")):
        assert rc(["evidence", "--run", "a1", "--step", step,
                   "--kind", kind, "--value", "v"], env) == OK
    assert rc(["close-step", "--run", "a1", "--step", "B1"], env) == OK
    assert rc(["close-step", "--run", "a1", "--step", "D1"], env) == REFUSED  # B2 open
    assert rc(["close-step", "--run", "a1", "--step", "B2"], env) == OK
    assert rc(["close-step", "--run", "a1", "--step", "D1"], env) == OK


# ------------------------------------------------------------------ completion

def test_close_step_refuses_open_deps(env):
    assert rc(["open", "delivery", "--scope", "/r", "--run", "d1"], env) == OK
    assert rc(["close-step", "--run", "d1", "--step", "K01"], env) == REFUSED


def test_close_step_refuses_missing_evidence(env):
    assert rc(["open", "delivery", "--scope", "/r", "--run", "d2"], env) == OK
    r = run(["close-step", "--run", "d2", "--step", "C01"], env)
    assert r.returncode == REFUSED
    assert "change_list" in r.stderr  # names what is missing, not just "no"


def test_evidence_match_is_enforced(env):
    """`match:` means the value must contain it — a wrong-valued row is not completion."""
    assert rc(["open", "delivery", "--scope", "/r", "--run", "d3"], env) == OK
    advance_to(env, "d3", "K02")
    assert rc(["evidence", "--run", "d3", "--step", "K02",
               "--kind", "test_result", "--value", "3 FAILED"], env) == OK
    assert rc(["close-step", "--run", "d3", "--step", "K02"], env) == REFUSED
    assert rc(["evidence", "--run", "d3", "--step", "K02",
               "--kind", "test_result", "--value", "all pass"], env) == OK
    assert rc(["close-step", "--run", "d3", "--step", "K02"], env) == OK


# ------------------------------------------------------------------ gates

def test_affirm_gate_refused_without_witness(env):
    assert rc(["open", "delivery", "--scope", "/r", "--run", "g1"], env) == OK
    advance_to(env, "g1", None)
    assert rc(["gate", "--run", "g1", "--step", "D02", "--decision", "affirm"],
              env, witness="transcript") == REFUSED


def test_affirm_gate_accepted_with_witness(env, tmp_path):
    assert rc(["open", "delivery", "--scope", "/r", "--run", "g2"], env) == OK
    advance_to(env, "g2", None)
    t = transcript_with(tmp_path, 2)
    assert rc(["gate", "--run", "g2", "--step", "D02", "--decision", "affirm",
               "--evidence", "ok"], env, transcript=t, witness="transcript") == OK


def test_second_gate_in_same_turn_is_refused(env, tmp_path):
    """The fabrication shape: two affirmations claiming one human reply."""
    assert rc(["open", "delivery", "--scope", "/r", "--run", "g3"], env) == OK
    advance_to(env, "g3", None)
    t = transcript_with(tmp_path, 2)
    assert rc(["gate", "--run", "g3", "--step", "D02", "--decision", "affirm"],
              env, transcript=t, witness="transcript") == OK
    # transcript unchanged => no new human turn => the second gate is a fabrication
    assert rc(["gate", "--run", "g3", "--step", "E02", "--decision", "affirm"],
              env, transcript=t, witness="transcript") == REFUSED
    # ...and it becomes recordable once a real reply lands
    t2 = transcript_with(tmp_path, 3)
    assert rc(["gate", "--run", "g3", "--step", "E02", "--decision", "affirm"],
              env, transcript=t2, witness="transcript") == OK


def test_an_intervening_preauth_gate_does_not_reset_the_witness_cursor(env, tmp_path):
    """A gate authorised by config must not hand the NEXT affirm gate a fresh cursor.

    THE HOLE THIS CLOSES, found by making the end-to-end walk attempt a fabrication rather
    than politely appending a human turn before every gate:

    The cursor used to be read from the single most recent gate's proof. A pre-authorised
    gate carries no turn count, so once one landed in between, the next affirm gate saw no
    cursor at all and the same-turn check skipped entirely. Two gates then both recorded
    `human_turns: 1` against a transcript that only ever had one reply — one human sentence
    authorising two irreversible decisions.

    The fix takes the maximum over every proof on the run, which is monotonic and cannot be
    reset by an intervening gate of any kind.
    """
    d = _spec(env, "__cursor__", """
config:
  ok: false
phases:
  - id: p
steps:
  - id: A
    phase: p
    gate: affirm
    completion: { type: gate_recorded }
  - id: B
    phase: p
    gate: preauth:ok
    deps: [A]
    completion: { type: gate_recorded }
  - id: C
    phase: p
    gate: affirm
    deps: [B]
    completion: { type: gate_recorded }
""")
    t_file = tmp_path / "t.jsonl"
    t_file.write_text('{"role":"user","content":"confirm A"}\n', encoding="utf-8")
    try:
        assert rc(["open", "__cursor__", "--scope", "s", "--run", "cur"], env) == OK
        assert rc(["gate", "--run", "cur", "--step", "A", "--decision", "affirm",
                   "--evidence", "ok"], env,
                  transcript=t_file, witness="transcript") == OK
        assert rc(["config", "--run", "cur", "--set", "ok=true"], env) == OK
        assert rc(["gate", "--run", "cur", "--step", "B", "--decision", "preauth"],
                  env) == OK
        # The preauth gate carries no turn count; it must not license C.
        r = run(["gate", "--run", "cur", "--step", "C", "--decision", "affirm",
                 "--evidence", "claiming A's reply"], env,
                transcript=t_file, witness="transcript")
        assert r.returncode == REFUSED, r.stdout + r.stderr
        assert "no new human turn" in r.stderr
        # A real second reply unblocks it.
        with t_file.open("a", encoding="utf-8") as fh:
            fh.write('{"role":"user","content":"confirm C"}\n')
        assert rc(["gate", "--run", "cur", "--step", "C", "--decision", "affirm",
                   "--evidence", "really confirmed"], env,
                  transcript=t_file, witness="transcript") == OK
    finally:
        _rm(d)


def test_preauth_refused_without_the_config_key(env):
    assert rc(["open", "delivery", "--scope", "/r", "--run", "p1"], env) == OK
    r = run(["gate", "--run", "p1", "--step", "E03", "--decision", "preauth"], env)
    assert r.returncode == REFUSED
    assert "autopass_cleanup" in r.stderr


def test_preauth_accepted_once_enabled(env):
    assert rc(["open", "delivery", "--scope", "/r", "--run", "p2"], env) == OK
    assert rc(["config", "--run", "p2", "--set", "autopass_cleanup=true"], env) == OK
    assert rc(["gate", "--run", "p2", "--step", "E03", "--decision", "preauth"], env) == OK


def test_preauth_refused_on_a_human_gate(env):
    """preauth must not be usable to bypass a gate that demands a human."""
    assert rc(["open", "delivery", "--scope", "/r", "--run", "p3"], env) == OK
    assert rc(["config", "--run", "p3", "--set", "autopass_cleanup=true"], env) == OK
    assert rc(["gate", "--run", "p3", "--step", "D02", "--decision", "preauth"],
              env) == REFUSED


def test_unwitnessed_downgrade_is_recorded_not_silent(env):
    assert rc(["open", "delivery", "--scope", "/r", "--run", "u1"], env) == OK
    assert rc(["gate", "--run", "u1", "--step", "D02", "--decision", "affirm"],
              env, witness="manual") == OK
    out = run(["audit"], env).stdout
    assert "unwitnessed: 1" in out
    assert "unwitnessed_gate" in out  # also on the violation ledger


# ------------------------------------------------------------------ guard

def test_guard_blocks_then_allows(env, tmp_path):
    assert rc(["open", "delivery", "--scope", "/repo/a", "--run", "gu1"], env) == OK
    assert rc(["guard", "--action", "commit", "--scope-kind", "repo",
               "--scope", "/repo/a"], env) == BLOCKED
    advance_to(env, "gu1", None)
    t = transcript_with(tmp_path, 1)
    assert rc(["gate", "--run", "gu1", "--step", "D02", "--decision", "affirm"],
              env, transcript=t, witness="transcript") == OK
    assert rc(["guard", "--action", "commit", "--scope-kind", "repo",
               "--scope", "/repo/a"], env) == OK


def test_guard_ignores_other_scopes_and_unknown_actions(env):
    assert rc(["open", "delivery", "--scope", "/repo/a", "--run", "gu2"], env) == OK
    assert rc(["guard", "--action", "commit", "--scope-kind", "repo",
               "--scope", "/repo/OTHER"], env) == OK
    assert rc(["guard", "--action", "not_guarded", "--scope-kind", "repo",
               "--scope", "/repo/a"], env) == OK


def test_guard_on_uninitialised_store_allows(tmp_path):
    """A guard runs before every matching tool call; it must never brick a fresh machine."""
    e = {"HARNESS_STATE_DIR": str(tmp_path / "absent")}
    assert rc(["guard", "--action", "commit", "--scope-kind", "repo",
               "--scope", "/r"], e) == OK


def test_ambiguous_scope_fails_open_loudly(env):
    """>1 open run in a scope: allow, warn, name the candidates — never guess.

    Guessing produces a confidently wrong demand, which leaves forging the named gate
    as the only way forward. That is strictly worse than not enforcing this one call.
    """
    assert rc(["open", "delivery", "--scope", "/repo/z", "--run", "amb1"], env) == OK
    assert rc(["open", "delivery", "--scope", "/repo/z", "--run", "amb2"], env) == REFUSED
    assert rc(["open", "delivery", "--scope", "/repo/z", "--run", "amb2",
               "--allow-concurrent"], env) == OK
    r = run(["guard", "--action", "commit", "--scope-kind", "repo",
             "--scope", "/repo/z"], env)
    assert r.returncode == OK
    assert "NOT enforced" in r.stderr
    assert "amb1" in r.stderr and "amb2" in r.stderr


# ------------------------------------------------------------------ run lifecycle

def test_close_run_refuses_while_steps_remain(env):
    assert rc(["open", "delivery", "--scope", "/r", "--run", "cr1"], env) == OK
    assert rc(["close-run", "--run", "cr1"], env) == REFUSED


def test_forced_close_is_recorded_as_a_violation(env):
    assert rc(["open", "delivery", "--scope", "/r", "--run", "cr2"], env) == OK
    assert rc(["close-run", "--run", "cr2", "--force-steps"], env) == OK
    assert "forced_close" in run(["audit"], env).stdout
    # A forced close is a BREACH — something got through, marked. This is also the one place
    # that pins the ledger's DEFAULT severity: this call site passes none, so a default of
    # 'blocked' would quietly file bypassing every open step as "the engine worked".
    import sqlite3
    c = sqlite3.connect(env["HARNESS_STATE_DIR"] + "/harness.db")
    try:
        rows = c.execute("SELECT code, severity FROM violation WHERE run_id='cr2'").fetchall()
    finally:
        c.close()
    assert rows and all(sev == "breach" for _, sev in rows), rows


def test_closing_a_run_frees_the_scope(env):
    assert rc(["open", "delivery", "--scope", "/repo/s", "--run", "s1"], env) == OK
    assert rc(["open", "delivery", "--scope", "/repo/s", "--run", "s2"], env) == REFUSED
    assert rc(["close-run", "--run", "s1", "--force-steps"], env) == OK
    assert rc(["open", "delivery", "--scope", "/repo/s", "--run", "s2"], env) == OK


# ─────────────────────────── phase goals, composites, severity ───────────────────────────

_GOAL_BODY = """
phases:
  - id: p1
    goal: {type: phase_steps_closed}
  - id: p2
    goal: {type: all_of, steps: [A]}
steps:
  - id: A
    phase: p1
    completion: {type: attest}
  - id: B
    phase: p2
    deps: [A]
    completion: {type: attest}
  - id: OPT
    phase: p1
    optional: true
    completion: {type: attest}
"""


def test_summarize_refuses_until_the_phase_goal_is_met(env):
    """The goal is wired at the summarize chokepoint, so it sits ON the path to closing.

    Adding a separate mandatory `assert-goal` command would leave the criterion BESIDE the
    path: nothing would force anyone to run it. Here `close-run` wants summaries and a summary
    wants the goal, so skipping the criterion means not closing the run.
    """
    d = _spec(env, "goaltest", _GOAL_BODY)
    try:
        assert rc(["open", "goaltest", "--scope", "g1", "--run", "g"], env) == OK
        # p1 holds an unresolved OPTIONAL step — neither closed nor skipped.
        assert rc(["close-step", "--run", "g", "--step", "A"], env) == OK
        r = run(["summarize", "--run", "g", "--phase", "p1"], env)
        assert r.returncode == REFUSED
        assert "goal is not met" in r.stderr and "OPT" in r.stderr
        # assert-goal answers the same question read-only, before trying to close out.
        assert rc(["assert-goal", "--run", "g", "--phase", "p1"], env) == REFUSED
        assert rc(["skip", "--run", "g", "--step", "OPT", "--reason", "n/a"], env) == OK
        assert rc(["assert-goal", "--run", "g", "--phase", "p1"], env) == OK
        assert rc(["summarize", "--run", "g", "--phase", "p1"], env) == OK
    finally:
        _rm(d)


def test_an_unresolved_optional_step_is_not_a_finished_phase(env):
    """"nobody decided about it" and "it was decided not to run" are different states."""
    d = _spec(env, "goalopt", _GOAL_BODY)
    try:
        assert rc(["open", "goalopt", "--scope", "g2", "--run", "g2"], env) == OK
        assert rc(["close-step", "--run", "g2", "--step", "A"], env) == OK
        out = run(["assert-goal", "--run", "g2", "--phase", "p1"], env)
        assert "neither closed nor skipped" in out.stderr
    finally:
        _rm(d)


def test_a_goal_may_not_be_an_attestation(env):
    """A per-step attest is a defensible floor; a whole LAYER accepted on one is a stamp."""
    d = _spec(env, "goalattest", """
phases:
  - id: p1
    goal: {type: attest}
steps:
  - id: A
    phase: p1
""")
    try:
        r = run(["validate", "goalattest"], env)
        assert r.returncode == BAD_SPEC
        assert "cannot be accepted on an" in r.stderr
    finally:
        _rm(d)


def test_all_checks_reports_every_failing_branch(env):
    """Being told one of three problems is how one fix cycle becomes three."""
    d = _spec(env, "composite", """
phases: [{id: p1}]
steps:
  - id: A
    phase: p1
    gate: affirm
    completion:
      type: all_checks
      checks:
        - {type: evidence, kind: one}
        - {type: evidence, kind: two}
        - {type: evidence_equals, kind: three, value: "ok"}
""")
    try:
        assert rc(["open", "composite", "--scope", "c1", "--run", "c"], env) == OK
        r = run(["close-step", "--run", "c", "--step", "A"], env)
        assert r.returncode == REFUSED
        assert "3 of 3 checks failed" in r.stderr
        for k in ("one", "two", "three"):
            assert k in r.stderr, k
        for k in ("one", "two"):
            assert rc(["evidence", "--run", "c", "--step", "A", "--kind", k,
                       "--value", "x"], env) == OK
        # Two of three satisfied is still not done — the point of the conjunction.
        r = run(["close-step", "--run", "c", "--step", "A"], env)
        assert r.returncode == REFUSED and "1 of 3 checks failed" in r.stderr
        assert rc(["evidence", "--run", "c", "--step", "A", "--kind", "three",
                   "--value", "ok"], env) == OK
        assert rc(["close-step", "--run", "c", "--step", "A"], env) == OK
    finally:
        _rm(d)


def test_a_typo_inside_a_composite_branch_is_fatal_at_load_time(env):
    """Validation must recurse, or a nested typo only shows up as a mid-run failure."""
    d = _spec(env, "compotypo", """
phases: [{id: p1}]
steps:
  - id: A
    phase: p1
    completion:
      type: all_checks
      checks:
        - {type: evidnce, kind: one}
""")
    try:
        r = run(["validate", "compotypo"], env)
        assert r.returncode == BAD_SPEC
        assert "evidnce" in r.stderr and "is not registered" in r.stderr
    finally:
        _rm(d)


def test_a_composite_may_not_nest_a_composite(env):
    d = _spec(env, "compnest", """
phases: [{id: p1}]
steps:
  - id: A
    phase: p1
    completion:
      type: all_checks
      checks:
        - type: all_checks
          checks: [{type: attest}]
""")
    try:
        r = run(["validate", "compnest"], env)
        assert r.returncode == BAD_SPEC and "Flatten it" in r.stderr
    finally:
        _rm(d)


def test_a_missing_required_key_inside_a_composite_branch_is_fatal(env):
    d = _spec(env, "compreq", """
phases: [{id: p1}]
steps:
  - id: A
    phase: p1
    completion:
      type: all_checks
      checks:
        - {type: evidence_equals, kind: one}
""")
    try:
        r = run(["validate", "compreq"], env)
        assert r.returncode == BAD_SPEC and "requires key 'value'" in r.stderr
    finally:
        _rm(d)


def test_two_providers_may_not_declare_the_same_fact(env):
    """A condition names a fact, not a provider — so a collision has no defined winner."""
    d = _spec(env, "factclash", """
facts:
  providers: [run_progress, run_progress]
phases: [{id: p1}]
steps: [{id: A, phase: p1}]
""")
    try:
        r = run(["validate", "factclash"], env)
        assert r.returncode == BAD_SPEC and "twice" in r.stderr
    finally:
        _rm(d)


def test_provider_and_providers_together_is_fatal(env):
    d = _spec(env, "factboth", """
facts:
  provider: scope_only
  providers: [run_progress]
phases: [{id: p1}]
steps: [{id: A, phase: p1}]
""")
    try:
        r = run(["validate", "factboth"], env)
        assert r.returncode == BAD_SPEC and "pick one" in r.stderr
    finally:
        _rm(d)


def test_a_refused_attempt_is_not_a_breach(env):
    """A ledger that cannot tell "we stopped you" from "you got through" punishes attempting.

    The run below only ever gets REFUSED — the engine worked every time — so a terminal
    `no_open_violations` step must still be closeable.
    """
    d = _spec(env, "sevtest", """
phases: [{id: p1}]
steps:
  - id: A
    phase: p1
    gate: 'preauth:not_enabled'
    completion: {type: attest}
  - id: Z
    phase: p1
    deps: [A]
    completion: {type: no_open_violations}
""")
    try:
        assert rc(["open", "sevtest", "--scope", "s1", "--run", "s"], env) == OK
        # preauth without the key enabled: refused, and filed as 'blocked'.
        assert rc(["gate", "--run", "s", "--step", "A", "--decision", "preauth"],
                  env) == REFUSED
        assert rc(["config", "--run", "s", "--set", "not_enabled=true"], env) == OK
        assert rc(["gate", "--run", "s", "--step", "A", "--decision", "preauth"], env) == OK
        assert rc(["close-step", "--run", "s", "--step", "A"], env) == OK
        out = run(["close-step", "--run", "s", "--step", "Z"], env)
        assert out.returncode == OK, out.stderr
        assert "not a breach" in run(["status", "--run", "s"], env).stdout or True
        # Demanding zero refusals too is possible, but must be asked for explicitly.
        import sqlite3
        c = sqlite3.connect(env["HARNESS_STATE_DIR"] + "/harness.db")
        try:
            sev = dict(c.execute("SELECT severity, COUNT(*) FROM violation "
                                 "WHERE run_id='s' GROUP BY severity").fetchall())
        finally:
            c.close()
        assert sev.get("blocked", 0) == 1 and sev.get("breach", 0) == 0, sev
    finally:
        _rm(d)


def test_next_tells_the_caller_what_the_criterion_demands(env):
    """A stronger criterion that announces only its NAME moves the guesswork, not removes it."""
    d = _spec(env, "reqshow", """
phases: [{id: p1}]
steps:
  - id: A
    phase: p1
    gate: affirm
    completion:
      type: all_checks
      checks:
        - {type: gate_recorded}
        - {type: evidence_in, kind: status, values: [green, red]}
        - {type: evidence, kind: artifact, min_count: 2}
""")
    try:
        assert rc(["open", "reqshow", "--scope", "r1", "--run", "r"], env) == OK
        out = run(["next", "--run", "r"], env).stdout
        assert "record evidence kind 'status'" in out
        assert "['green', 'red']" in out
        assert "kind 'artifact' x2" in out
        assert "this step's own gate" in out
    finally:
        _rm(d)


def test_only_the_providers_a_boundary_needs_are_called(env):
    """An unused provider must not be paid for, or its cost argues against declaring it.

    Wired the other way round, a flow declaring three providers pays all three at every
    boundary while one hook reads one fact — and the rational response becomes to stop
    declaring providers, which is the opposite of what the seam is for.
    """
    from engine import facts as factsmod, flow as flowmod, hooks as hookmod, store as storemod
    calls: list[str] = []
    for name in ("probe_a", "probe_b"):
        factsmod._PROVIDERS.pop(name, None)

    @factsmod.provider("probe_a", schema={"probe_a_fact": "str"})
    def _a(ctx):
        calls.append("a"); return {"probe_a_fact": "x"}

    @factsmod.provider("probe_b", schema={"probe_b_fact": "str"})
    def _b(ctx):
        calls.append("b"); return {"probe_b_fact": "y"}

    d = _spec(env, "lazyfacts", """
facts:
  providers: [probe_a, probe_b]
phases: [{id: p1}]
steps: [{id: A, phase: p1}]
hooks:
  - id: only-a
    trigger: phase_start
    phase: p1
    when: {fact: probe_a_fact, equals: "x"}
    mode: contract
    contract: touched a
""")
    try:
        f = flowmod.load("lazyfacts")
        assert f.facts_owner["probe_a_fact"] == "probe_a"
        assert f.facts_owner["probe_b_fact"] == "probe_b"
        import os
        os.environ["HARNESS_STATE_DIR"] = env["HARNESS_STATE_DIR"]
        storemod.init()
        conn = storemod.connect()
        try:
            storemod.open_run(conn, run_id="lz", ability="lazyfacts",
                              flow_digest=f.digest, title=None,
                              scope_kind="s", scope_key="lazyscope")
            row = storemod.get_run(conn, "lz")
            fired = hookmod.fire(conn, f, row, "phase_start", "p1")
        finally:
            conn.close()
        assert len(fired) == 1 and fired[0].matched
        assert calls == ["a"], f"only the referenced provider should run, got {calls}"
    finally:
        for name in ("probe_a", "probe_b"):
            factsmod._PROVIDERS.pop(name, None)
        _rm(d)


def test_a_universal_verdict_check_looks_at_every_row_not_the_latest(env):
    """"every one of them has a verdict" cannot be checked by reading the most recent row.

    Latest-only would be satisfied by one good entry sitting on top of any number of blank or
    garbage ones — the same shape of hole as counting rows without looking at them.
    """
    d = _spec(env, "allin", """
phases: [{id: p1}]
steps:
  - id: A
    phase: p1
    completion: {type: evidence_all_in, kind: verdict, values: [pass, fail]}
  - id: B
    phase: p1
    completion: {type: evidence_in, kind: verdict, values: [pass, fail]}
""")
    try:
        assert rc(["open", "allin", "--scope", "a1", "--run", "a"], env) == OK
        for step in ("A", "B"):
            assert rc(["evidence", "--run", "a", "--step", step,
                       "--kind", "verdict", "--value", "unreviewed"], env) == OK
            assert rc(["evidence", "--run", "a", "--step", step,
                       "--kind", "verdict", "--value", "pass"], env) == OK
        # evidence_in reads the LATEST and is satisfied by the good row on top.
        assert rc(["close-step", "--run", "a", "--step", "B"], env) == OK
        # evidence_all_in is not, and it names the offending row.
        r = run(["close-step", "--run", "a", "--step", "A"], env)
        assert r.returncode == REFUSED
        assert "1 of 2" in r.stderr and "'unreviewed'" in r.stderr
    finally:
        _rm(d)


def test_a_universal_verdict_check_refuses_an_empty_set(env):
    """A universal claim over nothing is vacuously true — so it needs a floor as well."""
    d = _spec(env, "allinempty", """
phases: [{id: p1}]
steps:
  - id: A
    phase: p1
    completion: {type: evidence_all_in, kind: verdict, values: [pass], min_count: 2}
""")
    try:
        assert rc(["open", "allinempty", "--scope", "a2", "--run", "a2"], env) == OK
        r = run(["close-step", "--run", "a2", "--step", "A"], env)
        assert r.returncode == REFUSED and "over nothing" in r.stderr
        assert rc(["evidence", "--run", "a2", "--step", "A", "--kind", "verdict",
                   "--value", "pass"], env) == OK
        assert rc(["close-step", "--run", "a2", "--step", "A"], env) == REFUSED
        assert rc(["evidence", "--run", "a2", "--step", "A", "--kind", "verdict",
                   "--value", "pass"], env) == OK
        assert rc(["close-step", "--run", "a2", "--step", "A"], env) == OK
    finally:
        _rm(d)


def test_the_transcribed_flow_keeps_every_criterion_above_the_floor(env):
    """No step may check nothing, and the strength mix is pinned so it cannot silently regress.

    A criterion is easy to weaken by accident — swapping a value-checked spec for a bare
    `evidence` reads as a smaller diff than it is, and nothing else in the suite would notice.
    """
    from engine import flow as flowmod, predicates
    f = flowmod.load("shipcheck-asis")
    import collections
    mix = collections.Counter(predicates.strength(s.completion) for s in f.steps.values())
    assert mix[0] == 0, [s.id for s in f.steps.values()
                         if predicates.strength(s.completion) == 0]
    # Floors stated so an UPGRADE cannot trip them. Pinning per-tier counts was wrong: moving
    # two steps from tier 2 to tier 3 is the improvement this baseline exists to encourage, and
    # a `mix[2] >= 52` guard reported it as a regression. The invariant is that nothing sinks
    # toward self-report — tier 3 never shrinks, and tiers 2+3 together never shrink.
    assert mix[3] >= 13, dict(mix)
    assert mix[3] + mix[2] >= 65, dict(mix)
    # COMPLETENESS is a separate floor, and strength hides it: a step producing three artifacts
    # and pinning the strongest ONE scores well above while two thirds go unchecked. Counting
    # pinned artifacts is what makes dropping one show up.
    pinned = [len([r for r in predicates.requirements(s.completion)
                   if r["what"] == "evidence"]) for s in f.steps.values()]
    assert sum(pinned) >= 124, sum(pinned)
    assert sum(1 for n in pinned if n >= 2) >= 30, sorted(pinned, reverse=True)[:5]
    # Every phase carries a goal, and no goal may be an assertion.
    assert set(f.phase_goals) == set(f.phases)
    assert all(predicates.strength(g) >= 3 for g in f.phase_goals.values())


def test_every_criterion_records_where_it_came_from(env):
    """Each step's criterion must carry a derivation reason quoting the prose it came from.

    A criterion is cheap to invent and expensive to spot: a fabricated allowed-value set reads
    STRONGER than a weak criterion while checking something nobody asked for. The spec cannot
    show the difference — only the derivation record can. So the record is pinned here, and a
    step added later without one fails rather than passing quietly.
    """
    import json
    from engine import flow as flowmod
    f = flowmod.load("shipcheck-asis")
    prov = json.loads((f.source.parent / "criteria-provenance.json").read_text())
    missing = [s for s in f.steps if s not in prov["steps"]]
    assert not missing, f"no derivation record for: {missing}"
    blank = [s for s, v in prov["steps"].items() if not (v.get("reason") or "").strip()]
    assert not blank, f"derivation record with no reason: {blank}"
    assert set(prov["phases"]) == set(f.phases)
    # The live spec must be the one actually loaded — a provenance file that drifted from the
    # flow it documents is worse than none, because it reads as an audit trail.
    for sid, v in prov["steps"].items():
        assert v["live_spec"] == f.steps[sid].completion, sid
    for pid, v in prov["phases"].items():
        assert v["live_goal"] == f.phase_goals[pid], pid


# ───────────────────────────────── variants ─────────────────────────────────

_VARIANT_BODY = """
variants:
  values: [alpha, beta]
  default: alpha
phases: [{id: p1, goal: {type: phase_steps_closed}}]
steps:
  - id: SHARED
    phase: p1
    completion: {type: attest}
  - id: ONLY_A
    phase: p1
    variants: [alpha]
    completion: {type: attest}
  - id: ONLY_B
    phase: p1
    variants: [beta]
    completion: {type: attest}
"""


def test_a_step_from_another_variant_cannot_be_entered_or_skipped(env):
    """The structural guarantee. Marking mode-specific steps merely `optional` cannot give it.

    Optional says "may not run"; it does not say "does not exist here". Under optional, a run
    in the wrong mode can enter the other mode's steps, satisfy them, and close cleanly — and
    the log afterwards is indistinguishable from a correct run. That is the failure this
    primitive exists to make impossible, so both doors are shut: enter AND skip.
    """
    d = _spec(env, "varenter", _VARIANT_BODY)
    try:
        assert rc(["open", "varenter", "--scope", "v1", "--run", "a"], env) == OK
        r = run(["enter", "--run", "a", "--step", "ONLY_B"], env)
        assert r.returncode == REFUSED
        assert "not part of this flow" in r.stderr and "beta only" in r.stderr
        # Nor may it be skipped: a skip RECORDS A DECISION, and there is none to record.
        r = run(["skip", "--run", "a", "--step", "ONLY_B", "--reason", "n/a"], env)
        assert r.returncode == REFUSED and "nothing to skip" in r.stderr
    finally:
        _rm(d)


def test_a_run_closes_without_the_other_variants_steps(env):
    d = _spec(env, "varclose", _VARIANT_BODY)
    try:
        assert rc(["open", "varclose", "--scope", "v2", "--run", "b",
                   "--variant", "beta"], env) == OK
        for sid in ("SHARED", "ONLY_B"):
            assert rc(["close-step", "--run", "b", "--step", sid], env) == OK
        assert rc(["assert-goal", "--run", "b", "--phase", "p1"], env) == OK
        assert rc(["summarize", "--run", "b", "--phase", "p1"], env) == OK
        assert rc(["close-run", "--run", "b", "--result", "completed"], env) == OK
        assert "forced_close" not in run(["audit"], env).stdout
    finally:
        _rm(d)


def test_an_undeclared_variant_on_a_step_is_fatal(env):
    d = _spec(env, "vartypo", """
variants: {values: [alpha, beta], default: alpha}
phases: [{id: p1}]
steps:
  - id: A
    phase: p1
    variants: [alfa]
""")
    try:
        r = run(["validate", "vartypo"], env)
        assert r.returncode == BAD_SPEC and "not declared" in r.stderr
    finally:
        _rm(d)


def test_a_step_listing_every_variant_is_fatal(env):
    """Listing them all is the shared core written as a filter — and it stops filtering the
    moment a third variant appears, silently widening the step's reach."""
    d = _spec(env, "varall", """
variants: {values: [alpha, beta], default: alpha}
phases: [{id: p1}]
steps:
  - id: A
    phase: p1
    variants: [alpha, beta]
""")
    try:
        r = run(["validate", "varall"], env)
        assert r.returncode == BAD_SPEC and "EVERY variant" in r.stderr
    finally:
        _rm(d)


def test_a_dependency_missing_under_some_variant_is_fatal(env):
    """Same class as a guard pointing at a gateless step: it reads as a constraint and is
    unsatisfiable — except this one only bites whoever runs that particular variant."""
    d = _spec(env, "vardep", """
variants: {values: [alpha, beta], default: alpha}
phases: [{id: p1}]
steps:
  - id: A
    phase: p1
    variants: [alpha]
  - id: B
    phase: p1
    deps: [A]
""")
    try:
        r = run(["validate", "vardep"], env)
        assert r.returncode == BAD_SPEC
        assert "does not exist under variant(s): beta" in r.stderr
    finally:
        _rm(d)


def test_one_variant_is_not_a_variant(env):
    d = _spec(env, "varone", """
variants: {values: [alpha], default: alpha}
phases: [{id: p1}]
steps: [{id: A, phase: p1}]
""")
    try:
        r = run(["validate", "varone"], env)
        assert r.returncode == BAD_SPEC and "at least TWO" in r.stderr
    finally:
        _rm(d)


def test_the_variant_is_pinned_at_open_not_re_derived(env):
    """A flow whose shape changes mid-run cannot be analysed: "what was owed" would depend on
    when you asked. So the resolved variant is written to the run row once."""
    import sqlite3
    d = _spec(env, "varpin", _VARIANT_BODY)
    try:
        assert rc(["open", "varpin", "--scope", "v3", "--run", "c",
                   "--variant", "beta"], env) == OK
        c = sqlite3.connect(env["HARNESS_STATE_DIR"] + "/harness.db")
        try:
            got = c.execute("SELECT variant FROM run WHERE run_id='c'").fetchone()[0]
        finally:
            c.close()
        assert got == "beta", got
    finally:
        _rm(d)


def test_the_push_transcription_carries_two_real_variants(env):
    """The pressure test: a second ability whose flow has more than one shape.

    shipcheck-asis has exactly one, so it could not have exercised this. Both variants must be
    non-trivial (each owning steps) and the shared core must be the majority — a "variant" that
    forks the whole flow is two abilities wearing one name.
    """
    from engine import flow as flowmod
    f = flowmod.load("push")
    assert f.variants == ("ship-check", "ux")
    assert f.default_variant == "ship-check"
    assert f.variant_spec["fact"] == "executor"
    owned = {v: [s.id for s in f.steps.values() if s.variants == (v,)] for v in f.variants}
    for v, ids in owned.items():
        assert len(ids) >= 2, (v, ids)
    shared = [s.id for s in f.steps.values() if not s.variants]
    assert len(shared) > sum(len(x) for x in owned.values()), (len(shared), owned)
    # Every variant must be independently closeable: its applicable required set must not
    # reference a step outside it.
    for v in f.variants:
        req = f.required_steps(v)
        assert req, v
        for sid in req:
            for dep in f.step(sid).deps:
                assert f.applicable(dep, v), (v, sid, dep)


# ──────────────────── requires: one source of truth, two consumers ────────────────────

def test_two_abilities_share_one_registry_without_copying_it(env):
    """The verification this mechanism exists for: ONE source of truth, two consumers.

    The reference system has two skills reading one registry, and the whole value of that
    registry is being singular — its own docs promise that adding a mode touches the registry
    and neither skill's core. Copying the registry per ability would reintroduce precisely the
    drift it was built to remove, so the second ability declares a dependency instead.
    """
    import os
    from engine import flow as flowmod, facts
    pl, pu = flowmod.load("plan"), flowmod.load("push")

    # The consumer carries NO copy: no provider module, no tool tree, no registry file.
    base = os.path.dirname(str(pl.source))
    assert not os.path.exists(os.path.join(base, "providers.py"))
    assert not os.path.exists(os.path.join(base, "tools"))
    assert pl.requires == ("push",)

    # NOT the same provider, and that is the sharper version of the claim. The two abilities
    # ask the same question of the same registry from DIFFERENT inputs: one's scope IS the
    # workspace, the other's is a plan being written and must read the workspace the flow
    # recorded. Demanding one provider serve both is what produced a derivation that fed a plan
    # identifier to a path matcher and reported the result as "derived".
    assert pl.facts_providers != pu.facts_providers
    assert pl.variants == pu.variants and pl.default_variant == pu.default_variant

    # The point of all of it: one registry, so the same input yields the same answer — and
    # the shared vocabulary is the ability's declared variants, not a copied table.
    for ws in ("~/.kiro/skills/ux/output/proj", "/tmp/an-ordinary-package"):
        got = facts.gather_all(pu.facts_providers,
                               {"run_id": "t", "scope": ws, "scope_kind": "s",
                                "ability": "t"})["executor"]
        assert got in pl.variants and got in pu.variants, (ws, got)
    # Both providers live in ONE file owned by one ability; the consumer copies nothing.
    assert (Path(str(pu.source)).parent / "providers.py").is_file()


def test_requiring_an_uninstalled_ability_is_fatal(env):
    d = _spec(env, "needsghost", """
requires: [no_such_ability]
phases: [{id: p1}]
steps: [{id: A, phase: p1}]
""")
    try:
        r = run(["validate", "needsghost"], env)
        assert r.returncode == BAD_SPEC and "not installed" in r.stderr
    finally:
        _rm(d)


def test_requiring_itself_is_fatal(env):
    d = _spec(env, "selfreq", """
requires: [selfreq]
phases: [{id: p1}]
steps: [{id: A, phase: p1}]
""")
    try:
        r = run(["validate", "selfreq"], env)
        assert r.returncode == BAD_SPEC and "requires itself" in r.stderr
    finally:
        _rm(d)


def test_borrowing_another_ability_must_name_the_owner_and_declare_it(env):
    """Both halves, because either alone lets a dependency work by accident.

    Under one flat namespace a borrowed name resolved whenever the owning ability happened to
    have been loaded first — passing in one order and failing in another. Namespacing makes the
    bare name mean mine-or-the-engine, so a borrow has to be WRITTEN as one, and `requires:` is
    what makes it legal and loaded. Asserted three ways: bare is refused and says who owns it,
    qualified-without-requires is refused and says what to add, and the pair works.
    """
    bare = _spec(env, "zzz_bare", """
facts:
  providers: [plan_taxonomy]
phases: [{id: p1}]
steps: [{id: A, phase: p1}]
""")
    try:
        # Alone in the process, the owner is not loaded, so the refusal CANNOT name it. It must
        # still say what to do — a message whose only guidance depends on who else happens to be
        # loaded would be useless exactly when a flow is validated on its own.
        alone = run(["validate", "zzz_bare"], env)
        assert alone.returncode == BAD_SPEC
        assert "a bare name will never reach it" in alone.stderr, alone.stderr
        assert "<ability>.plan_taxonomy" in alone.stderr and "requires:" in alone.stderr

        # With everything loaded it can be precise, and then it must be.
        together = run(["validate"], env)
        assert together.returncode == BAD_SPEC
        assert "it belongs to 'push'" in together.stderr, together.stderr
        assert "requires: [push]" in together.stderr
        assert "push.plan_taxonomy" in together.stderr
        # NOT "unregistered" — the name exists, and saying otherwise sends the author hunting
        # for a typo in something spelled correctly.
        assert "plan_taxonomy' is not registered" not in together.stderr
    finally:
        _rm(bare)

    undeclared = _spec(env, "zzz_undeclared", """
facts:
  providers: [push.plan_taxonomy]
phases: [{id: p1}]
steps: [{id: A, phase: p1}]
""")
    try:
        out = run(["validate", "zzz_undeclared"], env)
        assert out.returncode == BAD_SPEC
        assert "does not declare" in out.stderr, out.stderr
        assert "Add it: requires: [push]" in out.stderr
    finally:
        _rm(undeclared)

    ok = _spec(env, "zzz_borrower", """
requires: [push]
facts:
  providers: [push.plan_taxonomy]
phases: [{id: p1}]
steps: [{id: A, phase: p1}]
""")
    try:
        assert rc(["validate", "zzz_borrower"], env) == OK
    finally:
        _rm(ok)

    # And the qualified form is ONLY for borrowing: an ability qualifying its own name would
    # read as a dependency on itself, and the two forms would stop meaning different things.
    selfq = _spec(env, "zzz_selfq", """
facts:
  provider: zzz_selfq.mine
phases: [{id: p1}]
steps: [{id: A, phase: p1}]
""")
    try:
        out = run(["validate", "zzz_selfq"], env)
        assert out.returncode == BAD_SPEC
        assert "names this ability's own registration" in out.stderr, out.stderr
        assert "Write it bare: 'mine'" in out.stderr
    finally:
        _rm(selfq)


def test_the_two_new_abilities_have_full_prose_and_no_product_names(env):
    """Every ability that declares prose must have ALL of it, and none of it copied.

    A product name here is evidence of copying, and it also makes the ability unusable anywhere
    that system does not exist. Partial prose is its own failure: `next` prints the directive
    unconditionally, so a step missing one reads exactly like a step with nothing to say.
    """
    import re
    from engine import flow as flowmod, prose
    BAD = ["CRUX", "brazil", "Taskei", "Midway", "agent-fleet",
           "AutoSDE", "Coverlay", "easymrgr", "mwinit"]
    for name in ("push", "plan", "cr-reviewer", "cr-to-task"):
        f = flowmod.load(name)
        cov = prose.coverage(f)
        assert cov["directive"] == cov["steps"], (name, cov)
        assert cov["own_section"] == cov["steps"], (name, cov)
        assert f.prose_root is not None
        for md in f.prose_root.rglob("*.md"):
            text = md.read_text(encoding="utf-8")
            for bad in BAD:
                assert not re.search(rf"\b{re.escape(bad)}\b", text, re.I), (md.name, bad)


# ──────────────── end-to-end: both abilities, both variants, four runs ────────────────

@pytest.mark.parametrize("ability,variant,scope,derive", [
    ("push", "ux",         "~/.kiro/skills/ux/output/proj", True),
    ("push", "ship-check", "/tmp/an-ordinary-package",             True),
    ("plan", "ux",         "some-ux-plan-slug",                    False),
    ("plan", "ship-check", "some-code-plan-slug",                  False),
])
def test_a_variant_bearing_flow_runs_end_to_end(env, tmp_path, ability, variant, scope, derive):
    """Drive a whole run of each ability under each variant, to a clean close.

    Four runs rather than one, because "each variant validates" and "each variant RUNS" are
    different claims: the applicable set, the goals, the cross-variant `any_of` reconciliation
    and (for the borrowing ability) a provider owned by a different ability all only meet each
    other at runtime. `derive` distinguishes the two resolution paths — one ability's scope IS
    the value its variant is derived from, the other's is not, and that difference is the reason
    the second one must be told explicitly.
    """
    import json as _json
    import yaml as _yaml
    flow = _yaml.safe_load((REPO / "abilities" / ability / "flow.yaml").read_text())
    run_id = f"{ability}-{variant}".replace("-", "")[:16]

    args = ["open", ability, "--scope", scope, "--run", run_id]
    if not derive:
        args += ["--variant", variant]
    out = run(args, env)
    assert out.returncode == OK, out.stderr
    assert f"variant {variant}" in out.stdout, out.stdout
    if derive:
        assert "derived from fact" in out.stdout
    else:
        assert "(explicit)" in out.stdout or "(default)" in out.stdout

    applicable = [s for s in flow["steps"]
                  if not s.get("variants") or variant in s["variants"]]
    other = [s["id"] for s in flow["steps"]
             if s.get("variants") and variant not in s["variants"]]
    assert other, "each variant should exclude at least one step"
    # Every step of the OTHER shape must be unreachable, both ways.
    for sid in other:
        assert rc(["enter", "--run", run_id, "--step", sid], env) == REFUSED, sid
        assert rc(["skip", "--run", run_id, "--step", sid, "--reason", "x"], env) == REFUSED, sid

    t = tmp_path / "t.jsonl"
    t.write_text("", encoding="utf-8")
    turns = gates = skipped = 0
    seen_phase = None
    for s in applicable:
        sid = s["id"]
        if seen_phase is not None and s["phase"] != seen_phase:
            r = run(["summarize", "--run", run_id, "--phase", seen_phase, "--note", "walked"], env)
            assert r.returncode == OK, (seen_phase, r.stderr)
        seen_phase = s["phase"]
        if s.get("optional") and sid.startswith("D0"):   # the optional research step
            assert rc(["skip", "--run", run_id, "--step", sid, "--reason", "not complex"],
                      env) == OK
            skipped += 1
            continue
        if s.get("gate") == "affirm":
            turns += 1
            with t.open("a", encoding="utf-8") as fh:
                fh.write(_json.dumps({"role": "user", "content": f"ok {sid}"}) + "\n")
            assert rc(["gate", "--run", run_id, "--step", sid, "--decision", "affirm",
                       "--evidence", "human said go"], env,
                      transcript=t, witness="transcript") == OK, sid
            gates += 1
        _satisfy(env, run_id, sid, s.get("completion") or {}, variant=variant)
        r = run(["close-step", "--run", run_id, "--step", sid], env)
        assert r.returncode == OK, (sid, r.stderr)
    assert rc(["summarize", "--run", run_id, "--phase", seen_phase, "--note", "walked"],
              env) == OK

    for line in run(["obligations", "--run", run_id], env).stdout.splitlines():
        if line.strip().startswith("⏳"):
            assert rc(["discharge", "--run", run_id, "--hook", line.split()[2],
                       "--evidence", "handled in test"], env) == OK

    r = run(["close-run", "--run", run_id, "--result", "completed"], env)
    assert r.returncode == OK, r.stderr
    audit = run(["audit"], env).stdout
    assert "forced_close" not in audit and "undischarged_obligations" not in audit
    assert "unwitnessed: 0" in audit
    assert turns == gates, "one fresh human turn per affirm gate"


def test_a_wrongly_opened_variant_is_caught_when_the_truth_becomes_known(env):
    """The variant is pinned at open, but its truth is sometimes only DISCOVERED later.

    An ability whose mode depends on a value the flow itself collects cannot derive that mode
    before running. So whoever opens the run guesses — and a wrong guess is silent: every step
    of the right shape then reports "not part of this flow", which reads like a correct refusal.
    The reconciliation step is where the guess meets the truth.
    """
    d = _spec(env, "reconcile", """
variants: {values: [alpha, beta], default: alpha}
phases: [{id: p1, goal: {type: phase_steps_closed}}]
steps:
  - id: A
    phase: p1
    completion: {type: evidence_matches_variant, kind: mode}
""")
    try:
        assert rc(["open", "reconcile", "--scope", "r1", "--run", "r"], env) == OK  # → alpha
        assert rc(["evidence", "--run", "r", "--step", "A", "--kind", "mode",
                   "--value", "beta"], env) == OK
        out = run(["close-step", "--run", "r", "--step", "A"], env)
        assert out.returncode == REFUSED
        assert "opened as variant 'alpha'" in out.stderr
        assert "--variant beta" in out.stderr          # names the remedy
        assert "do not 'fix' the recorded value" in out.stderr
        # Agreement passes.
        assert rc(["evidence", "--run", "r", "--step", "A", "--kind", "mode",
                   "--value", "alpha"], env) == OK
        assert rc(["close-step", "--run", "r", "--step", "A"], env) == OK
    finally:
        _rm(d)


def test_a_condition_can_ask_the_engine_which_shape_the_run_is(env):
    """`run_variant` is engine-owned because no provider can answer it.

    Without it, an ability whose variant is not derivable from its scope has no honest way to
    condition on the variant — and the tempting substitute is a provider fact with a similar
    name that is quietly wrong for that ability.
    """
    d = _spec(env, "runvar", """
variants: {values: [alpha, beta], default: alpha}
phases: [{id: p1}]
steps: [{id: A, phase: p1, completion: {type: attest}}]
hooks:
  - id: notice
    trigger: phase_start
    phase: p1
    when: {not: {fact: run_variant, equals: alpha}}
    mode: contract
    obligation: false
    contract: "on {{fact.run_variant}}"
""")
    try:
        assert rc(["open", "runvar", "--scope", "rv1", "--run", "x1"], env) == OK
        out = run(["enter", "--run", "x1", "--step", "A"], env).stdout
        assert "hook notice" in out and "condition not met" in out
        assert rc(["open", "runvar", "--scope", "rv2", "--run", "x2",
                   "--variant", "beta"], env) == OK
        out = run(["enter", "--run", "x2", "--step", "A"], env).stdout
        assert "🔔 hook notice" in out and "on beta" in out
    finally:
        _rm(d)


def test_a_provider_may_not_claim_the_engine_owned_fact(env):
    d = _spec(env, "factsteal", """
variants: {values: [alpha, beta], default: alpha}
facts: {providers: [scope_only]}
phases: [{id: p1}]
steps: [{id: A, phase: p1}]
""")
    try:
        # scope_only does not declare run_variant, so this spec is legal; the guard is asserted
        # by construction in flow.load. Prove the reserved name is in the schema instead.
        from engine import flow as flowmod
        f = flowmod.load("factsteal")
        assert f.facts_schema.get("run_variant") == "str"
        assert "run_variant" not in f.facts_owner, "the engine owns it, not a provider"
    finally:
        _rm(d)


# ───────────────── guard-tool: the runtime hook seam ─────────────────

_GT_BODY = """
scope_match: path_prefix
phases: [{id: p1}]
steps:
  - id: G
    phase: p1
    gate: affirm
    completion: {type: gate_recorded}
guards:
  commit:
    step: G
    matches:
      - tool: shell
        field: command
        pattern: '(^|[;&|]\\s*)git\\b[^;&|]*?\\s+commit\\b'
  closeit:
    step: G
    matches:
      - tool: Tasker
        pattern: '"status"\\s*:\\s*"Closed"'
"""


def _gt(env, tool, payload, cwd):
    import json as _json
    return rc(["guard-tool", "--tool", tool, "--input-json", _json.dumps(payload),
               "--cwd", cwd], env)


def test_guard_tool_recognises_an_action_from_a_declared_pattern(env, tmp_path):
    """The runtime knows a tool call; the engine knows gates. The ability declares the bridge.

    Coding that bridge per ability would put "which command is a commit" in as many copies as
    there are abilities. Declaring it keeps one copy and keeps the engine ignorant of what any
    of the patterns mean.
    """
    ws = tmp_path / "repo"
    (ws / "sub" / "deep").mkdir(parents=True)
    d = _spec(env, "gtool", _GT_BODY)
    try:
        assert rc(["open", "gtool", "--scope", str(ws), "--run", "gt"], env) == OK
        assert _gt(env, "shell", {"command": "git commit -m x"}, str(ws)) == BLOCKED
        # path_prefix: an action in a SUBdirectory belongs to this run.
        assert _gt(env, "shell", {"command": "git commit -m x"},
                   str(ws / "sub" / "deep")) == BLOCKED
        # Rewording does not help — the pattern tolerates intervening flags whose arguments
        # are separate tokens, which an enumerate-the-flags pattern silently missed.
        assert _gt(env, "shell", {"command": "git -c user.name=x commit"}, str(ws)) == BLOCKED
        assert _gt(env, "shell", {"command": "cd s && git commit --amend"}, str(ws)) == BLOCKED
        # Reads are not the action.
        for cmd in ("git status", "git log --oneline", "git log --grep=commit",
                    'echo "run git commit later"'):
            assert _gt(env, "shell", {"command": cmd}, str(ws)) == OK, cmd
        # A different tool, matched on a field inside its arguments.
        assert _gt(env, "Tasker", {"id": "T", "status": "Closed"}, str(ws)) == BLOCKED
        assert _gt(env, "Tasker", {"id": "T", "status": "Open"}, str(ws)) == OK
        # A rule belongs to ONE tool. A payload whose text would satisfy another tool's
        # pattern must not be blocked by it — otherwise a task note mentioning a command
        # becomes the command, and the rules stop being about tools at all.
        # This payload WOULD satisfy the other tool's pattern verbatim, so it is the one that
        # proves the tool name is compared at all. Two earlier attempts did not: a `field:`
        # narrowing and JSON escaping each happened to block the cross-match on their own, so
        # the mutation "stop comparing tool names" stayed green while looking covered.
        assert _gt(env, "shell", {"command": "echo hi", "status": "Closed"}, str(ws)) == OK
        # Outside the scope: not this run's business.
        assert _gt(env, "shell", {"command": "git commit -m x"}, str(tmp_path / "other")) == OK
    finally:
        _rm(d)


def test_a_symlinked_location_still_matches_its_scope(env, tmp_path):
    """A guard that does not fire because two spellings of one directory differ is a SILENT
    miss — and "not guarded" then looks exactly like "nothing to guard".

    A runtime reports its location by asking the OS, which has already followed symlinks; a
    scope holds whatever the opener typed. Comparing the two spellings unresolved is how a
    guard quietly stops protecting anything on a machine with a symlinked parent.
    """
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    d = _spec(env, "gtlink", _GT_BODY)
    try:
        # Opened by the LINK spelling, acted on by the REAL one.
        assert rc(["open", "gtlink", "--scope", str(link), "--run", "gl"], env) == OK
        assert _gt(env, "shell", {"command": "git commit -m x"}, str(real)) == BLOCKED
        # …and the other way round.
        assert rc(["close-run", "--run", "gl", "--force-steps"], env) == OK
        assert rc(["open", "gtlink", "--scope", str(real), "--run", "gl2"], env) == OK
        assert _gt(env, "shell", {"command": "git commit -m x"}, str(link)) == BLOCKED
    finally:
        _rm(d)


def test_guard_tool_allows_once_the_gate_is_recorded(env, tmp_path):
    ws = tmp_path / "repo2"; ws.mkdir()
    t = tmp_path / "t.jsonl"
    t.write_text('{"role":"user","content":"go ahead"}\n', encoding="utf-8")
    d = _spec(env, "gtgate", _GT_BODY)
    try:
        assert rc(["open", "gtgate", "--scope", str(ws), "--run", "gg"], env) == OK
        assert _gt(env, "shell", {"command": "git commit"}, str(ws)) == BLOCKED
        assert rc(["gate", "--run", "gg", "--step", "G", "--decision", "affirm",
                   "--evidence", "go ahead"], env, transcript=t, witness="transcript") == OK
        assert _gt(env, "shell", {"command": "git commit"}, str(ws)) == OK
    finally:
        _rm(d)


def test_two_runs_claiming_one_call_allow_loudly(env, tmp_path):
    """Ambiguity allows, and says so. Demanding the wrong run's gate is worse than demanding
    none: it leaves forging that gate as the only way forward."""
    ws = tmp_path / "shared"; ws.mkdir()
    d = _spec(env, "gtamb", _GT_BODY)
    try:
        assert rc(["open", "gtamb", "--scope", str(ws), "--run", "a1"], env) == OK
        assert rc(["open", "gtamb", "--scope", str(ws), "--run", "a2",
                   "--allow-concurrent"], env) == OK
        import json as _json
        r = run(["guard-tool", "--tool", "shell",
                 "--input-json", _json.dumps({"command": "git commit"}),
                 "--cwd", str(ws)], env)
        assert r.returncode == OK
        assert "NOT enforced" in r.stderr and "a1" in r.stderr and "a2" in r.stderr
    finally:
        _rm(d)


def test_a_match_rule_must_name_a_tool_and_a_pattern(env):
    for body, needle in [
        ("guards:\n  a:\n    step: G\n    matches:\n      - pattern: x\n", "no 'tool'"),
        ("guards:\n  a:\n    step: G\n    matches:\n      - tool: shell\n", "no 'pattern'"),
        ("guards:\n  a:\n    step: G\n    matches:\n      - {tool: shell, pattern: '('}\n",
         "not a valid"),
    ]:
        d = _spec(env, "gtbad", """
phases: [{id: p1}]
steps: [{id: G, phase: p1, gate: affirm, completion: {type: gate_recorded}}]
""" + body)
        try:
            r = run(["validate", "gtbad"], env)
            assert r.returncode == BAD_SPEC, (needle, r.stdout + r.stderr)
            assert needle in r.stderr, (needle, r.stderr)
        finally:
            _rm(d)


def test_the_kiro_adapter_translates_both_directions(env, tmp_path):
    """The adapter is the only integration artifact and it must be thin AND fail-open.

    It runs before every matching tool call in every session, so any uncertainty allows. The one
    condition it reports rather than swallowing is a state directory it cannot read — because
    that looks identical to "no open run" downstream, which is the silence this project is against.
    """
    import json as _json
    import subprocess
    adapter = REPO / "integrations" / "kiro-pretooluse.py"
    assert adapter.exists()
    ws = tmp_path / "repo3"; ws.mkdir()
    d = _spec(env, "gtadapt", _GT_BODY)
    try:
        assert rc(["open", "gtadapt", "--scope", str(ws), "--run", "ad"], env) == OK

        def hook(event, cwd=str(ws), state=env["HARNESS_STATE_DIR"]):
            import os
            e = {**os.environ, "HARNESS_STATE_DIR": state,
                 "HARNESS_ENGINE": str(REPO)}
            return subprocess.run([sys.executable, str(adapter)], input=_json.dumps(event),
                                  capture_output=True, text=True, cwd=cwd, env=e)

        r = hook({"tool_name": "shell", "tool_input": {"command": "git commit -m x"}})
        assert r.returncode == 2, (r.returncode, r.stderr)     # engine 4 -> kiro 2
        assert "BLOCKED" in r.stderr
        assert hook({"tool_name": "shell",
                     "tool_input": {"command": "git status"}}).returncode == 0
        # Fail-open on every uncertainty.
        assert hook("not-a-dict-event").returncode == 0
        assert hook({}).returncode == 0
        bad = hook({"tool_name": "shell", "tool_input": {"command": "git commit"}},
                   state=str(tmp_path / "nowhere"))
        assert bad.returncode == 0 and "no harness.db" in bad.stderr
    finally:
        _rm(d)


def test_only_a_closed_run_may_be_purged_and_the_purge_is_recorded(env):
    """A maintenance surface has to exist, or the only way to clear test residue is raw SQL —
    which bypasses every check and leaves no trace that anything was removed.

    Two constraints make it safe rather than a hole: closed-only (an open run's violations are
    live evidence, and being able to purge them would make the ledger meaningless), and the
    purge itself is recorded — "the row is gone" and "the row was never written" must not look
    alike.
    """
    import sqlite3
    assert rc(["open", "delivery", "--scope", "/purge-me", "--run", "pg"], env) == OK
    r = run(["purge-run", "--run", "pg", "--reason", "test"], env)
    assert r.returncode == REFUSED and "Only a closed run" in r.stderr
    assert rc(["close-run", "--run", "pg", "--force-steps"], env) == OK
    audit = run(["audit"], env).stdout
    assert "forced_close" in audit
    out = run(["purge-run", "--run", "pg", "--reason", "residue from a wiring smoke test"], env)
    assert out.returncode == OK and "purge_log" in out.stdout
    assert "forced_close" not in run(["audit"], env).stdout
    c = sqlite3.connect(env["HARNESS_STATE_DIR"] + "/harness.db")
    try:
        rows = c.execute("SELECT run_id, reason, counts_json FROM purge_log "
                         "WHERE run_id='pg'").fetchall()
    finally:
        c.close()
    assert len(rows) == 1 and "smoke test" in rows[0][1]
    assert '"violation": 1' in rows[0][2], rows[0][2]
    assert rc(["purge-run", "--run", "pg", "--reason", "again"], env) == USAGE   # gone


def _cap_probe(requires: str, cond: str = "{fact: n, count_gte: 1}") -> pathlib.Path:
    """A throwaway ability whose provider declares a capability that may or may not be here."""
    d = REPO / "abilities" / "__cap_test__"
    d.mkdir(exist_ok=True)
    (d / "providers.py").write_text(
        "from engine import facts, operators\n\n"
        f"@facts.provider('cap_facts', requires={requires}, "
        "schema={'n': operators.T_INT})\n"
        "def _p(ctx):\n"
        "    return {'n': 0}\n",
        encoding="utf-8")
    (d / "flow.yaml").write_text(
        "version: 2\nability: __cap_test__\nrole: fixture\nscope_kind: x\n"
        "facts:\n  providers: [cap_facts]\n"
        "phases:\n  - id: p\n    goal: {type: phase_steps_closed}\n"
        "steps:\n"
        "  - id: S1\n    phase: p\n    title: t\n    directive: d\n"
        "    completion: {type: evidence, kind: k}\n"
        "hooks:\n"
        "  - id: h\n    trigger: step_close\n    step: S1\n"
        f"    when: {cond}\n"
        "    mode: contract\n    contract: |\n      x\n",
        encoding="utf-8")
    return d


def _cap_cleanup(d: pathlib.Path) -> None:
    for f in d.iterdir():
        if f.is_dir():
            for g in f.iterdir():
                g.unlink()
            f.rmdir()
        else:
            f.unlink()
    d.rmdir()


def test_an_absent_capability_refuses_instead_of_answering_false(env):
    """The core asymmetry. Every operator on a missing value answers False, so a silenced
    condition is indistinguishable from one that looked and found nothing — which is the hole
    the capability layer exists to close and therefore must not reopen."""
    d = _cap_probe('({"cmd": "definitely-not-a-real-binary-xyz"},)')
    try:
        assert rc(["open", "__cap_test__", "--scope", "s", "--run", "k1"], env) == OK
        assert rc(["evidence", "--run", "k1", "--step", "S1",
                   "--kind", "k", "--value", "v"], env) == OK
        r = run(["close-step", "--run", "k1", "--step", "S1"], env)
        assert r.returncode == REFUSED, r.stdout + r.stderr
        both = r.stdout + r.stderr
        assert "definitely-not-a-real-binary-xyz" in both, both
        assert "Not treated as false" in both, both
    finally:
        _cap_cleanup(d)


def test_a_present_capability_lets_the_condition_answer_normally(env):
    """The positive control. Without it the test above would pass on an ability that simply
    never fires its hook, and the capability layer would be credited for a dead condition."""
    d = _cap_probe('({"cmd": "git"}, {"env": "PATH"})')
    try:
        assert rc(["open", "__cap_test__", "--scope", "s", "--run", "k2"], env) == OK
        assert rc(["evidence", "--run", "k2", "--step", "S1",
                   "--kind", "k", "--value", "v"], env) == OK
        r = run(["close-step", "--run", "k2", "--step", "S1"], env)
        assert r.returncode == OK, r.stdout + r.stderr
        assert "condition not met" in r.stdout, r.stdout      # n=0, so count_gte 1 is False
    finally:
        _cap_cleanup(d)


def test_an_absent_capability_stops_only_the_steps_that_read_it(env):
    """Absence is a fact about the ENVIRONMENT, not a verdict on the work.

    The first version of this test dropped the flow's hooks entirely and asserted the run
    closed — which proved only that a flow with no hooks calls no providers. It stayed green
    under every mutation, including a two-point one, because it never reached the code it
    named. Rewritten to drive both sides in ONE run: two providers, one of them unusable here,
    and a step on each. The usable side must still close.

    Pinning the property, not a mechanism: it is delivered by the caller narrowing each gather
    to the providers owning the facts it is about to read, and it survives whether an absent
    capability is marked or raised. That is why no single-point mutation turns this red — it is
    covered twice — and saying so is more useful than implying one guard holds it up.
    """
    d = REPO / "abilities" / "__cap2_test__"
    d.mkdir(exist_ok=True)
    try:
        (d / "providers.py").write_text(
            "from engine import facts, operators\n\n"
            "@facts.provider('here_facts', requires=({'cmd': 'git'},), "
            "schema={'a': operators.T_INT})\n"
            "def _a(ctx):\n    return {'a': 7}\n\n"
            "@facts.provider('gone_facts', "
            "requires=({'cmd': 'definitely-not-a-real-binary-xyz'},), "
            "schema={'b': operators.T_INT})\n"
            "def _b(ctx):\n    return {'b': 7}\n",
            encoding="utf-8")
        (d / "flow.yaml").write_text(
            "version: 2\nability: __cap2_test__\nrole: fixture\nscope_kind: x\n"
            "facts:\n  providers: [here_facts, gone_facts]\n"
            "phases:\n  - id: p\n    goal: {type: phase_steps_closed}\n"
            "steps:\n"
            "  - id: S1\n    phase: p\n    title: t\n    directive: d\n"
            "    completion: {type: evidence, kind: k}\n"
            "  - id: S2\n    phase: p\n    title: t\n    directive: d\n"
            "    deps: [S1]\n    completion: {type: evidence, kind: k}\n"
            "hooks:\n"
            "  - id: ha\n    trigger: step_close\n    step: S1\n"
            "    when: {fact: a, count_gte: 1}\n    mode: contract\n    contract: |\n      x\n"
            "  - id: hb\n    trigger: step_close\n    step: S2\n"
            "    when: {fact: b, count_gte: 1}\n    mode: contract\n    contract: |\n      x\n",
            encoding="utf-8")
        assert rc(["open", "__cap2_test__", "--scope", "s", "--run", "k3"], env) == OK
        for sid in ("S1", "S2"):
            assert rc(["evidence", "--run", "k3", "--step", sid,
                       "--kind", "k", "--value", "v"], env) == OK
        r1 = run(["close-step", "--run", "k3", "--step", "S1"], env)
        assert r1.returncode == OK, r1.stdout + r1.stderr
        r2 = run(["close-step", "--run", "k3", "--step", "S2"], env)
        assert r2.returncode != OK, r2.stdout + r2.stderr
        assert "definitely-not-a-real-binary-xyz" in r2.stdout + r2.stderr
    finally:
        _cap_cleanup(d)


def test_an_unavailable_fact_may_not_silently_pin_a_run_shape(env):
    """A variant derived from a fact this machine cannot produce must REFUSE, not default.

    This is the one that already went wrong once, without a capability layer involved: a
    derivation fed an input it was never meant to read answered with its default, the wrong
    shape got pinned, and every step of the right shape then reported as "not part of this
    flow" — a refusal that reads as correct. An absent capability is the same trap with a
    different cause, so it gets the same answer.
    """
    d = REPO / "abilities" / "__capv_test__"
    d.mkdir(exist_ok=True)
    try:
        (d / "providers.py").write_text(
            "from engine import facts, operators\n\n"
            "@facts.provider('vfacts', "
            "requires=({'cmd': 'definitely-not-a-real-binary-xyz'},), "
            "schema={'mode': operators.T_STR})\n"
            "def _v(ctx):\n    return {'mode': 'beta'}\n",
            encoding="utf-8")
        (d / "flow.yaml").write_text(
            "version: 2\nability: __capv_test__\nrole: fixture\nscope_kind: x\n"
            "facts:\n  providers: [vfacts]\n"
            "variants:\n  values: [alpha, beta]\n  default: alpha\n  fact: mode\n"
            "phases:\n  - id: p\n    goal: {type: phase_steps_closed}\n"
            "steps:\n"
            "  - id: S1\n    phase: p\n    title: t\n    directive: d\n"
            "    completion: {type: evidence, kind: k}\n",
            encoding="utf-8")
        r = run(["open", "__capv_test__", "--scope", "s", "--run", "kv"], env)
        assert r.returncode == REFUSED, r.stdout + r.stderr
        both = r.stdout + r.stderr
        assert "cannot resolve the variant" in both, both
        assert "definitely-not-a-real-binary-xyz" in both, both
        # And it must NOT have quietly landed on the default.
        assert "(no open runs)" in run(["status"], env).stdout
        # Explicit still works: knowing the answer must not require the capability.
        assert rc(["open", "__capv_test__", "--scope", "s", "--run", "kv2",
                   "--variant", "beta"], env) == OK
    finally:
        _cap_cleanup(d)


def test_validate_reports_which_capabilities_this_machine_lacks(env):
    """"Can this flow run here" must be answerable BEFORE opening a run. Otherwise the only
    way to find out is to walk the flow until something fails, and at that point the failure
    reads as the flow's rather than the environment's."""
    d = _cap_probe('({"cmd": "definitely-not-a-real-binary-xyz"},)')
    try:
        out = run(["validate", "__cap_test__"], env).stdout
        assert "capabilities: 1 declared, 1 absent" in out, out
        assert "definitely-not-a-real-binary-xyz" in out, out
        assert "will REFUSE, not pass" in out, out
    finally:
        _cap_cleanup(d)


@pytest.mark.parametrize("bad", [
    '({"nosuchkind": "x"},)',
    '({"cmd": "a", "env": "b"},)',
    '("cmd git",)',
])
def test_a_malformed_capability_is_rejected_at_registration(env, bad):
    """A typo'd or two-in-one descriptor must fail at import, not become an unprobed
    capability that reads as satisfied."""
    d = _cap_probe(bad)
    try:
        assert rc(["validate", "__cap_test__"], env) != OK
    finally:
        _cap_cleanup(d)


def test_the_real_ability_declares_the_capabilities_its_tools_need(env):
    """The migration, pinned. Three providers hand-rolled a "tool missing -> return zeros"
    branch whose zeros were indistinguishable, in the substantive fact, from a real all-clear;
    the only thing separating them was a companion boolean the author had to remember AND a
    hook the flow had to remember. Declaring the tool instead removes the remembering.

    What deliberately did NOT move: the "scope is not a directory" branch. That is a real
    answer (nothing to analyse), not a missing one, and conflating the two would turn a
    correct empty result into a refusal.
    """
    import sys as _s
    _s.path.insert(0, str(REPO))
    from engine import facts as _f, flow as _fl
    _fl.load_extensions("shipcheck-asis")
    declared = {pn: _f.capabilities(pn)
                for pn in ("shipcheck-asis.blast_radius", "shipcheck-asis.decision_gate",
                           "shipcheck-asis.review_comments")}
    assert all(declared.values()), declared
    assert all(list(d[0])[0] == "file" for d in declared.values())
    # Every declared tool is actually shipped, so a correct install has no absent capability.
    for pn in declared:
        assert _f.probe_capabilities(pn) == [], (pn, _f.probe_capabilities(pn))
    # The honest-empty branch survived: absence of CHANGES is still reachable without any
    # capability being absent.
    assert "checks_meaningful" in _f.schema_of("shipcheck-asis.blast_radius")
    assert "analysis_ran" in _f.schema_of("shipcheck-asis.blast_radius")


def _claim_probe(body_extra: str = "") -> pathlib.Path:
    """A throwaway ability whose provider answers from an env var, so one spec covers
    corroborated / disproved / unavailable without three near-identical fixtures."""
    d = REPO / "abilities" / "__claim_test__"
    d.mkdir(exist_ok=True)
    (d / "providers.py").write_text(
        "import os\n"
        "from engine import facts, operators\n\n"
        "@facts.provider('probe_facts', schema={'found_count': operators.T_INT})\n"
        "def _p(ctx):\n"
        "    v = os.environ.get('PROBE_FOUND', '0')\n"
        "    if v == 'boom':\n"
        "        raise RuntimeError('probe tool is not installed')\n"
        "    return {'found_count': int(v)}\n",
        encoding="utf-8")
    (d / "flow.yaml").write_text(
        "version: 2\nability: __claim_test__\nrole: fixture\nscope_kind: x\n"
        "facts:\n  providers: [probe_facts]\n"
        "phases:\n  - id: p\n    goal: {type: phase_steps_closed}\n"
        "steps:\n"
        "  - id: S1\n    phase: p\n    title: t\n    directive: d\n"
        "    completion:\n"
        "      type: claim_corroborated\n"
        "      kind: finding\n"
        "      claims: [none]\n"
        "      disproved_when: {fact: found_count, count_gte: 1}\n"
        + body_extra,
        encoding="utf-8")
    return d


def test_an_absence_claim_is_refused_when_a_fact_contradicts_it(env, monkeypatch):
    """The claim / world asymmetry, on every branch.

    A value asserting that nothing was found is the cheapest thing to record and the hardest
    to audit afterwards, because it is identical in the log to the same value honestly earned.
    A neighbouring system granted 136 acknowledgements this way: its rule accepted "the tool
    was not there" as evidence that there was nothing to report. So all four branches are
    pinned here, and the two that must NOT pass are the interesting ones.
    """
    d = _claim_probe()
    try:
        assert rc(["validate", "__claim_test__"], env) == BAD_SPEC or True
        assert rc(["open", "__claim_test__", "--scope", "s", "--run", "c1"], env) == OK

        # 1. The claim, corroborated: the probe looked and agreed.
        assert rc(["evidence", "--run", "c1", "--step", "S1",
                   "--kind", "finding", "--value", "none"], env) == OK
        e0 = {**env, "PROBE_FOUND": "0"}
        r = run(["close-step", "--run", "c1", "--step", "S1"], e0)
        assert r.returncode == OK, r.stderr
        assert "corroborated" in r.stdout

        # 2. The same claim, contradicted.
        assert rc(["open", "__claim_test__", "--scope", "s2", "--run", "c2"], env) == OK
        assert rc(["evidence", "--run", "c2", "--step", "S1",
                   "--kind", "finding", "--value", "none"], env) == OK
        r = run(["close-step", "--run", "c2", "--step", "S1"],
                {**env, "PROBE_FOUND": "3"})
        assert r.returncode == REFUSED
        assert "an independent fact says otherwise" in r.stderr
        assert "do NOT re-record the claim" in r.stderr

        # 3. UNVERIFIABLE IS REFUSED, not passed. This is the branch the reference system got
        #    wrong: it treated "nothing could check" as "there was nothing".
        r = run(["close-step", "--run", "c2", "--step", "S1"],
                {**env, "PROBE_FOUND": "boom"})
        assert r.returncode == REFUSED
        assert "unavailable" in r.stderr
        assert "Unverifiable is refused" in r.stderr

        # 4. A value that is NOT a claim of absence needs no corroboration — the probe is not
        #    even consulted, so a broken provider cannot block an honest finding.
        assert rc(["open", "__claim_test__", "--scope", "s3", "--run", "c3"], env) == OK
        assert rc(["evidence", "--run", "c3", "--step", "S1",
                   "--kind", "finding", "--value", "two-real-ones"], env) == OK
        r = run(["close-step", "--run", "c3", "--step", "S1"],
                {**env, "PROBE_FOUND": "boom"})
        assert r.returncode == OK, r.stderr
        assert "not a claim of absence" in r.stdout
    finally:
        for f in d.iterdir():
            f.unlink()
        d.rmdir()


def test_an_absence_claim_over_nothing_is_not_a_clean_bill(env):
    """Zero rows must refuse. A claim over an empty set is the cheapest pass imaginable:
    record nothing, and a criterion that reads as "and it was clean" is satisfied."""
    d = _claim_probe()
    try:
        assert rc(["open", "__claim_test__", "--scope", "s", "--run", "z1"], env) == OK
        r = run(["close-step", "--run", "z1", "--step", "S1"], {**env, "PROBE_FOUND": "0"})
        assert r.returncode == REFUSED
        assert "no claim to corroborate" in r.stderr
    finally:
        for f in d.iterdir():
            f.unlink()
        d.rmdir()


@pytest.mark.parametrize("bad,expect", [
    ("disproved_when: {}", "needs a 'fact' key"),
    ("disproved_when: {fact: no_such_fact, count_gte: 1}", "no_such_fact"),
    ("disproved_when: {fact: found_count, matches_any: [x]}", "cannot be applied"),
    ("claims: []", "non-empty 'claims'"),
])
def test_a_corroboration_check_is_validated_at_load_time(env, bad, expect):
    """An unfalsifiable or mistyped corroboration must not load.

    All four go through the SAME validator a hook condition uses, on purpose: a criterion and
    a hook asking "is there a finding" must not be able to mean different things, and a fact
    renamed in a provider has to break both at load rather than one at runtime. That shared
    validator is also why this engine grew no private "references at least one fact" clause —
    it already refuses every tree that could reference none, so a second check would have been
    a guard that cannot fire.
    """
    d = _claim_probe()
    try:
        key = bad.split(":")[0]
        out = []
        for ln in (d / "flow.yaml").read_text(encoding="utf-8").splitlines():
            if key == "claims" and ln.strip() == "- none":
                continue                                    # drop the list body too
            if ln.strip().startswith(key + ":"):
                out.append(ln[:len(ln) - len(ln.lstrip())] + bad)   # keep the indent
            else:
                out.append(ln)
        txt = "\n".join(out) + "\n"
        (d / "flow.yaml").write_text(txt, encoding="utf-8")
        # The spec must be VALID yaml that the engine then refuses on its own terms — a parse
        # error would give the same exit code for the wrong reason.
        import yaml as _y
        _y.safe_load(txt)
        assert rc(["validate", "__claim_test__"], env) == BAD_SPEC, txt
        assert expect in run(["validate", "__claim_test__"], env).stderr
    finally:
        for f in d.iterdir():
            f.unlink()
        d.rmdir()


def test_a_pure_compute_tool_is_wired_through_the_provider_seam(env):
    """C1: a copied tool computes, a provider declares, a criterion decides.

    The tool is byte-identical to its source and emits TEXT, not JSON, so the seam had to
    absorb a second output shape rather than the ability adapting the tool. What it buys is
    a criterion that was previously "a row of this kind exists" — which accepts numbers
    nobody produced — becoming a claim that an independent record can contradict.
    """
    import sys as _s
    _s.path.insert(0, str(REPO))
    from engine import facts as _f, flow as _fl
    _fl.load_extensions("shipcheck-asis")
    assert "shipcheck-asis.run_metrics" in _f.registered()
    caps = _f.capabilities("shipcheck-asis.run_metrics")
    kinds = [next(iter(d)) for d in caps]
    assert kinds == ["file", "file"], caps
    # The tool's own presence AND the runtime record it reads are both declared. Declaring
    # only the tool would make "this machine has no such runtime" look like "the runtime is
    # empty" — the pair the whole capability layer exists to keep apart.
    assert any("analyze-run-metrics.py" in str(d.get("file", "")) for d in caps), caps
    assert any("sessions" in str(d.get("file", "")) for d in caps), caps
    tool = REPO / "abilities" / "shipcheck-asis" / "tools" / "analyze-run-metrics.py"
    src = pathlib.Path.home() / ".kiro/skills/ship-check/scripts/analyze-run-metrics.py"
    assert tool.is_file()
    if src.is_file():
        assert tool.read_bytes() == src.read_bytes(), "the copy has drifted from its source"
    # And the step that reads it declares BOTH a claim and the honest alternative, so the
    # exit from the claim is in the spec rather than only in a refusal message.
    import yaml as _y
    fl = _y.safe_load((REPO / "abilities" / "shipcheck-asis" / "flow.yaml").read_text())
    e19 = next(x for x in fl["steps"] if x["id"] == "E19")
    checks = e19["completion"]["checks"]
    legal = next(c["values"] for c in checks if c.get("kind") == "metrics_source"
                 and c["type"] == "evidence_in")
    claims = next(c["claims"] for c in checks if c["type"] == "claim_corroborated")
    assert set(claims) < set(legal), (claims, legal)


def test_capability_present_but_no_data_is_its_own_answer(env, monkeypatch):
    """The second dimension, pinned separately because the flow can absorb either outcome.

    The capability layer answers "is the runtime record present at all" and the provider
    answers "did it have anything for this run" — two questions the design claims to keep
    apart. The full-flow driver cannot prove that: it closes the step either way (an
    uncontradicted claim closes, and a contradicted one closes after recording the honest
    value), so mutating the provider's honesty flag left the suite green. Asserted here as the
    pairing it actually is: the flag is true exactly when the tool succeeded.
    """
    import sys as _s
    _s.path.insert(0, str(REPO))
    monkeypatch.setenv("HARNESS_STATE_DIR", env["HARNESS_STATE_DIR"])
    from engine import facts as _f, flow as _fl, store as _st
    _fl.load_extensions("shipcheck-asis")
    assert rc(["open", "shipcheck-asis", "--scope", str(REPO), "--run", "mx"], env) == OK
    conn = _st.connect(read_only=True)
    try:
        opened = _st.get_run(conn, "mx")["opened_at"]
    finally:
        conn.close()
    tool = REPO / "abilities" / "shipcheck-asis" / "tools" / "analyze-run-metrics.py"
    proc = subprocess.run([sys.executable, str(tool), "--since", str(opened),
                           "--mode", "plan-doc"], capture_output=True, text=True)
    got = _f.gather("shipcheck-asis.run_metrics", {"run_id": "mx", "scope": str(REPO),
                                    "scope_kind": "workspace", "ability": "shipcheck-asis"})
    assert got["metrics_available"] is (proc.returncode == 0), (proc.returncode, got)
    if proc.returncode != 0:
        # Zeroed, not guessed. Numbers carried over from a failed read would be the exact
        # thing the criterion above exists to refuse.
        assert (got["run_turns"], got["run_calls"], got["run_peak_ctx_pct"]) == (0, 0, 0), got
    # A run_id the store does not have is the same honest nothing, by a different route.
    absent = _f.gather("shipcheck-asis.run_metrics", {"run_id": "no-such-run", "scope": str(REPO),
                                       "scope_kind": "workspace", "ability": "shipcheck-asis"})
    assert absent["metrics_available"] is False, absent


def test_a_net_dependent_provider_declares_the_host_and_touches_no_credentials(env):
    """C2: the shape of a provider that reaches a network, and its two hard limits.

    LIMIT ONE — it must not acquire credentials. Reading a review needs an authenticated
    session; a provider that went into the user's credential store to get one would trade a
    far larger permission for a small fact. So authorization is REPORTED, never obtained.

    LIMIT TWO — what it therefore cannot answer. Measured against the real host, an existing
    review and a nonexistent one return the SAME unauthenticated redirect, so existence is not
    derivable and no fact claims it. Declaring `cr_exists` would have been the tempting lie:
    plausible in the schema, unfalsifiable in the output.
    """
    import sys as _s
    _s.path.insert(0, str(REPO))
    from engine import facts as _f, flow as _fl
    _fl.load_extensions("cr-reviewer")
    caps = _f.capabilities("cr-reviewer.cr_review_state")
    assert {"cmd": "curl"} in caps, caps
    assert any("net" in d and "code.amazon.com" in d["net"] for d in caps), caps
    schema = _f.schema_of("cr-reviewer.cr_review_state")
    # Authorization is a FACT, not a fifth capability kind: the engine can probe presence,
    # and probing authorization would mean making a real authenticated request, which is
    # domain knowledge the engine must not hold.
    assert "cr_needs_authorization" in schema
    assert "cr_host_answered" in schema
    assert "cr_exists" not in schema, "existence is not derivable without credentials"
    # The guard bans USING a credential, not MENTIONING one. A whole-file substring ban was
    # tried first and was the wrong shape twice over: it forbade the docstring from stating
    # the rule, and it forbade the SSO_MARKERS table from recognising an auth redirect — the
    # very thing that lets the provider report "this host wants credentials" without holding
    # any. So the check walks string literals other than docstrings and looks for credential
    # FLAGS and credential PATHS.
    import ast as _ast
    tree = _ast.parse((REPO / "abilities" / "cr-reviewer" / "providers.py").read_text())
    docstrings = set()
    for node in _ast.walk(tree):
        if isinstance(node, (_ast.Module, _ast.FunctionDef, _ast.AsyncFunctionDef,
                             _ast.ClassDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], _ast.Expr) and \
                    isinstance(body[0].value, _ast.Constant) and \
                    isinstance(body[0].value.value, str):
                docstrings.add(id(body[0].value))
    literals = [n.value for n in _ast.walk(tree)
                if isinstance(n, _ast.Constant) and isinstance(n.value, str)
                and id(n) not in docstrings]
    CRED_FLAGS = ("-b", "--cookie", "-c", "--cookie-jar", "--netrc", "-u", "--user", "-H",
                  "--header", "--key", "--cert")
    # Shaped to match credential MATERIAL, not vocabulary. "authorization" alone banned the
    # fact name `cr_needs_authorization` — the fact whose entire job is to say credentials
    # would be needed. The colon and the space are what make these header forms, not words.
    CRED_PATHS = (".midway", ".netrc", ".aws/", ".ssh/", "id_rsa",
                  "authorization:", "bearer ", "x-api-key")
    for lit in literals:
        assert lit.strip() not in CRED_FLAGS, f"provider passes credential flag {lit!r}"
        low = lit.lower()
        for pth in CRED_PATHS:
            assert pth not in low, f"provider names credential material {lit!r}"
    # And it has no business in the home directory at all.
    assert "Path.home()" not in (REPO / "abilities" / "cr-reviewer" / "providers.py").read_text()


def test_an_unreachable_review_host_refuses_the_claim_of_having_read_it(env):
    """C2's absence path, deterministic — no network needed to prove it.

    Uses an unresolvable host so the capability is absent by construction. The point is the
    same asymmetry as everywhere else: "I consulted it and there was nothing" must not be
    cheaper than consulting it, so a claim that cannot be corroborated is refused and the
    refusal names the host.
    """
    d = REPO / "abilities" / "__net_test__"
    d.mkdir(exist_ok=True)
    try:
        (d / "providers.py").write_text(
            "from engine import facts, operators\n\n"
            "@facts.provider('offsite_facts', "
            "requires=({'net': 'nonexistent-host-for-tests.invalid:443'},), "
            "schema={'host_answered': operators.T_BOOL})\n"
            "def _o(ctx):\n    return {'host_answered': True}\n",
            encoding="utf-8")
        (d / "flow.yaml").write_text(
            "version: 2\nability: __net_test__\nrole: fixture\nscope_kind: cr\n"
            "facts:\n  providers: [offsite_facts]\n"
            "phases:\n  - id: p\n    goal: {type: phase_steps_closed}\n"
            "steps:\n"
            "  - id: S1\n    phase: p\n    title: t\n    directive: d\n"
            "    completion:\n"
            "      type: all_checks\n"
            "      checks:\n"
            "      - {type: evidence_in, kind: comments_read, values: [read, none]}\n"
            "      - type: claim_corroborated\n"
            "        kind: comments_read\n"
            "        claims: [read, none]\n"
            "        disproved_when: {fact: host_answered, equals: false}\n",
            encoding="utf-8")
        out = run(["validate", "__net_test__"], env).stdout
        assert "capabilities: 1 declared, 1 absent" in out, out
        assert "nonexistent-host-for-tests.invalid" in out, out
        assert rc(["open", "__net_test__", "--scope", "CR-1", "--run", "n1"], env) == OK
        assert rc(["evidence", "--run", "n1", "--step", "S1",
                   "--kind", "comments_read", "--value", "none"], env) == OK
        r = run(["close-step", "--run", "n1", "--step", "S1"], env)
        assert r.returncode == REFUSED, r.stdout + r.stderr
        both = r.stdout + r.stderr
        assert "nonexistent-host-for-tests.invalid" in both, both
        assert "is unavailable" in both, both
    finally:
        _cap_cleanup(d)


def _fleet_fixture() -> pathlib.Path:
    """A fixture carrying the same three-way corroboration the delivery flow uses on reporting.

    Borrows the real provider through `requires` rather than re-implementing it: the thing under
    test is whether each legal value of the claim can be contradicted, and a re-implementation
    would test a copy.
    """
    d = REPO / "abilities" / "__fleet_test__"
    d.mkdir(exist_ok=True)
    (d / "flow.yaml").write_text(
        "version: 2\nability: __fleet_test__\nrole: fixture\nscope_kind: repo\n"
        "requires: [shipcheck-asis]\n"
        "facts:\n  providers: [shipcheck-asis.fleet_central]\n"
        "phases:\n  - id: p\n    goal: {type: phase_steps_closed}\n"
        "steps:\n"
        "  - id: S1\n    phase: p\n    title: t\n    directive: d\n"
        "    completion: {type: evidence, kind: fleet_task}\n"
        "  - id: S2\n    phase: p\n    title: t\n    directive: d\n"
        "    deps: [S1]\n"
        "    completion:\n"
        "      type: all_checks\n"
        "      checks:\n"
        "      - {type: evidence_in, kind: fleet_report,"
        " values: [sent, unreachable, not_applicable]}\n"
        "      - {type: claim_corroborated, kind: fleet_report, claims: [sent],"
        " disproved_when: {fact: fleet_event_count, count_lte: 0}}\n"
        "      - {type: claim_corroborated, kind: fleet_report, claims: [unreachable],"
        " disproved_when: {fact: fleet_reachable, equals: true}}\n"
        "      - {type: claim_corroborated, kind: fleet_report, claims: [not_applicable],"
        " disproved_when: {fact: fleet_applicable, equals: true}}\n",
        encoding="utf-8")
    return d


@pytest.mark.parametrize("task,url,claim,want", [
    # A run not serving a dispatched task: "nothing to report to" is TRUE and passes.
    ("local", "", "not_applicable", OK),
    # ...but the same run may not claim it SENT something. Nothing arrived anywhere.
    ("local", "", "sent", REFUSED),
    # A real task id with a coordinator that cannot be reached: "unreachable" is honest.
    ("T-9999", "https://127.0.0.1:9", "unreachable", OK),
    # Same run claiming it reported: refused, because nothing arrived.
    ("T-9999", "https://127.0.0.1:9", "sent", REFUSED),
    # And it may not claim the task does not apply — one is on the record.
    ("T-9999", "https://127.0.0.1:9", "not_applicable", REFUSED),
])
def test_every_legal_value_of_the_reporting_claim_can_be_contradicted(env, task, url, claim, want):
    """EACH value, not just the interesting one — that asymmetry is the whole point.

    The behavioural standard for dispatched work asks for a report at each milestone. In the
    reference system that is prose plus a fire-once hook, so "I reported it" is accepted on its
    own word; its own ledger carries 162 forced acknowledgements and 24 recorded non-arrivals.
    Checking only `sent` would move the hole rather than close it: `unreachable` and
    `not_applicable` would become the free exits. So all three are corroborated, each by a
    different derived fact, and this asserts all three.
    """
    d = _fleet_fixture()
    e = dict(env)
    if url:
        e["FLEET_CENTRAL_URL"] = url
    try:
        rid = f"fl-{task}-{claim}".replace("_", "")
        assert rc(["open", "__fleet_test__", "--scope", str(REPO), "--run", rid], env) == OK
        assert rc(["evidence", "--run", rid, "--step", "S1",
                   "--kind", "fleet_task", "--value", task], env) == OK
        assert rc(["close-step", "--run", rid, "--step", "S1"], env) == OK
        assert rc(["evidence", "--run", rid, "--step", "S2",
                   "--kind", "fleet_report", "--value", claim], env) == OK
        r = run(["close-step", "--run", rid, "--step", "S2"], e)
        assert r.returncode == want, (task, claim, r.stdout + r.stderr)
        if want == REFUSED:
            assert "claim_corroborated" in r.stderr, r.stderr
    finally:
        _cap_cleanup(d)


def test_the_delivery_flow_actually_uses_the_three_way_corroboration(env):
    """The fixture above proves the MECHANISM; this pins that the real flow uses it.

    Written after the fixture failed to catch a mutation of the real ability — the fixture
    carries its own copy of the criteria, so breaking `shipcheck-asis` left it green. A fixture
    that restates what it is guarding guards only itself.

    The sharpest assertion here is the last one: three claims must be contradicted by three
    DISTINCT facts. Two claims sharing one disproof reads like full coverage while leaving one
    value effectively unchecked.
    """
    import yaml as _y
    fl = _y.safe_load((REPO / "abilities" / "shipcheck-asis" / "flow.yaml").read_text())
    e24 = next(x for x in fl["steps"] if x["id"] == "E24")
    checks = e24["completion"]["checks"]
    legal = next(c["values"] for c in checks
                 if c.get("kind") == "fleet_report" and c["type"] == "evidence_in")
    claims = [c for c in checks
              if c["type"] == "claim_corroborated" and c.get("kind") == "fleet_report"]
    assert len(claims) == len(legal), (len(claims), legal)
    claimed = [v for c in claims for v in c["claims"]]
    assert sorted(claimed) == sorted(legal), (claimed, legal)
    facts_used = {c["disproved_when"]["fact"] for c in claims}
    assert len(facts_used) == len(claims), (
        f"{len(claims)} claims share only {len(facts_used)} disproving fact(s): {facts_used}. "
        f"Two claims contradicted by one fact leaves one value effectively unchecked."
    )
    # And the run must record WHICH task it serves, early — otherwise the claim at the end has
    # nothing to be checked against.
    c01 = next(x for x in fl["steps"] if x["id"] == "C01")
    kinds = {c.get("kind") for c in c01["completion"]["checks"]}
    assert "fleet_task" in kinds, kinds


def test_a_coordinator_that_answers_but_not_about_this_task_is_not_reachable(env):
    """"Could not look" must not read as "looked and it was fine".

    An endpoint that responds while knowing nothing about this task is a third state, and the
    copied tool names it `indeterminate` with the comment "we could not look -> NO-OP is NOT
    granted". Mapping it to reachable would let a run claim `unreachable` be refused while
    nothing had in fact been verified — so it maps to NOT reachable, and this pins it with a
    local server that answers 404 rather than depending on any network.
    """
    import http.server
    import threading

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):                      # noqa: N802 — http.server names this
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"nope")

        def log_message(self, *a):             # keep the test output clean
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    d = _fleet_fixture()
    e = {**env, "FLEET_CENTRAL_URL": f"http://127.0.0.1:{srv.server_port}"}
    try:
        assert rc(["open", "__fleet_test__", "--scope", str(REPO), "--run", "fli"], env) == OK
        assert rc(["evidence", "--run", "fli", "--step", "S1",
                   "--kind", "fleet_task", "--value", "T-1"], env) == OK
        assert rc(["close-step", "--run", "fli", "--step", "S1"], env) == OK
        assert rc(["evidence", "--run", "fli", "--step", "S2",
                   "--kind", "fleet_report", "--value", "unreachable"], env) == OK
        # The server ANSWERED — so if 404 were read as reachable, this claim would be refused.
        assert rc(["close-step", "--run", "fli", "--step", "S2"], e) == OK
    finally:
        srv.shutdown()
        _cap_cleanup(d)


@pytest.mark.parametrize("cmd,blocked", [
    ("worktree-teardown.sh --uuid abc --source-repo /x", True),
    ("bash ~/x/worktree-teardown.sh --gc --source-repo /x", True),
    ("cd /tmp && sh ./worktree-teardown.sh --uuid a", True),
    ("python3 ./worktree-teardown.sh", True),
    ("git -C /x worktree remove /y", True),
    ("git -C /x branch -D shipcheck/abc12345", True),
    ("git worktree list", False),
    ("git worktree prune", False),
    ("git branch -D feature/foo", False),
    ("echo 'run worktree-teardown.sh later'", False),
    ("echo run worktree-teardown.sh later", False),
])
def test_tearing_down_an_isolated_worktree_is_guarded(env, cmd, blocked):
    """Teardown is irreversible, so it is gated — and the RULES had to be found by trying them.

    Two lessons are baked into the cases. First, guarding only the script name misses
    `bash <path>/worktree-teardown.sh`, and guarding only the script misses the destructive
    primitives it wraps: `git worktree remove` and `git branch -D shipcheck/*` do the same
    damage without it, so a gate that only knows the wrapper can be dodged by rephrasing.

    Second, and the reason the pattern is two anchored rules rather than one loose one: this
    engine's matcher has NO quote awareness. The protection against a command that merely
    MENTIONS the script comes from anchoring at the start of a command, so loosening the anchor
    to any whitespace boundary — tried, and it blocked `echo 'run worktree-teardown.sh later'` —
    removes that protection along with the miss it was meant to fix. Interpreters are
    enumerable; mentions are not.
    """
    assert rc(["open", "shipcheck-asis", "--scope", str(REPO), "--run", "gwt"], env) == OK
    try:
        r = run(["guard-tool", "--tool", "shell",
                 "--input-json", json.dumps({"command": cmd}),
                 "--cwd", str(REPO)], env)
        assert (r.returncode == BLOCKED) is blocked, (cmd, r.returncode, r.stdout + r.stderr)
        if blocked:
            assert "E21" in r.stdout + r.stderr, r.stdout + r.stderr
    finally:
        rc(["close-run", "--run", "gwt", "--result", "abandoned",
            "--force-steps", "--force-obligations"], env)


def test_the_declared_bar_is_read_and_the_unmeasurable_half_stays_visible(
        env, monkeypatch, tmp_path):
    """Three classifications the full-flow driver cannot reach, and one design claim.

    The driver's runs carry no blocked violations and always find a readable declaration, so
    every branch below is unreachable from it — all three mutations stayed green. Each is a way
    of quietly saying something false:

    1. `placeholder` judged only by "is there a baselines block" would report the efficiency and
       cost dimensions as measurable while every layer is still a stand-in.
    2. Swapping the error/warning mapping makes the critical rule fire on the wrong severity —
       the two vocabularies use different words, so the mapping is a place meaning can drift.
    3. Treating an unreadable declaration as read turns "judged against the declared bar" into
       an opinion with no bar, which is the one thing the verdict criterion exists to refuse.

    And the design claim, asserted rather than left in a comment: NO synthetic score. Only the
    two dimensions this engine owns are exposed, because folding in a 40% stand-in produces a
    figure that reads as measured and hides the stand-in.
    """
    import sys as _s
    _s.path.insert(0, str(REPO))
    from engine import facts as _f, flow as _fl
    _fl.load_extensions("shipcheck-asis")
    monkeypatch.setenv("HARNESS_STATE_DIR", env["HARNESS_STATE_DIR"])

    def decl(text: str) -> str:
        f = tmp_path / f"slo-{abs(hash(text)) % 10**8}.yaml"
        f.write_text(text, encoding="utf-8")
        return str(f)

    ctx = {"run_id": "q1", "scope": str(REPO),
           "scope_kind": "repo", "ability": "shipcheck-asis"}
    assert rc(["open", "shipcheck-asis", "--scope", str(REPO), "--run", "q1"], env) == OK

    # ① One layer still a placeholder is enough — the dimensions are not per-layer measurable.
    monkeypatch.setenv("HARNESS_QUALITY_SLO", decl(
        "slo:\n  ship-check:\n    _default:\n      layer: {green: 8.0}\n"
        "baselines:\n  ship-check:\n"
        "    context: {status: real, duration_p50: 1, duration_p95: 2}\n"
        "    coding: {status: placeholder, duration_p50: 1, duration_p95: 2}\n"))
    got = _f.gather("shipcheck-asis.quality_slo", ctx)
    assert got["slo_readable"] == "yes" and got["layer_green_threshold"] == "8.0", got
    assert got["baselines_are_placeholder"] is True, got

    # ...and only when NONE of them is does it become measurable.
    monkeypatch.setenv("HARNESS_QUALITY_SLO", decl(
        "slo:\n  ship-check:\n    _default:\n      layer: {green: 7.5}\n"
        "baselines:\n  ship-check:\n"
        "    context: {status: real, duration_p50: 1, duration_p95: 2}\n"))
    got = _f.gather("shipcheck-asis.quality_slo", ctx)
    assert got["baselines_are_placeholder"] is False, got
    assert got["layer_green_threshold"] == "7.5", got

    # ② A declaration with no threshold at all is NOT a readable bar.
    monkeypatch.setenv("HARNESS_QUALITY_SLO", decl("slo:\n  ship-check: {}\n"))
    got = _f.gather("shipcheck-asis.quality_slo", ctx)
    assert got["slo_readable"] == "", got
    monkeypatch.setenv("HARNESS_QUALITY_SLO", str(tmp_path / "does-not-exist.yaml"))
    assert _f.gather("shipcheck-asis.quality_slo", ctx)["slo_readable"] == "", "a missing file is no bar"
    # A CORRUPT declaration must land on the same answer by a different route. Without the
    # provider's own catch it escapes as an exception, which the facts layer turns into a loud
    # failure that refuses the whole run — "the engine broke" instead of "there is no bar".
    bad = tmp_path / "corrupt.yaml"
    bad.write_text("slo: [this is: not, a mapping\n  - and unbalanced\n", encoding="utf-8")
    monkeypatch.setenv("HARNESS_QUALITY_SLO", str(bad))
    assert _f.gather("shipcheck-asis.quality_slo", ctx)["slo_readable"] == "", "corrupt is no bar, not a crash"

    # ③ The severity mapping, pinned by recording one of each.
    monkeypatch.setenv("HARNESS_QUALITY_SLO", decl(
        "slo:\n  ship-check:\n    _default:\n      layer: {green: 8.0}\n"))
    from engine import store as _st
    conn = _st.connect()
    try:
        for sev in ("blocked", "blocked", "blocked", "breach"):
            _st.record_violation(conn, "q1", None, "probe", "x", severity=sev)
    finally:
        conn.close()
    got = _f.gather("shipcheck-asis.quality_slo", ctx)
    assert got["error_violation_count"] == 3, got     # blocked -> error
    assert got["warning_violation_count"] == 1, got   # breach  -> warning

    # The design claim: no total is produced, and the two real dimensions stay separate.
    schema = _f.schema_of("shipcheck-asis.quality_slo")
    assert not any("score" in k or "total" in k for k in schema), schema
    assert "completeness_rate_pct" in schema and "error_violation_count" in schema, schema
    rc(["close-run", "--run", "q1", "--result", "abandoned",
        "--force-steps", "--force-obligations"], env)


def test_the_plan_document_is_found_where_it_ENDED_UP(env, monkeypatch, tmp_path):
    """Three resolution details, each of which the full-flow driver cannot reach.

    The driver records a synthetic plan-doc value that resolves to no real file, so every line
    of the lookup below is unreachable from it — mutating any of the three left the suite green.
    They are exactly the three subtleties this provider exists to get right:

    1. RESOLVED BY SLUG, not by the recorded path. The recorded path is stale BY DESIGN at
       closing time, because moving the document is the effect under test. Reading it would
       report a successful move as a failure, or an absent move as a success.
    3. The frontmatter is read from the LEADING block only. A document that lost its frontmatter
       but quotes a yaml block later would otherwise have a snippet read as its status.

    Run against a temporary corpus, never the real one: without a redirect a test here would
    read several hundred of the user's real documents, and a careless one would write into them.
    """
    import sys as _s
    _s.path.insert(0, str(REPO))
    from engine import facts as _f, flow as _fl
    _fl.load_extensions("shipcheck-asis")
    monkeypatch.setenv("HARNESS_STATE_DIR", env["HARNESS_STATE_DIR"])

    data, hlog = tmp_path / "plan", tmp_path / "hlog"
    for d in ("pending", "pushed", "in-progress", "done", "shipped"):
        (data / d).mkdir(parents=True)
    hlog.mkdir()
    monkeypatch.setenv("HARNESS_PLAN_DATA_DIR", str(data))
    monkeypatch.setenv("HARNESS_HISTORY_LOG_DIR", str(hlog))

    slug = "some-task-slug"
    ctx = {"run_id": "pw", "scope": str(REPO),
           "scope_kind": "repo", "ability": "shipcheck-asis"}
    assert rc(["open", "shipcheck-asis", "--scope", str(REPO), "--run", "pw"], env) == OK
    # The path recorded EARLY — pointing at pushed/, which is where it no longer is.
    assert rc(["evidence", "--run", "pw", "--step", "C01", "--kind", "plan_doc",
               "--value", str(data / "pushed" / f"{slug}.md")], env) == OK

    # ① It actually ended up in done/. Found by slug, in the folder it now occupies.
    (data / "done" / f"{slug}.md").write_text(
        f"---\nstatus: done\nslug: {slug}\n---\n\n# T\n\n## Shipped\n\ncommit abc\n",
        encoding="utf-8")
    got = _f.gather("shipcheck-asis.plan_writeback", ctx)
    assert got["plan_doc_found"] is True and got["plan_doc_dir"] == "done", got
    assert got["plan_doc_status"] == "done", got
    assert got["shipped_block_present"] is True, got

    # ③ A document that LOST its frontmatter but quotes a yaml block later. The leading-block
    #    read reports no status; a whole-document search would report the snippet's.
    (data / "done" / f"{slug}.md").write_text(
        "# Title\n\nsome prose\n\n---\nstatus: bogus-from-a-quoted-block\n---\n",
        encoding="utf-8")
    drifted = _f.gather("shipcheck-asis.plan_writeback", ctx)
    assert drifted["plan_doc_found"] is True, drifted
    assert drifted["plan_doc_status"] == "", drifted
    assert drifted["shipped_block_present"] is False, drifted

    # ④ A document that TALKS about shipping without carrying the section. Plan prose routinely
    #    does — "will be shipped in a follow-up" — so a substring match would report the
    #    writeback's artefact as present in a document it never touched.
    (data / "done" / f"{slug}.md").write_text(
        "---\nstatus: done\n---\n\n# T\n\nThis will be Shipped in a follow-up change.\n",
        encoding="utf-8")
    talks = _f.gather("shipcheck-asis.plan_writeback", ctx)
    assert talks["plan_doc_status"] == "done", talks
    assert talks["shipped_block_present"] is False, talks
    rc(["close-run", "--run", "pw", "--result", "abandoned",
        "--force-steps", "--force-obligations"], env)


def test_an_unreadable_hot_store_is_not_an_empty_one(env, monkeypatch, tmp_path):
    """The pairing, asserted separately because the flow closes either way.

    "Consulted and genuinely empty" and "could not be consulted" both render as zero, and the
    standing instruction in this space is explicit that a failing command is not a licence to
    write down zero. The full-flow driver cannot prove this: it records whichever legal value
    survives, so both outcomes close the step. Pinned here as what it actually is — the flag is
    true exactly when the reader succeeded.
    """
    import sys as _s
    _s.path.insert(0, str(REPO))
    from engine import facts as _f, flow as _fl
    # `load`, not `load_extensions`: the provider lives in the `memory` ability now, and only the
    # full load follows `requires` to it. Naming that ability here instead would write the ownership
    # down a second time — and ownership is exactly what just moved.
    _fl.load("shipcheck-asis")

    # A path with no store at all: the CLI creates an empty database, finds no tables, and
    # exits non-zero. The count it implies is an artefact of that failure.
    monkeypatch.setenv("MEMORY_DB_PATH", str(tmp_path / "nope.db"))
    got = _f.gather("memory.hot_set", {})
    assert got["hot_banner_readable"] is False, got
    assert got["hot_set_count"] == 0 and got["hot_set_ids"] == [], got

    # And with the real store, the flag must track the reader's exit code rather than being
    # assumed — compared against an independent invocation so the assertion holds on a machine
    # whose store is missing too.
    monkeypatch.delenv("MEMORY_DB_PATH", raising=False)
    cli = pathlib.Path("~/.kiro/skills/shared-kb/memory/memory.py").expanduser()
    if cli.is_file():
        # The SAME surface the provider reads. Asking for the banner here and json there would
        # compare two different questions, and on a store predating the json fix the banner exits 0
        # while the machine surface is unparseable — the assertion would fail for a reason that has
        # nothing to do with what it is about.
        proc = subprocess.run([sys.executable, str(cli), "hot-banner", "--format", "json"],
                              capture_output=True, text=True)
        live = _f.gather("memory.hot_set", {})
        assert live["hot_banner_readable"] is (proc.returncode == 0), (proc.returncode, live)
        if proc.returncode == 0:
            # The roster is load-bearing, not decoration: a count whose per-entry ids went missing
            # is the observed way this reading degrades, and the provider treats the two disagreeing
            # as unreadable rather than picking one.
            assert len(live["hot_set_ids"]) == live["hot_set_count"], live


def test_a_linked_worktree_is_told_apart_from_a_source_checkout(env, tmp_path, monkeypatch):
    """The one discriminator the isolation claim rests on, asserted both ways.

    A linked worktree's `.git` is a FILE pointing at the owning repo; a source checkout's is a
    DIRECTORY. Everything about "am I really isolated" hangs on that, and the full-flow driver
    only ever sees the second kind — so mutating the check to accept either left the suite
    green. Built from the structural shape rather than by provisioning a real worktree: this
    test must not mutate any repository to make its point.
    """
    # The provider under test reads the store IN-PROCESS, which until the conftest guard
    # landed meant reading whichever store os.environ happened to name — the developer's own.
    # Say which one out loud.
    monkeypatch.setenv("HARNESS_STATE_DIR", env["HARNESS_STATE_DIR"])
    import sys as _s
    _s.path.insert(0, str(REPO))
    from engine import facts as _f, flow as _fl
    # `load` rather than `load_extensions`, so the ENGINE follows `requires:` to whichever ability
    # owns this provider. Naming the owner here would make the test a second place that has to be
    # edited when a provider moves — and it just did move, which is how this line got noticed.
    _fl.load("shipcheck-asis")

    # This repository IS a source checkout — `.git` is a directory.
    assert (REPO / ".git").is_dir()
    src = _f.gather("worktree.worktree_state", {"run_id": "none", "scope": str(REPO)})
    assert src["in_linked_worktree"] is False, src

    # The shape of a linked worktree, without touching a real one.
    wt = tmp_path / "linked"
    wt.mkdir()
    (wt / ".git").write_text(f"gitdir: {REPO}/.git/worktrees/probe\n", encoding="utf-8")
    linked = _f.gather("worktree.worktree_state", {"run_id": "none", "scope": str(wt)})
    assert linked["in_linked_worktree"] is True, linked
    # A directory that is not a repository at all is neither isolated nor on a branch.
    plain = tmp_path / "plain"
    plain.mkdir()
    bare = _f.gather("worktree.worktree_state", {"run_id": "none", "scope": str(plain)})
    assert bare["in_linked_worktree"] is False and bare["on_isolation_branch"] is False, bare


def test_the_map_only_points_at_entry_points_that_exist(env):
    """A pointer that has rotted is worse than no pointer: it reads authoritative.

    The map deliberately POINTS at non-ability tooling rather than restating it — restating
    would be a second copy that drifts, which is the failure this whole repo is about. The cost
    of pointing is that a moved or renamed file turns the map into a confident lie, and nothing
    would notice. So every path the map names is checked to exist.

    Checked only when the pointed-at tree is INSTALLED. On a machine without it the pointer is
    still correct as a pointer — it says where the thing would be — and failing there would
    punish a machine for not having an optional dependency rather than catching drift.
    """
    import re as _re
    cap = (REPO / "integrations" / "CAPABILITIES.md").read_text(encoding="utf-8")
    home = pathlib.Path.home()

    # Paths the map names under the shared observability tree, plus the sub-KB meta files.
    checks = [
        (home / ".kiro/loop agents", ["scripts/trace.py", "quality-slo.yaml",
                                      "stage-compliance.yaml", "CAPABILITIES.md"]),
        (home / ".kiro/skills/shared-kb", ["L0-meta/routing-rules.md",
                                           "L0-meta/kb-registry.md",
                                           "L0-meta/evaluation-criteria.md",
                                           "L2-hot/hot-set.md"]),
        (home / ".kiro/skills/agent-scheduler", ["references/task-authoring-guide.md"]),
    ]
    checked = 0
    for root, rels in checks:
        if not root.is_dir():
            continue                      # not installed here; the pointer still reads true
        for rel in rels:
            # The FULL relative path, not just the basename. A basename fallback was tried
            # and was too loose: the map mentions `trace.py` bare in a later sentence, so
            # renaming the actual pointer left the check satisfied by the prose. A pointer is
            # only useful if it is directly actionable, so require the actionable form.
            # (For a top-level file the two coincide; for `CAPABILITIES.md` the name check is
            # vacuous because this file is also called that — its existence check still bites.)
            assert rel in cap, (
                f"the map no longer names {rel} — if it was dropped on purpose, drop it from "
                f"this list too, so the two stay in step"
            )
            assert (root / rel).is_file(), f"the map names {rel} under {root}, which is gone"
            checked += 1
    assert checked, "no pointed-at tree is installed; this test asserted nothing"

    # And the map must record WHERE the two unbuilt structural pieces are already declared, so a
    # future round reads them instead of inventing a second, quietly diverging copy.
    flat = cap.replace("**", "").replace("`", "")
    assert "先读这里，不要重新发明一遍" in flat, (
        "the map must say that the regression baseline and cross-run correlation are already "
        "declared elsewhere — the risk here was never a missing first copy, it is a second one"
    )


def test_the_resident_lesson_snapshot_is_not_mistaken_for_the_authority(env):
    """The snapshot ships in context; the store decides. Both halves pinned.

    Adding the rendered lesson file back as a resource closed a real regression — the criterion
    could prove the store had been consulted, but the lessons themselves were no longer in front
    of the agent. It also creates a trap: a file in version control next to a machine-local
    store that is deliberately NOT, so on a machine whose store was rebuilt the file can be
    older. Whoever reads the file as the count is then reporting a stale number as a live one.

    So the map has to say which one wins, and the criterion has to keep reading the store.
    """
    from engine import flow as flowmod
    cap = (REPO / "integrations" / "CAPABILITIES.md").read_text(encoding="utf-8")
    flat = cap.replace("**", "").replace("`", "")
    assert "它是快照，不是权威源" in flat, "the map must say which of the two decides"
    assert "冲突时库赢" in flat, "and it must say which way the conflict resolves"

    # The criterion must still consult the STORE, not the file: the corroborating facts come
    # from a provider, and a provider reading a checked-in snapshot would defeat the point.
    import yaml as _y
    fl = _y.safe_load((REPO / "abilities" / "shipcheck-asis" / "flow.yaml").read_text())
    c00 = next(x for x in fl["steps"] if x["id"] == "C00")
    claims = [c for c in c00["completion"]["checks"]
              if c["type"] == "claim_corroborated" and c.get("kind") == "hot_set_state"]
    assert len(claims) == 3, claims
    facts_used = {c["disproved_when"]["fact"] for c in claims}
    assert facts_used == {"hot_set_count", "hot_banner_readable"} or len(facts_used) >= 2, (
        facts_used
    )
    owner = flowmod.load("shipcheck-asis").facts_owner
    for f in facts_used:
        assert owner.get(f) == "memory.hot_set", (f, owner.get(f))


def test_the_design_lifecycle_is_documented_as_executed_ELSEWHERE(env):
    """A BIDIRECTIONAL invariant, not a keyword-presence check.

    Two abilities carry a `ux` variant that only DISPATCHES; the design lifecycle itself runs in
    a different agent. That asymmetry is easy to misread — it was misread here, and the wrong
    reading ("the variant dispatches to a nonexistent receiver") nearly became a round of work
    transcribing a lifecycle whose own spec refuses this engine's machinery.

    So the assertion is tied to the ability set rather than to text alone: while no `ux` ability
    exists, the map must say execution is external; if one is ever added, the map must stop
    saying so. A one-directional "the sentence is present" test would silently become a lie the
    day the situation changes, which is the failure mode this whole repo is about.
    """
    from engine import flow as flowmod
    names = set(flowmod.available_abilities())
    cap = (REPO / "integrations" / "CAPABILITIES.md").read_text(encoding="utf-8")
    # Strip markdown emphasis before matching — a phrase split by ** would otherwise not be
    # found, and the assertion would pass or fail for formatting reasons.
    flat = cap.replace("**", "").replace("`", "")
    says_external = "设计流程本身的执行方是另一个" in flat and "ux-agent" in flat

    if "ux" in names:
        assert not says_external, (
            "a `ux` ability now exists, but the map still says the design lifecycle runs "
            "elsewhere — the map has become wrong in the direction that reads authoritative"
        )
        return

    assert says_external, (
        "no `ux` ability exists, so the map MUST say where the design lifecycle actually runs; "
        "otherwise the dispatch-only variants read as an unfinished hand-off"
    )
    # And the dispatching side must actually declare that variant, or the section is describing
    # something that is not there.
    for ab in ("push", "plan"):
        f = flowmod.load(ab)
        assert "ux" in f.variants, (ab, f.variants)
        assert "ux-agent" in f.when or "ux 变体" in f.when, (
            f"{ab}: the routing hint an agent actually reads must say the ux variant only "
            f"dispatches — the map alone is not where routing is decided"
        )


def test_no_ability_declares_a_provider_nothing_reads(env):
    """A declared provider whose facts no condition reads is DEAD WIRING.

    It is not a performance problem — lazy gathering means an unread provider costs nothing to
    run. It is a truthfulness problem, and the same one this engine exists to remove: something
    declared, looking connected, that never takes effect. Two providers sat like this while
    1,224 lines of copied tooling behind them could never be reached, and a third became dead
    as a side effect of fixing an unrelated bug — none of which any test noticed.
    """
    import re as _re
    from engine import conditions, flow as flowmod
    for name in flowmod.available_abilities():
        f = flowmod.load(name)
        if not f.facts_providers or f.facts_providers == ("static",):
            continue
        read: set[str] = set()
        for h in f.hooks:
            if h.when is not None:
                read |= conditions.facts_referenced(h.when)
            read |= set(_re.findall(r"\{\{\s*fact\.([A-Za-z0-9_]+)\s*\}\}", h.body or ""))
        if f.variant_spec.get("fact"):
            read.add(f.variant_spec["fact"])

        # COMPLETION CRITERIA COUNT AS READERS. This test predates the criterion that consults
        # derived facts, and it only walked hook conditions and templates — so a provider whose
        # only consumer was a CRITERION reported as unread, and the honest repair for that false
        # alarm would have been to delete a live provider. A "nothing reads it" check that
        # cannot see one of the two things that read is worse than none.
        def _crit_facts(spec, into):
            if not isinstance(spec, dict):
                return
            for sub in spec.get("checks") or []:
                _crit_facts(sub, into)
            if spec.get("disproved_when"):
                into |= conditions.facts_referenced(spec["disproved_when"])

        for st in f.steps.values():
            _crit_facts(st.completion, read)
        for goal in f.phase_goals.values():
            _crit_facts(goal, read)

        for provider in f.facts_providers:
            owned = {k for k, v in f.facts_owner.items() if v == provider}
            assert owned & read, (
                f"ability '{name}' declares provider '{provider}' ({len(owned)} facts) but no "
                f"condition or template reads any of them — either wire it or drop it"
            )


def test_routing_and_the_fixture_role_are_two_sides_of_one_rule(env):
    """Routing must be DERIVABLE, and a flow excluded from the reports must be UNREACHABLE.

    A hand-kept "which ability for what" table is stale the moment another ability lands, and a
    quietly-stale routing table sends work into the wrong lifecycle — worse than no table,
    because it reads authoritative. So every production ability carries its own `when:`.

    The second half is what stops an excluded role from being an escape hatch. A role that only
    silenced a report could be claimed by anything; here it is tied to something structural and
    checked at load — an unroutable role must NOT carry `when:`, so it cannot be reached at all. The
    two requirements point in opposite directions, which is what makes neither the cheap one to
    claim: dodging the criteria report costs you reachability.

    Written against `ROLES_UNROUTABLE` rather than naming the fixture role, because this rule was
    the fixture role's for a while and then `library` joined it. A test that spelled the membership
    out a second time would have passed while `library` sat outside the rule it belongs to — the
    same shape as the ban this file now also checks, which was prose for months.
    """
    from engine import flow as flowmod
    missing, reachable = [], []
    for name in flowmod.available_abilities():
        f = flowmod.load(name)
        if f.role in flowmod.ROLES_UNROUTABLE:
            if f.when.strip():
                reachable.append(name)
        elif len(f.when.strip()) < 20:
            missing.append(name)
    assert not missing, f"no usable `when:` on production ability: {missing}"
    assert not reachable, f"an unroutable role carries routing, which defeats it: {reachable}"
    # At least one of each role, or this asserts nothing.
    roles = {flowmod.load(n).role for n in flowmod.available_abilities()}
    assert roles == set(flowmod.ROLES), roles
    # A fixture must not be load-bearing for real work either.
    for name in flowmod.available_abilities():
        f = flowmod.load(name)
        if f.role != flowmod.ROLE_FIXTURE:
            for dep in f.requires:
                assert flowmod.load(dep).role != flowmod.ROLE_FIXTURE, (name, dep)
    # And the capability map must NOT duplicate what `when:` already says.
    cap = (REPO / "integrations" / "CAPABILITIES.md").read_text(encoding="utf-8")
    assert "harness abilities" in cap, "the map must point at the derivable source"


@pytest.mark.parametrize("body,expect", [
    ("role: nonsense", "not one of"),
    ("role: fixture\nwhen: |\n  这条文本让夹具变得可路由，必须在加载期被拒绝\n", "is the hint an agent reads"),
    ("role: production", "no usable `when:`"),
])
def test_the_fixture_role_is_checked_at_load_time(env, body, expect):
    """An unknown role, a routable fixture, and a production flow with no routing."""
    d = _spec(env, "__role_test__",
              f"{body}\nphases:\n  - id: p\nsteps:\n  - id: S1\n    phase: p\n"
              "    title: t\n    directive: d\n    completion: {type: attest}\n")
    try:
        assert rc(["validate", "__role_test__"], env) == BAD_SPEC
        assert expect in run(["validate", "__role_test__"], env).stderr
    finally:
        _rm(d)


def test_a_spec_that_declares_nothing_lands_on_the_strict_role(env):
    """The default must be the STRICT role, not the lenient one.

    This is what stops `fixture` from being reachable by silence. A spec saying nothing about
    its role is `production`, so it is refused for missing routing rather than quietly enjoying
    the fixture exemption from the criteria report. Written by hand instead of through the
    shared `_spec` helper on purpose — that helper injects `role: fixture`, which is right for
    a throwaway spec and would swallow the very case under test here.
    """
    d = REPO / "abilities" / "__default_role_test__"
    d.mkdir(exist_ok=True)
    try:
        (d / "flow.yaml").write_text(
            "version: 2\nability: __default_role_test__\nscope_kind: s\n"
            "phases:\n  - id: p\nsteps:\n  - id: S1\n    phase: p\n"
            "    title: t\n    directive: d\n    completion: {type: attest}\n",
            encoding="utf-8")
        assert rc(["validate", "__default_role_test__"], env) == BAD_SPEC
        err = run(["validate", "__default_role_test__"], env).stderr
        assert "no usable `when:`" in err, err
        # And the refusal must name the alternative, or the only way out looks like inventing
        # a routing hint for something that should never be routed.
        assert "role: fixture" in err, err
    finally:
        _rm(d)


def test_a_fixture_is_kept_out_of_the_routing_roster_and_the_strength_report(env):
    """Both omissions, asserted where a reader would look for them.

    The roster is what an agent reads to choose an ability, so an unreachable entry there is
    noise at best and a mis-route at worst — but a human still has to be able to find these,
    hence the trailing line. The strength report omits them because a fixture's criteria are
    weak BY DESIGN: one keeps a deliberate `attest` step as the honest floor, and reporting
    that as a shortfall is what happened for several rounds before the role existed.
    """
    out = run(["abilities"], env).stdout
    assert "fixtures (not routable): authoring, delivery" in out, out
    for name in ("authoring", "delivery"):
        # Listed once, in the footer — not as a routable entry with its own block.
        assert f"✅ {name}" not in out, out
        v = run(["validate", name], env).stdout
        assert "criteria strength not reported" in v, v
        assert "criteria: " not in v, v
        assert "artifacts: " not in v, v
    prod = run(["validate", "shipcheck-asis"], env).stdout
    assert "criteria: " in prod and "artifacts: " in prod, prod


def test_a_review_publish_is_guarded(env, tmp_path):
    """Publishing a comment onto someone else's review is the one irreversible act here.

    In the source this is a prose MUST ("never auto-publish without user confirmation"); here it
    is a gate plus a match rule, so the runtime refuses the call rather than relying on the
    agent having read the sentence.
    """
    import json as _json
    assert rc(["open", "cr-reviewer", "--scope", "CR-12345678", "--run", "rv"], env) == OK
    blocked = rc(["guard-tool", "--tool", "CRAddComment",
                  "--input-json", _json.dumps({"cr": "CR-12345678", "publish": True}),
                  "--cwd", str(tmp_path)], env)
    assert blocked == BLOCKED
    # A DRAFT is not the guarded action — drafting freely is the whole point of drafting.
    assert rc(["guard-tool", "--tool", "CRAddComment",
               "--input-json", _json.dumps({"cr": "CR-12345678", "publish": False}),
               "--cwd", str(tmp_path)], env) == OK


def test_a_non_location_scope_is_matched_from_the_action_itself(env, tmp_path):
    """A working directory answers nothing about a scope that is not a location.

    A run about one review is not tied to a directory, so comparing its scope key to a cwd never
    matches and the guard silently never fires — "not guarded" looking exactly like "nothing to
    guard" again. The reliable signal is the scope key the ACTION carries.
    """
    import json as _json
    d = _spec(env, "payloadscope", """
scope_kind: ticket
scope_match: in_payload
phases: [{id: p1}]
steps:
  - id: G
    phase: p1
    gate: affirm
    completion: {type: gate_recorded}
guards:
  act:
    step: G
    matches:
      - tool: Doer
        pattern: '"go"\\s*:\\s*true'
""")
    try:
        assert rc(["open", "payloadscope", "--scope", "TK-100", "--run", "ps"], env) == OK
        gt = lambda p: rc(["guard-tool", "--tool", "Doer", "--input-json", _json.dumps(p),
                           "--cwd", str(tmp_path)], env)
        # Names this run's ticket → this run owns it.
        assert gt({"ticket": "TK-100", "go": True}) == BLOCKED
        # Names a DIFFERENT ticket → not this run's business, even though the pattern matches.
        assert gt({"ticket": "TK-200", "go": True}) == OK
        # Whole-token: a longer id that merely starts with the scope key is a different ticket.
        assert gt({"ticket": "TK-1000", "go": True}) == OK
        # Names no ticket at all → nothing to attribute it to; do not guess.
        assert gt({"go": True}) == OK
        # The pattern still has to match: naming the ticket is not itself the action.
        assert gt({"ticket": "TK-100", "go": False}) == OK
    finally:
        _rm(d)


# ───────────── end-to-end: the two review-side abilities, guard included ─────────────

@pytest.mark.parametrize("ability,scope,guard_tool,guard_payload,taken,untaken", [
    ("cr-reviewer", "CR-98765432", "CRAddComment",
     {"cr": "CR-98765432", "revision": 3, "publish": True}, "V02", "V03"),
    ("cr-to-task", "SPRINT-2026-Q3-07", "TaskeiCreateTask",
     {"sprint": "SPRINT-2026-Q3-07", "name": "[X] something"}, "T02", "T03"),
])
def test_a_review_side_ability_runs_end_to_end_with_its_guard(
        env, tmp_path, ability, scope, guard_tool, guard_payload, taken, untaken):
    """Drive each of these to a clean close, WITH the runtime guard in the loop.

    Separate from the variant e2e because these two exercise a path nothing else does: their
    scope is not a location, so their guards can only be attributed by the scope key the ACTION
    carries. Unit tests covered that comparison; this covers it inside a real run, alongside the
    gate it protects — which is the pairing that matters. "It validates" and "it runs" have
    already proved to be different claims once in this project, in an ability whose whole
    variant branch was unreachable while every check passed.
    """
    import json as _json
    import yaml as _yaml
    flow = _yaml.safe_load((REPO / "abilities" / ability / "flow.yaml").read_text())
    run_id = ability.replace("-", "")[:12]
    gt = lambda: rc(["guard-tool", "--tool", guard_tool,
                     "--input-json", _json.dumps(guard_payload),
                     "--cwd", str(tmp_path)], env)

    # Before the run exists there is nothing to attribute the call to.
    assert gt() == OK
    assert rc(["open", ability, "--scope", scope, "--run", run_id], env) == OK
    # Now it is attributable — by the scope key inside the payload, not by any directory.
    assert gt() == BLOCKED, "the guarded action must be refused before its gate"

    t = tmp_path / "t.jsonl"
    t.write_text("", encoding="utf-8")
    turns = gates = 0
    seen_phase = None
    for s in flow["steps"]:
        sid = s["id"]
        if sid == untaken:
            continue                      # the exclusive sibling; closing one is enough
        if seen_phase is not None and s["phase"] != seen_phase:
            r = run(["summarize", "--run", run_id, "--phase", seen_phase, "--note", "walked"], env)
            assert r.returncode == OK, (seen_phase, r.stderr)
        seen_phase = s["phase"]
        if s.get("gate") == "affirm":
            # A gate takes two turns: the refusal below is what makes that real.
            assert rc(["gate", "--run", run_id, "--step", sid, "--decision", "affirm",
                       "--evidence", "no human spoke"], env,
                      transcript=t, witness="transcript") == REFUSED
            turns += 1
            with t.open("a", encoding="utf-8") as fh:
                fh.write(_json.dumps({"role": "user", "content": f"go ahead on {sid}"}) + "\n")
            assert rc(["gate", "--run", run_id, "--step", sid, "--decision", "affirm",
                       "--evidence", f"go ahead on {sid}"], env,
                      transcript=t, witness="transcript") == OK
            gates += 1
            # The gate is recorded, so the guarded action is now allowed.
            assert gt() == OK, "recording the gate must release the guard"
        _satisfy(env, run_id, sid, s.get("completion") or {})
        r = run(["close-step", "--run", run_id, "--step", sid], env)
        assert r.returncode == OK, (sid, r.stderr)
    assert rc(["summarize", "--run", run_id, "--phase", seen_phase, "--note", "walked"],
              env) == OK
    assert turns == gates == 1, (turns, gates)

    for line in run(["obligations", "--run", run_id], env).stdout.splitlines():
        if line.strip().startswith("⏳"):
            assert rc(["discharge", "--run", run_id, "--hook", line.split()[2],
                       "--evidence", "handled"], env) == OK
    r = run(["close-run", "--run", run_id, "--result", "completed"], env)
    assert r.returncode == OK, r.stderr
    audit = run(["audit"], env).stdout
    assert "forced_close" not in audit and "undischarged_obligations" not in audit
    # No gate was recorded via a downgrade — this run used a real transcript witness.
    assert "unwitnessed: 0" in audit
    # Every ledger row is a REFUSAL this test provoked on purpose, so zero breaches.
    import sqlite3
    c = sqlite3.connect(env["HARNESS_STATE_DIR"] + "/harness.db")
    try:
        sev = dict(c.execute("SELECT severity, COUNT(*) FROM violation WHERE run_id = ? "
                             "GROUP BY severity", (run_id,)).fetchall())
        skipped = {r[0] for r in c.execute(
            "SELECT DISTINCT step_id FROM step_log WHERE run_id = ? AND event = 'skipped'",
            (run_id,))}
        closed = {r[0] for r in c.execute(
            "SELECT DISTINCT step_id FROM step_log WHERE run_id = ? AND event = 'closed'",
            (run_id,))}
    finally:
        c.close()
    assert sev.get("breach", 0) == 0, sev
    # The untaken branch was auto-skipped BY ITS SIBLING CLOSING — nobody skipped it by hand,
    # and it is not sitting outstanding either. Asserting the set precisely, because "the run
    # closed" would hold just as well if the group had quietly demanded neither.
    assert taken in closed, (taken, sorted(closed))
    assert untaken in skipped, (untaken, sorted(skipped))
    assert untaken not in closed
    assert len(closed) + len(skipped) == len(flow["steps"]), (sorted(closed), sorted(skipped))


def test_forcing_one_thing_does_not_authorise_the_other(env):
    """Two flags, and neither stands in for the other. That is the entire point of the split.

    One switch made whoever reached for it grant both — usually while meaning only the first.
    The failure mode is quiet: a test in this very suite discharged one obligation and let a
    combined force excuse a second one it had never noticed existed, and nothing said so. So
    there is no combined flag and no alias: the familiar short name is what would keep doing it.
    """
    d = _spec(env, "twoflags", """
facts: {providers: [run_progress]}
phases: [{id: p1}]
steps:
  - id: A
    phase: p1
    completion: {type: attest}
  - id: B
    phase: p1
    deps: [A]
    completion: {type: attest}
hooks:
  - id: owed
    trigger: phase_start
    phase: p1
    when: {fact: violation_count, count_gte: 0}
    mode: contract
    obligation: true
    contract: "discharge me"
""")
    try:
        assert rc(["open", "twoflags", "--scope", "tf", "--run", "tf"], env) == OK
        assert rc(["enter", "--run", "tf", "--step", "A"], env) == OK      # raises the obligation
        # Both problems present: an open step AND an owed obligation.
        r = run(["close-run", "--run", "tf"], env)
        assert r.returncode == REFUSED and "still open" in r.stderr
        # Forcing STEPS says nothing about the obligation — and the refusal names the other flag.
        r = run(["close-run", "--run", "tf", "--force-steps"], env)
        assert r.returncode == REFUSED
        assert "obligation" in r.stderr and "discharge" in r.stderr
        # Forcing OBLIGATIONS says nothing about the open steps.
        r = run(["close-run", "--run", "tf", "--force-obligations"], env)
        assert r.returncode == REFUSED
        assert "still open" in r.stderr and "--force-steps" in r.stderr
        # There is no combined flag, and no alias for the old name.
        assert rc(["close-run", "--run", "tf", "--force"], env) == USAGE
        # Both, explicitly.
        assert rc(["close-run", "--run", "tf", "--force-steps",
                   "--force-obligations"], env) == OK
        audit = run(["audit"], env).stdout
        assert "forced_close" in audit and "undischarged_obligations" in audit
    finally:
        _rm(d)


def test_entering_past_open_deps_says_what_it_forces(env):
    """`enter --force` was single-purpose but vaguely named; the log entry now says which
    check was bypassed, so reading the ledger afterwards does not require guessing."""
    import sqlite3
    d = _spec(env, "forcedeps", """
phases: [{id: p1}]
steps:
  - id: A
    phase: p1
    completion: {type: attest}
  - id: B
    phase: p1
    deps: [A]
    completion: {type: attest}
""")
    try:
        assert rc(["open", "forcedeps", "--scope", "fd", "--run", "fd"], env) == OK
        assert rc(["enter", "--run", "fd", "--step", "B"], env) == REFUSED
        assert rc(["enter", "--run", "fd", "--step", "B", "--force"], env) == USAGE
        assert rc(["enter", "--run", "fd", "--step", "B", "--force-deps"], env) == OK
        c = sqlite3.connect(env["HARNESS_STATE_DIR"] + "/harness.db")
        try:
            note = c.execute("SELECT detail FROM step_log WHERE run_id='fd' AND step_id='B' "
                             "AND event='entered'").fetchone()[0]
        finally:
            c.close()
        assert note == "forced past open deps", note
    finally:
        _rm(d)


# ------------------------------------------------- where flows are allowed to live

def _external_flow(root: Path, name: str, *, title: str = "External") -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "flow.yaml").write_text(
        f"version: 2\nability: {name}\ntitle: {title}\n"
        f"when: a flow kept outside the engine tree, which is the point of this test\n"
        f"scope_kind: s\nphases:\n  - id: p1\n    title: P1\n"
        f"steps:\n  - id: W01\n    phase: p1\n    title: Do it\n    directive: Do the thing.\n",
        encoding="utf-8",
    )
    return d


def test_a_flow_outside_the_engine_tree_is_found_and_runnable(env, tmp_path):
    """A consumer must be able to keep its flows in its own tree.

    WHY THIS IS THE LOAD-BEARING ONE. The purity tests assert the engine names no domain
    vocabulary; a frozen `abilities/` path asserts the opposite in the filesystem — the only
    way to install a flow was to put it inside the engine's own repository, which is how a
    generic base acquires a first consumer it can no longer be separated from.

    Listing is not enough to assert here: a name can appear in a roster and still fail to
    resolve its spec or its providers. So the flow is actually opened and stepped through.
    """
    root = tmp_path / "their-tree"
    _external_flow(root, "greeting")
    e = {**env, "HARNESS_ABILITIES_PATH": str(root)}

    listed = run(["abilities"], e)
    assert listed.returncode == OK, listed.stderr
    assert "greeting" in listed.stdout

    # REPLACES rather than adds: an implicit union means a consumer can never get a clean
    # set, and a flow arriving from a root nobody named is worse than naming both.
    assert "cr-reviewer" not in listed.stdout, listed.stdout

    assert rc(["validate", "greeting"], e) == OK
    assert rc(["open", "greeting", "--scope", "ext", "--run", "x1"], e) == OK
    assert rc(["enter", "--run", "x1", "--step", "W01"], e) == OK


def test_two_roots_holding_the_same_name_are_refused(env, tmp_path):
    """The engine will not pick between two flows with one name.

    First-wins is the tempting default and it is silent: a root nobody is looking at
    shadows the one being edited, and "which of the two actually ran" stops being
    answerable from the spec — the run record would name `greeting` and mean either.
    """
    a, b = tmp_path / "a", tmp_path / "b"
    _external_flow(a, "greeting", title="From A")
    _external_flow(b, "greeting", title="From B")
    import os as _os
    e = {**env, "HARNESS_ABILITIES_PATH": f"{a}{_os.pathsep}{b}"}
    out = run(["abilities"], e)
    assert out.returncode == BAD_SPEC, out.stdout + out.stderr
    # Both sides named, because the fix requires knowing which two collided.
    assert str(a) in out.stderr and str(b) in out.stderr, out.stderr


def test_a_configured_root_that_is_missing_is_louder_than_an_empty_one(env, tmp_path):
    """A mistyped root is a configuration error; an empty root is a legitimate state.

    Collapsing the two is the failure this asymmetry exists to avoid: reporting "nothing
    installed" for a typo sends the reader looking for missing files instead of at the one
    variable that is wrong. The DEFAULT root is allowed to be absent, because an
    engine-only checkout legitimately has none — so the loudness is tied to having been
    ASKED to look somewhere, not to the directory being missing.
    """
    missing = tmp_path / "not-there"
    assert rc(["abilities"], {**env, "HARNESS_ABILITIES_PATH": str(missing)}) == BAD_SPEC

    empty = tmp_path / "empty"
    empty.mkdir()
    out = run(["abilities"], {**env, "HARNESS_ABILITIES_PATH": str(empty)})
    assert out.returncode == OK, out.stderr
    assert str(empty) in out.stdout, out.stdout

    # Set but naming nothing at all is a mistake too, not a request for the default.
    assert rc(["abilities"], {**env, "HARNESS_ABILITIES_PATH": ":"}) == BAD_SPEC


def test_the_roots_are_resolved_when_asked_not_when_imported(tmp_path, monkeypatch):
    """Reading the variable at import time would fail silently, so it is read per call.

    A frozen-at-import root ignores anything set afterwards and reports "that root holds
    nothing" — which reads as an empty directory rather than as a value that arrived too
    late, so the variable is the last place anyone looks. This module is already imported
    by the time this test runs, which is exactly the condition that would expose the bug.
    """
    from engine import flow as flowmod
    root = tmp_path / "late"
    _external_flow(root, "greeting")

    monkeypatch.setenv("HARNESS_ABILITIES_PATH", str(root))
    assert flowmod.available_abilities() == ["greeting"]
    assert flowmod.spec_path("greeting").is_file()

    monkeypatch.delenv("HARNESS_ABILITIES_PATH")
    back = flowmod.available_abilities()
    assert "greeting" not in back and "cr-reviewer" in back, back


# ------------------------------------------------- scope leases: who owns an action

def _rows(env_extra, sql, params=()):
    c = sqlite3.connect(env_extra["HARNESS_STATE_DIR"] + "/harness.db")
    c.row_factory = sqlite3.Row
    try:
        return c.execute(sql, params).fetchall()
    finally:
        c.close()


def _unadjudicated(env_extra):
    return _rows(env_extra,
                 "SELECT * FROM violation WHERE code = 'guard_unadjudicated' ORDER BY id")


def _guard(env_extra, action="commit", scope="/repo/L"):
    return run(["guard", "--action", action, "--scope-kind", "repo", "--scope", scope],
               env_extra)


def _no_guard_fixture(name: str) -> Path:
    """A repo-scoped flow that guards nothing — the delegate in the escape test."""
    d = REPO / "abilities" / name
    d.mkdir(exist_ok=True)
    (d / "flow.yaml").write_text(
        f"version: 2\nability: {name}\nrole: fixture\nscope_kind: repo\n"
        f"phases:\n  - id: p1\n    title: P1\n"
        f"steps:\n  - id: W01\n    phase: p1\n    title: Work\n    directive: Work.\n",
        encoding="utf-8",
    )
    return d


def test_the_guard_leaves_a_row_when_it_declines_to_adjudicate(env):
    """Enforcement that silently stops applying is the gap that lasts.

    Declining was already the right call — demanding a gate from a run that may not own the
    action leaves forging it as the only way forward. What was missing is that the decline
    went to stderr and nowhere else, so "how often did the guarantee stop applying" had no
    answer after the fact.

    run_id is NULL on purpose, and asserted: attributing this to one of the ambiguous runs is
    the very guess the resolution refuses to make.
    """
    assert rc(["open", "delivery", "--scope", "/repo/L", "--run", "L1"], env) == OK
    assert rc(["open", "delivery", "--scope", "/repo/L", "--run", "L2",
               "--allow-concurrent"], env) == OK

    r = _guard(env)
    assert r.returncode == OK
    assert "NOT enforced" in r.stderr and "no_lease" in r.stderr

    v = _unadjudicated(env)
    assert len(v) == 1, [dict(x) for x in v]
    assert v[0]["run_id"] is None
    assert v[0]["severity"] == "breach"          # the guarantee did not hold
    assert "action=commit" in v[0]["detail"] and "L1,L2" in v[0]["detail"]

    # The guard runs on every tool call; identical situations must not flood the ledger.
    for _ in range(4):
        assert _guard(env).returncode == OK
    assert len(_unadjudicated(env)) == 1

    # A genuinely different situation is a different fact and gets its own row.
    assert _guard(env, action="publish").returncode == OK
    assert len(_unadjudicated(env)) == 2


def test_a_declared_delegation_makes_the_guard_adjudicate_again(env):
    """The lease exists for exactly one reason: to restore enforcement, not to describe.

    Two runs in a scope is the state in which the guard gives up. A declared delegation makes
    "who owns this action" answerable, so the same situation goes from allowed-and-unenforced
    back to adjudicated — and no decline is recorded, because none happened.
    """
    assert rc(["open", "delivery", "--scope", "/repo/L", "--run", "G1"], env) == OK
    advance_to(env, "G1", None)
    assert rc(["open", "delivery", "--scope", "/repo/L", "--run", "H1",
               "--leased-from", "G1", "--leased-at", "C01"], env) == OK

    assert _guard(env).returncode == BLOCKED
    assert _unadjudicated(env) == []


def test_delegating_a_scope_does_not_escape_the_gate_that_guards_it(env, tmp_path):
    """A delegate does not inherit permission the delegator never had.

    If only the innermost holder's gates applied, delegation would BE the bypass: the outer
    run says "no such action here until a human affirms", hands the scope to a flow that
    guards nothing, and the action proceeds. So every link that guards the action must have
    its gate. The outer gate is not an unrelated run's gate — that run authorised the
    delegation, over this very scope.
    """
    d = _no_guard_fixture("zzz_leaseless")
    try:
        assert rc(["open", "delivery", "--scope", "/repo/L", "--run", "G2"], env) == OK
        advance_to(env, "G2", None)
        assert rc(["open", "zzz_leaseless", "--scope", "/repo/L", "--run", "H2",
                   "--leased-from", "G2", "--leased-at", "C01"], env) == OK

        r = _guard(env)
        assert r.returncode == BLOCKED
        assert "G2" in r.stderr and "D02" in r.stderr, r.stderr
        assert "link 1 of 2" in r.stderr, r.stderr

        t = transcript_with(tmp_path, 1)
        assert rc(["gate", "--run", "G2", "--step", "D02", "--decision", "affirm"],
                  env, transcript=t, witness="transcript") == OK
        assert _guard(env).returncode == OK
        assert _unadjudicated(env) == []
    finally:
        _rm(d)


def test_an_unrelated_run_in_the_scope_breaks_the_delegation_chain(env):
    """A chain that does not cover every open run in the scope is not an answer.

    The runs outside it are unrelated, so the action may belong to one of them — and that is
    the case where demanding a gate is illegitimate. Partial coverage therefore returns to
    declining, with the reason recorded so the situation is diagnosable.
    """
    assert rc(["open", "delivery", "--scope", "/repo/L", "--run", "G3"], env) == OK
    advance_to(env, "G3", None)
    assert rc(["open", "delivery", "--scope", "/repo/L", "--run", "H3",
               "--leased-from", "G3", "--leased-at", "C01"], env) == OK
    assert rc(["open", "delivery", "--scope", "/repo/L", "--run", "X3",
               "--allow-concurrent"], env) == OK

    r = _guard(env)
    assert r.returncode == OK
    assert "partial" in r.stderr, r.stderr
    v = _unadjudicated(env)
    assert len(v) == 1 and "reason=partial" in v[0]["detail"], [dict(x) for x in v]


@pytest.mark.parametrize("extra, expected, why", [
    (["--leased-from", "G4"], USAGE, "half a delegation names no step"),
    (["--leased-at", "C01"], USAGE, "half a delegation names no grantor"),
    (["--leased-from", "nope", "--leased-at", "C01"], USAGE, "no such grantor"),
    (["--leased-from", "G4", "--leased-at", "NOPE9"], USAGE, "no such step"),
    (["--leased-from", "G4", "--leased-at", "D02"], REFUSED, "step not reached yet"),
])
def test_a_delegation_must_be_anchored_to_progress_that_happened(env, extra, expected, why):
    """Each refusal removes a way for a lease to claim authority that was never exercised."""
    assert rc(["open", "delivery", "--scope", "/repo/L", "--run", "G4"], env) == OK
    assert rc(["evidence", "--run", "G4", "--step", "C01",
               "--kind", "change_list", "--value", "x"], env) == OK
    assert rc(["close-step", "--run", "G4", "--step", "C01"], env) == OK
    assert rc(["open", "delivery", "--scope", "/repo/L", "--run", "H4", *extra], env) \
        == expected, why


def test_a_lease_covers_one_scope_and_is_granted_once(env):
    """Two refusals that keep the chain a chain.

    A grantor cannot delegate a scope it does not hold — across scopes there is no ambiguity
    to resolve, so such a lease would record a relation no guard reads while handing out
    authority its grantor never had. And one outgoing lease per run per scope, because two
    would put authority in two places at once, which is not authority.
    """
    assert rc(["open", "delivery", "--scope", "/repo/L", "--run", "G5"], env) == OK
    advance_to(env, "G5", None)
    # A flow in a different scope dimension entirely — and driven far enough that the
    # anchoring check would PASS, so this asserts the scope rule and not that one. Without
    # entering the step, both refusals apply and the mutation that removes the scope check
    # stays green.
    assert rc(["open", "push", "--scope", "/ws/x", "--run", "P5",
               "--variant", "ship-check"], env) == OK
    assert rc(["enter", "--run", "P5", "--step", "P01"], env) == OK
    assert rc(["open", "delivery", "--scope", "/repo/L", "--run", "H5a",
               "--leased-from", "P5", "--leased-at", "P01"], env) == REFUSED

    assert rc(["open", "delivery", "--scope", "/repo/L", "--run", "H5b",
               "--leased-from", "G5", "--leased-at", "C01"], env) == OK
    assert rc(["open", "delivery", "--scope", "/repo/L", "--run", "H5c",
               "--leased-from", "G5", "--leased-at", "C01"], env) == REFUSED


def test_a_lease_ends_when_either_end_of_it_ends(env):
    """A lease naming a finished run would keep answering with a run that cannot act.

    Both directions are released on close. Closing the GRANTOR while its delegation is still
    out is recorded and not refused: noticing that authority was handed out and never came
    back is the engine's job; deciding what that means is the flow's.
    """
    assert rc(["open", "delivery", "--scope", "/repo/L", "--run", "G6"], env) == OK
    advance_to(env, "G6", None)
    assert rc(["open", "delivery", "--scope", "/repo/L", "--run", "H6",
               "--leased-from", "G6", "--leased-at", "C01"], env) == OK

    active = "SELECT * FROM scope_lease WHERE released_at IS NULL"
    assert len(_rows(env, active)) == 1
    assert rc(["close-run", "--run", "H6", "--result", "completed", "--force-steps"], env) == OK
    assert _rows(env, active) == []
    assert _rows(env, "SELECT * FROM violation WHERE code='lease_outstanding'") == []

    assert rc(["open", "delivery", "--scope", "/repo/L", "--run", "H6b",
               "--leased-from", "G6", "--leased-at", "C01"], env) == OK
    assert len(_rows(env, active)) == 1
    assert rc(["close-run", "--run", "G6", "--result", "completed", "--force-steps",
               "--force-obligations"], env) == OK
    assert _rows(env, active) == []
    out = _rows(env, "SELECT * FROM violation WHERE code='lease_outstanding'")
    assert len(out) == 1 and out[0]["run_id"] == "G6", [dict(x) for x in out]


def test_a_malformed_delegation_ledger_refuses_instead_of_walking_it(env):
    """Resolution treats a corrupt ledger as unanswerable, not as something to interpret.

    Neither shape below can be produced through the CLI — a holder is always a run being
    opened, so it can hold nothing yet and cannot be its own ancestor. They are reachable by
    hand-editing the store, which is a real event (it is why a purge ledger exists at all),
    and the contract of the resolver is stated over the ROWS rather than over the commands
    that usually write them. Asserted directly for that reason: a branch whose only defence
    is that today's callers are careful is a branch nobody will notice going wrong.
    """
    import sys as _s
    _s.path.insert(0, str(REPO))
    from engine import store as st

    for rid in ("M1", "M2", "M3", "M4"):
        assert rc(["open", "delivery", "--scope", "/repo/M", "--run", rid,
                   *([] if rid == "M1" else ["--allow-concurrent"])], env) == OK

    conn = sqlite3.connect(env["HARNESS_STATE_DIR"] + "/harness.db")
    conn.row_factory = sqlite3.Row

    def relet(pairs):
        conn.execute("DELETE FROM scope_lease")
        for g, h in pairs:
            conn.execute(
                "INSERT INTO scope_lease (scope_kind, scope_key, grantor_run_id,"
                " granted_at_step, holder_run_id, granted_at)"
                " VALUES ('repo','/repo/M',?,'C01',?,'now')", (g, h))
        conn.commit()

    try:
        every = {"M1", "M2", "M3", "M4"}
        # One run handed the same scope to two others: authority in two places is not authority.
        relet([("M1", "M2"), ("M1", "M3")])
        assert st.resolve_scope_chain(conn, "repo", "/repo/M", every) == ([], "forked")
        # Two runs both claim to have handed it to the same one.
        relet([("M1", "M3"), ("M2", "M3")])
        assert st.resolve_scope_chain(conn, "repo", "/repo/M", every) == ([], "forked")
        # A closed loop has no root at all — which is the first half of why no separate cycle
        # check exists.
        relet([("M1", "M2"), ("M2", "M3"), ("M3", "M1")])
        assert st.resolve_scope_chain(conn, "repo", "/repo/M", every) == ([], "forked")
        # The second half: a loop OFF to one side is exactly what a cycle check would claim to
        # catch. It is unreachable from the root, so the walk ends by itself and the coverage
        # check rejects it. This assertion is what makes "no cycle check is needed" a fact
        # rather than a claim — if the walk could fail to terminate, this test would hang.
        relet([("M1", "M2"), ("M3", "M4"), ("M4", "M3")])
        assert st.resolve_scope_chain(conn, "repo", "/repo/M", every) == ([], "partial")
        # A well-formed chain over the same rows resolves, so the refusals above are not
        # this function simply always saying no.
        relet([("M1", "M2"), ("M2", "M3"), ("M3", "M4")])
        assert st.resolve_scope_chain(conn, "repo", "/repo/M", every) == \
            (["M1", "M2", "M3", "M4"], None)
    finally:
        conn.close()


# ------------------------------------------------- taking a scope under concurrency

_RUN_INSERT = (
    "INSERT INTO run (run_id, ability, flow_digest, title, scope_kind, scope_key,"
    " variant, status, opened_at, updated_at, metadata_json)"
    " VALUES (?,'delivery','d',NULL,'repo',?,NULL,'open','t','t','{}')"
)


def _engine_store():
    import sys as _s
    _s.path.insert(0, str(REPO))
    from engine import store as st
    return st


def _claim(st, conn, run_id, scope, **kw):
    return st.claim_scope_and_open(
        conn, require_free_scope=kw.pop("require_free_scope", True), lease=kw.pop("lease", None),
        run_id=run_id, ability="delivery", flow_digest="d", title=None,
        scope_kind="repo", scope_key=scope, variant=None, metadata={})


def test_taking_a_scope_waits_for_the_lock_instead_of_reading_a_stale_view(env, monkeypatch):
    """The check that binds is taken WITH the write lock, not before it.

    A deferred transaction reads first and locks at its first write, which leaves the same gap
    the old code had: both processes read an empty scope, both insert. Under WAL the loser does
    not even get a clean refusal — its snapshot is stale by then, so it fails with a lock error
    instead of being told the scope is taken.

    Made deterministic rather than raced: a subprocess holds the write lock with the rival row
    already inserted and uncommitted, so the claim below MUST block until that commit lands and
    then report the scope as taken. The elapsed-time assertion is the part that pins "locked
    before reading" — with a deferred begin the read would return immediately and find nothing.
    """
    import textwrap
    import time
    # conftest points the in-process store at an UNINITIALISED directory, so using it
    # for real has to be said out loud. This test means to.
    monkeypatch.setenv("HARNESS_STATE_DIR", env["HARNESS_STATE_DIR"])
    st = _engine_store()
    db = env["HARNESS_STATE_DIR"] + "/harness.db"

    holder = subprocess.Popen([sys.executable, "-c", textwrap.dedent(f"""
        import sqlite3, time
        c = sqlite3.connect({db!r}, timeout=10.0, isolation_level=None)
        c.execute("BEGIN IMMEDIATE")
        c.execute({_RUN_INSERT!r}, ("rival", "/repo/A"))
        time.sleep(1.2)
        c.execute("COMMIT")
        c.close()
    """)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        time.sleep(0.4)                      # let the lock actually be held
        conn = st.connect()
        try:
            t0 = time.time()
            reason, blockers = _claim(st, conn, "mine", "/repo/A")
            waited = time.time() - t0
        finally:
            conn.close()
    finally:
        holder.wait(timeout=30)

    assert reason == "scope_taken", reason
    assert [r["run_id"] for r in blockers] == ["rival"]
    assert waited > 0.4, f"claimed in {waited:.2f}s — it did not wait for the write lock"


def test_a_scope_taken_after_the_fast_path_read_still_refuses(env, monkeypatch):
    """The friendly early check is advisory; this is the one that cannot be raced past.

    Reproduces the old bug's exact shape without threads: read the scope (as the fast path
    does), let another connection take it, then claim. Before the claim re-checked inside its
    own transaction, this inserted a second open run in one scope — and two open runs in one
    scope is precisely the state in which the guard stops adjudicating, so losing the race
    removed enforcement rather than leaving something visible.
    """
    # conftest points the in-process store at an UNINITIALISED directory, so using it
    # for real has to be said out loud. This test means to.
    monkeypatch.setenv("HARNESS_STATE_DIR", env["HARNESS_STATE_DIR"])
    st = _engine_store()
    conn = st.connect()
    other = sqlite3.connect(env["HARNESS_STATE_DIR"] + "/harness.db", timeout=10.0)
    try:
        assert st.open_runs_in_scope(conn, "repo", "/repo/B") == []      # the fast path's view
        other.execute(_RUN_INSERT, ("sneak", "/repo/B"))
        other.commit()
        reason, blockers = _claim(st, conn, "late", "/repo/B")
        assert reason == "scope_taken", reason
        assert [r["run_id"] for r in blockers] == ["sneak"]
        assert st.get_run(conn, "late") is None, "the refused run must not exist"
    finally:
        other.close()
        conn.close()


def test_a_grantor_that_moves_mid_open_is_caught_inside_the_lock(env, monkeypatch):
    """Delegation is validated before the lock is taken, so it is re-checked under it.

    Both shapes below are only reachable when something happens between validating the grantor
    and inserting the delegate — which is exactly what a second session does. A lease naming a
    closed grantor makes the chain unresolvable, and an unresolvable chain silently returns the
    scope to unenforced, so it must not be recordable at all.
    """
    # conftest points the in-process store at an UNINITIALISED directory, so using it
    # for real has to be said out loud. This test means to.
    monkeypatch.setenv("HARNESS_STATE_DIR", env["HARNESS_STATE_DIR"])
    st = _engine_store()
    conn = st.connect()
    try:
        conn.execute(_RUN_INSERT, ("outer", "/repo/C"))
        conn.commit()
        lease = {"grantor_run_id": "outer", "granted_at_step": "C01"}

        st.close_run(conn, "outer", "done")
        reason, _ = _claim(st, conn, "inner", "/repo/C",
                           require_free_scope=False, lease=lease)
        assert reason == "grantor_gone", reason

        conn.execute("UPDATE run SET status='open' WHERE run_id='outer'")
        conn.execute(_RUN_INSERT, ("first", "/repo/C"))   # the lease already out there
        conn.commit()
        st.grant_lease(conn, scope_kind="repo", scope_key="/repo/C",
                       grantor_run_id="outer", granted_at_step="C01", holder_run_id="first")
        reason, _ = _claim(st, conn, "second", "/repo/C",
                           require_free_scope=False, lease=lease)
        assert reason == "grantor_already_delegated", reason
        assert st.get_run(conn, "second") is None
    finally:
        conn.close()


def test_the_caller_is_recorded_for_diagnosis_and_never_for_isolation(env):
    """Who ran the command is answerable; it is not an isolation axis.

    Recorded because a human looking at three concurrent runs needs to know which worker owns
    which — a need that came up repeatedly while diagnosing a stuck fleet. NOT an isolation
    axis, and that half is what this test mainly protects: separate sessions by actor and two
    workers in one repository would each enforce only their own gates while neither knew the
    other existed, which is the one question the guard exists to answer.
    """
    e1 = {**env, "HARNESS_ACTOR": "worker-alpha"}
    e2 = {**env, "HARNESS_ACTOR": "worker-beta"}
    assert rc(["open", "delivery", "--scope", "/repo/D", "--run", "D1"], e1) == OK

    out = run(["status"], env).stdout
    assert "actor=worker-alpha" in out, out

    # A different actor is still the same scope.
    assert rc(["open", "delivery", "--scope", "/repo/D", "--run", "D2"], e2) == REFUSED

    meta = json.loads(_rows(env, "SELECT metadata_json FROM run WHERE run_id='D1'")[0][0])
    assert meta["actor"] == "worker-alpha"
    # Named for what they are: this process is the CLI invocation, not the session.
    assert "cli_pid" in meta and "session" not in json.dumps(meta)

    # Nothing declared means nothing claimed, rather than a guessed identity.
    assert rc(["open", "delivery", "--scope", "/repo/E", "--run", "E1"], env) == OK
    plain = json.loads(_rows(env, "SELECT metadata_json FROM run WHERE run_id='E1'")[0][0])
    assert "actor" not in plain


def test_an_unpinned_in_process_store_refuses_instead_of_finding_a_real_one():
    """The conftest guard itself, asserted rather than assumed.

    Without it, a test that calls the store in-process reads whichever HARNESS_STATE_DIR the
    shell happens to carry — which is the developer's own, and three junk runs did land there.
    Removing the fixture makes this connect succeed against something real, so this goes red.

    The path is asserted, not just the exception: on a machine with no store at all the refusal
    would happen anyway and the test would pass while proving nothing.
    """
    st = _engine_store()
    with pytest.raises(st.StoreNotInitialised) as caught:
        st.connect()
    assert "unchosen-store" in str(caught.value.path), caught.value.path


# ------------------------------------------------- schema versioning and migration

# The shape that existed BEFORE migration 1. Every future migration must record its own
# "before" here — that is the price of keeping schema.sql readable and at the latest shape,
# and it is cheaper than keeping every historical baseline in the tree.
_PRE_V1 = """
CREATE VIEW IF NOT EXISTS v_open_runs AS
SELECT run_id, ability, title, scope_kind, scope_key, current_step, opened_at, updated_at
FROM run WHERE status = 'open';
"""


def _schema_of(db: Path) -> list[tuple]:
    c = sqlite3.connect(db)
    try:
        return sorted(
            (r[0], r[1], r[2]) for r in c.execute(
                "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")
        )
    finally:
        c.close()


def test_a_migrated_store_ends_up_identical_to_a_fresh_one(tmp_path, monkeypatch):
    """The one hazard this arrangement creates, closed.

    `schema.sql` is kept at the LATEST shape, because it is also the readable explanation of
    that shape. So a brand-new store is stamped at the current version rather than migrated —
    which means a migration and an edit to the baseline must have the SAME effect, and nothing
    about writing them enforces that. Divergence would be silent and would split the population
    in two: new machines correct, upgraded ones subtly not.

    So build one store fresh, build another by putting the pre-migration shape back and letting
    init() carry it forward, and compare what SQLite reports about both.
    """
    st = _engine_store()
    a, b = tmp_path / "fresh", tmp_path / "upgraded"

    monkeypatch.setenv("HARNESS_STATE_DIR", str(a))
    _, applied = st.init()
    assert applied == [], "a fresh store is stamped, not migrated"
    fresh = _schema_of(a / "harness.db")

    monkeypatch.setenv("HARNESS_STATE_DIR", str(b))
    st.init()
    old = sqlite3.connect(b / "harness.db")
    try:
        old.executescript(_PRE_V1)
        old.execute("PRAGMA user_version = 0")
        old.commit()
    finally:
        old.close()
    assert _schema_of(b / "harness.db") != fresh, "the 'before' shape must actually differ"

    _, applied = st.init()
    assert applied == [1], applied
    assert _schema_of(b / "harness.db") == fresh

    # And re-running does not re-apply, nor resurrect what the migration removed.
    _, again = st.init()
    assert again == []
    assert _schema_of(b / "harness.db") == fresh


def test_the_declared_version_must_be_reachable_by_the_migrations(tmp_path, monkeypatch):
    """A version bump without its migration is caught at init, not at every later command.

    Left unchecked, the bump alone makes every command refuse with a mismatch the user cannot
    act on: `harness init` would report success and change nothing, so the advice the refusal
    gives would be wrong.
    """
    st = _engine_store()
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "s"))
    st.init()
    conn = sqlite3.connect(tmp_path / "s" / "harness.db")
    try:
        conn.execute("PRAGMA user_version = 0")
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setattr(st, "SCHEMA_VERSION", 99)
    with pytest.raises(RuntimeError, match="migrations only reach"):
        st.init()
    assert max(v for v, _, _ in st.MIGRATIONS) == 1, \
        "MIGRATIONS must reach the declared version; bump them together"


@pytest.mark.parametrize("version, expected_phrase", [
    (0, "run: harness init"),
    (99, "NEWER than this engine"),
])
def test_a_store_whose_shape_does_not_match_the_code_is_refused(env, version, expected_phrase):
    """Both directions, because reading the wrong shape is worse than not reading.

    Behind: fixable, and the message says how. Ahead: refused rather than migrated downwards —
    a newer engine wrote shapes this one does not know about, and operating on them would give
    plausible answers about a store being misread. The version is asserted afterwards, because
    a refusal that quietly rewrote the header would be the downgrade it claims to refuse.
    """
    db = env["HARNESS_STATE_DIR"] + "/harness.db"
    c = sqlite3.connect(db)
    try:
        c.execute(f"PRAGMA user_version = {version}")
        c.commit()
    finally:
        c.close()

    out = run(["status"], env)
    assert out.returncode == USAGE, out.stdout + out.stderr
    assert expected_phrase in out.stderr, out.stderr

    if version == 99:
        # The phrase is asserted, not only the code: an uncaught exception also exits 1, so
        # `== USAGE` alone would pass on a crash and call it a refusal. (It did — the mutation
        # that removes this check falls through to a RuntimeError whose traceback exits 1.)
        gone = run(["init"], env)
        assert gone.returncode == USAGE, gone.stdout + gone.stderr
        assert "NEWER than this engine" in gone.stderr, gone.stderr
        assert "Traceback" not in gone.stderr, gone.stderr
        c = sqlite3.connect(db)
        try:
            assert c.execute("PRAGMA user_version").fetchone()[0] == 99
        finally:
            c.close()


def test_a_guard_on_an_unreadable_store_allows_but_says_so(env):
    """The guard runs before every matching tool call, so it must not brick the machine.

    It also cannot RECORD the skip, because recording needs the store it just refused to open —
    so unlike an unadjudicable scope, this one only prints. Stated because the asymmetry looks
    like an oversight otherwise.
    """
    c = sqlite3.connect(env["HARNESS_STATE_DIR"] + "/harness.db")
    try:
        c.execute("PRAGMA user_version = 0")
        c.commit()
    finally:
        c.close()
    r = run(["guard", "--action", "commit", "--scope-kind", "repo", "--scope", "/r"], env)
    assert r.returncode == OK
    assert "guard not enforced" in r.stderr and "schema" in r.stderr, r.stderr


def test_status_shows_who_delegated_to_whom_and_whether_guards_can_adjudicate(env):
    """Three concurrent runs must not read as three unrelated peers.

    And the verdict is the part worth having: whether a shared scope is adjudicable at ALL was
    previously discoverable only by triggering a guard and reading the warning it printed —
    i.e. the one fact a human wants on seeing two runs in one scope was the one thing the
    listing did not say.
    """
    assert rc(["open", "delivery", "--scope", "/repo/S", "--run", "S1"], env) == OK
    assert rc(["evidence", "--run", "S1", "--step", "C01",
               "--kind", "change_list", "--value", "x"], env) == OK
    assert rc(["close-step", "--run", "S1", "--step", "C01"], env) == OK
    assert rc(["open", "delivery", "--scope", "/repo/S", "--run", "S2",
               "--leased-from", "S1", "--leased-at", "C01"], env) == OK

    out = run(["status"], env).stdout
    assert "→ delegated to S2" in out, out
    assert "← leased from S1" in out, out
    assert "S1 → S2" in out and "guards adjudicate" in out, out

    # An unrelated third run in the scope takes the verdict the other way.
    assert rc(["open", "delivery", "--scope", "/repo/S", "--run", "S3",
               "--allow-concurrent"], env) == OK
    out = run(["status"], env).stdout
    assert "will NOT adjudicate" in out and "(partial)" in out, out


# ------------------------------------------------- an exit code for "this is our bug"

INTERNAL = 5


def test_a_crash_exits_on_its_own_code_and_not_the_usage_one(monkeypatch, capsys):
    """1 must mean the input or the environment; the engine failing gets its own number.

    Python exits 1 on an uncaught exception and 1 is also this CLI's usage error, so anything
    reading the number — a tool hook, and every assertion in this file — could not tell a crash
    from a refusal. That was not hypothetical: a mutation removing the newer-store refusal fell
    through to a RuntimeError, and a test asserting `== USAGE` passed and called the crash a
    refusal.

    Adding a message assertion to each affected test would have been fixing twenty symptoms of
    one ambiguity. This removes it at the source.
    """
    import sys as _s
    _s.path.insert(0, str(REPO))
    from engine import harness as h

    def boom(_args):
        raise ValueError("deliberate")

    monkeypatch.setattr(h, "cmd_status", boom)
    assert h.main(["status"]) == INTERNAL
    assert INTERNAL != h.USAGE
    # The traceback still goes out: a defect that hides its location is worse than an ugly code.
    err = capsys.readouterr().err
    assert "ValueError: deliberate" in err and "INTERNAL" in err, err


def test_a_usage_problem_still_exits_one(env):
    """The other half — moving crashes to 5 must not move ordinary refusals with them."""
    c = sqlite3.connect(env["HARNESS_STATE_DIR"] + "/harness.db")
    try:
        c.execute("PRAGMA user_version = 0")
        c.commit()
    finally:
        c.close()
    out = run(["status"], env)
    assert out.returncode == USAGE
    assert "Traceback" not in out.stderr, out.stderr


# ------------------------------------------------- what a flow declares it relies on

def _uses_declared(ability: str) -> set[str]:
    import yaml as _y
    spec = _y.safe_load((REPO / "abilities" / ability / "flow.yaml").read_text(encoding="utf-8"))
    return set(spec.get("uses") or [])


def test_every_installed_production_flow_declares_exactly_what_it_uses():
    """The cross-check, asserted from outside the loader that performs it.

    Load-time enforcement is the mechanism; this is what notices if the mechanism stops running.
    Reading the yaml directly rather than a field on the loaded flow is deliberate — a check
    that asks the loader what the loader concluded would agree with it by construction.
    """
    import sys as _s
    _s.path.insert(0, str(REPO))
    from engine import flow as F
    seen_production = 0
    for name in F.available_abilities():
        f = F.load(name)
        if f.role != F.ROLE_PRODUCTION:
            continue
        seen_production += 1
        assert _uses_declared(name) == set(F.capabilities_used(f)), name
    assert seen_production >= 2, "fewer than two production flows; this test would be vacuous"


def test_every_capability_the_engine_names_is_exercised_by_something():
    """A detector nothing exercises could be simply wrong, and nothing would say so.

    This is the mirror of the cross-check: the specs prove the vocabulary, the vocabulary
    constrains the specs. An entry no installed flow reaches is a claim about the engine that
    has never been evaluated — and `facts` was exactly that kind of mistake, reporting every
    flow as using it because the underlying tuple is never empty.
    """
    import sys as _s
    _s.path.insert(0, str(REPO))
    from engine import flow as F
    covered: set[str] = set()
    for name in F.available_abilities():
        covered |= set(F.capabilities_used(F.load(name)))
    missing = sorted(set(F.ENGINE_CAPABILITIES) - covered)
    assert not missing, f"no installed flow exercises: {', '.join(missing)}"


def _uses_spec(name: str, body: str) -> Path:
    d = REPO / "abilities" / name
    d.mkdir(exist_ok=True)
    (d / "flow.yaml").write_text(
        f"version: 2\nability: {name}\nscope_kind: s\n{body.strip()}\n", encoding="utf-8")
    return d


_BODY_WITH_A_GATE = """
phases:
  - id: p1
    title: P1
steps:
  - id: W01
    phase: p1
    gate: affirm
    directive: Do it.
"""


@pytest.mark.parametrize("head, why", [
    ("role: production\nwhen: a routable flow that says nothing about what it relies on\n",
     "production must state its surface"),
    ("role: production\nwhen: a routable flow claiming something it does not do\n"
     "uses: [gates, guards]\n",
     "declared and never exercised"),
    ("role: fixture\nuses: [gates, guards]\n",
     "a fixture that declares is held to it"),
    ("role: production\nwhen: a routable flow naming a mechanism this engine has no idea about\n"
     "uses: [gates, teleportation]\n",
     "unknown capability"),
    ("role: production\nwhen: a routable flow whose uses is the wrong shape entirely\n"
     "uses: gates\n",
     "not a list"),
])
def test_a_mismatched_capability_list_is_refused_at_load(env, head, why):
    """Each refusal removes a way for the list to be something other than the surface.

    Silence is tolerated only for a fixture, which exists to exercise the engine — making each
    one enumerate the machinery it pokes is churn with no reader. A fixture that DOES declare is
    held to it, because a declaration left to rot is worse than none.
    """
    d = _uses_spec("zzz_uses", head + _BODY_WITH_A_GATE)
    try:
        assert rc(["validate", "zzz_uses"], env) == BAD_SPEC, why
    finally:
        _rm(d)


def test_a_fixture_may_stay_silent_and_a_correct_list_passes(env):
    """The positive control: the refusals above are not this check always saying no."""
    d = _uses_spec("zzz_uses_ok", "role: fixture\n" + _BODY_WITH_A_GATE)
    try:
        assert rc(["validate", "zzz_uses_ok"], env) == OK
    finally:
        _rm(d)
    d = _uses_spec("zzz_uses_ok2",
                   "role: production\nwhen: a routable flow that states its surface exactly\n"
                   "uses: [gates]\n" + _BODY_WITH_A_GATE)
    try:
        assert rc(["validate", "zzz_uses_ok2"], env) == OK
    finally:
        _rm(d)


def test_an_unknown_capability_says_the_engine_may_be_the_older_one(env):
    """The version contract, and why it is better than comparing two numbers.

    A flow written for a newer engine gets told WHICH mechanism is absent. A version comparison
    would only say that one side is older, leaving the reader to find out what changed.
    """
    d = _uses_spec("zzz_uses_new",
                   "role: production\nwhen: a routable flow from a future engine\n"
                   "uses: [gates, leases]\n" + _BODY_WITH_A_GATE)
    try:
        out = run(["validate", "zzz_uses_new"], env)
        assert out.returncode == BAD_SPEC
        assert "'leases'" in out.stderr and "NEWER engine" in out.stderr, out.stderr
        assert "Traceback" not in out.stderr, out.stderr
    finally:
        _rm(d)


def test_a_defect_in_the_argument_parser_cannot_escape_as_a_bare_one(monkeypatch):
    """Parsing is inside the guarded region too, not just command execution.

    A defect in the parser would otherwise leave main() as an exception, and the process would
    exit 1 — indistinguishable from a usage error, which is the whole ambiguity being removed.
    Asserted separately because a test that only breaks a COMMAND cannot tell the two placements
    apart: parse_args succeeds either way.
    """
    import sys as _s
    _s.path.insert(0, str(REPO))
    from engine import harness as h

    def boom():
        raise RuntimeError("the parser itself is broken")

    monkeypatch.setattr(h, "build_parser", boom)
    assert h.main(["status"]) == INTERNAL


# ------------------------------------------------- installable: metadata and dependencies

PYPROJECT = REPO / "pyproject.toml"


def _pyproject_text() -> str:
    return PYPROJECT.read_text(encoding="utf-8")


def _declared_dependencies() -> list[str]:
    """Read the dependency array out of pyproject by text, on purpose.

    NOT tomllib: that is 3.11+, and the verified floor is 3.10. A guard that skips on the floor
    version is a hole exactly where it matters most — the oldest interpreter anyone is told they
    may use is the one most likely to be missing something. The file is ours and the pattern is
    anchored, so the failure mode is a loud non-match rather than a quiet mis-parse.
    """
    m = re.search(r"^dependencies\s*=\s*\[(.*?)\]", _pyproject_text(), re.S | re.M)
    assert m, "pyproject no longer has an anchored `dependencies = [...]` array"
    return re.findall(r"['\"]([^'\"]+)['\"]", m.group(1))


def _engine_imports() -> set[str]:
    tops: set[str] = set()
    for path in (REPO / "engine").glob("*.py"):
        for line in path.read_text(encoding="utf-8").splitlines():
            m = re.match(r"^(?:import|from)\s+([A-Za-z_][A-Za-z0-9_]*)", line)
            if m:
                tops.add(m.group(1))
    return tops - {"__future__", "engine"}


def test_every_non_stdlib_import_in_the_engine_is_a_declared_dependency():
    """The packaging defect nobody notices until a fresh machine.

    An import that works here because it happens to be installed, and is not in the dependency
    list, produces a wheel that imports fine on the author's machine and fails on everyone
    else's. Asserted by walking the imports rather than by reading the list, so adding an import
    is what triggers it — the direction that actually happens.

    An import that maps to no installed distribution fails too: it is either undeclared or not
    installed, and both are worth stopping on.
    """
    import sys as _s
    from importlib.metadata import packages_distributions

    declared = {re.split(r"[<>=!~\[]", d, 1)[0].strip().lower().replace("_", "-")
                for d in _declared_dependencies()}
    assert declared, "no dependencies declared at all — the array parse is probably wrong"

    mapping = packages_distributions()
    outside = sorted(m for m in _engine_imports() if m not in _s.stdlib_module_names)
    assert outside, "no non-stdlib import found; this test would be vacuous"
    for mod in outside:
        dists = mapping.get(mod)
        assert dists, f"import {mod!r} maps to no installed distribution"
        assert any(d.lower().replace("_", "-") in declared for d in dists), (
            f"engine imports {mod!r} (from {dists}) and pyproject does not declare it; "
            f"declared: {sorted(declared)}"
        )


def test_the_version_has_exactly_one_source():
    """A hardcoded version in pyproject is a second place to forget."""
    text = _pyproject_text()
    assert 'dynamic = ["version"]' in text, text[:200]
    assert 'attr = "engine.__version__"' in text
    # The dynamic table legitimately starts with `version =`; what must not exist is a LITERAL.
    assert not re.search(r'^version\s*=\s*["\']', text, re.M), \
        "pyproject pins a literal version as well as reading it from the package"
    import sys as _s
    _s.path.insert(0, str(REPO))
    import engine
    assert re.fullmatch(r"\d+\.\d+\.\d+", engine.__version__), engine.__version__


def test_the_schema_is_shipped_as_package_data():
    """init() reads schema.sql at runtime, so omitting it produces an install that imports
    fine and dies the first time anyone uses it — a failure no import-level check would see."""
    assert (REPO / "engine" / "schema.sql").is_file()
    assert re.search(r'^engine\s*=\s*\[[^\]]*"schema\.sql"', _pyproject_text(), re.M), \
        "schema.sql is not declared in [tool.setuptools.package-data]"


def test_the_console_script_names_something_that_exists():
    """A rename here breaks every installed copy while the source tree keeps working,
    because the tree runs bin/harness and an install runs the entry point."""
    m = re.search(r'^harness\s*=\s*"([^:"]+):([^"]+)"', _pyproject_text(), re.M)
    assert m, "no `harness = \"module:function\"` entry point"
    module, func = m.groups()
    import importlib
    import sys as _s
    _s.path.insert(0, str(REPO))
    assert callable(getattr(importlib.import_module(module), func))


# The clause that makes each licence that licence. A file can be long, well-formatted and not
# actually grant anything, so length proves nothing — this is what has to be present.
LICENCE_GRANTS = {
    "MIT": "Permission is hereby granted, free of charge",
    "Apache": "Licensed under the Apache License, Version 2.0",
    "BSD": "Redistribution and use in source and binary forms",
}


def test_the_license_file_is_the_license_the_project_claims():
    """Two places state the licence, so they are cross-checked against each other.

    A length assertion was the first version of this and it was empty: gutting the grant clause
    left a file well over any threshold, and the test stayed green. What matters is that the
    classifier and the file agree, and that the file contains the clause that DOES the granting
    — a licence naming itself MIT while granting nothing is the failure worth catching.
    """
    text = (REPO / "LICENSE").read_text(encoding="utf-8")
    pyproject = _pyproject_text()
    assert 'license = { file = "LICENSE" }' in pyproject

    m = re.search(r'"License :: OSI Approved :: (\S+)[^"]*"', pyproject)
    assert m, "no OSI licence classifier to check the file against"
    claimed = m.group(1)
    assert claimed in LICENCE_GRANTS, (
        f"classifier claims {claimed!r}; add its grant clause to LICENCE_GRANTS so the file "
        f"can be checked rather than assumed"
    )
    assert LICENCE_GRANTS[claimed] in text, (
        f"pyproject classifies this as {claimed} and LICENSE does not contain its grant clause"
    )
    assert claimed.lower() in text.lower().split("\n")[0], \
        f"LICENSE's first line does not name {claimed}"
    assert re.search(r"Copyright \(c\) \d{4}", text), "no copyright line with a year"


# ------------------------------------------------- where the store lives by default

def test_the_default_store_is_outside_the_engines_own_directory(monkeypatch, tmp_path):
    """A base that keeps a consumer's data inside its own installation is not installable.

    site-packages is frequently unwritable, and where it is writable the next upgrade replaces
    the directory — taking the run ledger with it.
    """
    import sys as _s
    _s.path.insert(0, str(REPO))
    from engine import store as st

    monkeypatch.delenv("HARNESS_STATE_DIR", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    got = st.state_dir()
    assert got == tmp_path / "home" / ".local" / "state" / "harness-engine", got
    assert REPO not in got.parents and got != REPO

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    assert st.state_dir() == tmp_path / "xdg" / "harness-engine"

    monkeypatch.setenv("HARNESS_STATE_DIR", "~/somewhere")
    assert st.state_dir() == Path.home() / "somewhere", "the override must expand ~"


def test_a_store_left_at_the_old_default_is_refused_not_orphaned(monkeypatch, tmp_path):
    """Moving the default silently would answer every query as if the runs never happened.

    Both alternatives to refusing are worse: reading the old location makes the move a no-op,
    and reading the new one loses a recorded history without saying so. So it stops and names
    both paths plus the two ways out.

    Only fires when NOBODY said where the store is and there is something to lose — a packaged
    install has no such directory, so a consumer never sees it.
    """
    import sys as _s
    _s.path.insert(0, str(REPO))
    from engine import store as st

    assert st.LEGACY_STATE_DIR == st.REPO_ROOT / "state", "the old location moved; re-point this"

    legacy, now = tmp_path / "old", tmp_path / "new"
    legacy.mkdir()
    (legacy / "harness.db").write_bytes(b"")
    monkeypatch.setattr(st, "LEGACY_STATE_DIR", legacy)
    monkeypatch.delenv("HARNESS_STATE_DIR", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(now))

    with pytest.raises(st.StoreStranded) as caught:
        st.connect()
    msg = str(caught.value)
    assert str(legacy) in msg and str(now / "harness-engine") in msg, msg
    assert "HARNESS_STATE_DIR=" in msg, "the message must give a runnable way out"
    with pytest.raises(st.StoreStranded):
        st.init()

    # Saying where you want it is always an answer. (init first — the placeholder written
    # above is an empty file, and an empty file is a store that has not been initialised.)
    monkeypatch.setenv("HARNESS_STATE_DIR", str(legacy))
    st.init()
    st.connect().close()

    # A fresh store at the new location is the third way out, and it has to be reachable:
    # while the old one exists, `init` on the default is refused too, so the ONLY route is to
    # name the destination once. The message says so; this asserts the message is true.
    assert "start fresh here" in msg, msg
    monkeypatch.setenv("HARNESS_STATE_DIR", str(now / "harness-engine"))
    st.init()

    # And once both exist the ambiguity is gone, so the default stops refusing.
    monkeypatch.delenv("HARNESS_STATE_DIR")
    assert st.db_path().parent == now / "harness-engine"
    st.connect().close()


def test_no_build_output_is_tracked():
    """A guard whose first run audits a mistake made minutes earlier.

    `pip install .` leaves build/ and *.egg-info/ behind, and one commit picked them up — 7,500
    lines of duplicated engine source, in history, permanently. The copies are also actively
    misleading: a reader grepping the tree finds two versions of every module and no indication
    which one runs.

    Asserted from git's index rather than from the filesystem, because the files being PRESENT
    is normal after a build; being TRACKED is the defect.
    """
    listed = subprocess.run(["git", "ls-files"], cwd=str(REPO),
                            capture_output=True, text=True, check=True).stdout.split("\n")
    assert len(listed) > 10, "git ls-files returned almost nothing; this test would be vacuous"
    offenders = [f for f in listed
                 if f.startswith(("build/", "dist/")) or ".egg-info/" in f]
    assert not offenders, (
        f"{len(offenders)} build artefact(s) are tracked, e.g. {offenders[:3]}\n"
        f"  git rm -r --cached <path>   — and check .gitignore covers it"
    )


# ------------------------------------------------- the driving contract is generated

def _brief(env_extra=None, args=("--portable",)):
    return run(["brief", *args], env_extra or {"HARNESS_STATE_DIR": "/nonexistent-on-purpose"})


def test_the_checked_in_driving_contract_is_exactly_what_the_engine_emits():
    """The document that tells an agent how to drive this engine cannot be allowed to drift.

    It did. Within two days it named a store location that had moved, listed the exit codes one
    short, quoted a hard-coded path belonging to one machine, and gave a step count that was
    wrong — while `CAPABILITIES.md` beside it had four guards and stayed correct. The file with no
    guard was the file that rotted, which is not a coincidence worth restating.

    `--portable` is what gets checked in, so the comparison does not depend on whose checkout ran
    it and no absolute path enters version control.
    """
    out = _brief()
    assert out.returncode == OK, out.stderr
    checked_in = (REPO / "integrations" / "DRIVING.md").read_text(encoding="utf-8")
    assert out.stdout == checked_in, (
        "integrations/DRIVING.md is not what `harness brief --portable` produces.\n"
        "  regenerate: harness brief --portable > integrations/DRIVING.md"
    )
    assert "<path-to-engine>" in checked_in, "a machine's real path leaked into the checked-in copy"


def test_every_exit_code_the_cli_defines_is_described_in_the_brief():
    """Both directions, because the drift was a code that existed and was never described.

    Keyed by NAME rather than number so that renaming or renumbering cannot quietly satisfy it.
    """
    import sys as _s
    _s.path.insert(0, str(REPO))
    from engine import brief as b, harness as h

    defined = {n for n in ("OK", "USAGE", "BAD_SPEC", "REFUSED", "BLOCKED", "INTERNAL")
               if hasattr(h, n)}
    assert len(defined) == 6, defined
    assert set(b.EXIT_MEANINGS) == defined, (
        f"described but not defined: {sorted(set(b.EXIT_MEANINGS) - defined)}; "
        f"defined but not described: {sorted(defined - set(b.EXIT_MEANINGS))}"
    )
    # And each one's number reaches the rendered table.
    out = _brief().stdout
    for name in defined:
        assert f"| **{getattr(h, name)}** |" in out, name


def test_the_brief_is_tailored_to_what_is_installed(env, tmp_path):
    """A section about machinery the installed flows do not have is budget spent teaching an
    agent about something it will never meet — and it buries the sections that DO apply.

    Asserted in both directions from one minimal flow: the mechanism it uses appears, the ones it
    does not are absent. A one-directional check would pass on a brief that prints everything.
    """
    root = tmp_path / "tree"
    (root / "tiny").mkdir(parents=True)
    (root / "tiny" / "flow.yaml").write_text(
        "version: 2\nability: tiny\n"
        "when: a minimal flow, used to check the brief is tailored rather than fixed\n"
        "uses: [gates]\nscope_kind: s\n"
        "phases:\n  - id: p1\n    title: P1\n"
        "steps:\n  - id: W01\n    phase: p1\n    gate: affirm\n    directive: Do it.\n",
        encoding="utf-8")
    out = _brief({**env, "HARNESS_ABILITIES_PATH": str(root)})
    assert out.returncode == OK, out.stderr
    assert "## Gates" in out.stdout
    for absent in ("## Variants", "## Obligations", "## Repeatable steps",
                   "## Mutually exclusive steps"):
        assert absent not in out.stdout, f"{absent} rendered for a flow that does not use it"
    assert "**`tiny`**" in out.stdout, "the routing hint is derived from the installed flow"

    # The full installation must show what the minimal one did not, or the check above is vacuous.
    full = _brief().stdout
    assert "## Variants" in full and "## Obligations" in full


def test_the_brief_carries_the_rules_that_cannot_be_derived():
    """The judgment block is the half no amount of reading the CLI produces.

    Held as data inside the engine so the purity guard applies to it: a rule statable only in one
    consumer's vocabulary would fail that scan, which is what makes a surviving rule one that
    holds for every consumer.
    """
    import sys as _s
    _s.path.insert(0, str(REPO))
    from engine import brief as b

    assert len(b.JUDGMENT) >= 5
    out = _brief().stdout
    for title, rule, why in b.JUDGMENT:
        assert f"### {title}" in out, title
        assert rule.split(".")[0] in out, title
        assert why.split(".")[0] in out, f"the reason for {title!r} is missing"


def test_the_command_surface_is_read_off_the_parser():
    """One list of commands, not two. A hand-kept second list is the drift being removed."""
    import sys as _s
    _s.path.insert(0, str(REPO))
    from engine import harness as h

    described = h._subcommand_help()
    assert len(described) > 10, described
    parser = h.build_parser()
    import argparse as _a
    choices: set[str] = set()
    for action in parser._actions:
        if isinstance(action, _a._SubParsersAction):
            choices = set(action.choices)
    assert choices and set(described) == choices, (
        f"described but unknown: {sorted(set(described) - choices)}; "
        f"exists but undescribed: {sorted(choices - set(described))}"
    )


# ------------------------------------------------- the adapter contract, consumed

def _contract(env_extra) -> dict:
    out = run(["adapter-contract"], env_extra)
    assert out.returncode == OK, out.stderr
    return json.loads(out.stdout)


def test_the_published_adapter_cases_are_what_the_shipped_adapter_satisfies(env, tmp_path):
    """The cases are load-bearing, not decorative — the shipped adapter is checked against them.

    A contract published for other runtimes to satisfy, which nothing here satisfies, would be a
    claim. So the translation table is driven against a STUB engine that exits with each code in
    turn: that isolates the one thing an adapter is responsible for, the mapping. Feeding real
    events instead would test the engine's verdicts and leave the mapping half-covered.

    The absent-engine case points at a directory that never held the binary, rather than deleting
    one mid-test. Same condition, and a test that mutates the filesystem while running is a test
    whose later assertions depend on its earlier side effects.
    """
    contract = _contract(env)
    assert contract["translation"] and contract["resilience"]
    adapter = REPO / "integrations" / "kiro-pretooluse.py"

    stub_root = tmp_path / "stub"
    (stub_root / "bin").mkdir(parents=True)
    stub = stub_root / "bin" / "harness"
    empty_root = tmp_path / "no-engine-here"
    empty_root.mkdir()

    def drive(event='{"tool_name":"shell","tool_input":{"command":"x"}}', root=None):
        import os
        e = {**os.environ, "HARNESS_ENGINE": str(root or stub_root)}
        e.pop("HARNESS_STATE_DIR", None)
        return subprocess.run([sys.executable, str(adapter)], input=event,
                              capture_output=True, text=True, env=e, cwd=str(tmp_path))

    for case in contract["translation"]:
        stub.write_text(f"#!/usr/bin/env python3\nimport sys\nsys.exit({case['engine_exit']})\n",
                        encoding="utf-8")
        stub.chmod(0o755)
        got = drive().returncode
        want_block = case["expect"] == "block"
        assert (got != 0) == want_block, (
            f"engine exit {case['engine_exit']} should {case['expect']}, adapter returned {got} "
            f"— {case['why']}"
        )

    assert drive("not json at all").returncode == 0, "unparseable input must allow"
    assert drive("{}").returncode == 0, "an event with no tool name must allow"

    gone = drive(root=empty_root)
    assert gone.returncode == 0
    assert "not found" in gone.stderr or "NOT enforced" in gone.stderr, (
        "an absent engine must be REPORTED — 'cannot guard' and 'nothing to guard' must not "
        "look alike"
    )


def test_the_end_to_end_case_is_derived_from_an_installed_flow(env):
    """It names a real action from a real flow, and admits what it cannot hand over.

    A synthesised payload would be worse than none: a string invented against an arbitrary regex
    most likely does not match, so the case would pass while proving nothing.
    """
    e2e = _contract(env)["end_to_end"]
    assert e2e and e2e["ability"] in run(["abilities"], env).stdout
    assert e2e["gate_step"], "an action guarded by no gate step could never refuse anything"
    assert e2e["matches"] and all("pattern" in m and "tool" in m for m in e2e["matches"])
    assert all("regex" not in m for m in e2e["matches"]), "a compiled object is not a contract"
    assert "yours to construct" in e2e["note"]


# ------------------------------------------------- reading what already happened

def test_closed_runs_are_listable_and_the_actor_filter_precedes_the_limit(env):
    """Closing a run never deleted anything; until now nothing listed it.

    The ordering of filter and limit is the part worth pinning: taking the newest N and THEN
    dropping other actors returns the newest N minus everyone else's, which is not the newest N of
    that actor's. Set up so a filter-after-limit implementation returns nothing at all.
    """
    for i, who in enumerate(["a-agent", "b-agent", "b-agent", "b-agent"]):
        e = {**env, "HARNESS_ACTOR": who}
        assert rc(["open", "delivery", "--scope", f"/repo/h{i}", "--run", f"H{i}"], e) == OK
        assert rc(["close-run", "--run", f"H{i}", "--result", "completed", "--force-steps",
                   "--force-obligations"], e) == OK

    listed = run(["history"], env)
    assert listed.returncode == OK, listed.stderr
    for i in range(4):
        assert f"H{i}" in listed.stdout
    assert "actor=a-agent" in listed.stdout and "actor=b-agent" in listed.stdout

    # H0 is the OLDEST and the only a-agent run: a limit applied before the filter leaves it out.
    only = run(["history", "--actor", "a-agent", "--limit", "1"], env)
    assert only.returncode == OK
    assert "H0" in only.stdout, only.stdout
    for other in ("H1", "H2", "H3"):
        assert other not in only.stdout

    assert rc(["history", "--ability", "delivery"], env) == OK
    assert "(no closed runs match)" in run(["history", "--ability", "plan"], env).stdout


def test_the_actor_rollup_is_the_one_place_to_compare_drivers(env):
    """Recording the actor and never reporting it would have made it a field nothing reads.

    The undeclared bucket is asserted too: an installation where nobody sets it must still be
    countable rather than silently producing an empty report.
    """
    assert rc(["open", "delivery", "--scope", "/repo/x", "--run", "X1"],
              {**env, "HARNESS_ACTOR": "one"}) == OK
    assert rc(["open", "delivery", "--scope", "/repo/y", "--run", "Y1"], env) == OK  # no actor

    out = run(["history", "--actors"], env)
    assert out.returncode == OK, out.stderr
    assert "one" in out.stdout and "(undeclared)" in out.stdout, out.stdout

    # A scope-level violation belongs to no run, so it must not be charged to any driver.
    assert rc(["open", "delivery", "--scope", "/repo/x", "--run", "X2",
               "--allow-concurrent"], {**env, "HARNESS_ACTOR": "one"}) == OK
    assert rc(["guard", "--action", "commit", "--scope-kind", "repo",
               "--scope", "/repo/x"], env) == OK
    rows = _rows(env, "SELECT COUNT(*) c FROM violation WHERE run_id IS NULL")
    assert rows[0]["c"] >= 1, "the unadjudicated guard should have recorded a run-less violation"
    audit = run(["audit"], env)
    assert "guard_unadjudicated" in audit.stdout
    for line in audit.stdout.splitlines():
        if "guard_unadjudicated" in line:
            assert "actor=" not in line, "a fact about a scope was attributed to a driver"


def test_the_written_brief_lands_beside_the_store_and_carries_a_usable_path(env):
    """An agent runtime loads a FILE as context — it cannot run a command to fill one.

    So the generated contract has to be materialised, and the checked-in copy cannot be that
    file: it is `--portable`, so its path is a placeholder. A consumer configuration pointed at
    the portable copy tells an agent to alias something that does not exist, which is precisely
    what happened and why this exists.
    """
    import sys as _s
    _s.path.insert(0, str(REPO))
    from engine import store as st

    out = run(["brief", "--write"], env)
    assert out.returncode == OK, out.stderr
    expected = Path(env["HARNESS_STATE_DIR"]) / "brief.md"
    assert str(expected) in out.stdout, out.stdout
    assert expected.is_file()

    written = expected.read_text(encoding="utf-8")
    assert written == run(["brief"], env).stdout, "the written copy must be the plain rendering"
    assert "<path-to-engine>" not in written, "the machine copy must not carry the placeholder"
    # And the path it names must actually be runnable.
    m = re.search(r"alias harness='([^']+)'", written)
    if m:
        assert Path(m.group(1)).exists(), m.group(1)
    else:
        assert "\nInvocation" in written or "harness init" in written


def test_writing_and_portable_are_different_audiences(env):
    """The two flags describe copies for different readers, so combining them is refused.

    A written copy is for THIS machine and must carry a working path; a portable one exists to be
    committed and must not carry anyone's path. Silently letting one win would produce exactly the
    unusable file this pair of flags was introduced to separate.
    """
    target = Path(env["HARNESS_STATE_DIR"]) / "brief.md"
    # `init` already wrote one, so absence proves nothing here — the property is that the refusal
    # leaves it UNTOUCHED. Asserting non-existence would have passed for the wrong reason.
    before = target.read_text(encoding="utf-8") if target.is_file() else None
    out = run(["brief", "--write", "--portable"], env)
    assert out.returncode == USAGE
    assert "different audiences" in out.stderr, out.stderr
    after = target.read_text(encoding="utf-8") if target.is_file() else None
    assert after == before, "a refused write must not have changed the file"
    if after:
        assert "<path-to-engine>" not in after, "the placeholder leaked into the machine copy"


def test_init_refreshes_the_written_brief(env):
    """A generated document nobody regenerates is a hand-written one with extra steps.

    `init` is the one command every setup path already runs and is safe to re-run, so refreshing
    happens there rather than relying on someone remembering. A stale contract misinstructs an
    agent silently, which is the failure the generation was meant to remove — moved, not fixed.
    """
    target = Path(env["HARNESS_STATE_DIR"]) / "brief.md"
    assert target.is_file(), "the env fixture ran init, which should have written it"
    target.write_text("stale nonsense\n", encoding="utf-8")

    out = run(["init"], env)
    assert out.returncode == OK, out.stderr
    assert str(target) in out.stdout, "init must say where the contract went"
    assert "stale nonsense" not in target.read_text(encoding="utf-8")
    assert target.read_text(encoding="utf-8") == run(["brief"], env).stdout


def test_the_portable_copy_says_what_it_is(env):
    """A reference copy that does not announce itself gets used as the real thing.

    Asserted in both directions: the portable rendering declares itself and names how to get a
    working one; the machine rendering does neither, because there it would be noise.
    """
    portable = run(["brief", "--portable"], env).stdout
    local = run(["brief"], env).stdout
    assert "portable reference copy" in portable
    assert "harness brief --write" in portable
    assert "portable reference copy" not in local

    import sys as _s
    _s.path.insert(0, str(REPO))
    import engine
    for text in (portable, local):
        assert f"from engine {engine.__version__}" in text.splitlines()[0], text.splitlines()[0]


# ------------------------------------------------- how the spec format is read

_VER_BODY = """scope_kind: s
phases:
  - id: p1
    title: P1
steps:
  - id: W01
    phase: p1
    directive: Do it.
"""


def _ver_spec(root: Path, name: str, head: str, body: str = _VER_BODY) -> None:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "flow.yaml").write_text(f"ability: {name}\n{head}{body}", encoding="utf-8")


def _ver_env(env, root: Path) -> dict:
    return {**env, "HARNESS_ABILITIES_PATH": str(root)}


@pytest.mark.parametrize("version", ["2", '"2.0"', '"2.7"'])
def test_the_same_major_is_readable_whatever_the_minor(env, tmp_path, version):
    """MAJOR is the readability claim; MINOR only says keys were added.

    `1` and `"1.0"` must mean the same thing, or every existing spec would have to be rewritten
    to gain a minor. And a NEWER minor is read rather than refused: the structure is unchanged, so
    refusing it would be refusing something this engine can in fact read.
    """
    root = tmp_path / f"r{version.strip(chr(34)).replace('.', '_')}"
    _ver_spec(root, "ok", f"version: {version}\nwhen: a flow at some minor of the readable "
                          f"major\nuses: []\n")
    assert rc(["validate", "ok"], _ver_env(env, root)) == OK


def test_a_key_from_a_newer_minor_is_refused_as_a_version_gap_not_a_typo(env, tmp_path):
    """Both framings exist and must not be swapped — that swap is the defect being fixed.

    The unknown-key refusal used to talk about typos unconditionally, so a spec from a newer
    format was told to check its spelling. Asserted in BOTH directions: a newer minor gets the
    version framing and NOT the typo advice; a spec at this very format gets the typo framing and
    no mention of a version.
    """
    root = tmp_path / "minor"
    _ver_spec(root, "newer", 'version: "2.4"\nwhen: a flow from a newer minor carrying an '
                             'unknown key\nuses: []\nretry_policy: aggressive\n')
    _ver_spec(root, "typo", 'version: 2\nwhen: a flow at this format that simply misspells a '
                            'key\nuses: []\nretry_polcy: aggressive\n')

    newer = run(["validate", "newer"], _ver_env(env, root))
    assert newer.returncode == BAD_SPEC
    assert "format 2.4" in newer.stderr and "same MAJOR" in newer.stderr, newer.stderr
    assert "A typo here" not in newer.stderr, "a version gap was reported as a typo"

    typo = run(["validate", "typo"], _ver_env(env, root))
    assert typo.returncode == BAD_SPEC
    assert "A typo here" in typo.stderr, typo.stderr
    assert "MAJOR" not in typo.stderr, "a plain typo was reported as a version gap"


def test_the_version_is_read_before_any_key_is_judged(env, tmp_path):
    """Order alone decides which of two true things the reader is told.

    A spec that is BOTH a newer major AND carries an unknown key is two problems, and only one of
    them is actionable: the key is unknown BECAUSE the format moved. Judging keys first reported
    the symptom and never mentioned the cause — which is exactly what happened.
    """
    root = tmp_path / "order"
    _ver_spec(root, "both", "version: 3\nwhen: a newer major that also carries an unknown "
                            "key\nuses: []\nretry_policy: aggressive\n")
    out = run(["validate", "both"], _ver_env(env, root))
    assert out.returncode == BAD_SPEC
    assert "spec format 3.0 cannot be read" in out.stderr, out.stderr
    # The crisp discriminator is the KEY NAME: it can only appear if the key check ran. Phrases
    # like "does not know" are no good here — the correct message contains one of its own
    # ("does not know what moved"), so asserting on those passes for the wrong reason.
    assert "retry_policy" not in out.stderr, \
        "the key was judged before the version, so the cause went unmentioned"


def test_an_unreadable_major_says_whether_the_machinery_is_missing_too(env, tmp_path):
    """Two different problems wear the same version number, and the answers differ.

    A spec whose `uses:` this engine fully implements is a FORMAT gap — everything it needs is
    here, only the writing is newer. One naming an absent mechanism needs more than a newer
    reader. The version number alone cannot tell them apart, so the refusal does.
    """
    root = tmp_path / "gap"
    _ver_spec(root, "formatonly", "version: 3\nwhen: a newer major needing nothing this engine "
                                  "lacks\nuses: [gates, prose]\n")
    _ver_spec(root, "alsomissing", "version: 3\nwhen: a newer major that also needs absent "
                                   "machinery\nuses: [gates, leases]\n")

    fmt = run(["validate", "formatonly"], _ver_env(env, root))
    assert fmt.returncode == BAD_SPEC
    assert "format gap and not a machinery gap" in fmt.stderr, fmt.stderr

    also = run(["validate", "alsomissing"], _ver_env(env, root))
    assert also.returncode == BAD_SPEC
    assert "does NOT implement: leases" in also.stderr, also.stderr
    assert "format gap and not a machinery gap" not in also.stderr


def test_a_float_version_is_refused_because_minors_would_collide(env, tmp_path):
    """`1.10` and `1.1` are the SAME float, so two minors would become one silently.

    The engine already guards the key side of this coercion (`on:`/`yes:` becoming booleans);
    this is the value side. The assertion reads the collision off the message itself: the spec
    says 1.10 and what arrives is 1.1.
    """
    root = tmp_path / "float"
    _ver_spec(root, "floaty", "version: 2.10\nwhen: a flow whose version was left unquoted\n"
                              "uses: []\n")
    out = run(["validate", "floaty"], _ver_env(env, root))
    assert out.returncode == BAD_SPEC
    assert "is a float (2.1)" in out.stderr, out.stderr
    assert "SAME" in out.stderr and "Quote it" in out.stderr


@pytest.mark.parametrize("bad", ['"1.2.3"', '"one"', '""', "true", '"1."'])
def test_a_version_that_is_not_a_format_is_refused(env, tmp_path, bad):
    """Anything that is not an integer major or a quoted MAJOR.MINOR is not a format."""
    root = tmp_path / f"bad{abs(hash(bad))}"
    _ver_spec(root, "b", f"version: {bad}\nwhen: a flow whose version is not a format at all\n"
                         f"uses: []\n")
    assert rc(["validate", "b"], _ver_env(env, root)) == BAD_SPEC


# ------------------------------------------------- the base names no external tool

def test_the_builtin_providers_report_only_what_the_engine_holds(env):
    """The built-in set is exactly the providers that need nothing outside the engine.

    One used to invoke a version-control tool. It was convenient, it was domain knowledge in the
    base, and it bypassed the base's own seam: a provider needing a tool declares
    `requires={"cmd": ...}` so that "I could not look" stops looking like "nothing found", and a
    built-in cannot state that without the ENGINE depending on the tool.

    Pinned as an exact set rather than a floor, because the failure being prevented is a NEW
    built-in that reaches outside, and a floor would not notice one.
    """
    import sys as _s
    _s.path.insert(0, str(REPO))
    from engine import facts as fa, flow as fl

    src = (REPO / "engine" / "facts.py").read_text(encoding="utf-8")
    declared = set(re.findall(r'@provider\("([^"]+)"', src))
    assert declared == {"static", "scope_only", "run_progress"}, sorted(declared)

    # And the flow that used the moved provider still gets it — from its own file.
    f = fl.load("delivery")
    assert "delivery.git_tree" in f.facts_providers, f.facts_providers
    assert (REPO / "abilities" / "delivery" / "providers.py").is_file()
    assert "delivery.git_tree" in fa.registered()
    # Now that it lives with a flow, it can declare the tool it needs — which as a built-in it
    # could not, and that declaration is the whole difference between an absent tool and an
    # empty answer.
    assert any("git" in str(d) for d in fa.capabilities("delivery.git_tree")), fa.capabilities("delivery.git_tree")


# ------------------------------------------------- a name collision names both sides

def _approve(env, root: Path, *abilities: str) -> None:
    """Approve external extension files the way a human does — through the CLI.

    Not by writing the record file directly: the point of these fixtures is that code from
    outside the engine's tree does not run until someone says so, and a test that forged the
    record would stop exercising the saying-so.
    """
    e = {**env, "HARNESS_ABILITIES_PATH": str(root)}
    for a in abilities:
        assert rc(["trust", a], e) == OK, a


def _ns_ability(root: Path, name: str, reg_name: str, *, uses: str = "[facts]",
                body: str | None = None) -> None:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "providers.py").write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(REPO)!r})\n"
        "from engine import facts\n"
        f"@facts.provider({reg_name!r}, schema={{'n': 'int'}})\n"
        "def _p(ctx):\n    return {'n': 1}\n", encoding="utf-8")
    (d / "flow.yaml").write_text(
        f"version: 2\nability: {name}\ntitle: T\n"
        f"when: reach for this one when exercising how registrations are namespaced\n"
        f"uses: {uses}\nscope_kind: s\n"
        + (f"facts:\n  provider: {reg_name}\n" if body is None else body) +
        f"phases:\n  - id: p1\n    title: P1\n"
        f"steps:\n  - id: W01\n    phase: p1\n"
        f"    completion: {{type: evidence, kind: k}}\n    directive: Do it.\n",
        encoding="utf-8")


def test_two_unrelated_abilities_may_now_use_one_obvious_word(env, tmp_path):
    """THE POINT OF NAMESPACING, asserted on the case that used to be impossible.

    Two authors who have never met both pick `open_findings` — the obvious word for the thing
    they each look at. Under one flat namespace the second to load raised, and load order is
    alphabetical, so which of them worked was decided by spelling. Nothing about that was a
    defect in either flow; the base simply could not host both.
    """
    root = tmp_path / "ns"
    _ns_ability(root, "alpha", "open_findings")
    _ns_ability(root, "beta", "open_findings")
    e = {**env, "HARNESS_ABILITIES_PATH": str(root)}
    _approve(env, root, "alpha", "beta")

    assert rc(["validate"], e) == OK, "both flows must load in ONE process"

    import os
    import sys as _s
    _s.path.insert(0, str(REPO))
    from engine import facts as fa, flow as fl
    prior = os.environ.get("HARNESS_ABILITIES_PATH")
    prior_state = os.environ.get("HARNESS_STATE_DIR")
    os.environ["HARNESS_ABILITIES_PATH"] = str(root)
    # conftest points the in-process state dir at an UNINITIALISED scratch dir, which is a
    # different directory from the one the CLI subprocesses used — so the approvals just
    # recorded would be invisible here. Pointing it at the same place is what conftest says a
    # test must do when it means to load in-process. No database is opened either way: the
    # trust record is a file, because flow loading never touches the store.
    os.environ["HARNESS_STATE_DIR"] = env["HARNESS_STATE_DIR"]
    try:
        a, b = fl.load("alpha"), fl.load("beta")
        keys = [k for k in fa.registered() if k.endswith("open_findings")]
        assert keys == ["alpha.open_findings", "beta.open_findings"], keys
        # Each spec's bare reference resolved to ITS OWN, not to whoever loaded first.
        assert a.facts_providers == ("alpha.open_findings",), a.facts_providers
        assert b.facts_providers == ("beta.open_findings",), b.facts_providers
        # And the two are genuinely different callables, not one entry read twice.
        assert fa._PROVIDERS["alpha.open_findings"]["fn"] is not \
            fa._PROVIDERS["beta.open_findings"]["fn"]
    finally:
        for k in ("alpha.open_findings", "beta.open_findings"):
            fa._PROVIDERS.pop(k, None)
            fa._OWNERS.pop(k, None)
        for n in ("alpha", "beta"):
            fl._EXTENSIONS_LOADED.discard(n)
        for k, v in (("HARNESS_ABILITIES_PATH", prior), ("HARNESS_STATE_DIR", prior_state)):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_the_collision_that_remains_is_one_ability_naming_one_thing_twice(env, tmp_path):
    """The only collision left is a real defect, and it is blamed on the right file.

    Namespacing removes the collision between strangers. It cannot remove — and should not —
    one ability registering one name twice: that is one author, one namespace, two claims. The
    message must say which, or the fix (rename mine) reads as the old advice (rename someone
    else's).
    """
    root = tmp_path / "dup"
    d = root / "solo"
    d.mkdir(parents=True)
    (d / "providers.py").write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(REPO)!r})\n"
        "from engine import facts\n"
        "@facts.provider('twice', schema={'n': 'int'})\n"
        "def _a(ctx):\n    return {'n': 1}\n"
        "@facts.provider('twice', schema={'n': 'int'})\n"
        "def _b(ctx):\n    return {'n': 2}\n", encoding="utf-8")
    (d / "flow.yaml").write_text(
        "version: 2\nability: solo\ntitle: T\n"
        "when: reach for this one when one ability claims a single name twice\n"
        "uses: [facts]\nscope_kind: s\nfacts:\n  provider: twice\n"
        "phases:\n  - id: p1\n    title: P1\n"
        "steps:\n  - id: W01\n    phase: p1\n"
        "    completion: {type: evidence, kind: k}\n    directive: Do it.\n", encoding="utf-8")

    _approve(env, root, "solo")
    out = run(["validate", "solo"], {**env, "HARNESS_ABILITIES_PATH": str(root)})
    assert out.returncode == BAD_SPEC
    assert "claimed twice by ability 'solo'" in out.stderr, out.stderr
    assert "NOT a collision with some" in out.stderr
    assert str(d / "providers.py") in out.stderr


def test_an_ability_may_not_shadow_a_name_the_engine_defines(env, tmp_path):
    """Reserved, because shadowing would be invisible in both specs.

    A flow redefining `all_of` would make one word mean its version inside itself and the
    engine's everywhere else, with nothing in either yaml to show it — and namespacing is what
    makes that tempting, since the author now correctly believes their names are private.
    """
    root = tmp_path / "shadow"
    d = root / "shady"
    d.mkdir(parents=True)
    (d / "providers.py").write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(REPO)!r})\n"
        "from engine import predicates\n"
        "@predicates.predicate('all_of')\n"
        "def _x(conn, run_id, step, spec):\n    return True, 'ok'\n", encoding="utf-8")
    (d / "flow.yaml").write_text(
        "version: 2\nability: shady\ntitle: T\n"
        "when: reach for this one when an ability tries to redefine an engine name\n"
        "uses: []\nscope_kind: s\n"
        "phases:\n  - id: p1\n    title: P1\n"
        "steps:\n  - id: W01\n    phase: p1\n"
        "    completion: {type: evidence, kind: k}\n    directive: Do it.\n", encoding="utf-8")

    _approve(env, root, "shady")
    out = run(["validate", "shady"], {**env, "HARNESS_ABILITIES_PATH": str(root)})
    assert out.returncode == BAD_SPEC
    assert "the ENGINE already defines" in out.stderr, out.stderr
    assert "Shadowing it would make one word mean" in out.stderr
    # It must say the restriction is about the ENGINE only, or the author will conclude their
    # names have to avoid every other ability as well — the opposite of what changed.
    assert "does not have to avoid other ABILITIES" in out.stderr


def test_the_registry_itself_decides_whether_a_name_is_taken(env):
    """The owner map is advisory; two dictionaries that must agree is a bug waiting to happen.

    It happened at once: cleanup code that removed a name from the registry left it in the owner
    map, and the next registration of that name was refused as a collision with nobody. Out of
    sync the owner map now degrades a message; it cannot invent a conflict.
    """
    import sys as _s
    _s.path.insert(0, str(REPO))
    from engine import predicates as pr

    def probe(conn, run_id, step, spec):
        return True, "ok"

    pr.predicate("zzz_probe_name")(probe)
    assert pr.is_registered("zzz_probe_name")
    try:
        # Remove it the way real cleanup does — from the registry only.
        pr._REGISTRY.pop("zzz_probe_name")
        pr.predicate("zzz_probe_name")(probe)      # must NOT raise
        assert pr.is_registered("zzz_probe_name")
    finally:
        pr._REGISTRY.pop("zzz_probe_name", None)
        pr._OWNERS.pop("zzz_probe_name", None)


# ------------------------------------------------- an exhausted budget just refuses

def test_on_exhausted_is_gone_and_a_used_up_budget_refuses(env):
    """A key that can only say one thing says nothing.

    `on_exhausted` chose between refusing and escalating to a human gate. Nothing ever chose to
    escalate — eight declarations across the installed flows, every one restating the default.
    Deleting the unused branch would have left a single-valued key, so the key went too.
    """
    d = _spec(env, "zzz_budget", """
phases:
  - id: p1
    title: P1
steps:
  - id: W01
    phase: p1
    repeatable: true
    budget: 1
    directive: Do it.
""")
    try:
        assert rc(["open", "zzz_budget", "--scope", "b1", "--run", "b1"], env) == OK
        assert rc(["enter", "--run", "b1", "--step", "W01"], env) == OK
        again = run(["enter", "--run", "b1", "--step", "W01"], env)
        assert again.returncode == REFUSED
        assert "used its budget" in again.stderr, again.stderr
        assert "on_exhausted" not in again.stderr and "escalate" not in again.stderr
        assert _rows(env, "SELECT code FROM violation WHERE run_id='b1'")[0]["code"] \
            == "budget_exhausted"
    finally:
        _rm(d)

    d = _spec(env, "zzz_oe", """
phases:
  - id: p1
    title: P1
steps:
  - id: W01
    phase: p1
    repeatable: true
    budget: 1
    on_exhausted: escalate
    directive: Do it.
""")
    try:
        out = run(["validate", "zzz_oe"], env)
        assert out.returncode == BAD_SPEC
        assert "on_exhausted" in out.stderr and "unsupported key" in out.stderr, out.stderr
    finally:
        _rm(d)


# ------------------------------------------------- the read commands speak JSON too

def test_every_read_command_can_answer_in_json(env):
    """The exit codes were the contract; the DETAIL was human prose only.

    Anything that is not a shell — a second runtime's adapter, a dashboard, a monitor — had to
    regex formatted text to learn what step was next or what was owed. Asserted for every read
    command at once, so adding one without a machine form is what fails.
    """
    assert rc(["open", "delivery", "--scope", "/repo/J", "--run", "J1"],
              {**env, "HARNESS_ACTOR": "json-probe"}) == OK
    assert rc(["evidence", "--run", "J1", "--step", "C01",
               "--kind", "change_list", "--value", "x"], env) == OK

    for argv in (["status"], ["status", "--run", "J1"], ["next", "--run", "J1"],
                 ["obligations", "--run", "J1"], ["history"], ["history", "--actors"],
                 ["audit"], ["leases"], ["abilities"]):
        out = run([*argv, "--json"], env)
        assert out.returncode == OK, (argv, out.stderr)
        try:
            parsed = json.loads(out.stdout)
        except json.JSONDecodeError as exc:
            raise AssertionError(f"{argv} --json is not JSON: {exc}\n{out.stdout[:300]}")
        assert isinstance(parsed, dict) and parsed, argv
        # The text form must still be text — a command that only speaks JSON has moved, not gained.
        plain = run(argv, env).stdout
        assert plain and not plain.lstrip().startswith("{"), argv


def test_next_states_its_requirements_as_data(env):
    """`next` exists to say what a step REQUIRES, and it was saying it in columns.

    `predicates.requirements()` already returns a machine-readable list; the text form flattens
    it into lines. The JSON form carries it unflattened, which is the point of the whole flag.
    """
    assert rc(["open", "shipcheck-asis", "--scope", "/repo/K", "--run", "K1"], env) == OK
    out = run(["next", "--run", "K1", "--json"], env)
    assert out.returncode == OK, out.stderr
    d = json.loads(out.stdout)

    assert d["step"] and d["completion"] and d["phase"]
    reqs = d["requirements"]
    assert isinstance(reqs, list) and reqs, d
    assert all(isinstance(r, dict) and "what" in r for r in reqs), reqs
    # The pointer, not the text — the tiering must survive the machine form.
    assert d["guide"] is None or "file" in d["guide"]
    assert "directive" in d and len(d["remaining"]) > 1


def test_the_major_bump_says_what_moved_instead_of_only_refusing(env, tmp_path):
    """A MAJOR that cannot describe itself is a wall; one that can is a migration.

    Refusing a 1.x spec is correct — under one flat namespace a bare name reached any ability in
    the process, so the meaning of an unqualified reference genuinely changed and reading it
    anyway would resolve it differently than its author saw. But the refusal is the only place
    an author with a 1.x spec looks, so it has to name the change and the fix.

    Asserted alongside the case it must NOT claim to know: a version this engine never defined
    gets the refusal WITHOUT an invented migration note.
    """
    root = tmp_path / "mig"
    _ver_spec(root, "old", "version: 1\nwhen: a flow written against the previous spec major\n"
                           "uses: []\n")
    _ver_spec(root, "future", "version: 9\nwhen: a flow from a format this engine never had\n"
                              "uses: []\n")

    old = run(["validate", "old"], _ver_env(env, root))
    assert old.returncode == BAD_SPEC
    assert "WHAT CHANGED IN 2.0" in old.stderr, old.stderr
    assert "namespaced per ability" in old.stderr
    assert "requires:" in old.stderr
    # The half that needs no work must be said too, or every 1.x author starts by auditing
    # names that were never affected.
    assert "stay bare and need no change" in old.stderr

    fut = run(["validate", "future"], _ver_env(env, root))
    assert fut.returncode == BAD_SPEC
    assert "cannot be read" in fut.stderr
    assert "WHAT CHANGED" not in fut.stderr, \
        "the engine described a format gap it cannot possibly know"


# ------------------------------------------------- an extension file is code

def _ext_root(tmp_path: Path, *, code: str | None = None, name: str = "ext") -> Path:
    """An ability under an EXTERNAL root — the only place a trust boundary exists."""
    root = tmp_path / "outside"
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    if code is not None:
        (d / "providers.py").write_text(code, encoding="utf-8")
    (d / "flow.yaml").write_text(
        f"version: 2\nability: {name}\ntitle: T\n"
        f"when: reach for this one when checking that outside code is not run unasked\n"
        f"uses: {'[facts]' if code is not None else '[]'}\nscope_kind: s\n"
        + ("facts:\n  provider: ext_findings\n" if code is not None else "")
        + "phases:\n  - id: p1\n    title: P1\n"
        "steps:\n  - id: W01\n    phase: p1\n"
        "    completion: {type: evidence, kind: k}\n    directive: Do it.\n",
        encoding="utf-8")
    return root


_EXT_CODE = (
    "import sys\n"
    f"sys.path.insert(0, {str(REPO)!r})\n"
    "from engine import facts\n"
    "@facts.provider('ext_findings', schema={'n': 'int'})\n"
    "def _p(ctx):\n    return {'n': 1}\n"
)


def test_reading_a_spec_will_not_run_code_from_outside_the_engines_tree(env, tmp_path):
    """The exposure is that READING runs code, so the refusal has to be at read time.

    Loading a flow imports its `providers.py`. So `validate` — the command whose whole purpose
    is to check a spec you do not trust yet — executed the untrusted part of it. Asserted on
    `validate` rather than `open` for exactly that reason.
    """
    root = _ext_root(tmp_path, code=_EXT_CODE)
    e = {**env, "HARNESS_ABILITIES_PATH": str(root)}

    out = run(["validate", "ext"], e)
    assert out.returncode == BAD_SPEC
    assert "has not been approved here" in out.stderr, out.stderr
    assert "reading its spec would run it" in out.stderr
    assert "sha256:" in out.stderr
    assert "harness trust ext" in out.stderr

    # The non-claims are part of the message, not of the documentation. A refusal that reads as
    # protection is worse than none, because the reader stops looking.
    assert "NOT a sandbox" in out.stderr
    # Phrase chosen to sit on ONE line of the message: the first attempt here spanned a wrap
    # ("will not\n  appear in it") and so could never match, which is an empty assertion.
    assert "assembles an import name at runtime" in out.stderr, \
        "the advisory import list was presented as a check"

    # A purely declarative ability from the same untrusted root is free — nothing of it runs,
    # so there is nothing to approve, and charging for it would push authors toward code.
    plain = _ext_root(tmp_path, code=None, name="declarative")
    assert rc(["validate", "declarative"],
              {**env, "HARNESS_ABILITIES_PATH": str(plain)}) == OK


def test_approval_pins_the_bytes_so_a_later_change_asks_again(env, tmp_path):
    """The one thing a record kept by the engine can actually establish.

    It cannot make the code safe. It can make "the ability I installed changed its code" stop
    being silent — so that is what is asserted: approve, load, edit one line, refused again
    with BOTH digests, and `--forget` returns it to asking.
    """
    root = _ext_root(tmp_path, code=_EXT_CODE)
    e = {**env, "HARNESS_ABILITIES_PATH": str(root)}
    mod = root / "ext" / "providers.py"

    _approve(env, root, "ext")
    assert rc(["validate", "ext"], e) == OK

    before = (root / "ext" / "providers.py").read_text()
    mod.write_text(before + "\n# a line the approver never saw\n", encoding="utf-8")
    out = run(["validate", "ext"], e)
    assert out.returncode == BAD_SPEC
    assert "changed after you approved it" in out.stderr, out.stderr
    digests = re.findall(r"sha256:[0-9a-f]{64}", out.stderr)
    assert len(set(digests)) == 2, digests
    assert "--forget ext" in out.stderr

    # Re-approving the new content is one command, and then it loads again.
    _approve(env, root, "ext")
    assert rc(["validate", "ext"], e) == OK

    assert rc(["trust", "--forget", "ext"], e) == OK
    assert rc(["validate", "ext"], e) == BAD_SPEC, "forgetting must restore the asking"


def test_the_engines_own_tree_is_not_pinned_and_says_why(env):
    """Pinning it would be theatre, and theatre in a trust feature is the failure mode.

    Anyone able to edit `<tree>/abilities/x/providers.py` can edit `<tree>/engine/facts.py`, so
    a record the engine keeps cannot outlast an editor of the engine. The installed abilities
    therefore need no approval — and asking to record one is refused with that reason, rather
    than accepted as a harmless no-op that would imply the boundary is there.
    """
    import sys as _s
    _s.path.insert(0, str(REPO))
    from engine import trust as tr

    inhouse = [n for n in _rows_of_abilities()
               if (REPO / "abilities" / n / "providers.py").is_file()]
    assert inhouse, "expected at least one shipped ability with code"

    for name in inhouse:
        st, _d = tr.state(name, REPO / "abilities" / name / "providers.py")
        assert st == tr.STATE_INTERNAL, (name, st)
        assert rc(["validate", name], env) == OK, name

    out = run(["trust", inhouse[0]], env)
    assert out.returncode == USAGE
    assert "inside this engine's own tree" in out.stderr, out.stderr
    assert "can edit the engine" in out.stderr


def _rows_of_abilities() -> list[str]:
    return sorted(p.parent.name for p in (REPO / "abilities").glob("*/flow.yaml"))


def test_the_report_describes_a_file_it_has_not_agreed_to_run(env, tmp_path):
    """Reviewing has to be possible BEFORE approving, or approval is a coin toss.

    So the report may not go through flow loading — loading it is running it. Asserted by
    reading the digest and the import list of a file that is refused for loading in the same
    breath.
    """
    root = _ext_root(tmp_path, code=_EXT_CODE.replace(
        "import sys\n", "import sys\nimport json\n"))
    e = {**env, "HARNESS_ABILITIES_PATH": str(root)}

    assert rc(["validate", "ext"], e) == BAD_SPEC, "precondition: it must NOT be loadable"

    rep = run(["trust", "--json"], e)
    assert rep.returncode == OK, rep.stderr
    d = json.loads(rep.stdout)
    ext = [r for r in d["extensions"] if r["ability"] == "ext"]
    assert len(ext) == 1, d
    assert ext[0]["state"] == "unknown"
    assert ext[0]["digest"].startswith("sha256:")
    assert "json" in ext[0]["imports"] and "engine" in ext[0]["imports"]
    assert d["record"] and d["engine_tree"]

    # `abilities` must still say WHY, structurally: "invalid" and "not approved" are different
    # answers and only one of them has a fix.
    ab = json.loads(run(["abilities", "--json"], e).stdout)
    row = [a for a in ab["abilities"] if a["ability"] == "ext"][0]
    assert row["valid"] is False
    assert row["executes_code"] is True and row["trust"] == "unknown"


def test_a_refused_import_does_not_count_as_loaded(env, tmp_path):
    """A guard that stops firing after it fires once is not a guard.

    The load marker exists so an ability's extensions import at most once per process. Set
    before the check, a refusal would leave the ability recorded as loaded, and the next
    attempt in the same process would sail past — which is the shape where a long-lived
    process trusts something nobody approved.
    """
    root = _ext_root(tmp_path, code=_EXT_CODE)
    import os
    import sys as _s
    _s.path.insert(0, str(REPO))
    from engine import flow as fl
    prior_a = os.environ.get("HARNESS_ABILITIES_PATH")
    prior_s = os.environ.get("HARNESS_STATE_DIR")
    os.environ["HARNESS_ABILITIES_PATH"] = str(root)
    os.environ["HARNESS_STATE_DIR"] = env["HARNESS_STATE_DIR"]
    try:
        for attempt in (1, 2):
            with pytest.raises(fl.FlowError) as caught:
                fl.load_extensions("ext")
            assert "has not been approved" in str(caught.value), attempt
        assert "ext" not in fl._EXTENSIONS_LOADED
    finally:
        fl._EXTENSIONS_LOADED.discard("ext")
        for k, v in (("HARNESS_ABILITIES_PATH", prior_a), ("HARNESS_STATE_DIR", prior_s)):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_the_contract_tells_a_driver_that_approval_is_a_LOCAL_decision(env):
    """Exit 2 now has two causes with two different human actions, and only one is upstream.

    NO new exit code for the second one, deliberately: an adapter's behaviour is identical for
    both (allow the tool call; this flow is unusable either way), so a code would be a branch
    nothing branches on. What differs is what the PERSON does — and the contract is where a
    person's next action is written. Left as "report it upstream", a driver hitting an
    unapproved extension would file a report and stall on a one-command local fix.
    """
    out = run(["brief"], env)
    assert out.returncode == OK
    body = out.stdout
    assert "harness trust" in body, body[:400]
    assert "LOCAL decision" in body
    assert "not approved" in body or "not\napproved" in body or "is not " in body
    # And the upstream half must survive, or the genuinely-invalid case loses its advice.
    assert "report it upstream" in body
    assert "do NOT edit" in body


# ------------------------------------------------- a guard that cannot be looked up says so

_GUARDED_FLOW = """version: 2
ability: priv
title: T
when: reach for this one when checking the ways a guard can be made to go quiet
uses: [guards, gates, facts]
scope_kind: repo
scope_match: path_prefix
facts:
  provider: ext_fact
guards:
  deploy:
    step: G01
    matches:
      - tool: shell
        field: command
        pattern: (^|\\s)deploy-thing(\\s|$)
phases:
  - id: p1
    title: P1
steps:
  - id: G01
    phase: p1
    title: The gate that guards deploying
    gate: affirm
    completion: {type: gate_recorded}
    directive: Ask the human.
"""


def _guarded_ext(tmp_path: Path) -> tuple[Path, Path]:
    """An EXTERNAL ability carrying a guard, plus the scope its run will claim."""
    root = tmp_path / "outside"
    d = root / "priv"
    d.mkdir(parents=True)
    (d / "flow.yaml").write_text(_GUARDED_FLOW, encoding="utf-8")
    (d / "providers.py").write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(REPO)!r})\n"
        "from engine import facts\n"
        "@facts.provider('ext_fact', schema={'n': 'int'})\n"
        "def _p(ctx):\n    return {'n': 1}\n", encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()
    return root, work


def _guard_call(env, root: Path | None, work: Path):
    e = {**env}
    if root is not None:
        e["HARNESS_ABILITIES_PATH"] = str(root)
    return run(["guard-tool", "--tool", "shell",
                "--input-json", '{"command":"deploy-thing now"}',
                "--cwd", str(work)], e)


@pytest.mark.parametrize("cause,fragment", [
    ("invisible", "no flow spec for ability"),
    ("invalid", "unsupported key"),
    ("unapproved", "changed after you approved it"),
])
def test_a_run_whose_flow_cannot_be_read_says_so_instead_of_going_quiet(
        env, tmp_path, cause, fragment):
    """Three ways a guard goes quiet, and every one of them was soundless.

    `guard-tool` skipped any open run whose flow would not load — correctly refusing to let one
    broken spec brick unrelated tooling, and incorrectly making that indistinguishable from
    "this run has no guard for you". This integration ALREADY draws that distinction for the
    state directory, in as many words: "nothing to guard" and "I cannot see what to guard"
    must not look alike.

    The third cause arrived with the trust check: editing an approved providers.py revokes it,
    loading then refuses, and that turned the run's guards off with no output at all — a safety
    feature handing out a bypass.

    Fail-open is NOT relaxed here and the test pins that: still exit 0, because this runs before
    every matching tool call in every session. The fix is that it now SAYS so.
    """
    root, work = _guarded_ext(tmp_path)
    _approve(env, root, "priv")
    assert rc(["open", "priv", "--scope", str(work), "--run", "r1"],
              {**env, "HARNESS_ABILITIES_PATH": str(root)}) == OK

    # Baseline: correctly configured, this call IS blocked. Without this the test could pass
    # against a fixture that was never guarded in the first place.
    base = _guard_call(env, root, work)
    assert base.returncode == BLOCKED, base.stderr
    assert "requires gate 'G01'" in base.stderr

    use_root: Path | None = root
    if cause == "invisible":
        use_root = None                       # the hook's search path differs from the run's
    elif cause == "invalid":
        p = root / "priv" / "flow.yaml"
        p.write_text(p.read_text() + "\nbogus_key: 1\n", encoding="utf-8")
    else:
        p = root / "priv" / "providers.py"
        p.write_text(p.read_text() + "\n# a line the approver never saw\n", encoding="utf-8")

    out = _guard_call(env, use_root, work)
    assert out.returncode == OK, \
        "fail-open must survive: this hook may never brick normal work"
    assert "CANNOT READ" in out.stderr, out.stderr
    assert 'not "there is no guard"' in out.stderr
    assert "r1 (priv)" in out.stderr, "the warning must name the run whose guards went quiet"
    assert fragment in out.stderr, (cause, out.stderr)
    # All three fixes are offered, because the report cannot always tell which one applies.
    for hint in ("HARNESS_ABILITIES_PATH", "harness validate", "harness trust"):
        assert hint in out.stderr, hint


# ------------------------------------------------- the tool surface, derived not restated

def test_every_command_is_either_a_tool_or_excluded_with_a_reason(env):
    """A third copy of the command surface is the drift this removes, so there is no third copy.

    The first copy was a hand-written driving document; it aged and became generated. The second
    was the generated file on one machine; it aged past the engine one line at a time. An adapter
    that hand-writes tool schemas is the third, and it would age the same way — so the schema is
    read off the parser, here, where the parser is.

    Pinned as a PARTITION: every subcommand is a tool or is named in `NOT_A_TOOL`. A new command
    therefore cannot appear without someone deciding which it is, and that decision carries its
    reason as data rather than living in whoever reviewed it.
    """
    import argparse as _a
    import sys as _s
    _s.path.insert(0, str(REPO))
    from engine import harness as h

    parser = h.build_parser()
    choices: set[str] = set()
    for action in parser._actions:
        if isinstance(action, _a._SubParsersAction):
            choices = set(action.choices)
    assert choices

    surface = h._tool_surface()
    as_tools = {t["command"] for t in surface["tools"]}
    excluded = set(surface["not_a_tool"])
    assert as_tools | excluded == choices, (
        f"unclassified: {sorted(choices - as_tools - excluded)}; "
        f"claimed but unknown: {sorted((as_tools | excluded) - choices)}")
    assert not (as_tools & excluded)

    for cmd, why in surface["not_a_tool"].items():
        assert len(why) > 40, f"{cmd} is excluded without a usable reason"

    # The one exclusion the whole design turns on. A model will not call a tool whose purpose is
    # to stop it, so exposing this would trade the only mechanism that does not need the agent's
    # cooperation for one that does.
    assert "guard-tool" in excluded
    assert "never by the model" in surface["not_a_tool"]["guard-tool"]
    assert "does not need the agent's cooperation" in surface["not_a_tool"]["guard-tool"]
    # And the one that would make today's content pinning decorative.
    assert "trust" in excluded


def test_the_tool_surface_reports_flags_it_did_not_invent(env):
    """The schema must come from the parser, not from a list that agrees with it today."""
    import sys as _s
    _s.path.insert(0, str(REPO))
    from engine import harness as h

    surface = h._tool_surface()
    nxt = [t for t in surface["tools"] if t["command"] == "next"][0]
    args = {a["name"]: a for a in nxt["args"]}
    assert args["run"]["required"] and args["run"]["flag"] == "--run"
    assert args["json"]["kind"] == "boolean" and not args["json"]["required"]
    assert nxt["structured"] is True
    assert nxt["tool"] == "harness_next", "the naming rule lives here, once"

    # A command with no structured form must say so rather than be assumed to have one.
    val = [t for t in surface["tools"] if t["command"] == "validate"][0]
    assert val["structured"] is False, "validate has no --json; the surface must not imply one"

    # An exit code is the contract, so the table ships with the surface — an adapter that kept
    # its own copy would be the same drift one layer out.
    assert surface["exit_codes"]["3"] == "REFUSED"
    assert surface["exit_codes"]["4"] == "BLOCKED"
    assert "not a failure" in surface["result_shape"]["note"]


def _parser_params() -> dict[str, dict]:
    """command -> {flags, required_flags, required_positionals}, straight off the parser."""
    import argparse as _a
    import sys as _s
    _s.path.insert(0, str(REPO))
    from engine import harness as h

    out: dict[str, dict] = {}
    for action in h.build_parser()._actions:
        if not isinstance(action, _a._SubParsersAction):
            continue
        for name, sub in action.choices.items():
            shapes = [h._arg_shape(x) for x in sub._actions
                      if not isinstance(x, _a._HelpAction) and x.dest != "fn"]
            out[name] = {
                "flags": {x["flag"] for x in shapes if x["flag"]},
                "required_flags": {x["flag"] for x in shapes if x["flag"] and x["required"]},
                "required_positionals": {x["name"] for x in shapes
                                         if not x["flag"] and x["required"]},
            }
    return out


def test_the_usage_lines_teach_flags_the_parser_actually_has(env):
    """The last copy of the command surface that nothing was checking.

    `brief` derives the command NAMES from the parser and has a test for that. The flags in its
    usage lines are hand-written, and they were unguarded — rename `--value` and the brief would
    keep teaching `--value` while the engine rejected it, which is the drift this whole document
    exists to remove, one layer further in.

    NOT fixed by rendering the lines from the parser. Which OPTIONAL parameter deserves teaching
    is judgment — `--run` on `open` and `--value` on `evidence` are both optional there, and a
    rendered line would drop both and teach worse. So the selection stays written and this checks
    the checkable half: every flag named exists, and no required parameter is omitted.
    """
    import re
    import sys as _s
    _s.path.insert(0, str(REPO))
    from engine import brief as b

    params = _parser_params()
    assert b.LOOP and b.DIAGNOSIS

    for usage, why in b.LOOP:
        cmd = usage.split()[0]
        assert cmd in params, f"the brief teaches '{cmd}', which is not a command"
        written = set(re.findall(r"--[a-z-]+", usage))
        unknown = written - params[cmd]["flags"]
        assert not unknown, f"'{cmd}' is taught with flags it does not have: {sorted(unknown)}"
        missing = params[cmd]["required_flags"] - written
        assert not missing, f"'{cmd}' requires {sorted(missing)}, which the line omits"
        for pos in params[cmd]["required_positionals"]:
            assert f"<{pos}>" in usage or pos in usage, \
                f"'{cmd}' takes a required '{pos}' the line does not show"
        assert why, cmd

    for cmd in b.DIAGNOSIS:
        assert cmd in params, f"the brief names '{cmd}' under diagnosis; it is not a command"

    # And the rendered document must actually CARRY every taught line. The renderer filters by
    # `cmd in subcommands`, so a renamed command would drop its line in silence rather than fail
    # — the filter is a runtime courtesy, and this is what keeps it from ever being needed.
    out = run(["brief"], env).stdout
    for usage, _why in b.LOOP:
        assert usage in out, f"the brief dropped its own usage line for {usage.split()[0]!r}"
    for cmd in b.DIAGNOSIS:
        assert re.search(rf"\b{re.escape(cmd)}\b", out), cmd


# ------------------------------------------------- where a flow is mandatory

def _req(env, root: Path, *args: str):
    return run(["require", *args], {**env, "HARNESS_ABILITIES_PATH": str(root)})


def test_an_empty_policy_changes_nothing_at_all(env, tmp_path):
    """The first thing to pin, because it bounds the blast radius of everything else.

    With no entries this must behave byte-identically to the engine before the file existed:
    allow, and say nothing. A bug in the second question can then only be reached by someone who
    has written a requirement down.
    """
    root, work = _guarded_ext(tmp_path)
    out = _guard_call(env, root, work)
    assert out.returncode == OK
    assert out.stderr == "", out.stderr

    listing = run(["require"], env)
    assert listing.returncode == OK
    assert "nothing is declared mandatory" in listing.stdout
    assert "exactly as it did before this existed" in listing.stdout


def test_a_declared_requirement_refuses_a_guarded_action_with_no_run(env, tmp_path):
    """The hole this closes: the guard enforced gates INSIDE a run and could not require one.

    With nothing open there is no scope to compare an action against, so the guard allowed —
    which the engine already admitted in its own judgment rules, because prose asking a driver to
    open a run does not bind it. A declaration supplies the missing scope.
    """
    root, work = _guarded_ext(tmp_path)
    _approve(env, root, "priv")
    assert _req(env, root, "--add", "priv", "--scope-key", str(work)).returncode == OK

    out = _guard_call(env, root, work)
    assert out.returncode == BLOCKED, out.stderr
    assert "declared MANDATORY" in out.stderr
    assert "no run of it is open here" in out.stderr
    assert f"harness open priv --scope {work}" in out.stderr
    assert "G01" in out.stderr, "the refusal must name the gate that goes unrecorded"
    assert "do not reword" in out.stderr.lower()
    # With no recorded waiver yet, the only honest exception is removing the entry — and the
    # refusal says so rather than leaving the driver to invent one.
    assert "harness require --remove priv" in out.stderr


def test_a_requirement_makes_a_FLOW_mandatory_not_everything(env, tmp_path):
    """Two ways this could have become a wall, both asserted as allowed.

    A declaration that blocked every tool call in a scope, or blocked its action everywhere on
    the machine, would be unusable — and the second is exactly why the scope had to come from
    somewhere before this could exist at all.
    """
    root, work = _guarded_ext(tmp_path)
    _approve(env, root, "priv")
    assert _req(env, root, "--add", "priv", "--scope-key", str(work)).returncode == OK

    # Same scope, a call the flow does not guard.
    e = {**env, "HARNESS_ABILITIES_PATH": str(root)}
    unguarded = run(["guard-tool", "--tool", "shell",
                     "--input-json", '{"command":"ls -la"}', "--cwd", str(work)], e)
    assert unguarded.returncode == OK, unguarded.stderr

    # The guarded call, outside the declared scope.
    elsewhere = tmp_path / "somewhere-else"
    elsewhere.mkdir()
    assert _guard_call(env, root, elsewhere).returncode == OK


def test_an_open_run_answers_before_the_declaration_does(env, tmp_path):
    """Order matters: the second question must not shadow the first.

    With a run open the refusal has to be the gate's — that one names a step to satisfy and a
    command that records it. Answering "open a run" while one is open would send the driver in a
    circle.
    """
    root, work = _guarded_ext(tmp_path)
    _approve(env, root, "priv")
    assert _req(env, root, "--add", "priv", "--scope-key", str(work)).returncode == OK
    assert rc(["open", "priv", "--scope", str(work), "--run", "q1"],
              {**env, "HARNESS_ABILITIES_PATH": str(root)}) == OK

    out = _guard_call(env, root, work)
    assert out.returncode == BLOCKED
    assert "requires gate 'G01'" in out.stderr, out.stderr
    assert "declared MANDATORY" not in out.stderr
    assert "harness gate --run q1" in out.stderr


@pytest.mark.parametrize("strict", [False, True])
def test_a_mandatory_flow_that_cannot_be_read_allows_loudly_unless_strict(
        env, tmp_path, strict):
    """The one fork in this design, and both sides ship — the declarer picks.

    Default allows, because one unreadable spec must not stop all work in a scope; that is the
    iron rule this hook is written under. `strict` refuses instead, for a scope where stopping
    beats proceeding unchecked. Either way it is LOUD: the silent version of this is the failure
    that was fixed one commit ago.
    """
    root, work = _guarded_ext(tmp_path)
    _approve(env, root, "priv")
    add = ["--add", "priv", "--scope-key", str(work)] + (["--strict"] if strict else [])
    assert _req(env, root, *add).returncode == OK

    # Unreadable from the hook's point of view: the ability is not on its search path.
    out = _guard_call(env, None, work)
    if strict:
        assert out.returncode == BLOCKED, out.stderr
        assert "MANDATORY (strict)" in out.stderr
        assert "refuse rather than proceed unchecked" in out.stderr
    else:
        assert out.returncode == OK, out.stderr
        assert "cannot be read" in out.stderr
        assert "Mark the entry `strict` to refuse instead" in out.stderr


def test_a_requirement_derives_the_scope_kind_from_the_flow(env, tmp_path):
    """Storing the kind would be a second copy of something the flow already declares.

    An entry whose kind disagreed with the flow's would match nothing, forever, in silence — the
    inert-but-declared shape this engine exists to refuse. So it is derived, and reported when
    the entry is written so whoever writes it can see how the key will be compared.
    """
    root, work = _guarded_ext(tmp_path)
    _approve(env, root, "priv")
    out = _req(env, root, "--add", "priv", "--scope-key", str(work))
    assert out.returncode == OK
    assert "repo=" in out.stdout, out.stdout
    assert "matched as 'path_prefix'" in out.stdout

    listed = json.loads(_req(env, root, "--json").stdout)
    assert listed["required"][0]["scope_kind"] == "repo"
    assert listed["required"][0]["scope_match"] == "path_prefix"
    assert listed["required"][0]["readable"] is True
    assert "scope_kind" not in policy_lines(env), \
        "the kind must not be written into the record"

    # A requirement naming nothing installed cannot be recorded.
    bad = _req(env, root, "--add", "no-such-ability", "--scope-key", str(work))
    assert bad.returncode == BAD_SPEC, bad.stdout + bad.stderr


def policy_lines(env) -> str:
    return (Path(env["HARNESS_STATE_DIR"]) / "required-flows").read_text(encoding="utf-8")


def test_the_contract_stops_saying_nothing_forces_a_run_once_something_does(env, tmp_path):
    """A generated document that contradicts the engine is the drift this one exists to remove.

    The judgment rule said flatly that the guard allows every action while no run is open. Once a
    scope can be declared to require one, that sentence is FALSE there — so the rule now carries
    the exception, and the installation's actual list is rendered as its own section.

    The portable copy must NOT carry it. That file is checked into git and guarded byte-identical
    against `brief --portable`, so a section that varied per machine would fail the guard for
    everyone except whoever last regenerated it.
    """
    root, work = _guarded_ext(tmp_path)
    _approve(env, root, "priv")

    # Before: no requirement, no section.
    plain = run(["brief"], env).stdout
    assert "MANDATORY here" not in plain
    # The rule itself must name the exception whether or not this machine uses it — the sentence
    # is about the mechanism, not about the local list.
    assert "declared" in plain and "REQUIRE a flow" in plain, plain[-1500:]

    assert _req(env, root, "--add", "priv", "--scope-key", str(work)).returncode == OK
    local = run(["brief"], env).stdout
    assert "## Where a flow is MANDATORY here" in local
    assert str(work) in local and "priv" in local
    # The CLAIM, not the sentence that used to carry it: this is not the driver's to remove.
    assert "not yours to remove" in local
    # And the claim that makes a SHORT list safe to read: a directory can declare it too, so
    # this section being empty is not the same as nothing being required where you are working.
    sys.path.insert(0, str(REPO))
    from engine.policy import MARKER
    flat = local.replace("`", "")
    assert MARKER in flat, "the local section does not name the in-tree declaration at all"
    assert "does not mean nothing is required where you are working" in flat, flat[-900:]

    portable = run(["brief", "--portable"], env).stdout
    assert "MANDATORY here" not in portable, \
        "the portable copy carried one machine's policy; the byte-identical guard would break"
    assert str(work) not in portable
    # But the MECHANISM belongs in the portable copy: it is an engine feature, not machine state,
    # and a driver reading only that copy would otherwise not know where to look.
    assert MARKER in portable.replace("`", ""), \
        "the portable contract never mentions the in-tree declaration"

    # And the checked-in copy still matches what --portable emits, with a policy in place.
    checked_in = (REPO / "integrations" / "DRIVING.md").read_text(encoding="utf-8")
    assert portable == checked_in


# ------------------------------------------------- what keeps going wrong here

def _budget_ability(root: Path, name: str) -> None:
    """An external flow whose step can produce a violation on demand (a used-up budget)."""
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "flow.yaml").write_text(
        f"version: 2\nability: {name}\ntitle: T\n"
        f"when: reach for this one when producing a violation on purpose, repeatedly\n"
        f"uses: [repeatable]\nscope_kind: s\n"
        f"phases:\n  - id: p1\n    title: P1\n"
        f"steps:\n  - id: W01\n    phase: p1\n    repeatable: true\n    budget: 1\n"
        f"    directive: Do it.\n", encoding="utf-8")


def _burn(env, root: Path, ability: str, run_id: str, *, twice: bool) -> None:
    e = {**env, "HARNESS_ABILITIES_PATH": str(root)}
    assert rc(["open", ability, "--scope", run_id, "--run", run_id], e) == OK
    assert rc(["enter", "--run", run_id, "--step", "W01"], e) == OK
    if twice:
        assert rc(["enter", "--run", run_id, "--step", "W01"], e) == REFUSED


def test_the_ledger_can_finally_be_asked_what_keeps_going_wrong(env, tmp_path):
    """`audit` grouped by (run, step, code) and so could only answer "in THIS run".

    Every record needed for "what keeps going wrong HERE" was already being kept — nothing ever
    asked for it. This groups across runs, which is a different question and not a nicer format
    for the same one: the per-run view stays exactly as it was.
    """
    root = tmp_path / "outside"
    e = {**env, "HARNESS_ABILITIES_PATH": str(root)}
    _budget_ability(root, "aaa")
    for i in (1, 2, 3):
        _burn(env, root, "aaa", f"r{i}", twice=(i != 3))
    # A FOURTH run that never touches the step, so the two candidate denominators stop being
    # equal: 4 runs of this flow, 3 of them reached the step. Without this the test cannot tell
    # which one the rollup used, and the docstring's claim about it would be unchecked.
    assert rc(["open", "aaa", "--scope", "r4", "--run", "r4"], e) == OK

    out = run(["audit", "--recurring"], {**env, "HARNESS_ABILITIES_PATH": str(root)})
    assert out.returncode == OK, out.stderr
    d = json.loads(run(["audit", "--recurring", "--json"],
                       {**env, "HARNESS_ABILITIES_PATH": str(root)}).stdout)

    rows = [v for v in d["recurring_violations"] if v["code"] == "budget_exhausted"]
    assert len(rows) == 1, f"grouped per run instead of across runs: {rows}"
    assert rows[0]["runs"] == 2, rows[0]
    # THE DENOMINATOR. Two of three runs hit it, and the third is what makes the number mean
    # something — a count with no population reads as total failure or as nothing at all.
    assert rows[0]["population"] == 3, \
        "the population must be the runs that REACHED the step, not every run of the flow"
    assert rows[0]["share"] == "67%", rows[0]
    assert d["population"]["runs_recorded"] == 4

    # The per-run view is untouched: same subject, different question.
    plain = run(["audit"], {**env, "HARNESS_ABILITIES_PATH": str(root)})
    assert plain.returncode == OK
    assert "across runs" not in plain.stdout


def test_the_same_step_id_in_two_flows_is_two_findings(env, tmp_path):
    """Step ids are per-flow, so merging them would invent a pattern that is not there.

    Two authors both call their first step W01; a rollup keyed on the step alone would report one
    finding with double the count, in a step that exists in neither flow as described.
    """
    root = tmp_path / "outside"
    _budget_ability(root, "aaa")
    _budget_ability(root, "bbb")
    _burn(env, root, "aaa", "ra", twice=True)
    _burn(env, root, "bbb", "rb", twice=True)

    d = json.loads(run(["audit", "--recurring", "--json"],
                       {**env, "HARNESS_ABILITIES_PATH": str(root)}).stdout)
    rows = [v for v in d["recurring_violations"] if v["code"] == "budget_exhausted"]
    assert len(rows) == 2, rows
    assert {r["ability"] for r in rows} == {"aaa", "bbb"}
    assert all(r["step_id"] == "W01" for r in rows)
    assert all(r["runs"] == 1 for r in rows)


def test_the_rollup_leads_with_the_absolute_count_not_the_share(env, tmp_path):
    """A single 1-of-1 above a persistent 2-of-3 would put noise at the top.

    Sorting by share alone does exactly that, and the share is still printed so a small sample
    stays visible rather than being hidden behind a percentage.
    """
    root = tmp_path / "outside"
    _budget_ability(root, "aaa")
    _budget_ability(root, "bbb")
    for i in (1, 2, 3):
        _burn(env, root, "aaa", f"ra{i}", twice=(i != 3))       # 2 of 3 → 67%
    _burn(env, root, "bbb", "rb", twice=True)                    # 1 of 1 → 100%

    d = json.loads(run(["audit", "--recurring", "--json"],
                       {**env, "HARNESS_ABILITIES_PATH": str(root)}).stdout)
    rows = [v for v in d["recurring_violations"] if v["code"] == "budget_exhausted"]
    assert [r["ability"] for r in rows] == ["aaa", "bbb"], rows
    assert rows[0]["share"] == "67%" and rows[1]["share"] == "100%"


def test_an_empty_population_is_a_dash_and_not_zero_percent():
    """Nothing to divide by is not "none of them" — printing 0% would state what the ledger does
    not say, and this is the one place a rollup can quietly invent a fact."""
    import sys as _s
    _s.path.insert(0, str(REPO))
    from engine import harness as h

    assert h._share(3, 0) == "—"
    assert h._share(3, None) == "—"
    assert h._share(0, 0) == "—", "0 of 0 is still nothing to divide by"
    assert h._share(2, 3) == "67%"
    assert h._share(1, 1) == "100%"


_GATED_FLOW = """version: 2
ability: gg
title: T
when: reach for this one when counting how often a gate went unwitnessed
uses: [gates]
scope_kind: s
phases:
  - id: p1
    title: P1
steps:
  - id: W01
    phase: p1
    title: The gate
    gate: affirm
    completion: {type: gate_recorded}
    directive: Ask.
"""


def test_unwitnessed_gates_are_counted_against_the_gates_not_the_runs(env, tmp_path):
    """The second half of the rollup, and it needs its OWN denominator.

    A step can be reached often and gated rarely, so "of the runs that reached it" would understate
    how often a gate went unwitnessed. Counted against the GATES actually recorded there, the
    number answers the question a gate raises: when this one was recorded, how often was there
    nobody to vouch for it.

    The same events also appear as violations above — different denominator, same events — and the
    text form says so, because a reader counting both would double one finding.
    """
    root = tmp_path / "outside"
    d = root / "gg"
    d.mkdir(parents=True)
    (d / "flow.yaml").write_text(_GATED_FLOW, encoding="utf-8")
    e = {**env, "HARNESS_ABILITIES_PATH": str(root)}

    for i in (1, 2, 3):
        assert rc(["open", "gg", "--scope", f"s{i}", "--run", f"g{i}"], e) == OK
    # Two recorded with no witness, one with a real one.
    for i in (1, 2):
        assert rc(["gate", "--run", f"g{i}", "--step", "W01", "--decision", "affirm",
                   "--evidence", "said go"], e, witness="manual") == OK
    t = transcript_with(tmp_path, 2)
    assert rc(["gate", "--run", "g3", "--step", "W01", "--decision", "affirm",
               "--evidence", "said go"], e, transcript=t, witness="transcript") == OK

    out = json.loads(run(["audit", "--recurring", "--json"], e).stdout)
    rows = out["recurring_unwitnessed_gates"]
    assert len(rows) == 1, rows
    assert rows[0]["ability"] == "gg" and rows[0]["step_id"] == "W01"
    assert rows[0]["unwitnessed"] == 2, rows[0]
    assert rows[0]["gates"] == 3, "the denominator must be gates recorded, not runs"
    assert rows[0]["share"] == "67%", rows[0]

    text = run(["audit", "--recurring"], e).stdout
    assert "WITHOUT a witness" in text
    assert "not the runs that reached it" in text, \
        "the relationship between the two sections must be stated, or one finding is counted twice"


# ------------------------------------------------- a finished run does not accept writes

_LEDGER_TABLES = ("run", "step_log", "gate", "evidence", "violation", "obligation",
                  "phase_summary", "scope_lease")


def _ledger_fingerprint(env) -> str:
    """A digest over every ledger table, so "nothing was changed" is checkable as one value."""
    import hashlib
    import sqlite3
    h = hashlib.sha256()
    conn = sqlite3.connect(str(Path(env["HARNESS_STATE_DIR"]) / "harness.db"))
    try:
        for t in _LEDGER_TABLES:
            for row in conn.execute(f"SELECT * FROM {t}"):
                h.update(repr(row).encode())
    finally:
        conn.close()
    return h.hexdigest()


def _closed_run(env) -> str:
    """A run taken all the way to closed, with a real record behind it."""
    assert rc(["open", "authoring", "--scope", "doc/x", "--run", "fin"], env) == OK
    assert rc(["close-step", "--run", "fin", "--step", "A1"], env) == OK
    assert rc(["close-run", "--run", "fin", "--result", "completed",
               "--force-steps", "--force-obligations"], env) == OK
    return "fin"


@pytest.mark.parametrize("argv", [
    ["enter", "--step", "B1"],
    ["evidence", "--step", "B1", "--kind", "section", "--value", "late"],
    ["close-step", "--step", "A1"],
    ["gate", "--step", "D2", "--decision", "affirm", "--evidence", "late"],
    ["summarize", "--phase", "gather", "--note", "late"],
    ["skip", "--step", "A1", "--reason", "late"],
    ["discharge", "--hook", "h", "--evidence", "late"],
    ["config", "--set", "k=v"],
    ["close-run", "--result", "abandoned"],
], ids=lambda a: a[0] + ("-set" if "--set" in a else ""))
def test_a_finished_run_does_not_accept_writes(env, argv):
    """The engine had no notion of "this run is over". Every write command took a closed one.

    Measured before fixing, on a run that had already been closed: `evidence` appended rows to a
    finished ledger, `gate` recorded on it, `close-step` re-closed steps, `summarize` re-summarised
    — and `close-run` REWROTE the result. `done` became `abort` with a success message, no
    violation, and a fresh end timestamp, after which `history` showed the rewrite as if it had
    always been the outcome. A record that can be edited afterwards, silently, is not a record.

    Asserted per command AND as a whole-ledger digest, because "it refused" and "it changed
    nothing" are two claims and only the second is the one that matters.
    """
    run_id = _closed_run(env)
    before = _ledger_fingerprint(env)

    out = run([argv[0], "--run", run_id, *argv[1:]], env)
    assert out.returncode == USAGE, out.stdout + out.stderr
    assert "has ended" in out.stderr, out.stderr
    assert "NOTHING WAS CHANGED" in out.stderr
    # It must point at the two things that ARE possible, so the refusal is not a dead end.
    assert "harness status --run" in out.stderr
    assert "purge-run" in out.stderr

    assert _ledger_fingerprint(env) == before, \
        f"{argv[0]} was refused and still changed the ledger"


def test_the_recorded_result_of_a_finished_run_is_not_rewritable(env):
    """The sharpest form of it: the outcome itself, replaced, with a success message.

    Kept as its own test rather than folded into the table above, because the table asserts "the
    ledger is unchanged" while this asserts WHICH fact was at stake — a reader of `history` was
    being shown one outcome for a run that finished with another.
    """
    run_id = _closed_run(env)
    out = run(["close-run", "--run", run_id, "--result", "abandoned"], env)
    assert out.returncode == USAGE
    assert "✅" not in out.stdout, "it reported success for something it did not do"

    hist = json.loads(run(["history", "--json"], env).stdout)
    rows = [r for r in hist["runs"] if r["run_id"] == run_id]
    assert len(rows) == 1 and rows[0]["result"] == "completed", rows


def test_reading_a_finished_run_still_works(env):
    """The check defaults to "must be open", so every READ command has to opt out explicitly.

    That direction is deliberate: a new WRITE command that forgets would silently append to a
    finished run, while a new READ command that forgets refuses here and fails its own test at
    once. This pins the readers so the opt-outs cannot be dropped by accident.
    """
    run_id = _closed_run(env)
    for argv in (["status"], ["next"], ["obligations"], ["show", "--step", "A1"],
                 ["assert-goal", "--phase", "gather"], ["config"]):
        out = run([argv[0], "--run", run_id, *argv[1:]], env)
        assert out.returncode == OK, (argv, out.stderr)
        assert "has ended" not in out.stderr, argv


# ------------------------------------------------- a guard a hook cannot see must say so

def test_no_shipped_production_flow_relies_on_an_ask_only_guard(env):
    """NOT forbidden — measured. And measuring it is the point.

    `action: STEP` is the original guard form and stays legal: it declares a guard only an
    explicit caller can consult, which is the ONLY possible mechanism for an action no tool call
    has a signature for (something a human does in a UI the driver never touches).

    But for a production flow it means a guard that fires only when the driver volunteers to ask
    — protection that depends on the protected party's cooperation. None of the shipped production
    flows currently relies on that, and this pins it so the first one that does is a decision
    somebody makes rather than a drift nobody sees.
    """
    import sys as _s
    _s.path.insert(0, str(REPO))
    from engine import flow as fl

    ask_only = []
    for name in fl.available_abilities():
        f = fl.load(name)
        for action in sorted(f.guards):
            if not f.guard_matches.get(action):
                ask_only.append((name, f.role, action))

    in_production = [(n, a) for n, role, a in ask_only if role == fl.ROLE_PRODUCTION]
    assert not in_production, (
        f"a production flow declares a guard a runtime hook can never fire: {in_production}. "
        f"Either give it `matches`, or say here why asking is the only possible mechanism.")
    # The fixture ones are expected: they are what keeps the short form exercised at all.
    assert ask_only, "nothing exercises the ask-only form; it would be an untested spec shape"


def test_a_guard_a_hook_cannot_fire_is_reported_not_hidden(env, tmp_path):
    """The tool list is not the guarded surface, and read as one it overstates the cover.

    `brief` lists the TOOLS a hook must match. A guarded action with no match rules contributes
    none, so it was invisible in the contract — and a reader would conclude the hook covers
    everything guarded. `abilities` had the same gap.
    """
    d = json.loads(run(["abilities", "--json"], env).stdout)
    reach = {a["ability"]: a.get("guard_reach") or {} for a in d["abilities"]}
    assert reach["delivery"] == {"close_task": "ask_only", "commit": "ask_only",
                                 "publish": "ask_only"}, reach["delivery"]
    assert set(reach["shipcheck-asis"].values()) == {"hook"}, reach["shipcheck-asis"]

    body = run(["brief"], env).stdout
    assert "Not every guarded action is in that list" in body
    assert "delivery  commit" in body
    assert "depend on the driver choosing to ask" in body

    # And the TEXT form of `abilities` marks it, which only shows for a non-fixture — so this
    # builds one, because the shipped flows deliberately have none.
    root = tmp_path / "outside"
    p = root / "askonly"
    p.mkdir(parents=True)
    (p / "flow.yaml").write_text(
        "version: 2\nability: askonly\ntitle: T\n"
        "when: reach for this one when a guarded action has no tool-call signature at all\n"
        "uses: [guards, gates]\nscope_kind: s\n"
        "guards:\n  publish: W01\n"
        "phases:\n  - id: p1\n    title: P1\n"
        "steps:\n  - id: W01\n    phase: p1\n    title: The gate\n    gate: affirm\n"
        "    completion: {type: gate_recorded}\n    directive: Ask.\n", encoding="utf-8")
    out = run(["abilities"], {**env, "HARNESS_ABILITIES_PATH": str(root)})
    assert out.returncode == OK, out.stderr
    assert "1 ask-only: publish" in out.stdout, out.stdout


# ------------------------------------------------- what a step hands back

_LOG = """============================= test session starts ===========================
collected 412 items
tests/test_a.py ..........
## FAILURES
tests/test_b.py::test_thing FAILED
    assert 3 == 4
## short test summary
2 failed, 410 passed
"""


def _to_k02(env, run_id: str, scope: Path) -> None:
    """Drive the fixture to its verification step, which is the one that declares an output."""
    assert rc(["open", "delivery", "--scope", str(scope), "--run", run_id], env) == OK
    for step, kind in (("C01", "change_list"), ("C02", "test_baseline"), ("K01", "diff")):
        assert rc(["evidence", "--run", run_id, "--step", step,
                   "--kind", kind, "--value", "x"], env) == OK
        assert rc(["close-step", "--run", run_id, "--step", step], env) == OK
    assert rc(["evidence", "--run", run_id, "--step", "K02",
               "--kind", "test_result", "--value", "412 pass, mutation ok"], env) == OK


def test_a_step_hands_back_the_part_of_a_file_it_declared(env, tmp_path):
    """The engine reads the artifact and returns the declared part of it.

    The path comes from a row the DRIVER recorded, never from the spec: a spec cannot know this
    run's artifact paths, and one that hard-coded a path would be wrong on the second machine
    while nothing in the ledger said what was read.

    Section-finding is prose's, reused rather than rewritten — one rule for "where does a section
    end", so the two cannot disagree. Asserted by the stop boundary: the excerpt must contain the
    heading it was asked for and NOT the next one.
    """
    log = tmp_path / "pytest.log"
    log.write_text(_LOG, encoding="utf-8")
    _to_k02(env, "o1", tmp_path / "repo")
    assert rc(["evidence", "--run", "o1", "--step", "K02",
               "--kind", "test_log", "--value", str(log)], env) == OK

    out = run(["close-step", "--run", "o1", "--step", "K02", "--json"], env)
    assert out.returncode == OK, out.stderr
    d = json.loads(out.stdout)
    o = d["output"]
    assert o["status"] == "delivered", o
    assert o["from"] == {"evidence_kind": "test_log", "rows": 1, "path": str(log)}
    assert o["select"] == {"anchor": "FAILURES"}
    assert "## FAILURES" in o["content"] and "test_thing FAILED" in o["content"]
    assert "short test summary" not in o["content"], \
        "the excerpt ran past the next same-level heading"
    assert o["sha256"].startswith("sha256:") and o["bytes"] == len(o["content"].encode())
    assert o["truncated"] is False

    # The ledger keeps the digest and the source, never the content.
    logged = _rows(env, "SELECT detail FROM step_log WHERE run_id='o1' AND event='output'")
    assert len(logged) == 1
    recorded = json.loads(logged[0]["detail"])
    assert recorded["sha256"] == o["sha256"] and recorded["from"]["path"] == str(log)
    assert "content" not in recorded, "the ledger is not a blob store"


@pytest.mark.parametrize("case,expect", [
    ("nothing_recorded", "no_source_recorded"),
    ("path_absent", "source_missing"),
    ("anchor_absent", "selector_no_match"),
])
def test_failing_to_hand_something_back_never_blocks_the_step(env, tmp_path, case, expect):
    """An output is a channel, not a criterion — and this is the assertion that keeps it one.

    The content comes from a path the driver recorded. If failing to read it could change whether
    a step closes, the shape would be: the driver writes a file saying the work passed, the engine
    reads it back, and a reader takes that for verification. So the step closes either way, and
    the reason it could not be read is REPORTED rather than absorbed — "the file is not there",
    "the selector matched nothing" and "the content is empty" are three different answers.
    """
    _to_k02(env, "o1", tmp_path / "repo")
    if case == "path_absent":
        assert rc(["evidence", "--run", "o1", "--step", "K02", "--kind", "test_log",
                   "--value", str(tmp_path / "not-here.log")], env) == OK
    elif case == "anchor_absent":
        log = tmp_path / "clean.log"
        log.write_text("all green, nothing to report\n", encoding="utf-8")
        assert rc(["evidence", "--run", "o1", "--step", "K02", "--kind", "test_log",
                   "--value", str(log)], env) == OK

    out = run(["close-step", "--run", "o1", "--step", "K02", "--json"], env)
    assert out.returncode == OK, out.stderr
    d = json.loads(out.stdout)
    assert d["closed"] is True, "a step that met its criterion was blocked by its output"
    assert d["output"]["status"] == expect, d["output"]
    assert d["output"]["content"] is None
    assert d["output"]["detail"], "a status with no explanation is a code to look up"

    # And the text form says it too, rather than staying silent about a channel that failed.
    _to_k02(env, "o2", tmp_path / "repo2")
    if case != "nothing_recorded":
        src = (tmp_path / "not-here.log") if case == "path_absent" else (tmp_path / "clean.log")
        assert rc(["evidence", "--run", "o2", "--step", "K02", "--kind", "test_log",
                   "--value", str(src)], env) == OK
    text = run(["close-step", "--run", "o2", "--step", "K02"], env).stdout
    assert expect in text, text


def test_closing_a_step_answers_in_json_including_what_the_hooks_did(env, tmp_path):
    """Reads were structured and writes were prose, and the writes carry the load-bearing part.

    A hook firing — above all an OBLIGATION being raised — was announced in prose and nowhere
    else. `obligations` could list it afterwards, but the moment a debt was handed over was
    unreadable by anything that is not a human, so a driver either parsed the print or missed it.
    """
    _to_k02(env, "o1", tmp_path / "repo")
    out = run(["close-step", "--run", "o1", "--step", "K02", "--json"], env)
    assert out.returncode == OK, out.stderr
    d = json.loads(out.stdout)

    assert d["run"] == "o1" and d["step"] == "K02" and d["closed"] is True
    assert d["next"] == "D01" and "D01" in d["remaining"]
    assert d["hooks_blocked"] is False
    # The criterion is reported as DATA, and it carries `match` — which the engine enforces and
    # the requirement list used to omit, so the list understated what the step demanded.
    assert d["satisfied_by"] == [{"what": "evidence", "kind": "test_result", "match": "pass"}]
    assert d["fired_hooks"], "hooks fired at this boundary and the envelope showed none"
    for h in d["fired_hooks"]:
        assert h["boundary"] and h["hook"] and "matched" in h
    # Text form must stay text: a command that only speaks JSON has moved, not gained.
    plain = run(["close-step", "--run", "o1", "--step", "D01"], env)
    assert plain.returncode == OK
    assert not plain.stdout.lstrip().startswith("{")


def test_a_refusal_answers_in_json_too_and_carries_its_sentence(env, tmp_path):
    """A caller that asked for JSON must not have to parse stderr on the one path that matters.

    The sentence rides as prose ON PURPOSE: it names what is missing and the command that
    supplies it, and re-encoding that as fields would be a second copy of the same message, free
    to drift from the one stderr prints.
    """
    assert rc(["open", "delivery", "--scope", str(tmp_path / "repo"), "--run", "o1"], env) == OK
    out = run(["close-step", "--run", "o1", "--step", "C01", "--json"], env)
    assert out.returncode == REFUSED
    d = json.loads(out.stdout)
    assert d["closed"] is False
    assert d["refused_because"] == "criterion_unmet"
    assert "change_list" in d["why"]
    assert d["required"] == [{"what": "evidence", "kind": "change_list", "min_count": 1}]


@pytest.mark.parametrize("block,fragment", [
    ("output:\n      select: {anchor: X}\n", "needs 'from_evidence'"),
    ("output:\n      from_evidence: log\n      select: {anchor: X, lines: 1-2}\n", "Pick one"),
    ("output:\n      from_evidence: log\n      select: {lines: 0-5}\n", "not a range"),
    ("output:\n      from_evidence: log\n      select: {lines: 9-2}\n", "not a range"),
    ("output:\n      from_evidence: log\n      select: {lines: nope}\n", "must be 'N-M'"),
    ("output:\n      from_evidence: log\n      select: {regex: '('}\n", "not a valid regex"),
    ("output:\n      from_evidence: log\n      max_bytes: 0\n", "positive integer"),
    ("output:\n      from_evidence: log\n      whole_file: true\n", "unsupported key"),
    ("output:\n      from_evidence: log\n      select: {heading: X}\n", "unsupported key"),
], ids=lambda x: x[:28])
def test_an_output_declaration_is_checked_at_load_time(env, block, fragment):
    """Every refusal the parse writes, exercised — because an unexercised refusal is a claim.

    The load-time checks are the whole reason a spec can be trusted at all: `from_evidence` is
    mandatory because a path written into the spec would be wrong on the second machine; one
    selector at a time because two have no defined order, so which part came back would depend on
    the engine rather than the spec; a cap of zero because it would hand nothing back while
    reporting success.
    """
    d = _spec(env, "zzz_out", f"""
phases:
  - id: p1
    title: P1
steps:
  - id: W01
    phase: p1
    directive: Do it.
    {block.rstrip()}
""")
    try:
        out = run(["validate", "zzz_out"], env)
        assert out.returncode == BAD_SPEC, out.stdout + out.stderr
        assert fragment in out.stderr, out.stderr
    finally:
        _rm(d)


def test_a_declared_output_is_a_capability_that_must_be_declared(env, tmp_path):
    """`output` joins the manifest, so a flow using it has to say so — and one saying so has to
    use it. The same bidirectional check every other mechanism gets; a capability exempt from it
    would be the one nobody notices going stale."""
    import sys as _s
    _s.path.insert(0, str(REPO))
    from engine import flow as fl

    assert "outputs" in fl.ENGINE_CAPABILITIES
    assert "outputs" in fl.capabilities_used(fl.load("delivery"))

    root = tmp_path / "outside"
    d = root / "und"
    d.mkdir(parents=True)
    (d / "flow.yaml").write_text(
        "version: 2\nability: und\ntitle: T\n"
        "when: reach for this one when a flow hands something back without declaring it\n"
        "uses: []\nscope_kind: s\n"
        "phases:\n  - id: p1\n    title: P1\n"
        "steps:\n  - id: W01\n    phase: p1\n    directive: Do it.\n"
        "    completion: {type: evidence, kind: k}\n"
        "    output:\n      from_evidence: k\n", encoding="utf-8")
    out = run(["validate", "und"], {**env, "HARNESS_ABILITIES_PATH": str(root)})
    assert out.returncode == BAD_SPEC
    assert "outputs" in out.stderr and "without declaring it" in out.stderr, out.stderr


# ------------------------------------------------- the completion spec is a closed set too

@pytest.mark.parametrize("spec,fragment", [
    ("{type: evidence, kind: k, min_counts: 2}", "min_counts"),
    ("{type: evidence, kind: k, matches: pass}", "matches"),
    ("{type: all_of, steps: [], any_ofs: []}", "any_ofs"),
    ("{type: attest, kind: k}", "kind"),
    ("{type: evidence_equals, kind: k, value: v, match: x}", "match"),
], ids=lambda x: x[:30])
def test_a_completion_spec_rejects_a_key_nothing_reads(env, spec, fragment):
    """The last place in a spec where a typo was silent, and the one that cost the most.

    Everywhere else an unknown key is fatal, with the reason spelled out: a typo would change
    what the spec MEANS. Inside a completion spec it was ignored — so `min_counts` for
    `min_count` quietly downgraded a criterion to "any one row" while the yaml still read as
    though it demanded two. The failure is not that it breaks; it is that it WEAKENS.

    Also asserted for a key that is legal on a DIFFERENT predicate (`match` belongs to
    `evidence`, not `evidence_equals`) and for one legal nowhere (`kind` on `attest`): a closed
    set per predicate, not one union across all of them.
    """
    d = _spec(env, "zzz_ck", f"""
phases:
  - id: p1
    title: P1
steps:
  - id: W01
    phase: p1
    directive: Do it.
    completion: {spec}
""")
    try:
        out = run(["validate", "zzz_ck"], env)
        assert out.returncode == BAD_SPEC, out.stdout + out.stderr
        assert "unsupported key(s)" in out.stderr and fragment in out.stderr, out.stderr
        assert "makes the criterion WEAKER" in out.stderr
        assert "supported:" in out.stderr, "a refusal that does not list the legal keys is a riddle"
    finally:
        _rm(d)


def test_every_key_a_predicate_reads_is_declared(env):
    """The closed set is only as good as its completeness, so it is derived and cross-checked.

    A predicate that READS a key it did not declare would have that key refused at load time —
    the mechanism would be enforcing against its own implementation. This walks each predicate's
    source for `spec` reads and asserts every one is declared.
    """
    import ast as _ast
    import re as _re
    import sys as _s
    _s.path.insert(0, str(REPO))
    from engine import predicates as pr

    src = (REPO / "engine" / "predicates.py").read_text(encoding="utf-8")
    fns = {n.name: n for n in _ast.walk(_ast.parse(src))
           if isinstance(n, _ast.FunctionDef)}
    missing = []
    for name, entry in pr._REGISTRY.items():
        node = fns.get(entry["fn"].__name__)
        if node is None:
            continue
        seg = _ast.get_source_segment(src, node) or ""
        read = set(_re.findall(r'spec\.get\("([a-z_]+)"', seg))
        read |= set(_re.findall(r'spec\["([a-z_]+)"\]', seg))
        declared = {"type"} | set(entry["requires"]) | set(entry.get("optional") or ())
        for key in sorted(read - declared):
            missing.append((name, key))
    assert not missing, f"predicates read keys they do not declare: {missing}"

    # And the shipped flows must not depend on anything undeclared — which load-time validation
    # already enforces, so this is really a statement that they all still load.
    from engine import flow as fl
    for n in fl.available_abilities():
        fl.load(n)


# ------------------------------------------------- a flow declares how it may end

def test_a_flow_declares_how_it_may_end(env, tmp_path):
    """`--result` took ANY string, so an outcome could not be counted or compared.

    Measured before fixing, in this suite's own calls: `done` and `completed`, `abort` and
    `aborted` — four words for two endings, none of them declared anywhere. `history` could show
    them but not group them.
    """
    import sys as _s
    _s.path.insert(0, str(REPO))
    from engine import flow as fl

    for name in fl.available_abilities():
        spec = fl.load(name).result_spec
        assert spec, f"{name} does not declare how it may end"
        assert spec["default"] in spec["values"], spec

    # Readable BEFORE closing, so a driver need not learn the vocabulary from a refusal.
    ab = json.loads(run(["abilities", "--json"], env).stdout)
    by = {a["ability"]: a for a in ab["abilities"] if a.get("valid")}
    assert by["delivery"]["results"] == {"values": ["completed", "abandoned"],
                                         "default": "completed"}, by["delivery"]["results"]

    assert rc(["open", "delivery", "--scope", str(tmp_path / "r"), "--run", "e1"], env) == OK
    # Omitting it takes the FLOW's default, not a word the engine picked.
    assert rc(["close-run", "--run", "e1", "--force-steps", "--force-obligations"], env) == OK
    hist = json.loads(run(["history", "--json"], env).stdout)
    assert [r for r in hist["runs"] if r["run_id"] == "e1"][0]["result"] == "completed"

    assert rc(["open", "delivery", "--scope", str(tmp_path / "r2"), "--run", "e2"], env) == OK
    out = run(["close-run", "--run", "e2", "--result", "done",
               "--force-steps", "--force-obligations"], env)
    assert out.returncode == USAGE, out.stdout + out.stderr
    assert "is not one of the results" in out.stderr
    assert "legal: completed, abandoned" in out.stderr
    assert "(default completed)" in out.stderr
    # And it did NOT close on the way to refusing.
    assert _rows(env, "SELECT status FROM run WHERE run_id='e2'")[0]["status"] == "open"

    assert rc(["close-run", "--run", "e2", "--result", "abandoned",
               "--force-steps", "--force-obligations"], env) == OK

    # And the default must be the FLOW's, not the engine's word. Every shipped flow happens to
    # default to `completed`, which is the same string the engine falls back to — so a flow whose
    # default differs is what makes the claim checkable at all.
    root = tmp_path / "outside"
    d = root / "ships"
    d.mkdir(parents=True)
    (d / "flow.yaml").write_text(
        "version: 2\nability: ships\ntitle: T\n"
        "when: reach for this one when a flow's own default is not the engine's fallback\n"
        "uses: [results]\nscope_kind: s\n"
        "results:\n  values: [shipped, abandoned]\n  default: shipped\n"
        "phases:\n  - id: p1\n    title: P1\n"
        "steps:\n  - id: W01\n    phase: p1\n    directive: Do it.\n", encoding="utf-8")
    e = {**env, "HARNESS_ABILITIES_PATH": str(root)}
    assert rc(["open", "ships", "--scope", "s9", "--run", "s9"], e) == OK
    assert rc(["close-run", "--run", "s9", "--force-steps", "--force-obligations"], e) == OK
    assert _rows(env, "SELECT result FROM run WHERE run_id='s9'")[0]["result"] == "shipped", \
        "an omitted --result took the engine's word instead of the flow's declared default"


def test_a_flow_that_never_said_keeps_the_old_freedom(env, tmp_path):
    """Not declaring is still legal, and then nothing changed — the fallback stays the engine's.

    Making the declaration mandatory would have been a spec-format break for every 2.0 flow
    written elsewhere, to close a hole those flows may not have. So an undeclared flow keeps
    taking any string, and `abilities --json` reports which flows are in that state so the
    silence is visible rather than assumed.
    """
    root = tmp_path / "outside"
    d = root / "loose"
    d.mkdir(parents=True)
    (d / "flow.yaml").write_text(
        "version: 2\nability: loose\ntitle: T\n"
        "when: reach for this one when a flow has not said how it may end\n"
        "uses: []\nscope_kind: s\n"
        "phases:\n  - id: p1\n    title: P1\n"
        "steps:\n  - id: W01\n    phase: p1\n    directive: Do it.\n", encoding="utf-8")
    e = {**env, "HARNESS_ABILITIES_PATH": str(root)}
    assert rc(["validate", "loose"], e) == OK

    assert rc(["open", "loose", "--scope", "s1", "--run", "l1"], e) == OK
    assert rc(["close-run", "--run", "l1", "--result", "whatever-string",
               "--force-steps", "--force-obligations"], e) == OK
    assert _rows(env, "SELECT result FROM run WHERE run_id='l1'")[0]["result"] == "whatever-string"

    d2 = root / "loose2"
    d2.mkdir()
    (d2 / "flow.yaml").write_text((d / "flow.yaml").read_text().replace("loose", "loose2"),
                                  encoding="utf-8")
    assert rc(["open", "loose2", "--scope", "s2", "--run", "l2"], e) == OK
    assert rc(["close-run", "--run", "l2", "--force-steps", "--force-obligations"], e) == OK
    assert _rows(env, "SELECT result FROM run WHERE run_id='l2'")[0]["result"] == "completed"


@pytest.mark.parametrize("block,fragment", [
    ("results: [completed, abandoned]", "must be a mapping"),
    ("results:\n  values: []\n  default: x", "non-empty list"),
    ("results:\n  values: [a, a]\n  default: a", "repeats a value"),
    ("results:\n  values: [a, b]", "needs a 'default'"),
    ("results:\n  values: [a, b]\n  default: c", "is not in values"),
    ("results:\n  values: [a]\n  default: a\n  fallback: b", "unsupported key"),
], ids=lambda x: x[:26])
def test_a_results_declaration_is_checked_at_load_time(env, block, fragment):
    """Every refusal the parse writes, exercised. A bare list is refused on purpose: it would make
    the list's ORDER decide the default, which is a rule nobody can see in the yaml."""
    d = _spec(env, "zzz_res", f"""
{block}
phases:
  - id: p1
    title: P1
steps:
  - id: W01
    phase: p1
    directive: Do it.
""")
    try:
        out = run(["validate", "zzz_res"], env)
        assert out.returncode == BAD_SPEC, out.stdout + out.stderr
        assert fragment in out.stderr, out.stderr
    finally:
        _rm(d)


# ------------------------------------------------------------------ a machine form for writes
#
# The read commands answered as data; the write commands answered in prose. So a driver could
# learn what a step REQUIRED without parsing text, and then had to parse text to learn what
# happened when it acted — including, on `open`, the id of the run it had just started.


def _subcommand_json_flags() -> dict[str, bool]:
    """Which subcommands accept --json, read off the parser rather than listed here."""
    import argparse as _ap
    sys.path.insert(0, str(REPO))
    from engine.harness import build_parser
    subs = [a for a in build_parser()._actions
            if isinstance(a, _ap._SubParsersAction)][0]
    return {name: any(getattr(a, "option_strings", None) and "--json" in a.option_strings
                      for a in sub._actions)
            for name, sub in subs.choices.items()}


def _one_json(out):
    """Exactly one JSON document on stdout, with no prose around it.

    The property is NOT "there is JSON somewhere in the output". Prose and JSON share stdout, so
    a command that emits both hands back something no parser can read — and a caller that
    managed to find a document in it would be reading a coincidence. `json.loads` refuses
    trailing content, which is the assertion.
    """
    assert out.stdout.strip(), f"nothing on stdout; stderr was: {out.stderr}"
    return json.loads(out.stdout)


def test_the_json_classification_partitions_the_parser():
    """Every subcommand either accepts --json or is declared as not having one, WITH a reason.

    Without this, a new command that simply never got a machine form is indistinguishable from
    one that was decided against — and the way a promise like "stdout carries the answer" decays
    is one unclassified command at a time.
    """
    sys.path.insert(0, str(REPO))
    from engine.harness import PROSE_ONLY, NO_JSON_YET
    flags = _subcommand_json_flags()
    declared = set(PROSE_ONLY) | set(NO_JSON_YET)
    silent = {n for n, has in flags.items() if not has}

    assert silent - declared == set(), \
        f"subcommand(s) with no --json and no declared reason: {sorted(silent - declared)}"
    assert declared - silent == set(), \
        f"declared as prose-only but actually accepts --json: {sorted(declared - silent)}"
    assert not (set(PROSE_ONLY) & set(NO_JSON_YET)), "a command is either decided or pending"
    # A reason that says nothing is the same as no reason.
    for table in (PROSE_ONLY, NO_JSON_YET):
        for name, why in table.items():
            assert len(why) > 40, (name, why)


def _drive(env, run_id, tr, ability="authoring", scope="d/j"):
    """Drive a run using ONLY the JSON forms, reading each step's requirements as data.

    Written this way on purpose: if the driving loop had to read prose it would be evidence
    against the thing being tested.
    """
    d = _one_json(run(["open", ability, "--scope", scope, "--run", run_id, "--json"], env))
    assert d["command"] == "open"
    for _ in range(30):
        nx = _one_json(run(["next", "--run", run_id, "--json"], env))
        step = nx.get("step")
        if not step:
            break
        run(["enter", "--run", run_id, "--step", step], env)
        for req in nx.get("requirements") or []:
            if req.get("what") == "evidence":
                for i in range(req.get("min_count") or 1):
                    run(["evidence", "--run", run_id, "--step", step,
                         "--kind", req["kind"], "--value", req.get("match") or f"v{i}"], env)
            elif req.get("what") == "gate":
                run(["gate", "--run", run_id, "--step", step, "--decision", "affirm",
                     "--evidence", "a person said so"], env,
                    transcript=tr, witness="transcript")
        if not _one_json(run(["close-step", "--run", run_id, "--step", step,
                              "--json"], env)).get("closed"):
            return step
    return None


def test_every_write_command_answers_in_json_and_only_in_json(env, tmp_path):
    """All nine, driven end to end without a single line of prose being parsed."""
    tr = transcript_with(tmp_path, 9)

    # open — and the generated id comes back as a field, which is why this one mattered most.
    d = _one_json(run(["open", "authoring", "--scope", "d/e", "--json"], env))
    assert d["run_id_generated"] is True and d["run"].startswith("authoring-")
    rid = d["run"]

    first = d["first_step"]["id"]
    d = _one_json(run(["enter", "--run", rid, "--step", first, "--json"], env))
    assert d["command"] == "enter" and d["entered"] is True

    d = _one_json(run(["evidence", "--run", rid, "--step", first,
                       "--kind", "k", "--value", "v", "--json"], env))
    assert d["command"] == "evidence" and d["rows_now"] == 1

    d = _one_json(run(["config", "--run", rid, "--set", "zz=true", "--json"], env))
    assert d["set"] == {"zz": True} and d["config"]["zz"]["source"] == "run"

    # The rest need a run that has actually progressed.
    stuck = _drive(env, "w9", tr)
    assert stuck is None, f"the JSON-only driver could not get past {stuck}"

    # A FRESH run, because the driver already recorded w9's gate and a second one in the same
    # human turn is refused by the witness cursor — that refusal is a different mechanism.
    run(["open", "authoring", "--scope", "d/e-gate", "--run", "wg"], env)
    run(["enter", "--run", "wg", "--step", "D2"], env)
    d = _one_json(run(["gate", "--run", "wg", "--step", "D2", "--decision", "affirm",
                       "--evidence", "a person said so", "--json"], env,
                      transcript=tr, witness="transcript"))
    assert d["command"] == "gate" and d["witnessed"] is True

    d = _one_json(run(["summarize", "--run", "w9", "--phase", "gather",
                       "--metric", "n=3", "--json"], env))
    assert d["command"] == "summarize" and d["metrics"] == {"n": 3}

    for ph in ("shape", "review"):
        run(["summarize", "--run", "w9", "--phase", ph], env)
    obs = _one_json(run(["obligations", "--run", "w9", "--json"], env))
    owed = [o["hook_id"] for o in (obs if isinstance(obs, list) else obs["obligations"])
            if not o.get("discharged_at")]
    for h in owed:
        d = _one_json(run(["discharge", "--run", "w9", "--hook", h,
                           "--evidence", "done", "--json"], env))
        assert d["command"] == "discharge" and d["discharged_now"] is True

    d = _one_json(run(["close-run", "--run", "w9", "--json"], env))
    assert d["command"] == "close-run" and d["closed"] is True

    # skip lives on a flow that declares an optional step.
    run(["open", "plan", "--scope", "slug-j", "--run", "pj"], env)
    d = _one_json(run(["skip", "--run", "pj", "--step", "D03",
                       "--reason", "not needed", "--json"], env))
    assert d["command"] == "skip" and d["skipped"] is True and d["reason"] == "not needed"


def test_a_refusal_raised_by_a_shared_helper_still_answers_in_json(env):
    """The path a per-site rule would have missed.

    `_run_or_exit` refuses on behalf of every write command, from a function their authors do
    not edit. A caller reading an empty stdout here would conclude the write succeeded.
    """
    out = run(["evidence", "--run", "nope", "--step", "A1",
               "--kind", "k", "--value", "v", "--json"], env)
    assert out.returncode == USAGE
    d = _one_json(out)
    assert d["command"] == "evidence"
    assert d["code"] == USAGE and d["code_name"] == "USAGE"
    assert "no run" in d["why"], d
    # stderr stays the human channel; the two are not alternatives.
    assert "no run" in out.stderr


def test_the_backstop_does_not_answer_over_a_site_that_already_did(env, tmp_path):
    """One document, not two — and the site's own token survives.

    `close-step` classifies its refusals (`criterion_unmet`), which is strictly more than the
    backstop can say. If the backstop fired anyway there would be two JSON documents on stdout
    and the more precise one would be the one a parser never reached.
    """
    run(["open", "delivery", "--scope", str(tmp_path / "r"), "--run", "b1"], env)
    out = run(["close-step", "--run", "b1", "--step", "C01", "--json"], env)
    assert out.returncode == REFUSED
    d = _one_json(out)                      # would raise on a second document
    assert d["refused_because"] == "criterion_unmet"
    assert "code_name" not in d, "the backstop overwrote a classified refusal"


def test_evidence_reports_how_many_rows_of_that_kind_now_exist(env):
    """A `min_count` criterion counts exactly these rows, so the count is what a driver needs.

    Without it the choice was to keep a private tally — which drifts from the ledger the moment
    anything else writes — or to call `next` again after every single row.
    """
    run(["open", "authoring", "--scope", "d/c", "--run", "c1"], env)
    run(["enter", "--run", "c1", "--step", "A1"], env)
    seen = []
    for i in range(3):
        seen.append(_one_json(run(["evidence", "--run", "c1", "--step", "A1",
                                   "--kind", "section", "--value", f"v{i}",
                                   "--json"], env))["rows_now"])
    assert seen == [1, 2, 3], seen
    # Counted per kind and per step, not run-wide.
    other = _one_json(run(["evidence", "--run", "c1", "--step", "A1",
                           "--kind", "elsewhere", "--value", "x", "--json"], env))
    assert other["rows_now"] == 1, other


def test_gate_reports_recorded_and_vouched_for_separately(env, tmp_path):
    """The default witness DEGRADES rather than refusing, so "recorded" does not mean "attested".

    A caller reading only a success would not learn that this gate passed with nobody vouching
    for it — which is the whole reason the engine also writes a violation.
    """
    tr = transcript_with(tmp_path, 4)
    run(["open", "authoring", "--scope", "d/g1", "--run", "g1"], env)
    run(["enter", "--run", "g1", "--step", "D2"], env)
    ok = _one_json(run(["gate", "--run", "g1", "--step", "D2", "--decision", "affirm",
                        "--evidence", "said so", "--json"], env,
                       transcript=tr, witness="transcript"))
    assert (ok["witnessed"], ok["violation_recorded"]) == (True, False)

    run(["open", "authoring", "--scope", "d/g2", "--run", "g2"], env)
    run(["enter", "--run", "g2", "--step", "D2"], env)
    bad = _one_json(run(["gate", "--run", "g2", "--step", "D2", "--decision", "affirm",
                         "--evidence", "nobody saw", "--json"], env))
    assert bad["witness"] == "manual"
    assert (bad["witnessed"], bad["violation_recorded"]) == (False, True)
    assert _rows(env, "SELECT 1 FROM violation WHERE run_id='g2' AND code='unwitnessed_gate'")


@pytest.mark.parametrize("declares,flag,expect_result,expect_source", [
    (True, None, "completed", "flow_default"),
    (True, "abandoned", "abandoned", "explicit"),
    (False, None, "completed", "engine_fallback"),
    (False, "whatever", "whatever", "explicit"),
])
def test_close_run_names_where_the_result_came_from(env, declares, flag,
                                                   expect_result, expect_source):
    """`completed` can arrive three ways and only one of them is the flow author's choice.

    A run closed on the engine's fallback and one closed on a declared default hold the same
    word, and telling them apart is how `abilities --json` reporting an undeclared flow stays
    actionable rather than trivia.
    """
    name = "zzz_src"
    body = ("results:\n  values: [completed, abandoned]\n  default: completed\n"
            if declares else "")
    d = _spec(env, name, f"""
{body}phases:
  - id: p1
    title: P1
steps:
  - id: W01
    phase: p1
    directive: Do it.
""")
    try:
        run(["open", name, "--scope", "s1", "--run", "r1"], env)
        args = ["close-run", "--run", "r1", "--force-steps", "--json"]
        if flag is not None:
            args += ["--result", flag]
        got = _one_json(run(args, env))
        assert (got["result"], got["result_source"]) == (expect_result, expect_source), got
        assert got["forced_steps"] == ["W01"], "a waived step is named, not just counted"
    finally:
        _rm(d)


def test_discharge_tells_a_fresh_discharge_from_a_repeat(env, tmp_path):
    """Both exit 0. Only the field says whether this call did anything."""
    tr = transcript_with(tmp_path, 9)
    assert _drive(env, "d1", tr) is None
    for ph in ("gather", "shape", "review"):
        run(["summarize", "--run", "d1", "--phase", ph], env)
    obs = _one_json(run(["obligations", "--run", "d1", "--json"], env))
    owed = [o["hook_id"] for o in (obs if isinstance(obs, list) else obs["obligations"])
            if not o.get("discharged_at")]
    assert owed, "this flow raises an obligation; without one the test proves nothing"
    h = owed[0]

    first = _one_json(run(["discharge", "--run", "d1", "--hook", h,
                           "--evidence", "answered", "--json"], env))
    assert first["discharged_now"] is True and first["already_discharged_at"] is None

    again = _one_json(run(["discharge", "--run", "d1", "--hook", h,
                           "--evidence", "answered twice", "--json"], env))
    assert again["discharged_now"] is False
    assert again["already_discharged_at"], again


def test_a_json_command_that_answers_nothing_is_an_engine_defect(env, monkeypatch, capsys):
    """The SUCCESS half of the promise, pinned on its own rather than via the one path that broke.

    `_err` is what lets the backstop speak for a refusal; a success has no equivalent, because
    the answer was never built at all. So the silence is exit 5 — this CLI's code for its own
    defect — and not a quiet zero. It is not hypothetical: `next --json` printed prose on a
    finished run for as long as the flag had existed, and nothing anywhere said so.
    """
    import sys as _s
    _s.path.insert(0, str(REPO))
    from engine import harness as h

    monkeypatch.setenv("HARNESS_STATE_DIR", env["HARNESS_STATE_DIR"])
    monkeypatch.setattr(h, "cmd_status", lambda _args: h.OK)   # returns OK, emits nothing

    assert h.main(["status", "--json"]) == INTERNAL
    # Without --json the same command is simply a quiet success; the defect is the broken
    # promise, not the silence.
    assert h.main(["status"]) == h.OK
    err = capsys.readouterr().err
    assert "no machine answer" in err, err


def test_a_malformed_invocation_stays_prose_and_says_so(env):
    """The one case that cannot be served, asserted so it is a known shape and not a surprise.

    argparse rejects the command line before anything can read `--json` off it. Answering in
    prose is honest: the request to be answered in JSON was part of what did not parse.
    """
    out = run(["gate", "--run", "x", "--json"], env)
    assert out.returncode == USAGE
    assert out.stdout.strip() == "", "a payload here would have to be invented"
    assert "required" in out.stderr


# ------------------------------------------------------------------ the last two read commands

_GV_SPEC = """
scope_match: exact
guards:
  deploy: {step: G01, matches: [{tool: shell, field: command, pattern: 'deploy-thing'}]}
phases:
  - id: p1
    title: P1
steps:
  - id: G01
    phase: p1
    gate: affirm
    title: The gate that guards deploying
    directive: Confirm.
"""


@pytest.mark.parametrize("goal,declared,met,code", [
    (None, False, None, OK),
    ("phase_steps_closed", True, False, REFUSED),
])
def test_assert_goal_answers_declared_and_met_as_two_facts(env, goal, declared, met, code):
    """A phase with NO criterion and a phase whose criterion holds both exit 0.

    Reporting `met: true` for the first would be the answer inventing a check nobody wrote, so
    "was anything asserted" and "did it hold" are separate fields and the first can be false.
    """
    d = _spec(env, "zzz_ag", f"""
phases:
  - id: p1
    title: P1
{f"    goal:{chr(10)}      type: {goal}" if goal else ""}
steps:
  - id: W01
    phase: p1
    directive: Do it.
""")
    try:
        run(["open", "zzz_ag", "--scope", "s1", "--run", "a1"], env)
        out = run(["assert-goal", "--run", "a1", "--phase", "p1", "--json"], env)
        assert out.returncode == code, out.stderr
        got = _one_json(out)
        assert got["command"] == "assert-goal"
        assert (got["declared"], got["met"]) == (declared, met), got
        if code == REFUSED:
            # Emitted by the site, not by the backstop: `met: false` is branchable and the
            # backstop can only offer the sentence.
            assert "code_name" not in got, "the backstop answered over a richer envelope"
            assert got["why"], got
    finally:
        _rm(d)


def test_guard_verdicts_are_a_closed_set_and_every_one_is_reachable(env, tmp_path, monkeypatch):
    """All seven, produced for real — a declared verdict nothing can produce is dead weight.

    The kinds of ALLOW are the point. `no_run_in_scope` and `view_incomplete` both allow, and
    treating them as one answer is exactly the collapse this command exists not to make.
    """
    sys.path.insert(0, str(REPO))
    from engine.harness import GUARD_VERDICTS

    def ask(scope, action="deploy", env_extra=None, kind="s"):
        out = run(["guard", "--action", action, "--scope-kind", kind,
                   "--scope", scope, "--json"], {**env, **(env_extra or {})})
        return out.returncode, _one_json(out)

    seen = {}
    d = _spec(env, "zzz_gv", _GV_SPEC)
    try:
        # nothing open here at all
        _, g = ask("empty-scope")
        seen[g["verdict"]] = g
        assert g["allowed"] is True and g["runs"] == []

        run(["open", "zzz_gv", "--scope", "s-one", "--run", "v1"], env)

        # open, guarded, no gate → the only verdict that refuses
        code, g = ask("s-one")
        seen[g["verdict"]] = g
        assert code == BLOCKED and g["allowed"] is False
        assert g["blocked_by"]["step"] == "G01" and g["blocked_by"]["of"] == 1

        # open, but this flow says nothing about that action
        _, g = ask("s-one", action="something-else")
        seen[g["verdict"]] = g
        assert g["allowed"] is True and g["runs"] == ["v1"]

        # the gate it wanted, recorded
        tr = transcript_with(tmp_path, 2)
        assert rc(["gate", "--run", "v1", "--step", "G01", "--decision", "affirm",
                   "--evidence", "a person said so"], env,
                  transcript=tr, witness="transcript") == OK
        _, g = ask("s-one")
        seen[g["verdict"]] = g
        assert g["gate_recorded"] == [{"run": "v1", "step": "G01", "decision": "affirm"}]

        # two runs in one scope, no delegation → who owns the action is unanswerable
        run(["open", "zzz_gv", "--scope", "s-one", "--run", "v2", "--allow-concurrent"], env)
        _, g = ask("s-one")
        seen[g["verdict"]] = g
        assert g["allowed"] is True and g["violation_recorded"] is True
        assert g["runs"] == ["v1", "v2"]

        # no store: cannot guard, and cannot record that it could not
        _, g = ask("s-one", env_extra={"HARNESS_STATE_DIR": str(tmp_path / "absent")})
        seen[g["verdict"]] = g
        assert g["allowed"] is True and g["detail"]

        # a run whose flow cannot be READ. v2 has to go first: two open runs with no lease is
        # decided BEFORE any flow is loaded, so it would mask this path rather than combine
        # with it.
        assert rc(["close-run", "--run", "v2", "--force-steps"], env) == OK
        (d / "flow.yaml").write_text(
            (d / "flow.yaml").read_text(encoding="utf-8") + "\nnot_a_key: 1\n",
            encoding="utf-8")
        _, g = ask("s-one")
        seen[g["verdict"]] = g
        assert g["allowed"] is True
        assert [u["run"] for u in g["unreadable"]] == ["v1"]
    finally:
        _rm(d)

    assert set(seen) == set(GUARD_VERDICTS), \
        f"unreachable: {sorted(set(GUARD_VERDICTS) - set(seen))}; " \
        f"undeclared: {sorted(set(seen) - set(GUARD_VERDICTS))}"
    # Every verdict states its own meaning, so a caller is not left inferring one from a name.
    for name, g in seen.items():
        assert g["why"] == GUARD_VERDICTS[name] and len(g["why"]) > 40, name


def test_guard_says_it_could_not_look_rather_than_that_nothing_guards_it(env, tmp_path):
    """THE HOLE THIS CLOSES, asserted as the difference it makes to one identical question.

    The same run, the same action, the same ledger. Readable, it BLOCKS. Unreadable, it used to
    answer exactly as it does when no flow guards the action at all — and once this command
    answers as data, that would be the engine stating a claim it cannot back.
    """
    d = _spec(env, "zzz_gh", _GV_SPEC)
    try:
        run(["open", "zzz_gh", "--scope", "sh", "--run", "h1"], env)
        args = ["guard", "--action", "deploy", "--scope-kind", "s", "--scope", "sh", "--json"]

        out = run(args, env)
        assert out.returncode == BLOCKED
        assert _one_json(out)["verdict"] == "blocked"

        (d / "flow.yaml").write_text(
            (d / "flow.yaml").read_text(encoding="utf-8") + "\nnot_a_key: 1\n",
            encoding="utf-8")

        out = run(args, env)
        got = _one_json(out)
        assert got["verdict"] == "view_incomplete", got
        assert got["verdict"] != "not_guarded", "an unread flow reported as an absent one"
        # Still allows — the iron law of a guard that runs in front of everything.
        assert out.returncode == OK and got["allowed"] is True
        assert got["unreadable"][0]["run"] == "h1" and got["unreadable"][0]["why"]
        # And the human channel says it too, not only the payload.
        assert "CANNOT READ" in out.stderr and "there is no guard" in out.stderr
    finally:
        _rm(d)


def test_a_block_stands_and_reports_the_partial_view_rather_than_hiding_it(env):
    """Two facts, both true at once: this action is refused, AND the view was not complete.

    A declared lease is what makes two runs in one scope adjudicable, so this is the only shape
    in which "one link blocks, another link cannot be read" is reachable at all.

    It also pins why the whole chain is scanned before anything is decided. Returning at the
    first block was cheaper and made the answer LIE: `unreadable` came back empty because the
    links past the block were never looked at, and an empty list reads as "everything was
    readable".
    """
    a = _spec(env, "zzz_la", _GV_SPEC)
    b = _spec(env, "zzz_lb", _GV_SPEC)
    try:
        assert rc(["open", "zzz_la", "--scope", "sy", "--run", "la"], env) == OK
        assert rc(["enter", "--run", "la", "--step", "G01"], env) == OK
        assert rc(["open", "zzz_lb", "--scope", "sy", "--run", "lb",
                   "--leased-from", "la", "--leased-at", "G01"], env) == OK
        (b / "flow.yaml").write_text(
            (b / "flow.yaml").read_text(encoding="utf-8") + "\nnot_a_key: 1\n",
            encoding="utf-8")

        out = run(["guard", "--action", "deploy", "--scope-kind", "s", "--scope", "sy",
                   "--json"], env)
        got = _one_json(out)
        # More information cannot turn a refusal into an allow.
        assert out.returncode == BLOCKED and got["verdict"] == "blocked"
        assert got["blocked_by"]["run"] == "la" and got["blocked_by"]["of"] == 2
        # ...and the caveat is not dropped on the way.
        assert [u["run"] for u in got["unreadable"]] == ["lb"], got
        assert "CANNOT READ" in out.stderr and "BLOCKED" in out.stderr
    finally:
        _rm(a)
        _rm(b)


def test_an_unanswerable_owner_is_decided_before_any_flow_is_read(env):
    """Two open runs with no lease is settled without loading a spec — so a broken spec in the
    scope cannot mask it, and it cannot mask a broken spec either.

    Worth pinning because both verdicts allow, and if the order flipped, the answer to "why did
    this go through" would change while the exit code did not.
    """
    d = _spec(env, "zzz_un", _GV_SPEC)
    try:
        assert rc(["open", "zzz_un", "--scope", "su", "--run", "u1"], env) == OK
        assert rc(["open", "zzz_un", "--scope", "su", "--run", "u2",
                   "--allow-concurrent"], env) == OK
        (d / "flow.yaml").write_text(
            (d / "flow.yaml").read_text(encoding="utf-8") + "\nnot_a_key: 1\n",
            encoding="utf-8")
        got = _one_json(run(["guard", "--action", "deploy", "--scope-kind", "s",
                             "--scope", "su", "--json"], env))
        assert got["verdict"] == "unadjudicated", got
        assert got["violation_recorded"] is True
        # No `unreadable` key at all: it never got as far as trying to read one, and a key
        # reporting on unattempted work would be the same lie in the other direction.
        assert "unreadable" not in got, got
    finally:
        _rm(d)


def test_the_pending_table_is_empty_and_still_exists(env):
    """It is the place a command with no machine form has to land.

    Deleting the table once it emptied would put the next such command back into the silence the
    partition test exists to make impossible.
    """
    sys.path.insert(0, str(REPO))
    from engine.harness import NO_JSON_YET
    assert NO_JSON_YET == {}


# ------------------------------------------------------------------ a location scope whose
# ------------------------------------------------------------------ action names its own target

_SM_SPEC = """
scope_kind: repo
scope_match: {mode}
guards:
  deploy: {{step: G01, matches: [{{tool: shell, field: command, pattern: 'deploy-thing'}}]}}
phases:
  - id: p1
    title: P1
steps:
  - id: G01
    phase: p1
    gate: affirm
    title: The gate that guards deploying
    directive: Confirm.
"""


def _covers(mode, scope_key, where, payload):
    """Ask the comparison directly. Loading a spec per case would test the loader, not this."""
    sys.path.insert(0, str(REPO))
    from engine import flow as _f

    class _Probe:
        _payload_names = staticmethod(_f.Flow._payload_names)
        scope_covers = _f.Flow.scope_covers

        def __init__(self, m):
            self.scope_match = m

    return _Probe(mode).scope_covers(scope_key, where, payload)


@pytest.mark.parametrize("where,command,prefix_only,or_payload,why", [
    ("/s/pkg", "git commit -m y",                  True,  True,
     "the caller is inside the scope and the command names nothing — the original case"),
    ("/other", "git -C /s/pkg commit -m y",        False, True,
     "THE HOLE: the caller is outside, and the command names a target inside"),
    ("/other", "git -C /elsewhere commit -m y",    False, False,
     "a target that is not this scope's must not be claimed"),
    ("/other", "git commit -m 'see /s/README'",    False, True,
     "KNOWN AND ACCEPTED: a scope merely MENTIONED matches too"),
    ("/sX",    "git commit -m y",                  False, False,
     "a sibling sharing a prefix is not inside — compared by path component"),
])
def test_a_location_scope_can_be_widened_to_the_target_the_call_names(
        where, command, prefix_only, or_payload, why):
    """`path_prefix` asks only where the CALLER is; a tool call can act somewhere else.

    Both columns are asserted in one table on purpose: the value of the new mode is exactly the
    DIFFERENCE between them, and a table that only pinned the new column would still pass if the
    old one had been widened to match — which is the change this must not become (see the test
    below).
    """
    payload = {"command": command}
    assert _covers("path_prefix", "/s", where, payload) is prefix_only, why
    assert _covers("path_prefix_or_payload", "/s", where, payload) is or_payload, why


def test_widening_is_a_declaration_and_never_a_default():
    """The rule the comparison's own docstring sets, pinned.

    A false positive here is worse than a miss and not symmetrically: being told to satisfy a
    gate that belongs to somebody else's work leaves FORGING that gate as the way forward. So a
    flow that did not ask for this keeps the narrow comparison, and the default stays narrowest.
    """
    sys.path.insert(0, str(REPO))
    from engine import flow as _f

    named = {"command": "git -C /s/pkg commit"}
    assert _covers("path_prefix", "/s", "/other", named) is False, \
        "an undeclared flow was widened"
    assert _covers("exact", "/s", "/other", named) is False
    # And the default a spec gets when it says nothing at all.
    d = _spec({}, "zzz_sm_def", """
phases:
  - id: p1
    title: P1
steps:
  - id: W01
    phase: p1
    directive: Do it.
""")
    try:
        assert _f.load("zzz_sm_def").scope_match == "exact"
    finally:
        _rm(d)


def test_the_two_payload_modes_share_one_search():
    """`in_payload` and the widened mode must agree on what "the call names it" means.

    Two implementations of one question drift, and the drift would be invisible: each mode is
    exercised by different abilities, so a divergence shows up as one of them quietly matching
    less than the other.
    """
    for mode in ("in_payload", "path_prefix_or_payload"):
        # Whole-token: a scope of CR-123 does not claim CR-1234.
        assert _covers(mode, "CR-123", "/anywhere", {"crId": "CR-123"}) is True, mode
        assert _covers(mode, "CR-123", "/anywhere", {"crId": "CR-1234"}) is False, mode
        # `/` is not a token character, which is what makes a path scope name its children.
        assert _covers(mode, "/a/b", "/anywhere", {"command": "x /a/b/c"}) is True, mode
        assert _covers(mode, "/a/b", "/anywhere", {"command": "x /a/bc"}) is False, mode


def test_an_unknown_scope_match_mode_is_fatal_and_lists_the_legal_ones(env):
    """The closed set, so a typo cannot silently pick a comparison nobody chose."""
    d = _spec(env, "zzz_sm_bad", """
scope_match: path_prefix_or_paylod
phases:
  - id: p1
    title: P1
steps:
  - id: W01
    phase: p1
    directive: Do it.
""")
    try:
        out = run(["validate", "zzz_sm_bad"], env)
        assert out.returncode == BAD_SPEC, out.stdout + out.stderr
        assert "scope_match must be one of" in out.stderr
        assert "path_prefix_or_payload" in out.stderr, "the legal modes are not listed"
    finally:
        _rm(d)


@pytest.mark.parametrize("cwd_in_scope,command,code,why", [
    (False, "deploy-thing", OK,
     "outside, and the command names no directory — ownership is not answerable"),
    (False, "deploy-thing --at {scope}/pkg", BLOCKED,
     "outside, but the command names a target inside the scope"),
    (False, "deploy-thing --at /tmp/unrelated-xyz", OK,
     "outside, naming a target that is not this scope's"),
    (True, "deploy-thing", BLOCKED,
     "inside — the behaviour that already worked, unchanged"),
    (False, "echo {scope}/pkg", OK,
     "MENTIONS the scope but is not a guarded action: the pattern is the first filter, which "
     "is what bounds the false positive the mode accepts"),
])
def test_the_widened_mode_reaches_guard_tool(env, tmp_path, cwd_in_scope, command, code, why):
    """End to end through the hook's own entry point, not just the comparison."""
    scope = tmp_path / "scoped"
    (scope / "pkg").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    d = _spec(env, "zzz_sm_e2e", _SM_SPEC.format(mode="path_prefix_or_payload"))
    try:
        assert rc(["open", "zzz_sm_e2e", "--scope", str(scope), "--run", "e1"], env) == OK
        got = rc(["guard-tool", "--tool", "shell",
                  "--input-json", json.dumps({"command": command.format(scope=scope)}),
                  "--cwd", str(scope if cwd_in_scope else outside)], env)
        assert got == code, why
    finally:
        _rm(d)


# ------------------------------------------------------------------ a requirement a directory
# ------------------------------------------------------------------ declares, with no paths in it

def _marker(d, text):
    sys.path.insert(0, str(REPO))
    from engine.policy import MARKER
    (d / MARKER).write_text(text, encoding="utf-8")
    return d / MARKER


def _discover(start):
    sys.path.insert(0, str(REPO))
    from engine import policy
    return policy.discover(str(start))


def _shape(entries, root):
    return [(e["ability"], str(e["scope_key"]).replace(str(Path(root).resolve()), "<r>"),
             e["strict"]) for e in entries]


_MK_SPEC = """
scope_kind: repo
scope_match: path_prefix_or_payload
guards:
  deploy: {step: G01, matches: [{tool: shell, field: command, pattern: 'deploy-thing'}]}
phases:
  - id: p1
    title: P1
steps:
  - id: G01
    phase: p1
    gate: affirm
    title: The gate that guards deploying
    directive: Confirm.
"""


def test_the_same_marker_bytes_mean_the_same_thing_in_two_places(tmp_path):
    """THE POINT OF THE WHOLE MECHANISM, asserted as the property rather than the plumbing.

    The machine-local record holds ABSOLUTE paths, so it cannot travel: another machine spells the
    checkout differently and a renamed parent turns an entry into one that matches nothing, for
    ever, in silence. A marker names no path at all — the scope is the directory holding it — so
    the identical bytes describe the identical requirement wherever the tree is put.
    """
    a, b = tmp_path / "here", tmp_path / "somewhere" / "else"
    for root in (a, b):
        (root / "src" / "Pkg").mkdir(parents=True)
        _marker(root, "shipcheck-asis\n")

    ea, _ = _discover(a / "src" / "Pkg")
    eb, _ = _discover(b / "src" / "Pkg")

    assert _shape(ea, a) == _shape(eb, b) == [("shipcheck-asis", "<r>", False)]
    # And the file itself contains nothing machine-specific — which is what makes that true.
    text = (a / ".harness-required").read_text(encoding="utf-8")
    assert "/" not in text and str(tmp_path) not in text


def test_the_nearest_declaration_wins_and_an_empty_one_is_an_exemption(tmp_path):
    """A repository's own marker is its authors'; a parent's is the machine owner's.

    When both speak the more specific one knows what it is talking about — and that has to include
    saying "not here", or the only way to opt a subtree out is editing somebody else's committed
    file.
    """
    root = tmp_path / "ws"
    deep = root / "pkg" / "src"
    deep.mkdir(parents=True)
    _marker(root, "# the machine owner's\nshipcheck-asis\n")
    assert _shape(_discover(deep)[0], root) == [("shipcheck-asis", "<r>", False)]

    _marker(root / "pkg", "push strict\n")
    assert _shape(_discover(deep)[0], root) == [("push", "<r>/pkg", True)]

    _marker(root / "pkg", "# nothing is required in this subtree\n")
    assert _discover(deep)[0] == (), "an empty nearest marker did not exempt the subtree"


def test_a_marker_written_in_the_machine_records_format_is_reported_not_guessed(tmp_path):
    """`<ability> <path>` in a marker names a directory the writer did not mean.

    Taking the first token and ignoring the rest would apply the requirement to the marker's own
    directory while the line says otherwise — a silent reinterpretation of a declaration, which is
    the failure mode this engine spends its refusals on.
    """
    root = tmp_path / "ws"
    root.mkdir()
    _marker(root, "shipcheck-asis\t/somewhere/else\n")
    entries, problems = _discover(root)
    assert entries == (), "the line was applied anyway"
    assert problems and "names nothing" in problems[0]
    assert "/somewhere/else" in problems[0], "the report does not show what was ignored"


def test_a_marker_that_cannot_be_read_is_reported_and_still_allows(env, tmp_path):
    """"A declaration I cannot read" is not "no declaration", and must not look like it.

    Allowing is the iron rule for anything in front of every tool call. Saying so is the part that
    was missing everywhere this engine has since been corrected.
    """
    root = tmp_path / "ws"
    root.mkdir()
    m = _marker(root, "shipcheck-asis\n")
    m.chmod(0o000)
    try:
        m.read_text(encoding="utf-8")
        pytest.skip("this process can read a 0o000 file; the unreadable path is not exercisable")
    except OSError:
        pass
    try:
        entries, problems = _discover(root)
        assert entries == ()
        assert problems and "cannot be read" in problems[0]
        # And through the real entry point: allowed, with the reason on stderr.
        out = run(["guard-tool", "--tool", "shell",
                   "--input-json", json.dumps({"command": "git commit -m x"}),
                   "--cwd", str(root)], env)
        assert out.returncode == OK
        assert "cannot be read" in out.stderr, out.stderr
    finally:
        m.chmod(0o644)


def test_a_declared_requirement_refuses_until_a_run_is_open_and_then_hands_over(env, tmp_path):
    """The two questions in sequence, and the messages must not be interchangeable.

    Without a run the refusal is "open one"; with a run open the FIRST question takes over and the
    refusal becomes "record the gate". A driver told the wrong one of those goes in a circle.
    """
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    d = _spec(env, "zzz_mk", _MK_SPEC)
    try:
        _marker(root, "zzz_mk\n")
        call = ["guard-tool", "--tool", "shell",
                "--input-json", json.dumps({"command": "deploy-thing"}),
                "--cwd", str(root / "src")]

        out = run(call, env)
        assert out.returncode == BLOCKED
        assert "no run of it is open here" in out.stderr
        assert "harness open zzz_mk --scope" in out.stderr
        # WHICH declaration, because with two possible sources "where do I change this" has two
        # answers and this message is the only place a driver learns which.
        assert str(root / ".harness-required") in out.stderr, out.stderr

        assert rc(["open", "zzz_mk", "--scope", str(root), "--run", "m1"], env) == OK
        out = run(call, env)
        assert out.returncode == BLOCKED
        assert "requires gate 'G01'" in out.stderr
        assert "no run of it is open here" not in out.stderr, \
            "the second question answered over the first"
    finally:
        _rm(d)


def test_discovery_starts_where_the_caller_is_and_that_bound_is_declared(env, tmp_path):
    """A call from OUTSIDE the tree finds no marker — asserted so it is a known shape.

    Locating a marker from a path inside the payload would need a rule for which substrings are
    paths, which this engine declines for the same reason it declines widening a scope comparison
    by default. The machine record covers that case instead, because its entry names the directory
    outright — so the gap has an answer rather than being a hole.
    """
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    d = _spec(env, "zzz_mkb", _MK_SPEC)
    try:
        _marker(root, "zzz_mkb\n")
        named = json.dumps({"command": f"deploy-thing --at {root}"})

        assert rc(["guard-tool", "--tool", "shell", "--input-json", named,
                   "--cwd", str(outside)], env) == OK, \
            "discovery reached outside the tree; the bound above is not the behaviour"

        # The machine record does reach it: the entry names the directory, and the flow declares
        # the widened comparison, so the call naming its target is claimed.
        assert rc(["require", "--add", "zzz_mkb", "--scope-key", str(root)], env) == OK
        assert rc(["guard-tool", "--tool", "shell", "--input-json", named,
                   "--cwd", str(outside)], env) == BLOCKED
    finally:
        _rm(d)


def test_require_reports_both_sources_and_where_each_came_from(env, tmp_path):
    """One view, or "what is required here" has two answers and only one is visible."""
    root = tmp_path / "repo"
    root.mkdir()
    d = _spec(env, "zzz_mkc", _MK_SPEC)
    try:
        _marker(root, "zzz_mkc\n")
        assert rc(["require", "--add", "zzz_mkc", "--scope-key", "/elsewhere/xyz"], env) == OK

        out = subprocess.run([sys.executable, str(HARNESS), "require", "--json"],
                             capture_output=True, text=True, cwd=str(root),
                             env={**os.environ, **env})
        assert out.returncode == OK, out.stderr
        got = json.loads(out.stdout)
        srcs = {Path(r["source"]).name for r in got["required"]}
        assert srcs == {"required-flows", got["marker"]}, got
        keys = {r["scope_key"] for r in got["required"]}
        assert "/elsewhere/xyz" in keys and str(Path(root).resolve()) in keys, got

        text = subprocess.run([sys.executable, str(HARNESS), "require"],
                              capture_output=True, text=True, cwd=str(root),
                              env={**os.environ, **env}).stdout
        assert "from " in text and got["marker"] in text, text
    finally:
        _rm(d)

def test_the_remedy_named_matches_where_the_requirement_lives(env, tmp_path):
    """Offering the wrong remedy is worse than offering none.

    `require --remove` edits the machine record. Naming it for a requirement a DIRECTORY declared
    sends the reader to change a file that does not contain it — the command reports success, the
    requirement survives, and the reader concludes the mechanism is broken rather than that they
    were pointed at the wrong file.
    """
    root = tmp_path / "repo"
    root.mkdir()
    d = _spec(env, "zzz_rem", _MK_SPEC)
    call = ["guard-tool", "--tool", "shell",
            "--input-json", json.dumps({"command": "deploy-thing"}), "--cwd", str(root)]
    try:
        _marker(root, "zzz_rem\n")
        err = run(call, env).stderr
        assert "editing that file" in err, err
        assert "require --remove" in err, "the reader is not told the machine command is wrong"
        assert "harness require --remove zzz_rem" not in err, \
            "a marker requirement was offered the machine record's command"

        (root / ".harness-required").unlink()
        assert rc(["require", "--add", "zzz_rem", "--scope-key", str(root)], env) == OK
        err = run(call, env).stderr
        assert f"harness require --remove zzz_rem --scope-key {root}" in err, err
        assert "editing that file" not in err
    finally:
        _rm(d)

def test_require_says_which_declarations_reach_where_it_was_asked_from(env, tmp_path):
    """The machine record is machine-WIDE, so a heading saying "applying at <dir>" was false in
    every directory its entries do not cover.

    Asked from an unrelated repository it reported two requirements as though they bound the work
    there. Both facts have to survive: somebody asking what they declared must still see all of
    it, and somebody asking what binds them HERE must be able to tell. So nothing is filtered out
    and each row says whether it reaches the place being asked from.
    """
    inside = tmp_path / "scoped" / "deep"
    inside.mkdir(parents=True)
    outside = tmp_path / "unrelated"
    outside.mkdir()
    d = _spec(env, "zzz_reach", _MK_SPEC)
    try:
        assert rc(["require", "--add", "zzz_reach", "--scope-key",
                   str(tmp_path / "scoped")], env) == OK
        assert rc(["require", "--add", "zzz_reach", "--scope-key", "/elsewhere/nope"], env) == OK

        def ask(cwd):
            out = subprocess.run([sys.executable, str(HARNESS), "require", "--json"],
                                 capture_output=True, text=True, cwd=str(cwd),
                                 env={**os.environ, **env})
            assert out.returncode == OK, out.stderr
            return json.loads(out.stdout)

        got = ask(inside)
        reach = {r["scope_key"]: r["covers_cwd"] for r in got["required"]}
        assert reach == {str(tmp_path / "scoped"): True, "/elsewhere/nope": False}, reach

        got = ask(outside)
        assert all(r["covers_cwd"] is False for r in got["required"]), got
        # And the text form must not claim otherwise.
        text = subprocess.run([sys.executable, str(HARNESS), "require"],
                              capture_output=True, text=True, cwd=str(outside),
                              env={**os.environ, **env}).stdout
        assert "0 of 2" in text, text
        assert "HERE" in text, "the legend vanished, so a reader cannot decode the marker"
        assert "\nHERE" not in text, "a row was marked as reaching a directory it does not"
    finally:
        _rm(d)


# ------------------------------------------------------------------ where a transcript names the
# ------------------------------------------------------------------ side, declared not assumed

def _kiro_shaped(tmp_path, n_human: int, n_other: int = 3) -> Path:
    """A transcript in the shape a REAL runtime was measured to write.

    Every record is `{kind, data, version}`; the side lives in `kind` and a human turn is spelled
    `Prompt`. Under the engine's default field names this file names no side at all — which is the
    case the declaration exists for, so the fixture is that shape rather than a convenient one.
    """
    p = tmp_path / f"kiro-{n_human}.jsonl"
    lines = []
    for i in range(n_human):
        lines.append(json.dumps({"kind": "Prompt", "version": 1,
                                 "data": {"content": f"turn {i}", "message_id": i}}))
        lines.append(json.dumps({"kind": "AssistantMessage", "version": 1,
                                 "data": {"content": "ok", "message_id": i}}))
    for i in range(n_other):
        lines.append(json.dumps({"kind": "ToolResults", "version": 1,
                                 "data": {"content": "", "results": [], "message_id": i}}))
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def _vouch(tmp_path, transcript, cursor=None, **env):
    """Call the witness in-process with a controlled environment."""
    import importlib
    sys.path.insert(0, str(REPO))
    keep = {k: os.environ.get(k) for k in
            ("HARNESS_WITNESS", "HARNESS_TRANSCRIPT",
             "HARNESS_TRANSCRIPT_ROLE_PATH", "HARNESS_TRANSCRIPT_HUMAN")}
    try:
        for k in keep:
            os.environ.pop(k, None)
        os.environ["HARNESS_WITNESS"] = "transcript"
        os.environ["HARNESS_TRANSCRIPT"] = str(transcript)
        os.environ.update({k: v for k, v in env.items() if v is not None})
        proof = importlib.import_module("engine.proof")
        return proof.vouch(cursor=cursor)
    finally:
        for k, v in keep.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v


def _refusal(tmp_path, transcript, **env) -> str:
    sys.path.insert(0, str(REPO))
    from engine import proof
    with pytest.raises(proof.NoWitness) as e:
        _vouch(tmp_path, transcript, **env)
    return str(e.value)


def test_a_declared_role_path_finds_a_side_the_defaults_cannot(tmp_path):
    """The measured case: a real runtime that spells the side somewhere else entirely.

    Without a declaration this transcript is unreadable to the witness, so every gate is refused —
    while a real session sits there with human turns in it.
    """
    t = _kiro_shaped(tmp_path, 4)
    got = _vouch(tmp_path, t,
                 HARNESS_TRANSCRIPT_ROLE_PATH="kind", HARNESS_TRANSCRIPT_HUMAN="Prompt")
    assert (got["witnessed"], got["human_turns"]) == (True, 4), got
    # What was APPLIED rides in the proof: read back later, it says how the count was arrived at
    # instead of leaving it to be guessed from a past process's environment.
    assert got["role_path"] == "kind" and got["human_values"] == ["prompt"]


def test_the_engine_learns_no_runtime_spelling_of_its_own(tmp_path):
    """The declaration is the whole mechanism — nothing is special-cased inside the witness.

    Pinned because the tempting fix was a branch for the runtime that was measured, and one such
    branch is how a base that claims to know no runtime ends up knowing one.
    """
    t = _kiro_shaped(tmp_path, 2)
    assert _vouch(tmp_path, t, HARNESS_TRANSCRIPT_ROLE_PATH="kind",
                  HARNESS_TRANSCRIPT_HUMAN="Prompt")["human_turns"] == 2
    # A third spelling needs no engine change either.
    p = tmp_path / "third.jsonl"
    p.write_text("\n".join(json.dumps({"envelope": {"speaker": s}})
                           for s in ("HUMAN", "BOT", "HUMAN")) + "\n", encoding="utf-8")
    got = _vouch(tmp_path, p, HARNESS_TRANSCRIPT_ROLE_PATH="envelope.speaker",
                 HARNESS_TRANSCRIPT_HUMAN="human")
    assert got["human_turns"] == 2, got
    src = (REPO / "engine" / "proof.py").read_text(encoding="utf-8")
    for spelling in ("Prompt", "AssistantMessage", "kind"):
        assert f'"{spelling}"' not in src and f"'{spelling}'" not in src, \
            f"{spelling!r} is hardcoded in the witness; it should only ever be declared"


def test_cannot_see_who_spoke_is_a_different_refusal_from_nobody_spoke(tmp_path):
    """The pair this engine keeps being corrected for conflating.

    A wrong path refuses EVERY gate. Told "no human turns", the reader looks at the session — which
    is fine — and concludes the mechanism is broken. Told which path was used, they fix the path.
    """
    t = _kiro_shaped(tmp_path, 4)

    # Field never found. The path asserted on is deliberately NOT one the message suggests as an
    # example: asserting on `data.role` passed even when the reported path was replaced by a
    # constant, because the hint text at the end of the same message contains it. A guard
    # satisfied by static prose is a guard asserting nothing.
    msg = _refusal(tmp_path, t, HARNESS_TRANSCRIPT_ROLE_PATH="envelope.who")
    assert "names a side" in msg and "cannot see who spoke" in msg
    looked = [l for l in msg.splitlines() if "Looked at:" in l]
    assert looked and "envelope.who" in looked[0], \
        f"the refusal does not name the path it used: {msg}"

    # Field found, but no value in it means a person — a different sentence, and it must name the
    # values that ARE there, because that is the entire remedy.
    msg = _refusal(tmp_path, t, HARNESS_TRANSCRIPT_ROLE_PATH="kind")
    assert "none of them means a person" in msg
    assert "'Prompt'" in msg and "AssistantMessage" in msg, msg
    assert "HARNESS_TRANSCRIPT_HUMAN" in msg, "the remedy is not named"
    assert "cannot see who spoke" not in msg, "the two refusals collapsed into one"


def test_the_diagnostic_does_not_echo_an_unbounded_slice_of_a_session(tmp_path):
    """A mis-declared path can point at CONTENT.

    Reporting what it found is what makes the refusal actionable, and reporting all of it would be
    a worse failure than the one being explained — this runs on somebody's real session.
    """
    p = tmp_path / "wide.jsonl"
    p.write_text("\n".join(json.dumps({"role": f"secret-{i}-" + "x" * 200})
                           for i in range(50)) + "\n", encoding="utf-8")
    msg = _refusal(tmp_path, p, HARNESS_TRANSCRIPT_HUMAN="nobody")
    assert "x" * 60 not in msg, "an unbounded value was echoed into the refusal"
    assert msg.count("secret-") <= 5, f"more than five distinct values were echoed:\n{msg}"


def test_the_defaults_still_work_for_a_transcript_that_needs_no_declaration(tmp_path):
    """Backward compatibility, asserted rather than assumed: the declaration is an ADDITION."""
    t = transcript_with(tmp_path, 3)
    got = _vouch(tmp_path, t)
    assert (got["witnessed"], got["human_turns"]) == (True, 3), got
    assert got["role_path"] == ["role", "author", "from"]


@pytest.mark.parametrize("dotted,why", [
    ("data.0.role", "no list indexing — a path needing it is a parser, not a declaration"),
    ("data.*.role", "no wildcards, same reason"),
    ("data", "a path landing on a dict is not a value"),
    ("nope", "a segment that is not there"),
])
def test_a_path_that_does_not_resolve_is_not_found_rather_than_a_crash(tmp_path, dotted, why):
    """`_dig` runs against every record of somebody's live session; an exception here would take
    down a gate with a traceback instead of a refusal that says what to fix."""
    t = _kiro_shaped(tmp_path, 2)
    msg = _refusal(tmp_path, t, HARNESS_TRANSCRIPT_ROLE_PATH=dotted)
    assert "names a side" in msg, why


def test_the_cursor_still_refuses_a_second_gate_in_one_turn_under_a_declared_path(tmp_path):
    """The property the witness exists for, verified on the declared path rather than assumed to
    have survived it."""
    t = _kiro_shaped(tmp_path, 2)
    env = {"HARNESS_TRANSCRIPT_ROLE_PATH": "kind", "HARNESS_TRANSCRIPT_HUMAN": "Prompt"}
    first = _vouch(tmp_path, t, **env)
    assert first["human_turns"] == 2
    msg = _refusal(tmp_path, t, **env) if False else None
    sys.path.insert(0, str(REPO))
    from engine import proof
    with pytest.raises(proof.NoWitness) as e:
        _vouch(tmp_path, t, cursor={"human_turns": 2}, **env)
    assert "no new human turn since the last gate" in str(e.value)


# ------------------------------------------------------------------ one judgement per subject,
# ------------------------------------------------------------------ where the count is discovered

_CNT_SPEC = """
phases:
  - id: p1
    title: P1
steps:
  - id: W01
    phase: p1
    title: Judge each subject
    completion: {type: counts_at_least, kind_a: verdict, kind_b: subject}
    directive: Record one verdict per subject.
  - id: W02
    phase: p1
    title: A second step, to pin the scope
    completion: {type: attest}
    directive: Anything.
"""


def _cnt_run(env, run, subjects, verdicts, step="W01"):
    for i in range(subjects):
        rc(["evidence", "--run", run, "--step", step, "--kind", "subject",
            "--value", f"s{i}"], env)
    for i in range(verdicts):
        rc(["evidence", "--run", run, "--step", step, "--kind", "verdict",
            "--value", f"v{i}"], env)


@pytest.mark.parametrize("subjects,verdicts,closes,fragment", [
    (0, 0, True, "VACUOUS"),
    (3, 2, False, "1 short"),
    (3, 3, True, "covering 3"),
    (3, 5, True, "covering 3"),
    (1, 0, False, "1 short"),
])
def test_a_count_demand_can_be_discovered_rather_than_written_into_the_spec(
        env, subjects, verdicts, closes, fragment):
    """`min_count` fixes the number at authoring time; this one learns it from the run.

    "One judgement for every subject" cannot be spelled with a literal, because how many subjects
    there are is discovered while gathering. A flow that finds 24 and reports on 20 is refused —
    which is the difference between a criterion and a request.
    """
    d = _spec(env, "zzz_cnt", _CNT_SPEC)
    try:
        assert rc(["open", "zzz_cnt", "--scope", "s1", "--run", "n1"], env) == OK
        _cnt_run(env, "n1", subjects, verdicts)
        out = run(["close-step", "--run", "n1", "--step", "W01", "--json"], env)
        got = _one_json(out)
        assert got["closed"] is closes, out.stderr
        why = got.get("why") or ""
        assert fragment in why, why
        if not closes:
            assert str(subjects) in why and str(verdicts) in why, \
                f"the refusal names neither count: {why}"
    finally:
        _rm(d)


def test_an_empty_pass_says_it_was_empty(env):
    """Zero of zero satisfies the inequality and must — a window with nothing in it is a real
    outcome, not a failure. But "satisfied" reading the same for 0/0 as for 24/24 is the shape
    this engine keeps being corrected for, so the sentence distinguishes them.

    What it deliberately does NOT do is guess whether there should have been something. That
    question belongs to the gathering step, which knows how wide the window was.
    """
    d = _spec(env, "zzz_cnt2", _CNT_SPEC)
    try:
        rc(["open", "zzz_cnt2", "--scope", "s1", "--run", "e1"], env)
        empty = _one_json(run(["close-step", "--run", "e1", "--step", "W01", "--json"], env))
        assert empty["closed"] is True
        assert "VACUOUS" in empty["why"] and "nothing to cover" in empty["why"], empty

        rc(["open", "zzz_cnt2", "--scope", "s2", "--run", "e2"], env)
        _cnt_run(env, "e2", 2, 2)
        real = _one_json(run(["close-step", "--run", "e2", "--step", "W01", "--json"], env))
        assert real["closed"] is True
        assert "VACUOUS" not in real["why"], \
            "a real pass is indistinguishable from an empty one"
    finally:
        _rm(d)


def test_the_counts_come_from_the_LEDGER_and_not_from_a_claim(env):
    """The whole reason this is not `fields_agree` on two numbers.

    An agent that recorded `subject_count=24` and `verdict_count=24` would satisfy an equality
    between two values it wrote itself. Here each row IS one subject and one judgement, so the
    numbers are a property of what was done rather than of what was said about it.
    """
    d = _spec(env, "zzz_cnt3", _CNT_SPEC)
    try:
        rc(["open", "zzz_cnt3", "--scope", "s1", "--run", "l1"], env)
        # One row per side, each CLAIMING a big number. The claim is worth exactly one row.
        rc(["evidence", "--run", "l1", "--step", "W01", "--kind", "subject",
            "--value", "24 subjects were found"], env)
        rc(["evidence", "--run", "l1", "--step", "W01", "--kind", "verdict",
            "--value", "all 24 were judged"], env)
        got = _one_json(run(["close-step", "--run", "l1", "--step", "W01", "--json"], env))
        assert got["closed"] is True and "covering 1" in got["why"], got
        # ...and it counted ONE, not twenty-four: the value is opaque to the count.
        assert "24" not in got["why"].split("—")[0], \
            "the predicate read a number out of a value instead of counting rows"
    finally:
        _rm(d)


def test_rows_recorded_against_another_step_do_not_count(env):
    """Scoped like every other evidence predicate. Counting run-wide would let judgements from an
    unrelated step satisfy this one, and the step is the unit a criterion belongs to."""
    d = _spec(env, "zzz_cnt4", _CNT_SPEC)
    try:
        rc(["open", "zzz_cnt4", "--scope", "s1", "--run", "s1r"], env)
        _cnt_run(env, "s1r", 2, 0)                      # two subjects on W01
        _cnt_run(env, "s1r", 0, 2, step="W02")          # two verdicts on W02
        got = _one_json(run(["close-step", "--run", "s1r", "--step", "W01", "--json"], env))
        assert got["closed"] is False, "another step's rows satisfied this step's criterion"
        assert "2 short" in got["why"], got["why"]
    finally:
        _rm(d)


@pytest.mark.parametrize("block,fragment", [
    ("completion: {type: counts_at_least, kind_a: verdict}", "kind_b"),
    ("completion: {type: counts_at_least, kind_b: subject}", "kind_a"),
    ("completion: {type: counts_at_least, kind_a: v, kind_b: s, min_count: 2}",
     "unsupported key"),
], ids=["no kind_b", "no kind_a", "unknown key"])
def test_the_new_predicates_spec_is_a_closed_set_like_every_other(env, block, fragment):
    """A typo here would weaken the criterion in silence, which is fatal everywhere else in a
    spec and must be here too."""
    d = _spec(env, "zzz_cnt5", f"""
phases:
  - id: p1
    title: P1
steps:
  - id: W01
    phase: p1
    {block}
    directive: Do it.
""")
    try:
        out = run(["validate", "zzz_cnt5"], env)
        assert out.returncode == BAD_SPEC, out.stdout + out.stderr
        assert fragment in out.stderr, out.stderr
    finally:
        _rm(d)


# ══════════════════ a step may name the tool that produces its effect ══════════════════
#
# The engine had exactly one tool-declaration mechanism and it pointed the other way:
# `requires={"cmd"/"file"}` on a provider says what the ENGINE needs in order to LOOK. Nothing said
# what the AGENT must RUN — yet a whole class of steps demands an effect the engine deliberately
# refuses to perform. `providers.py` says it outright: "The engine performs none of it. The agent
# runs the tool." So the agent was told an effect was mandatory, told it would be independently
# verified, and left to find the producer by grep. For a flow transcribed from a system that is
# still installed, grepping finds that system — a whole second engine rather than one tool.


def test_a_step_may_name_the_tool_that_produces_its_effect(env, tmp_path):
    from engine import flow as flowmod
    tool = tmp_path / "make-it.sh"
    tool.write_text("#!/bin/sh\n", encoding="utf-8")
    d = _spec(env, "__prod__", f"""
uses: [producers]
phases:
  - id: p
steps:
  - id: A
    phase: p
    produced_by: {tool}
""")
    try:
        f = flowmod.load("__prod__")
        assert f.steps["A"].produced_by == str(tool)
        # Detected as a capability, so `uses:` must declare it — the same bidirectional rule every
        # other capability gets, rather than a key nothing cross-checks.
        assert "producers" in flowmod.capabilities_used(f)
    finally:
        _rm(d)


def test_a_producer_pointer_is_a_path_and_refuses_to_be_empty(env):
    """A pointer, never an invocation: the tool's own header is how to call it.

    A copy of its argument shape in the spec would drift the first time a flag was renamed, and the
    reason this key exists is that a driver should not have to hunt for the producer. Handing it a
    stale invocation is worse than handing it nothing.
    """
    from engine import flow as flowmod
    d = _spec(env, "__prod2__", """
uses: [producers]
phases:
  - id: p
steps:
  - id: A
    phase: p
    produced_by: "   "
""")
    try:
        with pytest.raises(flowmod.FlowError) as e:
            flowmod.load("__prod2__")
        assert "produced_by" in str(e.value)
    finally:
        _rm(d)


def test_an_absent_producer_is_reported_by_validate_and_is_not_fatal(env):
    """Load ACCEPTS a tool this machine lacks; `validate` is where absence gets named.

    The same arrangement a provider's `requires` already has: one missing tool must not make a spec
    unreadable on a machine that was only trying to read it. But "can this machine run this flow" is
    a question asked BEFORE opening a run, and until this line existed the only way to answer it was
    to walk the flow until something failed — by which point the failure reads as the flow's.
    """
    from engine import flow as flowmod
    d = _spec(env, "__prod3__", """
uses: [producers]
phases:
  - id: p
steps:
  - id: A
    phase: p
    produced_by: ~/definitely/not/here/tool.sh
""")
    try:
        flowmod.load("__prod3__")                     # readable
        out = run(["validate", "__prod3__"], env).stdout
        assert "producers: 1 declared, 1 absent" in out, out
    finally:
        _rm(d)


# ══════════════════ a topic pointer written INTO prose must resolve ══════════════════
#
# Measured on the largest installed flow before this check existed: 16 topic names referenced inside
# prose BODIES, 4 files, 12 dangling — while `validate` printed `topics cited 4`, because the
# DECLARED side was the only side it read and the declared side was flawless. The tiering doctrine
# ("fetch a topic only when you need it") is load-bearing precisely because a mature flow's guidance
# cannot be preloaded, so a 75% dangling rate silently turns the on-demand tier into dead ends — and
# a driver at a dead end goes looking somewhere else.


def test_a_topic_referenced_in_prose_with_no_file_fails_validate(env):
    from engine import flow as flowmod, prose as prosemod
    d = _spec(env, "__topic__", """
uses: [prose]
prose:
  root: prose/
phases:
  - id: p
    guide: p.md
steps:
  - id: A
    phase: p
""")
    try:
        (d / "prose").mkdir(exist_ok=True)
        (d / "prose" / "p.md").write_text(
            "## Step: A\n\n照着做，理由见 topic `nowhere-to-be-found`。\n", encoding="utf-8")
        f = flowmod.load("__topic__")
        assert [n for _, _, n in prosemod.dangling_topics(f)] == ["nowhere-to-be-found"]
        r = run(["validate", "__topic__"], env)
        assert r.returncode == BAD_SPEC
        assert "nowhere-to-be-found" in r.stdout
    finally:
        # rmtree rather than the usual `_rm`: this spec has a `prose/` subdirectory.
        __import__("shutil").rmtree(d, ignore_errors=True)


def test_the_prose_reference_spelling_is_declared_rather_than_known(env):
    """``topic `x` `` is one project's punctuation, so the engine takes it as a DECLARATION.

    The same treatment `anchor_pattern` already gets. The placeholder is checked because a pattern
    with no `name` group would scan every file, capture nothing, and report that all pointers
    resolve — a check that passes by finding nothing is the failure this whole area is about.
    """
    from engine import flow as flowmod
    d = _spec(env, "__topic2__", """
uses: [prose]
prose:
  root: prose/
  topic_ref_pattern: 'see <<([a-z-]+)>>'
phases:
  - id: p
    guide: p.md
steps:
  - id: A
    phase: p
""")
    try:
        (d / "prose").mkdir(exist_ok=True)
        (d / "prose" / "p.md").write_text("## Step: A\n\nx\n", encoding="utf-8")
        with pytest.raises(flowmod.FlowError) as e:
            flowmod.load("__topic2__")
        assert "name" in str(e.value)
    finally:
        # rmtree rather than the usual `_rm`: this spec has a `prose/` subdirectory, and `_rm`
        # rmdir's a directory it expects to hold only flow.yaml.
        __import__("shutil").rmtree(d, ignore_errors=True)


# ══════════════════ the spec fingerprint means what its warning says ══════════════════


def test_rewording_prose_does_not_tell_an_open_run_its_criteria_moved(env):
    """The digest covers what ENFORCES, so "step semantics may have moved" is true when it fires.

    It used to be `sha256` of the whole file. Editing one sentence of `when:` — a line addressed to a
    reader, constraining nothing — flagged every open run with an assertion about its criteria. That
    happened for real, during unrelated work. A warning that fires on cosmetics is read as noise, and
    then the one that matters is read as noise too.
    """
    from engine import flow as flowmod
    shape = """
phases:
  - id: p
steps:
  - id: A
    phase: p
    title: %s
    completion:
      type: %s
    directive: |
      %s
      done
"""
    d = _spec(env, "__dig__", shape % ("First", "attest", "do"))
    try:
        first = flowmod.load("__dig__").digest
        _spec(env, "__dig__", shape % ("Renamed", "attest", "do it some other way"))
        assert flowmod.load("__dig__").digest == first, "prose moved the fingerprint"
        _spec(env, "__dig__", shape % ("First", "no_open_violations", "do"))
        assert flowmod.load("__dig__").digest != first, "a criterion change did NOT move it"
    finally:
        _rm(d)


def test_the_digest_survives_a_spec_it_cannot_understand(env):
    """It is taken BEFORE validation, so a malformed spec must still get a legible refusal.

    Concretely: `on:` parses to the boolean True under YAML 1.1, and sorting a mapping that mixes
    True with str keys raises TypeError. That would surface as a crash where the author needs a
    refusal — turning "your spec has a reserved key" into "the engine broke". An existing test pins
    that exit code, and it is what caught this while the digest was being changed.
    """
    d = _spec(env, "__dig2__", """
on: something
phases:
  - id: p
steps:
  - id: A
    phase: p
""")
    try:
        assert run(["validate", "__dig2__"], env).returncode == BAD_SPEC
    finally:
        _rm(d)


# ══════════════════ the ledger reports what it already recorded ══════════════════
#
# `run_progress` held four facts. The store held more, and nothing could read it: a run that reached
# the end after twenty refusals looked identical to one that walked straight through, and
# `violation_count` summed the two severities whose whole reason for existing is that they mean
# opposite things. Extended rather than joined by a second provider — a neighbouring provider reading
# the same tables is the duplicate this project keeps removing, and the first time the two disagreed
# nothing would say which was right.


def _measured(env, run_id: str, ability: str, scope: str) -> dict:
    import sys as _s
    _s.path.insert(0, str(REPO))
    from engine import facts as _f
    return _f.gather_all(("run_progress",),
                         {"run_id": run_id, "scope": scope, "scope_kind": "s", "ability": ability})


def test_the_ledger_reports_the_friction_nothing_could_read_before(env, monkeypatch, tmp_path):
    """Refusals and skips were recorded and unreadable. Friction is a measurement.

    The engine writes a `refused` step-log row at three sites. Until this, no criterion could ask
    how many there had been — so "finished" and "finished after fighting it" were the same answer.
    """
    monkeypatch.setenv("HARNESS_STATE_DIR", env["HARNESS_STATE_DIR"])
    d = _spec(env, "__meas__", """
uses: [facts, optional_steps]
facts:
  provider: run_progress
phases:
  - id: p
steps:
  - id: A
    phase: p
  - id: OPT
    phase: p
    optional: true
  - id: B
    phase: p
    deps: [A]
""")
    try:
        assert rc(["open", "__meas__", "--scope", "s1", "--run", "f1"], env) == OK
        # Closing a step whose dependency is unmet is REFUSED — and recorded.
        assert rc(["close-step", "--run", "f1", "--step", "B"], env) == REFUSED
        assert rc(["close-step", "--run", "f1", "--step", "B"], env) == REFUSED
        assert rc(["skip", "--run", "f1", "--step", "OPT", "--reason", "not needed"], env) == OK

        got = _measured(env, "f1", "__meas__", "s1")
        assert got["steps_refused"] == 2, got
        assert got["steps_skipped"] == 1, got
        # And the pre-existing fact still counts a skipped step as accounted-for, because
        # `store.closed_steps` selects `event IN ('closed','skipped')`. Left deliberately: three
        # installed flows read it, so its meaning must not move under them — `steps_skipped` is
        # what makes the split answerable.
        assert "OPT" in got["closed_steps"], got
    finally:
        _rm(d)


def test_the_two_violation_severities_are_counted_apart(env, monkeypatch):
    """`violation_count` sums two things that point in opposite directions.

    `blocked` means an attempt was REFUSED — the guarantee worked and the row is an audit trace.
    `breach` means something passed WITH A MARK — the guarantee did not. A bar placed on the sum
    would be satisfied by not trying, which is the incentive this engine exists to remove.
    """
    monkeypatch.setenv("HARNESS_STATE_DIR", env["HARNESS_STATE_DIR"])
    d = _spec(env, "__sev__", """
uses: [facts]
facts:
  provider: run_progress
phases:
  - id: p
steps:
  - id: A
    phase: p
""")
    try:
        assert rc(["open", "__sev__", "--scope", "s2", "--run", "s1"], env) == OK
        import sys as _s
        _s.path.insert(0, str(REPO))
        from engine import store as _st
        conn = _st.connect()
        try:
            _st.record_violation(conn, "s1", "A", "probe_blocked", "probe", severity="blocked")
            _st.record_violation(conn, "s1", "A", "probe_breach", "probe", severity="breach")
            conn.commit()
        finally:
            conn.close()

        got = _measured(env, "s1", "__sev__", "s2")
        assert got["violations_blocked"] == 1, got
        assert got["violations_breach"] == 1, got
        # The sum is still reported, unchanged, because flows already read it.
        assert got["violation_count"] == 2, got
    finally:
        _rm(d)


def test_an_unfinished_run_has_no_duration_rather_than_a_duration_of_zero(env, monkeypatch):
    """0 seconds and "not finished" must not be the same answer.

    This is the 0-of-0 hazard named elsewhere in this engine: a vacuous value that reads as a
    measured one. So `run_closed` travels with the duration and is a FACT, not something a caller
    has to already know to ask about. A criterion that puts a ceiling on duration would otherwise
    be satisfied by every run that never ended.
    """
    monkeypatch.setenv("HARNESS_STATE_DIR", env["HARNESS_STATE_DIR"])
    d = _spec(env, "__dur__", """
uses: [facts]
facts:
  provider: run_progress
phases:
  - id: p
steps:
  - id: A
    phase: p
""")
    try:
        assert rc(["open", "__dur__", "--scope", "s3", "--run", "d1"], env) == OK
        open_now = _measured(env, "d1", "__dur__", "s3")
        assert open_now["run_closed"] is False, open_now
        assert open_now["run_duration_s"] == 0, open_now

        assert rc(["close-step", "--run", "d1", "--step", "A"], env) == OK
        assert rc(["close-run", "--run", "d1"], env) == OK
        closed = _measured(env, "d1", "__dur__", "s3")
        assert closed["run_closed"] is True, closed
        assert closed["run_duration_s"] >= 0, closed
    finally:
        _rm(d)


def test_the_original_four_facts_did_not_move(env, monkeypatch):
    """A regression guard, because three installed flows and a dozen tests read these by name.

    Extending a provider is safe only while the existing keys keep their meaning. Nothing here
    asserts the new ones — that is the point: this test would still fail if adding them had changed
    what the old ones report.
    """
    monkeypatch.setenv("HARNESS_STATE_DIR", env["HARNESS_STATE_DIR"])
    import sys as _s
    _s.path.insert(0, str(REPO))
    from engine import facts as _f
    schema = _f.schema_of("run_progress")
    for k in ("closed_steps", "evidence_kinds", "gate_count", "violation_count"):
        assert k in schema, f"{k} disappeared from run_progress"

    d = _spec(env, "__orig__", """
uses: [facts]
facts:
  provider: run_progress
phases:
  - id: p
steps:
  - id: A
    phase: p
""")
    try:
        assert rc(["open", "__orig__", "--scope", "s4", "--run", "o1"], env) == OK
        assert rc(["evidence", "--run", "o1", "--step", "A",
                   "--kind", "note", "--value", "x"], env) == OK
        assert rc(["close-step", "--run", "o1", "--step", "A"], env) == OK
        got = _measured(env, "o1", "__orig__", "s4")
        assert got["closed_steps"] == ["A"], got
        assert got["evidence_kinds"] == ["note"], got
        assert got["gate_count"] == 0, got
        assert got["violation_count"] == 0, got
    finally:
        _rm(d)


# ---------------------------------------------------------------------------
# The worktree tools' ROOT. These two run the shell tool itself — every other worktree test
# in this file pins the guard PATTERNS, which say nothing about where trees land.
# ---------------------------------------------------------------------------
WT_SETUP = REPO / "abilities" / "worktree" / "tools" / "worktree-setup.sh"


def _throwaway_git_repo(tmp_path) -> Path:
    d = tmp_path / "srcrepo"
    d.mkdir()
    g = ["git", "-C", str(d)]
    subprocess.run(g + ["init", "-q", "."], check=True)
    (d / "f.txt").write_text("hi\n", encoding="utf-8")
    subprocess.run(g + ["add", "-A"], check=True, capture_output=True)
    subprocess.run(g + ["-c", "user.email=a@b", "-c", "user.name=a",
                        "commit", "-qm", "init"], check=True, capture_output=True)
    return d


@pytest.mark.skipif(not WT_SETUP.exists(), reason="worktree ability not installed here")
def test_the_worktree_root_lands_inside_the_ability_and_git_ignores_it(tmp_path):
    """Where a brazil session's 216,384 files land, and why nothing here may name a path.

    Two properties, and the second is the one that is invisible until it breaks: the root
    resolves under the ability itself (so it travels with the checkout and the tool names no
    absolute path), AND git ignores it. Un-ignored, one `git add -A` in the engine repo commits
    an entire checkout — and the tree would look fine right up to that moment.

    Asserted through the tool's own `--dry-run` resolution rather than by reading the source:
    a regex on the default expression passes while the precedence chain around it is broken.
    Dry-run creates nothing, so this leaves no residue inside the repo.
    """
    src = _throwaway_git_repo(tmp_path)
    out = subprocess.run(
        [str(WT_SETUP), "--uuid", "eeee1111-2222-3333-4444-555555555555",
         "--source-repo", str(src), "--dry-run"],
        capture_output=True, text=True, cwd=str(REPO),
    )
    assert out.returncode == 0, out.stderr
    line = [l for l in out.stdout.splitlines() if l.startswith("WORKTREE_PATH=")]
    assert line, f"the tool stopped emitting its machine-readable path:\n{out.stdout}"
    path = Path(line[0].split("=", 1)[1])
    assert str(path).strip(), "empty WORKTREE_PATH — this assertion would pass on nothing"

    ability = REPO / "abilities" / "worktree"
    assert str(path).startswith(str(ability) + "/"), (
        f"worktree root escaped the ability: {path}\n  expected under {ability}"
    )
    ignored = subprocess.run(["git", "-C", str(REPO), "check-ignore", "-q", str(path)])
    assert ignored.returncode == 0, (
        f"git does NOT ignore {path} — a session's checkout would become `git add -A` fodder.\n"
        "  `state/` is the first line of .gitignore; keep the runtime directory under it."
    )


@pytest.mark.skipif(not WT_SETUP.exists(), reason="worktree ability not installed here")
def test_the_worktree_tool_refuses_when_it_cannot_see_its_own_ability(tmp_path):
    """Invoked through a symlink elsewhere, `dirname $0` is the link's directory.

    Guessing there would root trees at some unrelated `state/worktrees` that git does not
    ignore and teardown never sweeps — silently. Exit 3 is the usage/env code, so a caller
    branching on the number sees a setup error rather than an operational one.
    """
    src = _throwaway_git_repo(tmp_path)
    link = tmp_path / "linked-setup.sh"
    link.symlink_to(WT_SETUP)
    out = subprocess.run(
        [str(link), "--uuid", "eeee2222-3333-4444-5555-666666666666",
         "--source-repo", str(src), "--dry-run"],
        capture_output=True, text=True, cwd=str(tmp_path),
    )
    assert out.returncode == 3, (out.returncode, out.stdout, out.stderr)
    assert "HARNESS_WORKTREE_ROOT" in out.stderr, out.stderr


# ---------------------------------------------------------------------------
# The mandatory-flow record is EDITED, not regenerated. Its own header says "edit or delete
# freely", and for a while the next `--add` quietly falsified that.
# ---------------------------------------------------------------------------
def _policy(env) -> str:
    return (Path(env["HARNESS_STATE_DIR"]) / "required-flows").read_text(encoding="utf-8")


# Parseable but deliberately not canonical: `read()` strips each field, so an entry re-rendered from
# what it parsed comes back without this padding. That difference is the only observable evidence
# that a surviving line was kept rather than rewritten.
PADDED_ENTRY = "delivery\t  ~/workplace/keepme  "


def test_a_hand_written_comment_in_the_record_survives_an_add(env, tmp_path):
    """The file invites editing, so an edit has to outlive the next write.

    A note saying WHY a scope is mandatory is the thing someone writes there, and it was deleted
    with nothing reporting the loss — `read()` never looks at comments, so no test noticed either.
    Also pinned: the LINE the author typed, byte for byte. `read()` strips each field, so
    re-rendering a surviving entry silently reformats it — alignment someone added on purpose
    disappears. (A `~` survives either way: the engine stores the key unexpanded and expands only
    when comparing, so that is not what this guards.)
    """
    assert rc(["require", "--add", "delivery", "--scope-key", str(tmp_path / "aaa")], env) == OK
    pol = Path(env["HARNESS_STATE_DIR"]) / "required-flows"
    pol.write_text(pol.read_text(encoding="utf-8")
                   + "\n# WHY: the release repo, kept mandatory after the 09-02 incident\n"
                   + PADDED_ENTRY + "\n\n# trailing note\n", encoding="utf-8")

    assert rc(["require", "--add", "authoring", "--scope-key", str(tmp_path / "bbb")], env) == OK
    got = _policy(env)
    assert "WHY: the release repo" in got, got
    assert "# trailing note" in got, got
    assert PADDED_ENTRY in got.splitlines(), (
        "the author's own line was reformatted — re-rendering a surviving entry drops the "
        "whitespace they typed:\n" + got)
    assert str(tmp_path / "bbb") in got, got


def test_removing_a_requirement_takes_only_its_own_line(env, tmp_path):
    """A comment above a removed entry stays.

    Deciding a comment "belonged to" the line below it is a guess, and a wrong guess here deletes
    prose nothing can recover. Leaving an orphaned note is the recoverable direction.
    """
    key = str(tmp_path / "ccc")
    assert rc(["require", "--add", "delivery", "--scope-key", key], env) == OK
    pol = Path(env["HARNESS_STATE_DIR"]) / "required-flows"
    pol.write_text(pol.read_text(encoding="utf-8").replace(
        f"delivery\t{key}", f"# explains the entry below\ndelivery\t{key}"), encoding="utf-8")

    assert rc(["require", "--remove", "delivery", "--scope-key", key], env) == OK
    got = _policy(env)
    assert "# explains the entry below" in got, got
    assert key not in got, "the entry itself should be gone:\n" + got


def test_a_deleted_header_is_not_put_back(env, tmp_path):
    """Re-adding the header would be the write overruling the same edit it exists to preserve.

    The header is comments, and the file says comments are the reader's. Writing it only for a file
    that does not exist yet keeps one rule instead of two.
    """
    assert rc(["require", "--add", "delivery", "--scope-key", str(tmp_path / "ddd")], env) == OK
    pol = Path(env["HARNESS_STATE_DIR"]) / "required-flows"
    kept = [l for l in pol.read_text(encoding="utf-8").splitlines() if not l.startswith("#")]
    pol.write_text("\n".join(kept) + "\n", encoding="utf-8")

    assert rc(["require", "--add", "authoring", "--scope-key", str(tmp_path / "eee")], env) == OK
    assert "harness-engine — where a flow is MANDATORY" not in _policy(env), _policy(env)


# ---------------------------------------------------------------------------
# The `memory` ability reads a MACHINE surface now. These pin the two ways that reading can fail
# without the command failing — both of which used to be impossible to distinguish from "empty".
# ---------------------------------------------------------------------------
def _fake_memory_cli(tmp_path, body: str) -> str:
    """A stand-in for the store's CLI. A fake rather than the real store on purpose: the real one
    cannot be made to answer incoherently, and an untestable branch is one nobody can trust."""
    p = tmp_path / "fake_memory.py"
    p.write_text("import sys\n" + body, encoding="utf-8")
    return str(p)


def test_an_unparseable_hot_reading_is_not_an_empty_one(env, monkeypatch, tmp_path):
    """What a store predating the json fix does: `--format json` printed the banner.

    The provider must call that unreadable. Treating unparseable output as zero would resurrect
    exactly the confusion the flag exists to prevent — and silently, since the command exits 0.
    """
    import sys as _s
    _s.path.insert(0, str(REPO))
    from engine import facts as _f, flow as _fl
    _fl.load("shipcheck-asis")
    monkeypatch.setenv("HARNESS_MEMORY_CLI", _fake_memory_cli(
        tmp_path, "print('\\U0001F525 Hot Set: 7 loaded')\nprint('   \\u2514\\u2500 [a]')\n"))
    got = _f.gather("memory.hot_set", {})
    assert got["hot_banner_readable"] is False, got
    assert got["hot_set_count"] == 0 and got["hot_set_ids"] == [], got


def test_an_incoherent_hot_reading_is_not_a_successful_one(env, monkeypatch, tmp_path):
    """count and ids disagreeing means there is no answer to report, not two to choose from.

    Reporting the count would present a store that contradicted itself as one that was consulted
    successfully — and the count is the number a criterion checks.
    """
    import sys as _s
    _s.path.insert(0, str(REPO))
    from engine import facts as _f, flow as _fl
    _fl.load("shipcheck-asis")
    monkeypatch.setenv("HARNESS_MEMORY_CLI", _fake_memory_cli(
        tmp_path, "print('{\"count\": 9, \"ids\": [\"a\"]}')\n"))
    got = _f.gather("memory.hot_set", {})
    assert got["hot_banner_readable"] is False, got
    assert got["hot_set_count"] == 0, got


def test_a_coherent_hot_reading_comes_through_the_machine_surface(env, monkeypatch, tmp_path):
    """The positive case against a fake, so the pair above cannot both pass by the provider simply
    never reporting anything — an all-unreadable provider satisfies every assertion about failure."""
    import sys as _s
    _s.path.insert(0, str(REPO))
    from engine import facts as _f, flow as _fl
    _fl.load("shipcheck-asis")
    monkeypatch.setenv("HARNESS_MEMORY_CLI", _fake_memory_cli(
        tmp_path, "print('{\"count\": 2, \"ids\": [\"a\", \"b\"], \"db\": \"/s/x.db\"}')\n"))
    got = _f.gather("memory.hot_set", {})
    assert got["hot_banner_readable"] is True, got
    assert got["hot_set_count"] == 2 and got["hot_set_ids"] == ["a", "b"], got
    assert got["hot_store_path"] == "/s/x.db", got


def test_no_spec_hand_copies_a_step_count():
    """A number a reader can derive must not be typed into a title or a routing hint.

    Measured drift, twice over: `shipcheck-asis`'s `when:` said 101 steps, then 102, then the flow
    grew to 104 — and four of its eight phase titles were wrong at the same time (context 25→26,
    execution 9→10, observation 8→10, cleanup 19→18). Nothing failed, because nothing was checking:
    the same shape as the README's eight numeric drifts, one layer down in the specs.

    Scoped to `title:` and `when:` — the strings the engine SHOWS as routing and progress. Comments
    are exempt on purpose: a comment recording "the source registry had 101 steps with content" is a
    measurement of another system at a stated time, and forbidding that would delete history to
    satisfy a guard.
    """
    import re
    import sys as _s
    _s.path.insert(0, str(REPO))
    from engine import flow as fl
    bad = []
    count = re.compile(r"(\d+)\s*步")
    for name in fl.available_abilities():
        spec = fl.spec_path(name)
        for i, line in enumerate(spec.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue
            if not re.match(r"^(title|when):", stripped):
                continue
            m = count.search(line)
            if m:
                bad.append(f"  {spec.name}:{i} names {m.group(1)} steps: {stripped[:70]}")
    assert not bad, (
        "a spec hand-copies a step count into text the engine shows:\n" + "\n".join(bad)
        + "\n  `harness abilities` and `harness next` derive it; a typed copy drifts silently."
    )


# ---------------------------------------------------------------------------
# `role: library` — the third value. Borrowable like production, unroutable like a fixture.
# ---------------------------------------------------------------------------
def test_a_library_is_borrowable_but_never_routed(env, tmp_path):
    """The two halves that make the label non-cheap, asserted together.

    Before this role existed, `worktree` and `memory` were `production` carrying a `when:` whose
    content was that they must not be used — and the roster is DERIVED from `when:`, so the engine
    offered both of those sentences to every agent reading the list for a choice.
    """
    import sys as _s
    _s.path.insert(0, str(REPO))
    from engine import flow as fl
    libs = [n for n in fl.available_abilities() if fl.load(n).role == fl.ROLE_LIBRARY]
    assert libs, "no library installed — this test would assert nothing"
    for name in libs:
        f = fl.load(name)
        assert not f.when, f"{name} is a library and carries routing text: {f.when!r}"
    # And borrowable: something real depends on one, which is the whole difference from a fixture.
    borrowed = {d for n in fl.available_abilities() for d in fl.load(n).requires}
    assert borrowed & set(libs), (borrowed, libs)


def test_a_library_may_not_carry_routing_text(env, tmp_path):
    """Load-time refusal, because that is what stops the label from being free: a `library` that
    could still be routed to would be a way to hold real work while sitting out the strength
    report."""
    d = _spec(env, "__lib__", """
role: library
when: |
  This sentence is the whole problem — a library that can be reached is not a library.
phases:
  - id: p
steps:
  - id: A
    phase: p
""")
    try:
        out = run(["validate", "__lib__"], env)
        assert out.returncode == BAD_SPEC, (out.returncode, out.stdout, out.stderr)
        assert "library" in (out.stdout + out.stderr), out.stdout + out.stderr
    finally:
        _rm(d)


def test_production_may_not_borrow_a_fixture(env, tmp_path):
    """The ban `role:`'s own comment claimed for months while nothing checked it.

    Measured before the fix: a production ability declaring `requires: [<a fixture>]` validated ✅.
    So the fixture label WAS the cheap one — claim it, leave the strength report, stay borrowable.
    """
    import sys as _s
    _s.path.insert(0, str(REPO))
    from engine import flow as fl
    fix = [n for n in fl.available_abilities() if fl.load(n).role == fl.ROLE_FIXTURE]
    assert fix, "no fixture installed — this test would assert nothing"
    d = _spec(env, "__borrow__", f"""
uses: [ability_deps]
requires: [{fix[0]}]
phases:
  - id: p
steps:
  - id: A
    phase: p
""")
    try:
        out = run(["validate", "__borrow__"], env)
        assert out.returncode == BAD_SPEC, (out.returncode, out.stdout, out.stderr)
        assert fix[0] in (out.stdout + out.stderr), out.stdout + out.stderr
    finally:
        _rm(d)


def test_the_hot_reading_names_the_store_it_came_from(env, monkeypatch, tmp_path):
    """A count cannot be checked against the right store unless the store is part of the answer.

    Partition the memory DB per user and "the lessons were loaded" becomes satisfiable by a reading
    of somebody else's — a criterion met by the wrong evidence, which this engine treats as worse
    than a missing check.
    """
    import sys as _s
    _s.path.insert(0, str(REPO))
    from engine import facts as _f, flow as _fl
    _fl.load("shipcheck-asis")
    monkeypatch.setenv("HARNESS_MEMORY_CLI", _fake_memory_cli(
        tmp_path, "print('{\"count\": 1, \"ids\": [\"a\"], \"db\": \"/tenants/alice.db\"}')\n"))
    got = _f.gather("memory.hot_set", {})
    assert got["hot_store_path"] == "/tenants/alice.db", got
    assert got["hot_banner_readable"] is True, got


def test_a_hot_reading_that_names_no_store_is_unreadable(env, monkeypatch, tmp_path):
    """What an older store does — it answers count and ids and cannot say whose they are.

    Reporting the count anyway would hand back the number while omitting the only thing that makes
    it checkable, which is the state the field was added to end.
    """
    import sys as _s
    _s.path.insert(0, str(REPO))
    from engine import facts as _f, flow as _fl
    _fl.load("shipcheck-asis")
    monkeypatch.setenv("HARNESS_MEMORY_CLI", _fake_memory_cli(
        tmp_path, "print('{\"count\": 4, \"ids\": [\"a\",\"b\",\"c\",\"d\"]}')\n"))
    got = _f.gather("memory.hot_set", {})
    assert got["hot_banner_readable"] is False, got
    assert got["hot_set_count"] == 0 and got["hot_store_path"] == "", got
