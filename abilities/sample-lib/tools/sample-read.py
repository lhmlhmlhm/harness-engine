#!/usr/bin/env python3
"""sample-read.py — read one item out of the sample directory and print JSON.

PURE COMPUTE, no side effects: it reads, it prints, it exits. That is what makes it safe for a fact
provider to call while a criterion is being evaluated — a criterion may be evaluated more than once,
and a tool that recorded or mutated anything would make the second evaluation disagree with the first.

WHY A SEPARATE TOOL AT ALL, when the provider could read the directory itself. Because the engine's
own contract has a seam here worth demonstrating: the AGENT runs tools that cause effects, and the
ENGINE runs tools that only observe. A provider shelling out to an observing tool is the shape a real
ability uses (its analysis lives in something testable on its own), and the provider declares BOTH
this file and the directory it reads as capabilities — so `validate` can say which half is missing
before a run starts rather than mid-step.

Usage:
  sample-read.py --dir <sample-dir> --item <name>

Output (stdout, always JSON):
  {"found": bool, "dir": "<status folder>", "status": "<status>",
   "failed": int, "failed_ids": ["<assertion id>", ...]}

WHY BOTH A COUNT AND THE IDS. A count tells you something is wrong; the ids tell you WHICH thing, and
only the second is actionable. A snapshot that carries "3 failed" sends the reader back to the raw
data to find out what 3 things — which is the work the snapshot existed to save.

Exit: 0 = looked (found or not) | 2 = could not look
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dir", required=True)
    ap.add_argument("--item", required=True)
    a = ap.parse_args(argv)

    base = Path(a.dir).expanduser()
    if not base.is_dir():
        # exit 2, and NOT an empty result: "there is no such directory" and "the item is not in it"
        # are different answers, and a caller that cannot tell them apart will read the first as the
        # second — which is how a missing store becomes a clean bill of health.
        print(json.dumps({"error": f"no such directory: {base}"}), file=sys.stderr)
        return 2
    name = Path(a.item).name
    # Status folders are DERIVED from the directory. A hard-coded list goes stale the first time one
    # is added, and the item in the folder nobody listed comes back "not found" while sitting on disk.
    for d in sorted(x for x in base.iterdir() if x.is_dir()):
        hit = d / f"{name}.json"
        if not hit.is_file():
            continue
        try:
            payload = json.loads(hit.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        try:
            failed = int(payload.get("failed") or 0)
        except (TypeError, ValueError):
            failed = 0
        ids = payload.get("failed_ids")
        # Names only, and de-duplicated in declared order: an id list that carries whole records
        # would make the snapshot as big as the data it summarises.
        seen, names = set(), []
        for x in (ids if isinstance(ids, list) else []):
            k = str(x).strip()
            if k and k not in seen:
                seen.add(k); names.append(k)
        print(json.dumps({"found": True, "dir": d.name,
                          "status": str(payload.get("status") or ""),
                          "failed": failed, "failed_ids": names}))
        return 0
    print(json.dumps({"found": False, "dir": "", "status": "", "failed": 0, "failed_ids": []}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
