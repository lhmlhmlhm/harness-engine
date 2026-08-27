"""Ability-supplied fact providers for `push`.

WHY THIS ABILITY EXISTS, AND WHY IT LOOKS DIFFERENT FROM THE OTHER TRANSCRIPTION.

The engine's first transcribed flow had ONE shape: 101 steps, every run walking the same
skeleton. This one has TWO, selected per run from a registry, and that difference is the
point — an execution mode is resolved once and then decides which steps exist at all.

The source system names this architecture explicitly and states its own promise:

    加新 executor = 1 条 registry + 每 skill 1 个 branch + 1 个 steering，不改核心

That promise is exactly what the `variants` primitive is meant to make testable: adding a
third mode should be a registry line plus mode-specific steps, with the shared core
untouched. If it isn't, the primitive is wrong.

THE DERIVATION IS DOMAIN KNOWLEDGE, WHICH IS WHY IT IS HERE AND NOT IN THE ENGINE.
"Which mode does this work belong to" is answered by matching a workspace path against a
registry of rules — path shapes, a default fallback, and an explicit override that wins over
both. The engine must not learn any of that: it asks a provider for a declared fact and
compares it against the variants the spec declares. It never learns what a workspace is.

The tool under `tools/` is copied BYTE-FOR-BYTE, including its directory layout, because its
config path resolution is relative to its own location. Adjusting the script to fit a nicer
layout would fork it; matching the layout keeps it a black box that can be re-copied when it
changes upstream.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from engine import facts  # noqa: E402

_TOOLS = Path(__file__).resolve().parent / "tools" / "scripts"
_TAXONOMY = _TOOLS / "taxonomy.py"


def _run_json(script: Path, *args: str, timeout: int = 60) -> dict:
    """Call a copied tool and parse its JSON. Any failure is LOUD.

    A provider that swallowed a failure and returned nothing would turn every condition false
    and mute every hook — and the run would look clean while the derivation never happened.
    """
    if not script.exists():
        raise facts.FactsError(f"tool not found: {script}")
    proc = subprocess.run(
        [sys.executable, str(script), *args],
        capture_output=True, text=True, timeout=timeout,
    )
    out = (proc.stdout or "").strip()
    if proc.returncode != 0 or not out:
        raise facts.FactsError(
            f"{script.name} {' '.join(args)} exited {proc.returncode}: "
            f"{(proc.stderr or out or '(no output)').strip()[:300]}"
        )
    try:
        return json.loads(out)
    except json.JSONDecodeError as exc:
        raise facts.FactsError(f"{script.name} did not return JSON: {exc}; got {out[:200]!r}")


@facts.provider("plan_taxonomy", schema={
    # The variant selector. Declared as a plain string so the engine can compare it against
    # the variants the spec declares — it has no idea what the values mean.
    "executor": "str",
    "executor_reason": "str",
    "executor_marker": "str",
    # Classification of the work, derived from the same registry.
    "scope": "str",
    "tags": "list[str]",
    "unknown_paths": "list[str]",
})
def _plan_taxonomy(ctx: dict) -> dict:
    """Derive the execution mode and classification for the plan this run is about.

    `ctx["scope"]` is the run's scope key — for this ability, the workspace the plan targets.
    Deriving from the workspace rather than asking is deliberate: the answer must be the same
    one every consumer gets, and a value typed in per run drifts from the registry silently.
    """
    workspace = str(ctx.get("scope") or "")
    ex = _run_json(_TAXONOMY, "executor", "--workspace", workspace, "--format", "json")
    sc = _run_json(_TAXONOMY, "scope", "--workspace", workspace, "--format", "json")
    tg = _run_json(_TAXONOMY, "tags", "--workspace", workspace, "--format", "json")
    return {
        "executor": str(ex.get("executor") or ""),
        "executor_reason": str(ex.get("reason") or ""),
        "executor_marker": str(ex.get("marker") or ""),
        "scope": str(sc.get("scope") or ""),
        "tags": [str(t) for t in (tg.get("tags") or [])],
        "unknown_paths": [str(u) for u in (tg.get("unknown") or [])],
    }
