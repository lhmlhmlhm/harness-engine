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

from . import registry

import inspect
import os
import re
import shutil
import socket
from pathlib import Path
from typing import Callable

from . import operators

_PROVIDERS: dict[str, dict] = {}
# Who claimed each name, so a collision can name BOTH sides rather than only the loser.
_OWNERS: dict[str, str] = {}


class FactsError(ValueError):
    pass


class FactUnavailable(FactsError):
    """A condition or criterion touched a fact whose provider could not run.

    Subclasses `FactsError` so every caller that already treats an unusable provider as
    loud — hook firing, variant derivation — treats an unusable CAPABILITY the same way,
    without each of them learning a second failure shape.
    """


# ----------------------------------------------------------------- capabilities
#
# WHY THE ENGINE PROBES INSTEAD OF THE PROVIDER CHECKING.
#
# A provider that reaches outside the process needs something to be there: a tool file, an
# executable, a credential in the environment, a reachable host. Before this existed, each
# provider hand-wrote its own "if the tool is missing, return zeros" branch — and the zeros
# it returned were indistinguishable, in the substantive fact, from a real all-clear. The
# only thing separating them was a companion boolean the author had to remember to declare
# AND a hook the flow had to remember to write for it. Two halves of one convention, held
# together by nothing. Measured before the change: ten hand-rolled branches across two
# abilities, four companion booleans, four hooks — and no check that any pair existed.
#
# That convention does not scale in the direction this is going. Wiring providers that read
# an internal network multiplies it by however many providers there are, and each omission
# produces the one failure this whole design exists to prevent: "could not check" reading
# downstream as "checked, and it was clean".
#
# So a provider DECLARES what it needs and the engine decides what absence means. Absence
# yields facts marked unavailable rather than zeroed, and touching one is refused rather
# than evaluated — the same asymmetry a completion criterion already applies to a claim it
# cannot corroborate.
#
# The engine understands four descriptor kinds and nothing about what any argument means:
#
#   {"file": "tools/x.py"}     a path exists (relative to the registering module's dir)
#   {"cmd": "some-tool"}       an executable is on PATH
#   {"env": "SOME_TOKEN"}      an environment variable is set and non-empty
#   {"net": "host[:port]"}     a TCP connection can be opened (port defaults to 443)
#
# `net` is the reason the whole mechanism is worth having, and also the reason it must be
# declared rather than discovered: a flow that needs a network can then be REPORTED as
# unrunnable here, instead of failing later as if the flow itself were wrong.

CAP_KINDS = ("file", "cmd", "env", "net")
NET_TIMEOUT = 2.0

_PROBE_CACHE: dict = {}


_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def expand_env(spec: str) -> str:
    """Expand `${VAR}` / `${VAR:-default}` in a capability argument, AT CALL TIME.

    THE POINT IS ONE RESOLVER. A provider whose data lives at an overridable location used to
    declare the DEFAULT in `requires` and read the OVERRIDE at runtime, so the probe and the read
    asked about different paths. On a machine where the override pointed elsewhere, `validate`
    reported the capability present because the default happened to exist — and where neither
    existed it still reported present if the default did. The failure that mattered was the first
    one: a green check for a path nothing would read.

    Shell notation rather than a second key, because `requires` entries are single-key on purpose
    ("one capability per descriptor keeps a refusal able to name exactly which one is missing"),
    and because a reader already knows what `${VAR:-default}` means without being taught.

    An unset variable with no default expands to empty, which resolves to a path that does not
    exist — reported absent, which is the honest answer.
    """
    return _ENV_REF.sub(lambda m: os.environ.get(m.group(1)) or (m.group(2) or ""), spec)


