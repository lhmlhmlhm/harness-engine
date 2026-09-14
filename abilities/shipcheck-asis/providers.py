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
import os
import re
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
RUN_METRICS = TOOLS / "analyze-run-metrics.py"
FLEET_PROBE = TOOLS / "fleet_probe.py"
# NOT copied into the ability, deliberately — see `_hot_set`.
MEMORY_CLI_DEFAULT = "${HARNESS_MEMORY_CLI:-~/.kiro/skills/shared-kb/memory/memory.py}"
# 与 worktree 同类：这两处的效果由 agent 跑脚本完成，引擎只读核实，所以脚本一个字节都不复制。
# 两个位置都可用环境变量覆盖，而这不是为了灵活：**没有覆盖口的话，测试只能读用户真实的
# 方案语料**（几百份文档），而一个粗心的测试会写进去。这类事在相邻项目里发生过一次——一套
# 测试扫掉了 772MB 真实存储——所以给读取真实位置的 provider 留一个重定向口是安全属性，
# 不是便利。被读的 CLI 自己也有同样的口（MEMORY_DB_PATH），这里与它对称。
PLAN_DATA_DEFAULT = "${HARNESS_PLAN_DATA_DIR:-~/.kiro/skills/plan/data}"
HISTORY_LOG_DEFAULT = "${HARNESS_HISTORY_LOG_DIR:-~/.kiro/skills/ship-check/history-log}"


def _spec_path(spec: str) -> Path:
    """Resolve a `${VAR:-default}` path spec with THE ENGINE'S expander, at call time.

    Deliberately not a local re-implementation: `facts.expand_env` is the same function the
    capability probe uses, so `validate` reporting a file present and this provider finding it
    cannot disagree. That disagreement was the defect — the probe read the default while the
    provider read the override.

    Read at import it would be frozen for the life of the process — correct for a one-shot CLI
    and wrong for anything long-lived, and it silently defeats a caller that sets the override
    after loading this module. A path that can change should be read when it is used.
    """
    return Path(facts.expand_env(spec))
PLAN_DIRS = ("pending", "pushed", "in-progress", "done", "shipped")
QUALITY_SLO_DEFAULT = "${HARNESS_QUALITY_SLO:-~/.kiro/loop agents/quality-slo.yaml}"
# NO SESSIONS_DIR HERE. It was declared and never read — the transcript directory is a fact
# about the host, and the only thing that needs it is `tools/analyze-run-metrics.py`, which
# derives it itself. A constant naming a private path that nothing reads reads as a
# dependency this provider has, and it did mislead a reader into counting it as one.


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


def _run_tool_text(script: Path, *args: str, timeout: int = 60):
    """Shell out to a copied tool that emits TEXT, not JSON.

    A separate runner rather than a flag on the JSON one: the two differ in what counts as
    a usable answer, and folding them together would mean one of the two loses its check.
    Here an empty stdout on a nominally-successful exit is a real failure — the tool is
    supposed to have printed a report — whereas a non-zero exit is information the caller
    interprets, not an error to raise on.
    """
    proc = subprocess.run(
        [sys.executable, str(script), *args],
        capture_output=True, text=True, timeout=timeout,
    )
    if proc.returncode == 0 and not proc.stdout.strip():
        raise RuntimeError(f"{script.name} exited 0 but printed nothing")
    return proc.stdout, proc.returncode


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


@facts.provider("run_metrics",
                requires=({"file": "tools/analyze-run-metrics.py"},
                          {"file": "~/.kiro/sessions/cli"}),
                schema={
    # Whether the runtime record could actually be READ and had turns in the window.
    # Distinct from the capability: the engine answers "is the session store present at
    # all", this answers "was there anything in it for this run". Collapsing the two would
    # put an environment question and a data question behind one boolean, and the flow could
    # then no longer tell "not running under that runtime" from "running, nothing recorded".
    "metrics_available": operators.T_BOOL,
    "run_turns": operators.T_INT,
    "run_calls": operators.T_INT,
    "run_credits": operators.T_STR,
    "run_peak_ctx_pct": operators.T_INT,
})
def _run_metrics(ctx: dict) -> dict:
    """Numbers about the run itself, derived from the agent runtime's own record.

    WHY THIS ONE, out of the pure-compute tools that were candidates. Its output is the only
    thing in that group this engine has something to compare AGAINST: a step records a
    metrics summary, and until now that criterion was "a row of this kind exists" — which
    accepts numbers nobody produced. The other candidates compute over the source system's
    OWN spec tree and history corpus, and this engine has neither those files nor a reason to
    audit them, so wiring them would have added providers with nothing to read them.

    The tool is copied verbatim and takes its window from the run's own open time, so the
    numbers describe this run rather than whatever else the machine has been doing.
    """
    empty = {"metrics_available": False, "run_turns": 0, "run_calls": 0,
             "run_credits": "0", "run_peak_ctx_pct": 0}
    from engine import store
    conn = store.connect(read_only=True)
    try:
        row = store.get_run(conn, str(ctx.get("run_id") or ""))
    finally:
        conn.close()
    if row is None:
        return empty
    out, rc = _run_tool_text(RUN_METRICS, "--since", str(row["opened_at"]),
                             "--mode", "plan-doc")
    # rc 1 = the runtime session could not be identified; 2 = identified but no turns in the
    # window; 3 = its record file is missing. All three are honest "no numbers", and none of
    # them is a capability problem — the store directory exists or this provider would not
    # have been called at all.
    if rc != 0:
        return empty
    got = dict(empty)
    got["metrics_available"] = True
    m = re.search(r"Turns:\s*(\d+)\s*/\s*Calls:\s*(\d+)\s*/\s*Credits:\s*([\d.]+)", out)
    if m:
        got["run_turns"] = int(m.group(1))
        got["run_calls"] = int(m.group(2))
        got["run_credits"] = m.group(3)
    m = re.search(r"Peak context:\s*([\d.]+)%", out)
    if m:
        got["run_peak_ctx_pct"] = int(float(m.group(1)))
    return got


