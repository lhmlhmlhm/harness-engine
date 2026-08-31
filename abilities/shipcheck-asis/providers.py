"""Ability-supplied fact providers.

WHY THIS FILE EXISTS — THE SEAM BEING PROVEN HERE

The engine can RECORD a gate. It cannot COMPUTE whether one should fire: deciding that
requires knowing what a change touches and which invariants that endangers, which is
domain analysis and has no business inside a flow engine.

A reference system in this space puts ~2,600 lines into exactly that computation (a
blast-radius assertion pipeline plus a decision classifier) and it is the single thing
that distinguishes it from a generic state machine. So a faithful port needs an answer to
"where does that live", and this is it:

    domain tool (opaque, copied verbatim)
        → emits structured JSON
            → a fact provider parses it into DECLARED facts
                → the flow's own conditions decide what to gate

The engine learns nothing. It calls a provider it was told about by name, receives a dict
whose shape was declared up front, and evaluates conditions the ability wrote. Swap the
analysis for a different one and only this file and the flow's `when:` clauses change.

WHY THE TOOL IS A SUBPROCESS AND NOT AN IMPORT

Two reasons, and the second is why it stays that way:

1. It is a copied artefact with its own CLI contract (`--workspace`, `--format json`) and
   a schema its docstring calls "frozen at commit-1". A subprocess boundary honours that
   contract exactly and keeps the copy diffable against its origin.
2. Importing it would fuse its module-level assumptions into this process. As a
   subprocess it can fail, time out, or be replaced without taking the run down — and a
   provider failure must be LOUD (`facts.gather` raises) rather than silently yielding no
   facts, because no-facts turns every condition false and mutes every hook.
"""
from __future__ import annotations

import json
import subprocess
import tempfile
import sys
from pathlib import Path

# The engine's registries. Imported for the decorators only — nothing here reaches into
# engine internals.
ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine import facts, operators  # noqa: E402

TOOLS = Path(__file__).resolve().parent / "tools"
ASSERTIONS = TOOLS / "run-assertions.py"
CLASSIFY_GATE = TOOLS / "classify-gate.py"
NOISE_RULES = TOOLS / "config" / "analyzer-noise-rules.yaml"
COMMENT_CLASSIFY = TOOLS / "analyzer-comment-classify.py"


def _change_set_from_evidence(run_id: str) -> dict:
    """Assemble the tool's `--change-set` input from evidence the run has recorded.

    WHY THIS MATTERS MORE THAN IT LOOKS. Without a change-set the analysis is nearly
    inert: measured against a real repository, twelve of its assertions return
    "not applicable" with reasons like *no `files` in change-set* and *no old_values
    declared*. It runs, it exits zero, it reports no failures — and it has checked almost
    nothing. That is the worst possible shape for a check: indistinguishable from a clean
    result.

    So the change-set is not optional plumbing, it is the input that makes the analysis
    mean anything. It comes from the run's own evidence rows, which is the right source:
    the flow's earlier steps are where a change list gets declared and recorded, so the
    facts stay derived from what the run actually said rather than from a second place
    that could disagree.

    Evidence kinds consumed (each optional; a run that records none gets an empty set and
    the honest `checks_meaningful: false` fact below):
        change_file        one path per row
        old_value          a value that must not survive the change
        api_constant       a constant whose value is part of a contract
        auto_generated     a glob for generated output
        module_under_test  a module the tests are supposed to exercise
    """
    from engine import store
    conn = store.connect(read_only=True)
    try:
        rows = conn.execute(
            "SELECT kind, value FROM evidence WHERE run_id = ? ORDER BY id", (run_id,)
        ).fetchall()
    finally:
        conn.close()
    bag: dict[str, list[str]] = {}
    for r in rows:
        if r["value"]:
            bag.setdefault(str(r["kind"]), []).append(str(r["value"]))

    cs: dict = {}
    if bag.get("change_file"):
        cs["files"] = sorted(set(bag["change_file"]))
    if bag.get("old_value"):
        cs["old_values"] = [{"value": v, "scope": "repo"} for v in sorted(set(bag["old_value"]))]
    if bag.get("api_constant"):
        cs["api_constants"] = sorted(set(bag["api_constant"]))
    if bag.get("auto_generated"):
        cs["auto_generated_files"] = sorted(set(bag["auto_generated"]))
    if bag.get("module_under_test"):
        cs["modules_under_test"] = sorted(set(bag["module_under_test"]))
    return cs