def _probe(kind: str, arg: str, base: Path) -> bool:
    """Answer one capability question, cached for the life of the process.

    Cached because a CLI invocation is short-lived and several providers routinely name the
    same host or tool; probing per provider per gather would multiply a network timeout by
    the number of providers. Short-lived also means the cache cannot go stale in a way that
    matters — a capability that appears mid-command is not a case worth serving.
    """
    # Keyed on the EXPANDED argument, not the spec. Keyed on the spec, `${VAR:-default}` would
    # answer for whatever VAR held the first time it was asked — and since the whole point of the
    # expansion is that VAR can differ between callers, that cache would serve a stale answer
    # precisely when the override mattered. Found by the suite: which tests failed moved with
    # execution order, because a test that set the variable poisoned the entry for the next one.
    # EVERY KIND IS EXPANDED, not just `file`. The asymmetry was arbitrary and it had a consequence:
    # a `net` capability could only ever name the host its author typed, so a provider that declared
    # one was undrivable in a test — the probe refused before the provider ran, and no amount of
    # arranging a local server could change that. The engine then shipped a capability kind nothing
    # could exercise. Same reasoning for `cmd` and `env`: the reason `file` needed it (a caller must
    # be able to point it somewhere else) does not depend on what is being pointed at.
    expanded = expand_env(arg)
    key = (kind, expanded, str(base))
    if key in _PROBE_CACHE:
        return _PROBE_CACHE[key]
    ok = False
    if kind == "file":
        pth = Path(expanded).expanduser()
        ok = (pth if pth.is_absolute() else base / pth).exists()
    elif kind == "cmd":
        ok = shutil.which(expanded) is not None
    elif kind == "env":
        ok = bool(os.environ.get(expanded, "").strip())
    elif kind == "net":
        host, _, port = expanded.partition(":")
        try:
            with socket.create_connection((host, int(port or 443)), NET_TIMEOUT):
                ok = True
        except OSError:
            ok = False
    _PROBE_CACHE[key] = ok
    return ok


class Unavailable:
    """Placeholder for a fact whose provider could not run. Never a value.

    Carries the reason so a refusal can name the missing capability instead of reporting a
    generic failure — "needs cmd 'kinit'" is actionable, "facts unavailable" is not.
    """

    __slots__ = ("provider", "missing")

    def __init__(self, provider_name: str, missing: list):
        self.provider = provider_name
        self.missing = list(missing)

    def why(self) -> str:
        parts = ", ".join(f"{k} {a!r}" for k, a in self.missing)
        return f"provider '{self.provider}' needs {parts}, which is absent here"

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return f"<unavailable: {self.why()}>"


def capabilities(name: str) -> tuple:
    return tuple(_PROVIDERS[name]["requires"])


def file_present(arg: str, base: Path) -> bool:
    """Is this file there — asked exactly the way a `requires={"file": ...}` capability asks it.

    Public so a step's `produced_by` pointer resolves by the SAME rule as a provider's declared
    file: `~` expanded, absolute taken as given, relative read against the ability's own directory.
    A second resolver would eventually disagree with this one, and the disagreement would surface as
    "your tool is missing" on a machine where it is not.
    """
    return _probe("file", str(arg), base)


def probe_capabilities(name: str) -> list:
    """Which of a provider's declared capabilities are ABSENT. Empty means all present."""
    entry = _PROVIDERS[name]
    out = []
    for desc in entry["requires"]:
        kind, arg = next(iter(desc.items()))
        if not _probe(kind, str(arg), entry["base"]):
            # REPORTED AS EXPANDED, not as written. The spec may be `${VAR:-default}`, and a message
            # naming the variable tells the reader about a knob when what they need is the value it
            # resolved to — the host that did not answer, the path that was not there. Measured on
            # the shipped sample: "needs net '${HARNESS_SAMPLE_ENDPOINT_HOST:-sample.invalid:443}'"
            # sends somebody to look up an environment variable to find out what was probed.
            out.append((kind, expand_env(str(arg))))
    return out