@facts.provider("fleet_central",
                requires=({"file": "tools/fleet_probe.py"},),
                schema={
    # Whether this run is serving a dispatched task at all. Recorded early by the flow, so
    # "there is nothing to report to" is a fact on the record rather than a late assertion.
    "fleet_applicable": operators.T_BOOL,
    # Whether the coordinator could actually be READ. The tool's own vocabulary has three
    # verdicts and only one of them is this: `indeterminate` (an endpoint that answers but
    # not about this task) maps to false here, deliberately — its source comment says it best,
    # "we could not look -> NO-OP is NOT granted".
    "fleet_reachable": operators.T_BOOL,
    # What has ARRIVED, which is the only thing that distinguishes reporting from saying so.
    "fleet_event_count": operators.T_INT,
    "fleet_artifact_count": operators.T_INT,
})
def _fleet_central(ctx: dict) -> dict:
    """Ask the coordinator what actually arrived for this run's task.

    WHY THIS EXISTS AND WHAT IT REPLACES. The behavioural standard for dispatched work says to
    report each milestone, and in the reference system that requirement is prose with a
    fire-once hook — which can be acknowledged with a sentence. Measured over its own ledger
    that produced 162 forced acknowledgements and 24 recorded non-arrivals: the claim "I
    reported it" was accepted at face value, and the one thing that could contradict it was
    never asked. So it is asked here.

    NOT A CAPABILITY, deliberately. The endpoint is DISCOVERED (from the tool's own config or
    an env override), so there is no fixed target for the engine to probe — a capability
    descriptor needs a target known when the spec is written. Reachability of a discovered
    endpoint is therefore a fact, and the tool file is the capability.
    """
    from engine import store
    empty = {"fleet_applicable": False, "fleet_reachable": False,
             "fleet_event_count": 0, "fleet_artifact_count": 0}
    conn = store.connect(read_only=True)
    try:
        rows = store.find_evidence(conn, str(ctx.get("run_id") or ""), None, "fleet_task")
    finally:
        conn.close()
    if not rows:
        return empty
    task_id = str(rows[-1]["value"]).strip()
    if not task_id or task_id.lower() in ("local", "none", "-"):
        # A run that is not serving a dispatched task. Recorded, not inferred from silence.
        return empty
    proc = subprocess.run(
        [sys.executable, str(FLEET_PROBE), "snapshot", "--task-id", task_id],
        capture_output=True, text=True, timeout=60,
    )
    got = dict(empty)
    got["fleet_applicable"] = True
    try:
        payload = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return got
    if str(payload.get("verdict")) != "reachable":
        return got
    got["fleet_reachable"] = True
    got["fleet_event_count"] = int(payload.get("events") or 0)
    got["fleet_artifact_count"] = int(payload.get("artifacts") or 0)
    return got


