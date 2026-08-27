"""plandoc.py — the SINGLE implementation of plan-doc frontmatter parsing.

Two parity gates read plan docs and must read them IDENTICALLY, or they produce phantom
divergences:

  - push/scripts/verify-taxonomy-parity.py         (taxonomy config vs frozen golden)
  - project-connector/scripts/verify-connector-parity.py  (project layer inert-ness)

Both previously carried their own byte-identical copies of `parse_frontmatter` and
`parse_packages`. When the `parse_packages` bug (below) was fixed, only the push copy got
the fix — the connector copy kept the buggy regex and kept feeding prose to derive_tags.
That is exactly the failure mode a shared module prevents, so the helpers live here and
nowhere else.

Placement rationale: `project-connector/scripts/verify-connector-parity.py` already does
`sys.path.insert(0, "~/.kiro/skills/push/scripts")` for `project_connector` / `taxonomy`,
so importing from here costs zero new wiring. Keep this module PURE STDLIB — anything else
would make it unimportable from the connector side.

Bug A (fixed, regression-locked by push/tests/test_parse_packages.py):
`parse_packages` used to `re.findall` a `path:`-anchored pattern over the WHOLE frontmatter.
With no word boundary it matched the tail of ANY `*_path:` key — notably
`known_uncertainties[].fallback_path:` and ad-hoc `verified_facts.*_path:` keys — and handed
that prose to derive_tags as if it were a package path. Structural block parsing fixes the
whole `*_path:` class rather than blocklisting one key.

(The old one-line pattern is deliberately NOT reproduced here: it is the exact string a future
reader could paste back in, and a repo-wide grep for it should stay at zero.)
"""
from __future__ import annotations

import re

__all__ = ["parse_frontmatter", "parse_packages"]


def parse_frontmatter(text: str) -> dict:
    """Flat scalar view of a plan doc's YAML frontmatter.

    Nested blocks are intentionally NOT parsed (callers only need top-level scalars like
    `slug` / `workspace` / `scope`); `packages[]` has its own reader below.
    """
    m = re.match(r"^---\n(.*?)\n---", text, re.DOTALL)
    if not m:
        return {}
    out: dict = {}
    for line in m.group(1).splitlines():
        stripped = line.split("#", 1)[0].rstrip()
        m2 = re.match(r"^([A-Za-z_][A-Za-z0-9_]*):\s*(.*)$", stripped)
        if not m2:
            continue
        key, val = m2.group(1), m2.group(2).strip()
        if val and val[0] in "\"'" and val[-1] == val[0]:
            val = val[1:-1]
        out[key] = val
    return out


def parse_packages(text: str) -> list[str]:
    """Extract `packages[].path` values from a plan doc's frontmatter.

    Only lines INSIDE the top-level `packages:` block are considered (the block ends at
    the first non-blank line that is not indented, i.e. the next top-level key), and only
    keys named EXACTLY `path` — everything before `path:` on the line must be whitespace
    or a YAML list dash. See the module docstring for the bug this replaced.
    """
    m = re.match(r"^---\n(.*?)\n---", text, re.DOTALL)
    if not m:
        return []
    out: list[str] = []
    in_block = False
    for line in m.group(1).splitlines():
        if re.match(r"^packages:", line):
            in_block = True
            continue
        if not in_block:
            continue
        # a non-blank, non-indented line ends the block (next top-level key or comment)
        if line.strip() and not line[:1].isspace():
            break
        m2 = re.match(r"^[ \t]*-?[ \t]*path:[ \t]*(.*)$", line)
        if not m2:
            continue
        val = m2.group(1).split("#", 1)[0].strip()
        if len(val) >= 2 and val[0] in "\"'" and val[-1] == val[0]:
            val = val[1:-1]
        if val:
            out.append(val)
    return out