@facts.provider("blast_radius", requires=({"file": "tools/run-assertions.py"},), schema={
    # Verdict counts, straight from the tool's own `summary` block.
    "any_fail": operators.T_BOOL,
    "auto_fail_count": operators.T_INT,
    "manual_count": operators.T_INT,
    "deferred_count": operators.T_INT,
    # Which assertions came back in each state. Lists, because a condition wants to ask
    # "did THIS one fail", not just how many did.
    "failed_assertions": operators.T_LIST,
    "manual_assertions": operators.T_LIST,
    "deferred_assertions": operators.T_LIST,
    # Whether the analysis ran at all. Distinguished from "ran and found nothing" on
    # purpose: those two look identical downstream and must not.
    "analysis_ran": operators.T_BOOL,
    # Whether it had enough input to check anything. A run that declared no change list
    # gets an all-clear that means nothing, and a flow should be able to refuse that.
    "checks_meaningful": operators.T_BOOL,
    "declared_file_count": operators.T_INT,
})
def _blast_radius(ctx: dict) -> dict:
    """Run the copied blast-radius pipeline over the run's scope and declare its verdicts.

    `analysis_ran: false` is returned only when the tool is genuinely absent — an
    honest "no information". A tool that RUNS and fails raises instead, so a broken
    analysis can never be mistaken for a clean one.
    """
    empty = {
        "any_fail": False, "auto_fail_count": 0, "manual_count": 0, "deferred_count": 0,
        "failed_assertions": [], "manual_assertions": [], "deferred_assertions": [],
        "analysis_ran": False, "checks_meaningful": False, "declared_file_count": 0,
    }
    # The tool's presence is a declared CAPABILITY now, so absence never reaches here — it
    # yields unavailable facts, and touching one is refused. What remains below is the honest
    # empty: a scope that is not a directory has nothing to analyse, and that is a real answer
    # rather than a missing one.
    workspace = Path(str(ctx.get("scope") or ".")).expanduser()
    if not workspace.is_dir():
        return empty

    cs = _change_set_from_evidence(str(ctx.get("run_id") or ""))
    args = [str(ASSERTIONS), "--workspace", str(workspace), "--format", "json"]
    tmp = None
    if cs:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                         encoding="utf-8") as fh:
            json.dump(cs, fh, ensure_ascii=False)
            tmp = fh.name
        args += ["--change-set", tmp]
    try:
        proc = subprocess.run([sys.executable, *args], capture_output=True,
                              text=True, timeout=180)
    finally:
        if tmp:
            Path(tmp).unlink(missing_ok=True)

    if proc.returncode != 0 and not proc.stdout.strip():
        raise RuntimeError(
            f"blast-radius analysis failed (exit {proc.returncode}): "
            f"{(proc.stderr or '').strip()[:400]}"
        )
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"blast-radius analysis emitted non-JSON: {exc}") from None

    rows = payload.get("assertions") or []
    summary = payload.get("summary") or {}

    def ids_where(state: str) -> list[str]:
        return sorted(str(r.get("assertion_id")) for r in rows
                      if str(r.get("verdict")) == state)

    files = cs.get("files") or []
    return {
        "any_fail": bool(summary.get("any_fail", False)),
        "auto_fail_count": int(summary.get("auto_fail", 0)),
        "manual_count": int(summary.get("manual", 0)),
        "deferred_count": int(summary.get("deferred", 0)),
        "failed_assertions": ids_where("fail"),
        "manual_assertions": ids_where("manual"),
        "deferred_assertions": ids_where("deferred"),
        "analysis_ran": True,
        "checks_meaningful": bool(files),
        "declared_file_count": len(files),
    }


def _run_tool(script: Path, *args: str, stdin: str | None = None, timeout: int = 180):
    """Shell out to a copied tool. Raises on a genuine failure — never returns silence."""
    proc = subprocess.run(
        [sys.executable, str(script), *args],
        input=stdin, capture_output=True, text=True, timeout=timeout,
    )
    if proc.returncode not in (0, 1, 2, 3) or not proc.stdout.strip():
        raise RuntimeError(
            f"{script.name} failed (exit {proc.returncode}): "
            f"{(proc.stderr or '').strip()[:400]}"
        )
    try:
        return json.loads(proc.stdout), proc.returncode
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{script.name} emitted non-JSON: {exc}") from None