def provider(name: str, *, schema: dict, requires: tuple = ()) -> Callable:
    """Register a fact provider, the schema it promises, and what it needs to run.

    `schema` maps fact name -> one of `operators.TYPES`.
    `requires` is a sequence of one-key capability descriptors (see CAP_KINDS).
    """
    for key, typ in schema.items():
        if typ not in operators.TYPES:
            raise RuntimeError(
                f"provider '{name}' declares fact '{key}' as type {typ!r}; "
                f"legal types are {', '.join(operators.TYPES)}"
            )
    reqs = []
    for desc in requires:
        if not isinstance(desc, dict) or len(desc) != 1:
            raise RuntimeError(
                f"provider '{name}': each entry of `requires` is a single-key mapping, "
                f"got {desc!r}. One capability per descriptor keeps a refusal able to name "
                f"exactly which one is missing."
            )
        kind = next(iter(desc))
        if kind not in CAP_KINDS:
            raise RuntimeError(
                f"provider '{name}' requires capability kind {kind!r}; the engine knows "
                f"{', '.join(CAP_KINDS)}. A new kind is an engine change on purpose — the "
                f"engine must be able to PROBE it, which means understanding it."
            )
        reqs.append({kind: str(desc[kind])})

    def deco(fn: Callable) -> Callable:
        key = registry.claim("fact provider", name, _PROVIDERS, _OWNERS, fn)
        # A relative `file` resolves against the directory that REGISTERED the provider, so
        # an ability names its own tools the way it stores them and stays movable.
        try:
            base = Path(inspect.getfile(fn)).resolve().parent
        except TypeError:  # pragma: no cover - builtins cannot register providers
            base = Path.cwd()
        _PROVIDERS[key] = {"fn": fn, "schema": dict(schema),
                           "requires": tuple(reqs), "base": base}
        return fn
    return deco


def is_registered(name: str) -> bool:
    return name in _PROVIDERS


def resolve(ref: str, *, asking: str | None, requires: tuple[str, ...] = ()) -> str:
    """A spec's provider reference -> a registry key. Raises registry.ResolveError."""
    return registry.resolve("fact provider", ref, _PROVIDERS,
                            asking=asking, requires=requires)


def visible(owner: str | None) -> list[str]:
    return registry.visible(_PROVIDERS, owner)


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
    # CAPABILITIES ARE CHECKED BEFORE THE CALL, and absence does not raise here — it yields
    # facts marked unavailable, so the refusal happens at the point of USE.
    #
    # WHAT THAT BUYS, stated precisely because the first version of this comment got it wrong.
    # It does NOT provide run isolation: "an absent capability only stops the steps that need
    # it" is already delivered by the caller narrowing each gather to the providers owning the
    # facts it is about to read, and mutating this return to a raise leaves that property
    # intact (verified — the mutation stays green, alone and together with removing the
    # narrowing). What it buys is ATTRIBUTION: a marker travels to the condition or criterion
    # that reads it, so the refusal names the fact and the missing capability instead of
    # reporting a generic provider failure from one frame up. "needs cmd 'kinit'" is
    # actionable; "facts unavailable" sends someone looking in the flow for an environment
    # problem. Absent capability is a fact about the environment, not a verdict about the work,
    # and the message has to be able to say so.
    missing = probe_capabilities(name)
    if missing:
        marker = Unavailable(name, missing)
        return {k: marker for k in entry["schema"]}
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


# NO BUILT-IN PROVIDER MAY SHELL OUT, AND THAT IS ENFORCED.
#
# One used to: a provider here invoked a specific version-control tool, because "what did I
# touch" is the most common thing a condition asks. It was a convenience and it was domain
# knowledge in the base, and it bypassed the very seam the capability layer provides — a
# provider that needs a tool declares `requires={"cmd": ...}` so that "I could not look" stops
# looking like "nothing found". A built-in cannot make that declaration meaningfully, because
# the engine would then depend on a tool for one of its own facts.
#
# It now lives with the one flow that used it. The built-ins that remain report only what the
# engine ALREADY holds: nothing, the run's own identity, and the run's own progress.

