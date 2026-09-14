#!/usr/bin/env python3
"""composite-analyzer-status.py — aggregate N CRs' analyzer/comment status into one
composite verdict. Standalone (does NOT modify cr-watch): reads per-CR state via
`cr-watch.py get --cr X` and rolls up.

Composite verdict is worst-of the per-CR statuses:
  analyzer-fail > has-comments > working/running > timeout > (all-pass|merged)
`all_clean` iff every CR is in {all-pass, merged}.

Public function `rollup(statuses: dict[str,str])` is pure and unit-tested; the CLI wires
cr-watch as the status source (override with --statuses-json for tests / dry runs).

Usage:
  composite-analyzer-status.py --cr CR-A --cr CR-B [--format md]
  composite-analyzer-status.py --cr CR-A --cr CR-B --statuses-json '{"CR-A":"all-pass","CR-B":"has-comments"}'
"""
import argparse
import json
import os
import subprocess
import sys

# worst-of severity ranking (higher = worse / more blocking)
SEVERITY = {
    "all-pass": 0,
    "merged": 0,
    "timeout": 1,
    "running": 2,
    "working": 2,
    "has-comments": 3,
    "analyzer-fail": 4,
    "unknown": 2,
}
CLEAN = {"all-pass", "merged"}

# Overridable, because the default names a tool that lives OUTSIDE this ability and may simply not
# be on another machine. `fetch_status` already tolerates its absence by returning "unknown" — which
# is the danger: without an override the only symptom of a wrong path is every CR reporting unknown,
# and "no analyzer has spoken yet" is a legitimate state, so nothing looks wrong.
CR_WATCH = os.environ.get("HARNESS_CR_WATCH") or os.path.expanduser(
    "~/.kiro/tools/cr-watch/cr-watch.py")


def rollup(statuses: dict) -> dict:
    """Pure: given {cr_id: status_str} → composite verdict."""
    if not statuses:
        return {"verdict": "unknown", "all_clean": False, "actionable_crs": [],
                "worst": "unknown", "per_cr": {}}
    worst_rank = -1
    worst = "all-pass"
    actionable = []
    for cid, st in statuses.items():
        st = (st or "unknown").strip()
        rank = SEVERITY.get(st, SEVERITY["unknown"])
        if rank > worst_rank:
            worst_rank, worst = rank, st
        if st not in CLEAN:
            actionable.append(cid)
    all_clean = all((s or "").strip() in CLEAN for s in statuses.values())
    # verdict = worst status name; all_clean is the convergence signal
    return {
        "verdict": worst,
        "all_clean": all_clean,
        "actionable_crs": actionable,
        "worst": worst,
        "per_cr": statuses,
    }


def fetch_status(cr: str) -> str:
    """Read one CR's status via cr-watch get. Tolerant: any failure → 'unknown'."""
    try:
        out = subprocess.check_output(
            [sys.executable, CR_WATCH, "get", "--cr", cr, "--format", "json"],
            text=True, stderr=subprocess.DEVNULL,
        )
        data = json.loads(out)
        # cr-watch state exposes a 'status' field
        return str(data.get("status") or data.get("state") or "unknown")
    except Exception:
        return "unknown"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cr", action="append", required=True, help="CR id (repeatable)")
    ap.add_argument("--statuses-json", help="override status source (JSON {cr:status}) for test/dry-run")
    ap.add_argument("--format", choices=["json", "md"], default="json")
    args = ap.parse_args()

    if args.statuses_json:
        statuses = json.loads(args.statuses_json)
    else:
        statuses = {cr: fetch_status(cr) for cr in args.cr}

    result = rollup(statuses)

    if args.format == "md":
        print(f"**Composite verdict**: {result['verdict']}  "
              f"(all_clean={result['all_clean']})")
        for cid, st in result["per_cr"].items():
            mark = "✅" if st in CLEAN else "⚠️"
            print(f"  {mark} {cid}: {st}")
        if result["actionable_crs"]:
            print(f"**Actionable CRs**: {', '.join(result['actionable_crs'])}")
    else:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
