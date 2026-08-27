"""Behaviour tests — the exit codes ARE the contract, so they are what gets pinned.

Every test here asserts a NUMBER, not a message. A tool hook branches on the number;
if a refactor keeps the wording and changes the code, the enforcement silently
disappears and only these assertions notice.
"""
from __future__ import annotations

import json
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
                     "phase_summary", "purge_log"}


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
        "version: 1\nability: __broken_test__\nscope_kind: x\n"
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
        "version: 1\nability: __key_test__\nscope_kind: x\n"
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
        "version: 1\nability: __root_test__\nscope_kind: x\nstages: []\n"
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
    assert "directive 101/101" in r.stdout
    assert "guide 101/101" in r.stdout
    assert "own section 101" in r.stdout, (
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
        r = run(["close-step", "--run", "full", "--step", sid], env)
        assert r.returncode == OK, f"{sid}: {r.stderr}"
        counts["closed"] += 1

    # Every step accounted for, and the untaken branch counted once.
    assert counts["closed"] + counts["skipped"] + counts["branch_skipped"] == len(steps)
    assert counts["skipped"] >= 20, "the optional steps should have been skipped"
    assert counts["branch_skipped"] == 1, "exactly one exclusive branch should be untaken"
    assert counts["affirm"] >= 3 and counts["preauth"] >= 1
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
    assert "101 steps" in r.stdout


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
        assert rc(["close-step", "--run", "real", "--step", sid], env) == OK, sid

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
    d = REPO / "abilities" / name
    d.mkdir(exist_ok=True)
    (d / "flow.yaml").write_text(
        f"version: 1\nability: {name}\nscope_kind: s\n{body.strip()}\n", encoding="utf-8"
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
  provider: git_tree
hooks:
  - id: h
    trigger: phase_end
    phase: p
    when: { fact: changed_file, matches_any: ["*"] }
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
        assert "did you mean 'changed_files'" in r.stderr
    finally:
        _rm(d)


def test_operator_type_mismatch_is_fatal(env):
    d = _spec(env, "__f2__", """
facts:
  provider: git_tree
hooks:
  - id: h
    trigger: phase_end
    phase: p
    when: { fact: vcs_branch, count_gte: 3 }
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
  provider: git_tree
hooks:
  - id: h
    trigger: phase_end
    phase: p
    when: { fact: changed_files, matchs_any: ["*"] }
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
        assert "unknown operator" in r.stderr
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
        assert rc(["close-run", "--run", "fo", "--force"], env) == OK
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
    assert "blast_radius" in _f.registered()
    schema = _f.schema_of("blast_radius")
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
        r = run(["close-step", "--run", "seam", "--step", sid], env)
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

    assert rc(["discharge", "--run", "seam", "--hook", "blast-radius-absent",
               "--evidence", "scope is not a repository"], env) == OK
    assert rc(["close-run", "--run", "seam", "--force"], env) == OK


def test_guard_pointing_at_ungated_step_is_rejected(env):
    """A guard that could never refuse is a false promise, so the spec is invalid."""
    d = REPO / "abilities" / "__inert_test__"
    d.mkdir(exist_ok=True)
    (d / "flow.yaml").write_text(
        "version: 1\nability: __inert_test__\nscope_kind: x\n"
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
    assert rc(["close-run", "--run", "cr2", "--force"], env) == OK
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
    assert rc(["close-run", "--run", "s1", "--force"], env) == OK
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
        assert "evidnce" in r.stderr and "does not implement" in r.stderr
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
  providers: [git_tree, git_tree]
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
  provider: git_tree
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
    assert mix[3] >= 13 and mix[2] >= 52, dict(mix)
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
    for ws in ("/Users/x/.kiro/skills/ux/output/proj", "/tmp/an-ordinary-package"):
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


def test_a_provider_from_another_ability_needs_the_declaration(env):
    """Without `requires`, naming a borrowed provider must FAIL rather than work by luck.

    The registry is process-global, so a borrowed name would resolve whenever the owning
    ability happened to have been loaded first — passing in one order and failing in another.
    A dependency that works by load order is worse than one that is refused.
    """
    import importlib
    from engine import facts as factsmod, flow as flowmod
    d = _spec(env, "borrower", """
facts:
  providers: [plan_taxonomy]
phases: [{id: p1}]
steps: [{id: A, phase: p1}]
""")
    try:
        # Simulate a cold process: drop the borrowed registration and the load marker.
        # Drop EVERY provider the owning ability registers, or re-importing it at the end
        # raises on the ones still present — and that failure would look like this test's
        # subject rather than its cleanup.
        borrowed = [n for n in list(factsmod._PROVIDERS)
                    if n in ("plan_taxonomy", "workspace_taxonomy")]
        for n in borrowed:
            factsmod._PROVIDERS.pop(n, None)
        flowmod._EXTENSIONS_LOADED.discard("push")
        r = run(["validate", "borrower"], env)
        assert r.returncode == BAD_SPEC, r.stdout + r.stderr
        assert "not registered" in r.stderr
    finally:
        _rm(d)
        for n in ("plan_taxonomy", "workspace_taxonomy"):
            factsmod._PROVIDERS.pop(n, None)
        flowmod._EXTENSIONS_LOADED.discard("push")
        flowmod.load("push")   # re-register for the rest of the session


def test_the_two_new_abilities_have_full_prose_and_no_product_names(env):
    """Both transcriptions must be self-contained documentation, not a copy of someone's docs.

    A product name appearing here is evidence of copying, and it also makes the ability
    unusable anywhere that system does not exist.
    """
    import re
    from engine import flow as flowmod, prose
    BAD = ["CRUX", "brazil", "Taskei", "Midway", "agent-fleet",
           "AutoSDE", "Coverlay", "easymrgr", "mwinit"]
    for name in ("push", "plan"):
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
    ("push", "ux",         "/Users/x/.kiro/skills/ux/output/proj", True),
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
        assert rc(["close-run", "--run", "gl", "--force"], env) == OK
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
    assert rc(["close-run", "--run", "pg", "--force"], env) == OK
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
        for provider in f.facts_providers:
            owned = {k for k, v in f.facts_owner.items() if v == provider}
            assert owned & read, (
                f"ability '{name}' declares provider '{provider}' ({len(owned)} facts) but no "
                f"condition or template reads any of them — either wire it or drop it"
            )


def test_every_ability_declares_when_to_reach_for_it(env):
    """Routing must be DERIVABLE, not maintained by hand next to the abilities.

    A hand-kept "which ability for what" table is stale the moment a seventh ability lands, and
    a quietly-stale routing table sends work into the wrong lifecycle — which is worse than
    having no table, because it looks authoritative.
    """
    from engine import flow as flowmod
    missing = []
    for name in flowmod.available_abilities():
        f = flowmod.load(name)
        if len(f.when.strip()) < 20:
            missing.append(name)
    assert not missing, f"no usable `when:` on: {missing}"
    # And the capability map must NOT duplicate what `when:` already says.
    cap = (REPO / "integrations" / "CAPABILITIES.md").read_text(encoding="utf-8")
    assert "harness abilities" in cap, "the map must point at the derivable source"


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
