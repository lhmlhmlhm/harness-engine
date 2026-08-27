#!/usr/bin/env python3
"""composite-topology.py — classify a composite plan's CRs as stacked vs independent
and derive the fix-propagation order.

This is the geodesic of the composite analyzer-convergence design: if the CRs form a
*stack* (share a package/workspace → sequential commits on one branch), a fix to an
upstream CR forces a rebase + re-push of every downstream CR, so fixes MUST propagate
in ship_order (upstream first). If the CRs are *independent* (distinct workspaces),
fixes can run in parallel.

Fail-safe: when topology cannot be determined, default to STACKED (conservative) —
treating independent CRs as stacked only costs serialization time, but treating stacked
CRs as independent corrupts downstream base commits.

Input : a plan doc (frontmatter with `crs[]` + `ship_order`), or explicit --cr specs.
Output: JSON {topology, fix_order, groups, workspaces, reason, fallback_used}

Usage:
  composite-topology.py --plan-doc <path>
  composite-topology.py --plan-doc <path> --format md
"""
import argparse
import json
import os
import sys

try:
    import yaml
except ImportError:  # graceful — topology is best-effort, never hard-crash the ring
    yaml = None


def _read_frontmatter(path: str) -> dict:
    """Parse the leading --- yaml --- block of a markdown plan doc."""
    with open(os.path.expanduser(path), encoding="utf-8") as fh:
        text = fh.read()
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    block = text[3:end]
    if yaml is None:
        raise RuntimeError("PyYAML unavailable; cannot parse frontmatter")
    return yaml.safe_load(block) or {}


def _norm_ws(cr: dict) -> str:
    """The package/workspace key that decides stacking. Prefer packages[0].path,
    fall back to workspace. Normalized to an absolute-ish string for comparison."""
    pkgs = cr.get("packages") or []
    if pkgs and isinstance(pkgs, list) and pkgs[0].get("path"):
        return os.path.normpath(os.path.expanduser(str(pkgs[0]["path"])))
    ws = cr.get("workspace") or ""
    return os.path.normpath(os.path.expanduser(str(ws))) if ws else ""


def classify(crs: list, ship_order: list) -> dict:
    """Core deterministic classification.

    stacked   : 2+ CRs resolve to the SAME package/workspace key (commit stack)
    independent: every CR has a distinct package/workspace key
    """
    fallback_used = False
    reason = ""

    ids = [c.get("id") for c in crs if c.get("id")]
    if len(ids) < 2:
        return {
            "topology": "independent",
            "fix_order": ids,
            "groups": [[i] for i in ids],
            "workspaces": {c.get("id"): _norm_ws(c) for c in crs},
            "reason": "fewer than 2 CRs — trivially independent",
            "fallback_used": False,
        }

    ws_by_id = {c["id"]: _norm_ws(c) for c in crs if c.get("id")}
    missing = [i for i, w in ws_by_id.items() if not w]
    if missing:
        # cannot resolve workspace for some CR → fail-safe to stacked
        fallback_used = True
        reason = f"workspace unresolved for {missing} → fail-safe STACKED"
        order = ship_order if ship_order else ids
        return {
            "topology": "stacked",
            "fix_order": order,
            "groups": [order],  # one serial group
            "workspaces": ws_by_id,
            "reason": reason,
            "fallback_used": fallback_used,
        }

    # group CRs by workspace key
    groups_by_ws: dict = {}
    for cid, w in ws_by_id.items():
        groups_by_ws.setdefault(w, []).append(cid)

    any_shared = any(len(v) > 1 for v in groups_by_ws.values())

    if any_shared:
        # stacked: at least one workspace hosts multiple CRs.
        # fix_order = ship_order (upstream-first); a fix upstream rebases downstream.
        order = ship_order if ship_order else ids
        # validate ship_order covers all ids
        if set(order) != set(ids):
            fallback_used = True
            reason = ("ship_order does not cover all CR ids → fail-safe STACKED "
                      f"(ship_order={order}, ids={ids})")
            order = ids
        else:
            reason = "shared package/workspace across CRs → stacked commit chain"
        return {
            "topology": "stacked",
            "fix_order": order,
            "groups": [order],  # single serial group, ship_order sequence
            "workspaces": ws_by_id,
            "reason": reason,
            "fallback_used": fallback_used,
        }

    # independent: every CR in its own workspace → parallel groups (each singleton)
    order = ship_order if ship_order and set(ship_order) == set(ids) else ids
    return {
        "topology": "independent",
        "fix_order": order,
        "groups": [[i] for i in order],  # each its own group → parallelizable
        "workspaces": ws_by_id,
        "reason": "all CRs in distinct packages/workspaces → independent",
        "fallback_used": fallback_used,
    }


def from_plan_doc(path: str) -> dict:
    try:
        fm = _read_frontmatter(path)
    except Exception as exc:  # unreadable frontmatter → fail-safe stacked, empty
        return {
            "topology": "stacked",
            "fix_order": [],
            "groups": [],
            "workspaces": {},
            "reason": f"frontmatter parse failed ({exc}) → fail-safe STACKED",
            "fallback_used": True,
        }
    if fm.get("type") != "composite":
        return {
            "topology": "n/a",
            "fix_order": [],
            "groups": [],
            "workspaces": {},
            "reason": f"plan type is {fm.get('type')!r}, not composite",
            "fallback_used": False,
        }
    crs = fm.get("crs") or []
    ship_order = fm.get("ship_order") or []
    return classify(crs, ship_order)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--plan-doc", required=True, help="path to composite plan doc")
    ap.add_argument("--format", choices=["json", "md"], default="json")
    args = ap.parse_args()

    result = from_plan_doc(args.plan_doc)

    if args.format == "md":
        print(f"**Topology**: {result['topology']}"
              + (" ⚠️ (fail-safe)" if result.get("fallback_used") else ""))
        print(f"**Fix order**: {' → '.join(result['fix_order']) or '(none)'}")
        print(f"**Reason**: {result['reason']}")
    else:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    # exit 0 always — topology is advisory data, not a gate
    return 0


if __name__ == "__main__":
    sys.exit(main())
