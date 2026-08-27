#!/usr/bin/env python3
"""composite-autofix-gate.py — aggregate convergence gate for composite CR auto_fix.

Decides, across ALL CRs in a composite, whether the analyzer/comment state has converged
(no actionable comments anywhere) or what the next action is. This is the un-spoofable
completion predicate for composite Observation: the gate's EXIT CODE is the gate.

Exit codes (usable directly as a shell gate, like yield-gate.py):
  0  converged  — no actionable comments across all CRs (all_clean); safe to proceed/yield-to-merge
  2  next-fix   — an actionable CR under cap exists; `target` is the next CR to fix (by fix_order)
  3  yield      — must hand to human: reason ∈ {manual, cap-hit, inherent-only}

Convergence rules:
  auto_fix == false:  any actionable  → yield(manual);            else converged
  auto_fix == true :  total actionable == 0:
                          inherent_noise > 0 → yield(inherent-only)   (need override decision)
                          else               → converged
                      total actionable  > 0:
                          some actionable CR under cap → next-fix (first by fix_order)
                          else (all hit cap)           → yield(cap-hit)

`decide(state)` is pure and unit-tested. Never loops: next-fix is only returned while a CR
is strictly under cap; the caller increments fix_iterations, guaranteeing termination.

Usage:
  composite-autofix-gate.py --state-json '{"auto_fix":true,"cap":3,"fix_order":["cr-1","cr-2"],
     "crs":{"cr-1":{"actionable":1,"inherent_noise":0,"fix_iterations":0},
            "cr-2":{"actionable":0,"inherent_noise":2,"fix_iterations":0}}}'
"""
import argparse
import json
import sys

CONVERGED, NEXT_FIX, YIELD = 0, 2, 3


def decide(state: dict) -> dict:
    auto_fix = bool(state.get("auto_fix", True))
    cap = int(state.get("cap", 3))
    crs = state.get("crs", {}) or {}
    fix_order = state.get("fix_order") or list(crs.keys())

    total_actionable = sum(int(c.get("actionable", 0)) for c in crs.values())
    total_inherent = sum(int(c.get("inherent_noise", 0)) for c in crs.values())

    if not auto_fix:
        if total_actionable > 0:
            return {"code": YIELD, "verdict": "yield", "reason": "manual",
                    "detail": "auto_fix=false and actionable comments exist",
                    "converged": False, "target": None}
        return {"code": CONVERGED, "verdict": "converged", "reason": "no-actionable",
                "detail": "no actionable comments", "converged": True, "target": None}

    # auto_fix == true
    if total_actionable == 0:
        if total_inherent > 0:
            return {"code": YIELD, "verdict": "yield", "reason": "inherent-only",
                    "detail": f"{total_inherent} inherent-noise comment(s) remain — "
                              "need human override decision (do NOT auto-fix copied content)",
                    "converged": True, "target": None}
        return {"code": CONVERGED, "verdict": "converged", "reason": "all-clean",
                "detail": "no actionable and no inherent-noise comments", "converged": True,
                "target": None}

    # actionable > 0 — find next CR to fix, in fix_order, that is under cap
    for cid in fix_order:
        c = crs.get(cid, {})
        if int(c.get("actionable", 0)) > 0 and int(c.get("fix_iterations", 0)) < cap:
            return {"code": NEXT_FIX, "verdict": "next-fix", "reason": "under-cap",
                    "detail": f"fix {cid} (iteration {int(c.get('fix_iterations', 0)) + 1}/{cap})",
                    "converged": False, "target": cid}

    # actionable remain but every such CR hit cap
    stuck = [cid for cid in fix_order
             if int(crs.get(cid, {}).get("actionable", 0)) > 0
             and int(crs.get(cid, {}).get("fix_iterations", 0)) >= cap]
    return {"code": YIELD, "verdict": "yield", "reason": "cap-hit",
            "detail": f"actionable comments remain after cap={cap} on: {', '.join(stuck)}",
            "converged": False, "target": None}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--state-json", required=True, help="convergence state as JSON")
    ap.add_argument("--format", choices=["json", "md"], default="json")
    args = ap.parse_args()

    result = decide(json.loads(args.state_json))

    if args.format == "md":
        print(f"**Gate**: {result['verdict']}"
              + (f" → {result['target']}" if result.get("target") else "")
              + f"  ({result['reason']})")
        print(f"  {result['detail']}")
    else:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    return result["code"]


if __name__ == "__main__":
    sys.exit(main())
