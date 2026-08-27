"""Facts — the third registry: where runtime truth comes from.

A condition needs to know something about the world (what changed, which branch, how many
open questions). The engine must NOT compute that: deriving the answer to "what is this
change about" requires knowing how a particular project lays out its checkouts and where it
records the subject of a change — domain knowledge that, once inside an engine, cannot be
got back out.

So a provider supplies an OPAQUE dict and DECLARES its schema. The engine only:
  1. checks that every fact a condition references is in the schema  (load time)
  2. checks that every operator is legal for that fact's type        (load time)
  3. calls the provider and evaluates                               (run time)

THE SCHEMA IS WHAT MAKES CONDITIONS SAFE. Without it, `changed_file` (a typo for
`changed_files`) would evaluate to nothing and the condition would come out false — the
hook silently never fires, which is strictly worse than the hook not existing. With it,
that typo is exit 2 plus a did-you-mean.

A REAL LESSON FROM A SYSTEM THAT DOES THIS — THE RAW FACT IS NOT THE FACT YOU WANT.
Its condition asked a question of the form "what is this change ABOUT", and the obvious
answer looked like "the directory I am running in". Then an isolation mechanism started
relocating those directories, so the obvious answer became a lie: the fact still resolved
to something, just not the right something, and a sibling's hooks were silently muted as a
result. The fix needed a derivation step AND a second source consulted to recover the full
SET of subjects one change spans.

Two things follow, and both are load-bearing here:

  * derivation is domain knowledge, so it lives in a provider — an engine that learned it
    could never unlearn it;
  * a fact is plural more often than it first appears, which is why the type vocabulary
    includes a list and the built-in matchers all accept one.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable

from . import operators

_PROVIDERS: dict[str, dict] = {}


class FactsError(ValueError):
    pass


def provider(name: str, *, schema: dict) -> Callable:
    """Register a fact provider and the schema it promises.

    `schema` maps fact name -> one of `operators.TYPES`.
    """
    for key, typ in schema.items():
        if typ not in operators.TYPES:
            raise RuntimeError(
                f"provider '{name}' declares fact '{key}' as type {typ!r}; "
                f"legal types are {', '.join(operators.TYPES)}"
            )

    def deco(fn: Callable) -> Callable:
        if name in _PROVIDERS:
            raise RuntimeError(f"fact provider '{name}' already registered")
        _PROVIDERS[name] = {"fn": fn, "schema": dict(schema)}
        return fn
    return deco


def is_registered(name: str) -> bool:
    return name in _PROVIDERS


def registered() -> list[str]:
    return sorted(_PROVIDERS)


def schema_of(name: str) -> dict:
    return dict(_PROVIDERS[name]["schema"])


def gather(name: str, ctx: dict) -> dict:
    """Call a provider. Any failure is loud.

    A provider that fails must not be silently treated as "no facts": that would turn
    every condition false and mute every hook, which is the failure mode this whole design
    exists to prevent.
    """
    entry = _PROVIDERS.get(name)
    if entry is None:
        raise FactsError(
            f"unknown fact provider '{name}'; registered: {', '.join(registered()) or '(none)'}"
        )
    try:
        raw = entry["fn"](ctx)
    except Exception as exc:  # noqa — surface anything, never swallow
        raise FactsError(f"fact provider '{name}' failed: {exc}") from None
    if not isinstance(raw, dict):
        raise FactsError(
            f"fact provider '{name}' returned {type(raw).__name__}, expected a dict"
        )
    declared = entry["schema"]
    unknown = sorted(set(raw) - set(declared))
    if unknown:
        # A provider returning more than it declared means the schema is stale, and a
        # condition could then reference a key that validates today and vanishes tomorrow.
        raise FactsError(
            f"fact provider '{name}' returned undeclared fact(s): {', '.join(unknown)}\n"
            f"  declared: {', '.join(sorted(declared))}"
        )
    missing = sorted(set(declared) - set(raw))
    if missing:
        raise FactsError(
            f"fact provider '{name}' declared but did not return: {', '.join(missing)}"
        )
    return raw


def gather_all(names, ctx: dict) -> dict:
    """Gather from several providers and merge into one flat fact namespace.

    Flat, not namespaced, on purpose: a condition names a fact, not a provider, so which
    provider supplies it is wiring the flow should be free to change. That only stays safe
    because two providers declaring the SAME fact name is refused at load time — a silent
    last-writer-wins here would make a condition's meaning depend on list order.
    """
    out: dict = {}
    for n in names:
        out.update(gather(n, ctx))
    return out


# ------------------------------------------------------------------ built-in providers

@provider("static", schema={})
def _static(ctx: dict) -> dict:
    """No facts at all. For an ability whose hooks are unconditional."""
    return {}


@provider("scope_only", schema={"scope": operators.T_STR, "run_id": operators.T_STR})
def _scope_only(ctx: dict) -> dict:
    """The run's own identity — the only facts the engine itself already holds.

    Useful for a condition that keys off which scope a run owns without needing to
    inspect the world at all.
    """
    return {"scope": str(ctx.get("scope") or ""), "run_id": str(ctx.get("run_id") or "")}


@provider("git_tree", schema={
    "changed_files": operators.T_LIST,
    "vcs_branch": operators.T_STR,
    "dirty": operators.T_BOOL,
    "change_count": operators.T_INT,
})
def _git_tree(ctx: dict) -> dict:
    """Uncommitted paths in the run's scope, treated as a directory.

    Ships as a built-in because "what did I touch" is the most common thing a condition
    asks, but it is still just a provider — an ability that means something else by
    "changed" registers its own and the engine is none the wiser.

    Degrades to empty when the scope is not a repository. That is NOT the silent-false
    problem: absence of changes is a real, correct answer here, whereas a provider that
    ERRORS must raise (see `gather`).
    """
    root = Path(str(ctx.get("scope") or ".")).expanduser()
    if not root.is_dir():
        return {"changed_files": [], "vcs_branch": "", "dirty": False, "change_count": 0}

    def git(*args: str) -> str:
        try:
            r = subprocess.run(
                ["git", "-C", str(root), *args],
                capture_output=True, text=True, timeout=15,
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        return r.stdout if r.returncode == 0 else ""

    inside = git("rev-parse", "--is-inside-work-tree").strip()
    if inside != "true":
        return {"changed_files": [], "vcs_branch": "", "dirty": False, "change_count": 0}

    paths: list[str] = []
    for line in git("status", "--porcelain").splitlines():
        if len(line) > 3:
            p = line[3:].strip()
            if " -> " in p:      # a rename reports old -> new; the new path is the subject
                p = p.split(" -> ", 1)[1]
            paths.append(p.strip('"'))
    vcs_branch = git("rev-parse", "--abbrev-ref", "HEAD").strip()
    return {
        "changed_files": sorted(set(paths)),
        "vcs_branch": vcs_branch,
        "dirty": bool(paths),
        "change_count": len(set(paths)),
    }


@provider("run_progress", schema={
    "closed_steps": operators.T_LIST,
    "evidence_kinds": operators.T_LIST,
    "gate_count": operators.T_INT,
    "violation_count": operators.T_INT,
})
def _run_progress(ctx: dict) -> dict:
    """Facts about the run's OWN progress — no filesystem, no external world.

    Ships alongside `git_tree` on purpose: the two have nothing in common, and an ability
    built on this one exercises the condition machinery without touching a repository at
    all. If any part of the engine had quietly assumed facts are file-shaped, an ability
    using this provider would not work — which is why one exists in the test set.
    """
    from . import store
    conn = store.connect(read_only=True)
    try:
        run_id = str(ctx.get("run_id") or "")
        closed = sorted(store.closed_steps(conn, run_id))
        kinds = sorted({r["kind"] for r in conn.execute(
            "SELECT DISTINCT kind FROM evidence WHERE run_id = ?", (run_id,)).fetchall()})
        gates = conn.execute(
            "SELECT COUNT(*) n FROM gate WHERE run_id = ?", (run_id,)).fetchone()["n"]
        viol = conn.execute(
            "SELECT COUNT(*) n FROM violation WHERE run_id = ?", (run_id,)).fetchone()["n"]
        return {"closed_steps": closed, "evidence_kinds": kinds,
                "gate_count": int(gates), "violation_count": int(viol)}
    finally:
        conn.close()