@provider("run_progress", schema={
    # ── the original four. Their meanings are FROZEN: three installed flows and a dozen tests
    # read them, so a redefinition here would move criteria without touching a single spec.
    # `closed_steps` includes SKIPPED steps (see `store.closed_steps`) — left as it is, with
    # `steps_skipped` below making the split answerable instead.
    "closed_steps": operators.T_LIST,
    "evidence_kinds": operators.T_LIST,
    "gate_count": operators.T_INT,
    "violation_count": operators.T_INT,
    # ── measures the ledger already supported and nothing exposed.
    #
    # Attempts that were TURNED DOWN. The engine writes a `refused` step-log row at three sites,
    # and until now that record could not be read by any criterion — so a run that reached the end
    # after twenty refusals looked identical to one that walked straight through. Friction is a
    # measurement, and it was being discarded.
    "steps_refused": operators.T_INT,
    # Skipped, separately from closed — see the note on `closed_steps`.
    "steps_skipped": operators.T_INT,
    # The severity split, which exists precisely so these two are not summed. `blocked` means an
    # attempt was REFUSED (the guarantee worked, this row is an audit trace); `breach` means
    # something passed WITH A MARK (the guarantee did not). `violation_count` above cannot tell
    # them apart, which makes it the wrong number to put a bar on.
    "violations_blocked": operators.T_INT,
    "violations_breach": operators.T_INT,
    # Debt still outstanding at this moment.
    "obligations_open": operators.T_INT,
    "phases_summarized": operators.T_INT,
    # WHY BOTH. `run_duration_s` is 0 for a run that has not closed, and 0 also means "took under a
    # second". Those must not read the same — it is the 0-of-0 hazard this engine names elsewhere:
    # a vacuous answer that looks like a measured one. `run_closed` is what makes the duration
    # interpretable, so it is a fact rather than something a caller has to know to ask about.
    "run_closed": operators.T_BOOL,
    "run_duration_s": operators.T_INT,
})
def _run_progress(ctx: dict) -> dict:
    """Facts about the run's OWN progress — no filesystem, no external world.

    The only kind of built-in left, and the shape the others share: it reports what the engine
    already holds. Worth having as a built-in precisely because it needs nothing — an ability
    built on this alone exercises the whole condition machinery without touching anything
    outside the store, so a hidden assumption that facts come from files would show up
    immediately. One such ability exists in the test set for that reason.
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

        def one(sql: str, *args) -> int:
            return int(conn.execute(sql, (run_id, *args)).fetchone()["n"])

        refused = one("SELECT COUNT(*) n FROM step_log WHERE run_id = ? AND event = 'refused'")
        skipped = one("SELECT COUNT(DISTINCT step_id) n FROM step_log "
                      "WHERE run_id = ? AND event = 'skipped'")
        # Counted by severity rather than filtered in Python, so the two numbers cannot drift
        # apart from `violation_count` above by rounding through a different code path.
        blocked = one("SELECT COUNT(*) n FROM violation WHERE run_id = ? AND severity = ?",
                      "blocked")
        breach = one("SELECT COUNT(*) n FROM violation WHERE run_id = ? AND severity = ?",
                     "breach")
        owed = one("SELECT COUNT(*) n FROM obligation "
                   "WHERE run_id = ? AND discharged_at IS NULL")
        phases = one("SELECT COUNT(*) n FROM phase_summary WHERE run_id = ?")

        # Duration only for a CLOSED run. An unfinished thing has no duration yet, and reporting
        # "elapsed so far" would make the fact change between two reads of the same run — which
        # would then reach criteria as a moving target. See the schema note above.
        row = store.get_run(conn, run_id)
        run_closed, duration = False, 0
        if row is not None and row["closed_at"]:
            run_closed = True
            try:
                from datetime import datetime
                a = datetime.fromisoformat(str(row["opened_at"]))
                b = datetime.fromisoformat(str(row["closed_at"]))
                duration = max(0, int((b - a).total_seconds()))
            except (ValueError, TypeError):
                # A timestamp this engine cannot parse is not a reason to fail a whole gather.
                # `run_closed` stays true, which is the fact that was actually established.
                duration = 0

        return {"closed_steps": closed, "evidence_kinds": kinds,
                "gate_count": int(gates), "violation_count": int(viol),
                "steps_refused": refused, "steps_skipped": skipped,
                "violations_blocked": blocked, "violations_breach": breach,
                "obligations_open": owed, "phases_summarized": phases,
                "run_closed": run_closed, "run_duration_s": duration}
    finally:
        conn.close()
