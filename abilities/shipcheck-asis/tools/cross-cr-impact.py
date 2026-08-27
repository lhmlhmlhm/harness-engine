#!/usr/bin/env python3
"""cross-cr-impact.py — after a fix lands on one CR, flag whether the changed symbols
also appear in OTHER CRs' workspaces (candidate cross-CR impact).

Deterministic + advisory ONLY: it reports candidate hits for a human/agent to judge; it
never auto-propagates a change (auto-propagation would risk spreading one mis-fix across
N CRs — see minimal-change principle). Exit 0 always.

`extract_symbols(files)` is pure and unit-tested (via the classify test suite / manual).

Usage:
  cross-cr-impact.py --fixed-cr CR-1 \
     --changed-file a.py --changed-file b.py \
     --other-workspace /ws/CR2 --other-workspace /ws/CR3
"""
import argparse
import json
import os
import re
import subprocess
import sys

# identifiers worth tracking across CRs (module-level defs / classes / consts / funcs)
SYMBOL_RE = re.compile(
    r"(?:^|\s)(?:def|class|function)\s+([A-Za-z_][A-Za-z0-9_]{3,})"
    r"|(?:^|\s)(?:const|let|var)\s+([A-Z_][A-Za-z0-9_]{3,})"
    r"|^([A-Z][A-Z0-9_]{3,})\s*[:=]",  # module-level CONSTANT = / :
    re.MULTILINE,
)


def extract_symbols(files: list) -> set:
    symbols = set()
    for f in files:
        p = os.path.expanduser(f)
        if not os.path.isfile(p):
            continue
        try:
            text = open(p, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        for m in SYMBOL_RE.finditer(text):
            name = next((g for g in m.groups() if g), None)
            if name:
                symbols.add(name)
    return symbols


def grep_symbol(symbol: str, workspaces: list) -> list:
    hits = []
    for ws in workspaces:
        wsp = os.path.expanduser(ws)
        if not os.path.exists(wsp):
            continue
        try:
            out = subprocess.run(
                ["grep", "-rnw", "--include=*.py", "--include=*.ts", "--include=*.js",
                 "--include=*.md", symbol, wsp],
                capture_output=True, text=True, timeout=30,
            ).stdout
        except (subprocess.SubprocessError, OSError):
            continue
        for line in out.splitlines()[:20]:  # cap per symbol/ws
            parts = line.split(":", 2)
            if len(parts) == 3:
                hits.append({"symbol": symbol, "file": parts[0],
                             "line": parts[1], "text": parts[2].strip()[:160]})
    return hits


def analyze(fixed_cr: str, changed_files: list, other_workspaces: list) -> dict:
    symbols = extract_symbols(changed_files)
    all_hits = []
    for s in sorted(symbols):
        all_hits.extend(grep_symbol(s, other_workspaces))
    impacted = sorted({h["symbol"] for h in all_hits})
    return {
        "fixed_cr": fixed_cr,
        "symbols_from_fix": sorted(symbols),
        "impacted_symbols": impacted,
        "hits": all_hits,
        "flagged": bool(all_hits),
        "note": ("advisory only — review whether these other-CR usages need the same fix; "
                 "do NOT auto-propagate"),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fixed-cr", required=True)
    ap.add_argument("--changed-file", action="append", default=[], dest="changed_files")
    ap.add_argument("--other-workspace", action="append", default=[], dest="other_workspaces")
    ap.add_argument("--format", choices=["json", "md"], default="json")
    args = ap.parse_args()

    result = analyze(args.fixed_cr, args.changed_files, args.other_workspaces)

    if args.format == "md":
        if result["flagged"]:
            print(f"⚠️ Cross-CR impact candidates from {result['fixed_cr']} fix "
                  f"({len(result['hits'])} hit(s)) — REVIEW, do not auto-propagate:")
            for h in result["hits"]:
                print(f"  - {h['symbol']}  {h['file']}:{h['line']}")
        else:
            print(f"✅ No cross-CR symbol overlap from {result['fixed_cr']} fix")
    else:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0  # advisory — never a gate


if __name__ == "__main__":
    sys.exit(main())
