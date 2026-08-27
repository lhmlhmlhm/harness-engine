#!/usr/bin/env python3
"""kiro-cli preToolUse adapter — the only integration artifact, shipped once.

WHAT THIS IS FOR

`harness guard-tool` answers "may this tool call proceed?" and says so with exit 4.
kiro-cli's preToolUse hook asks the same question but speaks a different dialect: it
passes a JSON event on stdin and reads exit 2 as "block". This translates between the
two, and does nothing else.

Deliberately thin, and deliberately ABILITY-AGNOSTIC. The tempting alternative is one
adapter per ability, each holding its own "which command counts as a commit" regexes —
and that mapping then exists in as many copies as there are abilities, drifting
independently. Here the mapping lives in each ability's `guards.<action>.matches`, so
this file never learns what any tool call means.

CONTRACT (both sides)

  in   : JSON on stdin, at least {"tool_name": str, "tool_input": {...}}
  out  : exit 0 = allow, exit 2 = BLOCK (stderr is shown to the model)
  env  : HARNESS_ENGINE  — the engine checkout (default: alongside this file)
         HARNESS_STATE_DIR — MUST match the value the agent's runs use

FAIL-OPEN IS THE IRON RULE

This runs before EVERY matching tool call in EVERY session of the agent it is wired
into. Any uncertainty — unparseable event, missing engine, crashed subprocess, timeout —
allows. A bug here must never brick normal work. Only a positive, unambiguous exit 4
from the engine becomes a block.

That rule has a cost worth stating plainly rather than hiding: because a missing or
mis-pointed state directory looks exactly like "no open run", a wrong HARNESS_STATE_DIR
silently allows everything. So that one condition is checked explicitly and reported —
it is the difference between "nothing to guard" and "I cannot see what to guard", and
those must not look alike.

NO SELF-EXCLUSION, ON PURPOSE

An earlier sketch skipped the check when the command looked like it was invoking the
harness itself, to save a subprocess. That is a bypass vector: anything an agent can put
in a command to be skipped, an agent can put in a command. The patterns already do not
match harness invocations, so the exclusion bought nothing and cost a hole.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ALLOW = 0
BLOCK = 2
ENGINE_BLOCK = 4          # the engine's own vocabulary; see harness guard-tool


def _engine_root() -> Path:
    env = os.environ.get("HARNESS_ENGINE")
    if env:
        return Path(env).expanduser()
    return Path(__file__).resolve().parents[1]


def main() -> int:
    raw = sys.stdin.read()
    try:
        event = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        return ALLOW
    if not isinstance(event, dict):
        return ALLOW

    tool = str(event.get("tool_name") or "").strip()
    if not tool:
        return ALLOW
    tool_input = event.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = {"value": tool_input}

    root = _engine_root()
    harness = root / "bin" / "harness"
    if not harness.exists():
        # Not "nothing to guard" — "cannot guard". Say so once; do not block.
        print(f"⚠️  harness engine not found at {root} — guard NOT enforced. "
              f"Set HARNESS_ENGINE.", file=sys.stderr)
        return ALLOW

    # A state dir that does not exist is indistinguishable downstream from "no open run",
    # which is exactly the silence this whole project is against. Report it.
    state = os.environ.get("HARNESS_STATE_DIR")
    if state and not (Path(state).expanduser() / "harness.db").exists():
        print(f"⚠️  HARNESS_STATE_DIR={state} has no harness.db — guard NOT enforced. "
              f"If the agent's runs live elsewhere, the hook is looking in the wrong place.",
              file=sys.stderr)
        return ALLOW

    try:
        proc = subprocess.run(
            [sys.executable, str(harness), "guard-tool",
             "--tool", tool,
             "--input-json", json.dumps(tool_input, ensure_ascii=False),
             "--cwd", os.getcwd()],
            capture_output=True, text=True, timeout=6,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        print(f"⚠️  guard-tool did not run ({type(exc).__name__}) — allowing", file=sys.stderr)
        return ALLOW

    if proc.returncode == ENGINE_BLOCK:
        sys.stderr.write(proc.stderr or "⛔ BLOCKED by a harness guard.\n")
        return BLOCK
    # Warnings (ambiguous ownership, broken spec) are worth surfacing even when allowing:
    # an unenforced guard the operator cannot see is the same failure in a quieter form.
    if proc.stderr.strip():
        sys.stderr.write(proc.stderr)
    return ALLOW


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:            # noqa — the iron rule outranks tidiness
        print(f"⚠️  guard adapter crashed ({exc}) — allowing", file=sys.stderr)
        sys.exit(ALLOW)
