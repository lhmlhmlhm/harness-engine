"""Sample facts a flow can borrow. Deliberately about nothing.

WHAT THIS EXISTS TO DEMONSTRATE, and why a fixture could not. The engine's borrowing machinery has
four properties that only appear across an ability BOUNDARY: a provider resolves as
`<owner>.<name>`, a consumer must declare `requires:` to reach it, a `file` capability is probed
before the call, and a `net` capability names a host without the provider ever reaching for a
credential. Proving those needs a real second ability — and until this one shipped, the only ones
available were somebody's private set, so a fresh clone got an engine whose borrowing was
undemonstrated.

THE SUBJECT MATTER IS INTENTIONALLY EMPTY: a directory that may or may not hold a file, a count of
failures, a host that cannot resolve. A sample that needed a build system, a review tool or a
ticketing system to say anything would teach the reader about those instead of about the seam.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

from engine import facts, operators

# Where the sample looks. Overridable for the same reason every data location in this engine is: a
# test must be able to point it at a scratch directory rather than whatever happens to be here.
# SHIPPED WITH THE ABILITY, resolved from this file's own location — so a fresh clone has it and the
# declared capability does not report absent on the first `validate`. Overridable for the usual
# reason: a test must be able to point it at a scratch directory rather than the shipped one.
SAMPLE_DIR = "${HARNESS_SAMPLE_DIR:-sample}"
# The observing tool this provider shells out to. Declared as a capability alongside the directory,
# so `validate` can say WHICH half is missing before a run starts rather than mid-step.
SAMPLE_TOOL = "tools/sample-read.py"

# A host that is guaranteed NOT to resolve. `.invalid` is reserved by RFC 2606 exactly so an example
# cannot accidentally name somebody's machine — which is what makes it safe to ship a `net`
# capability in a sample: the probe reports "could not reach", never "reached something unexpected".
SAMPLE_HOST = "sample.invalid:443"

# The statuses `sample_state` treats as a settled outcome. A closed set, so a criterion can ask
# "did this settle" without the flow re-deciding what settled means.
SETTLED = frozenset({"accepted", "rejected", "withdrawn"})


@facts.provider("sample_state",
                requires=({"file": SAMPLE_TOOL}, {"file": SAMPLE_DIR}),
                schema={
    # Whether the reader could look AT ALL, paired with everything else for the reason every provider
    # in this engine pairs them: "looked and found nothing" and "could not look" are different
    # answers that otherwise render identically, and only one of them is a reason to stop.
    "analysis_ran": operators.T_BOOL,
    # A count a hook can fire on, so a sample can demonstrate an obligation that BLOCKS a close.
    "assertions_failed": operators.T_INT,
    # WHICH ones failed, not just how many. A hook message naming the count sends the reader back to
    # the raw data to find out what failed; one naming the ids is actionable on its own. This is also
    # what lets a run's fact snapshot be checked for a SPECIFIC failure rather than for a total.
    "failed_assertions": operators.T_LIST,
    # Where the item sits — the directory IS the status in this layout, which is what makes a claimed
    # move checkable rather than self-reported.
    "item_dir": operators.T_STR,
    "item_found": operators.T_BOOL,
    "item_settled": operators.T_BOOL,
})
def _sample_state(ctx: dict) -> dict:
    """What the sample directory says about the item this run recorded.

    RESOLVED BY THE RUN'S OWN EVIDENCE, not by a parameter. A provider that accepted the item name
    from its caller would let a step be corroborated against somebody else's item — the failure this
    engine treats as worse than a missing check, because it is a criterion met by the wrong evidence.
    """
    empty = {"analysis_ran": False, "assertions_failed": 0, "failed_assertions": [],
             "item_dir": "", "item_found": False, "item_settled": False}
    here = Path(__file__).resolve().parent
    base = Path(facts.expand_env(SAMPLE_DIR)).expanduser()
    if not base.is_absolute():
        base = here / base
    tool = here / SAMPLE_TOOL

    from engine import store
    conn = store.connect(read_only=True)
    try:
        rows = store.find_evidence(conn, str(ctx.get("run_id") or ""), None, "sample_item")
    finally:
        conn.close()
    if not rows:
        # No item recorded, so there is nothing to look up. NOT the same as "looked and found
        # nothing": `analysis_ran` stays False because no reading happened at all.
        return empty
    name = Path(str(rows[-1]["value"]).strip()).name
    if not name:
        return empty

    try:
        proc = subprocess.run([sys.executable, str(tool), "--dir", str(base), "--item", name],
                              capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return empty
    if proc.returncode != 0:
        # exit 2 = the tool could not look. Reported as unreadable rather than empty, for the reason
        # the tool's own docstring gives: a caller that cannot tell them apart reads a missing store
        # as a clean bill of health.
        return empty
    try:
        payload = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return empty
    if not isinstance(payload, dict):
        return empty
    got = dict(empty)
    got["analysis_ran"] = True
    got["item_found"] = bool(payload.get("found"))
    got["item_dir"] = str(payload.get("dir") or "")
    got["item_settled"] = str(payload.get("status") or "") in SETTLED
    try:
        got["assertions_failed"] = int(payload.get("failed") or 0)
    except (TypeError, ValueError):
        got["assertions_failed"] = 0
    ids = payload.get("failed_ids")
    got["failed_assertions"] = [str(x) for x in ids] if isinstance(ids, list) else []
    return got


@facts.provider("sample_reach",
                requires=({"cmd": "curl"}, {"net": SAMPLE_HOST}),
                schema={
    # Whether the probe COULD be made at all — distinct from what it found. A network fact that
    # collapses "no tool / no network" into "nothing there" hands a flow the cheapest possible pass.
    "probe_ran": operators.T_BOOL,
    # Whether the host answered. Deliberately NOT a fact about what it said: an unauthenticated probe
    # yields reachability and the shape of the refusal, and that is the fact a criterion can use.
    "host_answered": operators.T_BOOL,
    # Whether the host demanded authorization. Reported as an OBSERVATION, because "this host wants
    # credentials" is a true and useful fact that costs nothing to obtain.
    "auth_needed": operators.T_BOOL,
})
def _sample_reach(ctx: dict) -> dict:
    """Probe a host WITHOUT credentials, and report what that alone establishes.

    NO CREDENTIAL IS READ, LOADED OR PASSED — not from the environment, not from a file, not from a
    keychain. That is the boundary this sample exists to show: trading a broad credential-store
    permission for a small fact is the wrong exchange, and an unauthenticated probe still yields
    reachability plus the auth-redirect shape, which is usually the fact you actually wanted.

    The host is `.invalid` (RFC 2606), so this never reaches anybody's machine. The consequence is
    honest and is the point: `host_answered` is False here, and a flow that needs a real host
    declares its own.
    """
    empty = {"probe_ran": False, "host_answered": False, "auth_needed": False}
    host = SAMPLE_HOST.split(":")[0]
    try:
        proc = subprocess.run(
            ["curl", "-s", "-o", os.devnull, "-w", "%{http_code}",
             "--max-time", "3", f"https://{host}/"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return empty
    got = dict(empty)
    got["probe_ran"] = True
    code = (proc.stdout or "").strip()
    if not code.isdigit() or code == "000":
        return got                       # no answer: reachability is False, not "fine"
    got["host_answered"] = True
    got["auth_needed"] = code in ("401", "403")
    return got