@facts.provider("decision_gate",
                requires=({"file": "tools/run-assertions.py"},
                          {"file": "tools/classify-gate.py"}),
                schema={
    # The classifier's own verdict vocabulary, passed through untouched.
    "verdict": operators.T_STR,
    "blocking_ids": operators.T_LIST,
    "warning_ids": operators.T_LIST,
    "blocking_count": operators.T_INT,
    "warning_count": operators.T_INT,
    "pipeline_ran": operators.T_BOOL,
})
def _decision_gate(ctx: dict) -> dict:
    """The two-stage pipeline: assertions → classifier → a gate verdict.

    THIS IS THE POINT OF THE WHOLE ARRANGEMENT. The engine can record a gate; it cannot
    work out whether one is warranted. Working that out is ~2,600 lines of analysis over
    what a change touches and which invariants that endangers. Those lines stay a black
    box here — copied verbatim, run as a subprocess, contract honoured — and what crosses
    into the engine is a handful of DECLARED facts the flow's own conditions read.

    Swap the analysis for a different one and only this function and the flow's `when:`
    clauses change. The engine never learns what a blocking finding is.
    """
    empty = {"verdict": "", "blocking_ids": [], "warning_ids": [],
             "blocking_count": 0, "warning_count": 0, "pipeline_ran": False}
    workspace = Path(str(ctx.get("scope") or ".")).expanduser()
    if not workspace.is_dir():
        return empty

    payload, _ = _run_tool(ASSERTIONS, "--workspace", str(workspace), "--format", "json")

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                     encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False)
        tmp = fh.name
    try:
        result, _rc = _run_tool(CLASSIFY_GATE, "--assertions", tmp, "--format", "json")
    finally:
        Path(tmp).unlink(missing_ok=True)

    # Read the classifier's shape defensively: it is an external contract, and guessing at
    # a renamed key would silently produce an empty verdict — which reads as "clean".
    blocking = result.get("blocking") or result.get("blockers") or []
    warning = result.get("warnings") or result.get("warning") or []
    def ids(rows) -> list[str]:
        out = []
        for r in rows:
            if isinstance(r, dict):
                out.append(str(r.get("assertion_id") or r.get("id") or r))
            else:
                out.append(str(r))
        return sorted(out)
    return {
        "verdict": str(result.get("verdict") or result.get("decision") or ""),
        "blocking_ids": ids(blocking),
        "warning_ids": ids(warning),
        "blocking_count": len(blocking),
        "warning_count": len(warning),
        "pipeline_ran": True,
    }


@facts.provider("review_comments",
                requires=({"file": "tools/analyzer-comment-classify.py"},),
                schema={
    "actionable_count": operators.T_INT,
    "noise_count": operators.T_INT,
    "actionable_ids": operators.T_LIST,
    "classifier_ran": operators.T_BOOL,
})
def _review_comments(ctx: dict) -> dict:
    """Classify external review comments into actionable vs inherent noise.

    Small (120 lines) and rule-driven, but it decides whether an auto-fix loop can close
    at all: treating noise as actionable spins forever, treating a real finding as noise
    ships a defect. The rules live in a yaml beside the tool, so adding a rule needs no
    code change here — which is exactly why the tool comes over as-is.

    Comments arrive through the run's own metadata (`comments`), because only the caller
    knows where they came from. No comments recorded is an honest zero, not a failure.
    """
    empty = {"actionable_count": 0, "noise_count": 0,
             "actionable_ids": [], "classifier_ran": False}
    raw = ctx.get("comments")
    if not raw:
        return {**empty, "classifier_ran": True}

    args = ["--batch"]
    if NOISE_RULES.is_file():
        args += ["--rules", str(NOISE_RULES)]
    result, _ = _run_tool(COMMENT_CLASSIFY, *args,
                          stdin=json.dumps(raw, ensure_ascii=False), timeout=60)
    rows = result if isinstance(result, list) else (result.get("comments") or [])
    actionable = [r for r in rows if str(r.get("classification")) == "actionable"]
    noise = [r for r in rows if str(r.get("classification")) != "actionable"]
    return {
        "actionable_count": len(actionable),
        "noise_count": len(noise),
        "actionable_ids": sorted(str(r.get("id") or r.get("comment_id") or "?")
                                 for r in actionable),
        "classifier_ran": True,
    }
