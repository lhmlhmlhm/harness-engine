#!/usr/bin/env python3
"""sample-move.py — move one item between status folders. THIS ONE CAUSES AN EFFECT.

WHY IT SHIPS BESIDE AN OBSERVING TOOL, AND WHY THE PAIR IS THE POINT. `sample-lib`'s
`tools/sample-read.py` only reads and prints; a fact provider calls it while a criterion is being
evaluated, and a criterion may be evaluated more than once. This tool is the other kind: it moves a
file, so calling it twice is not the same as calling it once.

That difference decides WHO RUNS IT, and the engine draws the line rather than leaving it to taste:

    observing tool    called by the ENGINE, through a fact provider. Declared as a `file`
                      capability so `validate` can say it is missing before a run starts.
    effecting tool    called by the AGENT, pointed at from the step that owes the effect via
                      `produced_by`. The engine never invokes it — it only records that the step
                      claimed the effect, and then checks the world through the observing one.

A flow that pointed `produced_by` at an observing tool, or that let a provider call this one, would
have crossed that line — and the symptom would be a criterion whose answer changes depending on how
many times it was checked.

Usage:
  sample-move.py --dir <sample-dir> --item <name> --to <status folder>

Output (stdout, always JSON):
  {"moved": bool, "from": "<folder or empty>", "to": "<folder>"}

Exit: 0 = moved, or already there | 2 = could not (no such directory, item not found)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dir", required=True, help="the sample directory holding the status folders")
    ap.add_argument("--item", required=True, help="the item's name, without .json")
    ap.add_argument("--to", required=True, help="the status folder to move it into")
    a = ap.parse_args(argv)

    base = Path(a.dir).expanduser()
    target = base / a.to
    if not base.is_dir():
        print(json.dumps({"error": f"no such directory: {base}"}), file=sys.stderr)
        return 2
    if not target.is_dir():
        # REFUSED rather than created. A folder this tool invented would be a status nothing else
        # knows about, and the reader that derives the status set from disk would then report an item
        # as sitting in a state the flow never declared.
        print(json.dumps({"error": f"not a declared status folder: {a.to}"}), file=sys.stderr)
        return 2

    name = f"{Path(a.item).name}.json"
    found = None
    for folder in sorted(d for d in base.iterdir() if d.is_dir()):
        if (folder / name).is_file():
            found = folder
            break
    if found is None:
        print(json.dumps({"error": f"no such item: {a.item}"}), file=sys.stderr)
        return 2
    if found == target:
        # ALREADY THERE IS SUCCESS, not a no-op to complain about: this tool is pointed at from a step
        # that may legitimately be walked twice, and a second call that failed would make retrying
        # look like a new problem.
        print(json.dumps({"moved": False, "from": found.name, "to": target.name}))
        return 0

    (found / name).replace(target / name)
    print(json.dumps({"moved": True, "from": found.name, "to": target.name}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