@facts.provider("hot_set",
                requires=({"file": MEMORY_CLI_DEFAULT},),
                schema={
    # Whether the store could be READ. Paired with the count on purpose: "consulted and
    # genuinely empty" and "never consulted" both render as zero, and the standing instruction
    # in this space is explicitly not to let the second become the first.
    "hot_banner_readable": operators.T_BOOL,
    "hot_set_count": operators.T_INT,
    "hot_set_ids": operators.T_LIST,
})
def _hot_set(ctx: dict) -> dict:
    """The always-resident slice of accumulated lessons, read from wherever it actually lives.

    WHY THIS TOOL IS NOT COPIED, unlike every other one here. The others are algorithms, and an
    algorithm travels. This one's value is a LIVE LOCAL STORE — a database that is deliberately
    not in version control because it accumulates per-machine usage signal. Copying the reader
    would produce a reader pointed at nothing. So the capability names the path where it lives,
    and absence there is reported rather than papered over.

    That is the general rule this case establishes: a tool whose value is its own accumulated
    state cannot be vendored, and the capability declaration is what keeps its absence honest.

    The command is a pure read (SELECTs only, no counter or timestamp touched), so calling it
    repeatedly to evaluate a criterion is safe. Its sibling `recall` is NOT — it records usage
    unless told otherwise — which is exactly why this provider calls the one that does not.
    """
    empty = {"hot_banner_readable": False, "hot_set_count": 0, "hot_set_ids": []}
    # Overridable for the same reason the other two are: a test must be able to point this at a
    # scratch store instead of the live one. Unlike the plan corpus this is a CLI rather than a
    # directory, so a wrong value fails loudly on exec rather than reading someone else's data.
    cli = _spec_path(MEMORY_CLI_DEFAULT).expanduser()
    try:
        proc = subprocess.run([sys.executable, str(cli), "hot-banner"],
                              capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return empty
    # exit 3 = the store exists but was never migrated; exit 1 = locked or crashed. Both are
    # "could not look", and the count they imply is an artefact of failure, not a result.
    if proc.returncode != 0:
        return empty
    ids = re.findall(r"└─ \[([^\]]+)\]", proc.stdout)
    m = re.search(r"Hot Set:\s*(\d+)\s*loaded", proc.stdout)
    if not m:
        return empty
    return {"hot_banner_readable": True,
            "hot_set_count": int(m.group(1)),
            "hot_set_ids": ids}


@facts.provider("plan_writeback",
                requires=({"file": PLAN_DATA_DEFAULT},),
                schema={
    # Where the plan document actually SITS now. The directory is the status in this layout —
    # measured over the real corpus, directory and frontmatter agree in 405 of 406 documents —
    # so a claim that it was filed away has something to be checked against.
    "plan_doc_dir": operators.T_STR,
    "plan_doc_status": operators.T_STR,
    "plan_doc_found": operators.T_BOOL,
    # The two artefacts the closing writeback leaves behind. Their presence is what makes
    # "the writeback ran" checkable without re-running anything.
    "shipped_block_present": operators.T_BOOL,
    "history_record_exists": operators.T_BOOL,
})
def _plan_writeback(ctx: dict) -> dict:
    """Where the plan document ended up, and whether the closing writeback left its traces.

    RESOLVED BY SLUG, NOT BY THE RECORDED PATH — and that is the whole subtlety. The path
    captured early in the run is stale BY DESIGN at closing time, because moving the document
    between status folders is precisely the effect under test. A provider that read the recorded
    path would find the file missing and report the move as a failure, or worse, find it still
    there and report success for a move that never happened. So the slug is taken from the
    recorded path and the document is looked up wherever it now lives.

    Same division of labour as worktree provisioning: the agent runs the writeback, the engine
    asks the filesystem what is true. Neither script is copied here — an effect does not belong
    to the engine, and these two move files, rewrite frontmatter and create directories.
    """
    from engine import store
    empty = {"plan_doc_dir": "", "plan_doc_status": "", "plan_doc_found": False,
             "shipped_block_present": False, "history_record_exists": False}
    conn = store.connect(read_only=True)
    try:
        rows = store.find_evidence(conn, str(ctx.get("run_id") or ""), None, "plan_doc")
    finally:
        conn.close()
    if not rows:
        return empty
    slug = Path(str(rows[-1]["value"]).strip()).name
    if slug.endswith(".md"):
        slug = slug[:-3]
    if not slug:
        return empty

    got = dict(empty)
    base = _spec_path(PLAN_DATA_DEFAULT).expanduser()
    hit = None
    for d in PLAN_DIRS:
        cand = base / d / f"{slug}.md"
        if cand.is_file():
            hit, got["plan_doc_dir"] = cand, d
            break
    if hit is None:
        return got
    got["plan_doc_found"] = True
    try:
        text = hit.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return got
    # Anchored to the LEADING frontmatter block. A first-match scan of the whole document
    # happens to agree across the corpus today, but documents quote code that contains the same
    # key, so anything that collected every match would read a snippet as the status.
    fm = re.match(r"---\n(.*?)\n---", text, re.S)
    if fm:
        m = re.search(r"^status:\s*(\S+)", fm.group(1), re.M)
        got["plan_doc_status"] = m.group(1) if m else ""
    got["shipped_block_present"] = bool(re.search(r"^##\s+Shipped\b", text, re.M))

    # Matched by CONTAINS, not by an exact directory name: the writer's naming has changed over
    # time (of 205 real entries only 18 carry a date prefix), so an exact-name lookup would
    # report a present record as missing for every older shape.
    hl = _spec_path(HISTORY_LOG_DEFAULT).expanduser()
    if hl.is_dir():
        got["history_record_exists"] = any(
            (d / "record.md").is_file() for d in hl.iterdir()
            if d.is_dir() and slug in d.name)
    return got


@facts.provider("quality_slo",
                requires=({"file": QUALITY_SLO_DEFAULT},),
                schema={
    # Whether the DECLARATION could be read. A verdict with no declared bar is an opinion.
    "slo_readable": operators.T_STR,
    "layer_green_threshold": operators.T_STR,
    # The honest caveat, as a fact rather than a footnote — see the docstring.
    "baselines_are_placeholder": operators.T_BOOL,
    # The two dimensions whose inputs this engine genuinely owns, from its own store.
    "completeness_rate_pct": operators.T_INT,
    "error_violation_count": operators.T_INT,
    "warning_violation_count": operators.T_INT,
})
def _quality_slo(ctx: dict) -> dict:
    """The declared quality bar, plus the two dimensions this engine can actually compute.

    NO SYNTHETIC SCORE, and that is the whole design. The declaration weights five dimensions;
    measured against what is actually available, only two of them are real here:

        completeness  0.3   passed / total steps          -> this engine knows exactly
        correctness   0.3   error and warning violations   -> this engine knows exactly
        efficiency    0.2   needs duration p50/p95         -> every layer's baseline is
        cost          0.1   needs cost p50/p95                marked `status: placeholder`
        adherence     0.1   the declaration itself says "MVP: assume 1.0 for both"

    So 40% of the weight is invented or constant. Computing one number from that and calling it
    the score would produce exactly the artefact this engine exists to refuse: a figure that
    reads as measured while nearly half of it is a stand-in, and the stand-in is invisible once
    the weights are folded together. The two real dimensions are therefore exposed SEPARATELY,
    the placeholder state is exposed AS A FACT, and no total is computed here.

    That also re-characterises a gap reported for several rounds. "Regression baselines are
    empty" is true, but the missing part is upstream: the declaration's own p50/p95 are
    placeholders, so there is nothing yet to import. Building a second set here would be the
    diverging copy this project keeps refusing.

    The declaration is READ, never copied — it is the single source of truth for the bar.
    """
    from engine import store
    empty = {"slo_readable": "", "layer_green_threshold": "",
             "baselines_are_placeholder": True, "completeness_rate_pct": 0,
             "error_violation_count": 0, "warning_violation_count": 0}
    run_id = str(ctx.get("run_id") or "")

    # ── this engine's own two dimensions, from its own store
    got = dict(empty)
    conn = store.connect(read_only=True)
    try:
        row = store.get_run(conn, run_id)
        if row is not None:
            from engine import flow as flowmod
            f = flowmod.load(str(row["ability"]))
            required = set(f.required_steps(row["variant"]))
            done = store.closed_steps(conn, run_id)
            if required:
                got["completeness_rate_pct"] = int(100 * len(done & required) / len(required))
            # blocked / breach map onto the declaration's error / warning. Named here because
            # the two vocabularies are not the same words and a silent mapping would be a
            # place for the meaning to drift.
            got["error_violation_count"] = len(
                store.violations(conn, run_id, severity="blocked"))
            got["warning_violation_count"] = len(
                store.violations(conn, run_id, severity="breach"))
    finally:
        conn.close()

    # ── the declared bar
    path = _spec_path(QUALITY_SLO_DEFAULT).expanduser()
    # NO `is_file()` PRE-CHECK — it was there and was removed as redundant: a missing file
    # raises below and lands on the same answer, and a guard that cannot change an outcome is
    # the thing this engine spends its refusals on elsewhere. (Verified by mutation: removing
    # it alone changes nothing.)
    #
    # The `except` is NOT redundant, and the difference is worth naming. Without it a corrupt
    # declaration escapes as an exception, which `facts.gather` turns into a loud failure that
    # REFUSES THE WHOLE RUN. A declaration that cannot be parsed should mean "there is no
    # declared bar" — the verdict claim below then refuses on its own terms — not "the engine
    # broke". Missing and corrupt land on the same honest answer by different routes.
    try:
        import yaml
        decl = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa — unreadable, by any route, is "no declared bar"
        return got
    slo = ((decl.get("slo") or {}).get("ship-check") or {}).get("_default") or {}
    layer = (slo.get("layer") or {})
    if "green" not in layer:
        return got
    got["slo_readable"] = "yes"
    got["layer_green_threshold"] = str(layer["green"])
    base = (decl.get("baselines") or {}).get("ship-check") or {}
    # Placeholder unless EVERY layer has moved off it: one real layer among seven does not make
    # the efficiency and cost dimensions measurable.
    got["baselines_are_placeholder"] = (
        not base or any(str(v.get("status")) == "placeholder" for v in base.values()))
    return got
