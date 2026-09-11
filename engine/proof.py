"""Proof — witnesses that an affirm gate really happened.

THE problem this solves: an agent recording its own gate is an agent marking its own
homework. A prior system in this space watched three affirmative gates get fabricated
inside a single turn, and its fix is the one that worked — check the claim against a
signal the agent does not author. Everything else there was prose, and prose did not
bind.

So a witness answers exactly one question:

    has a human said something since <cursor>?

...using a channel the agent cannot write. The engine does not care what the channel
is; it cares that the agent is not the author. That is why this is a registry rather
than a hardcoded transcript reader: a different runtime brings a different channel,
and the engine should not need editing to accept one.

DEGRADATION IS RECORDED, NEVER SILENT. If no witness can vouch, the gate is refused
(exit 3). The operator may deliberately downgrade to the `manual` witness, which
records the gate AND stamps `witnessed: false` into the proof, so `harness audit`
can list every gate that passed without a real witness. A guarantee you cannot audit
is not a guarantee; a degradation you cannot see is worse than none.
"""
from __future__ import annotations

from . import registry

import json
import os
from pathlib import Path
from typing import Callable

WITNESS_ENV = "HARNESS_WITNESS"
TRANSCRIPT_ENV = "HARNESS_TRANSCRIPT"

# WHERE THE SIDE IS NAMED, AND WHICH VALUE MEANS A PERSON — both DECLARED by the runtime.
#
# The three default field names cover transcripts that spell a turn's side the common way. A real
# runtime was measured that does not: every record is `{kind, data, version}`, the side lives in
# `kind`, and a human turn is spelled `Prompt`. Under the defaults that transcript counts zero
# human turns — which would refuse every gate, and refuse it while reporting "no human turns at
# all", a sentence that is true about the parse and false about the session.
#
# The fix is NOT to teach the engine that runtime's spelling. It is the same seam used for
# "which command counts as a commit": the knowledge is declared from outside and the engine only
# applies it. A second runtime with a third spelling then needs no engine change either.
ROLE_PATH_ENV = "HARNESS_TRANSCRIPT_ROLE_PATH"
HUMAN_ENV = "HARNESS_TRANSCRIPT_HUMAN"
_DEFAULT_ROLE_KEYS = ("role", "author", "from")
_DEFAULT_HUMAN = ("user", "human")
# Diagnostics only. Capped, because a mis-declared path can point at CONTENT, and a message that
# echoes an unbounded slice of a session back into a log is a worse failure than the one it
# explains.
_REPORT_VALUES = 5
_REPORT_CHARS = 24

_WITNESSES: dict[str, Callable] = {}
# Who claimed each name, so a collision can name BOTH sides rather than only the loser.
_OWNERS: dict[str, str] = {}


class NoWitness(RuntimeError):
    """No witness could vouch for a human affirmation."""


def witness(name: str) -> Callable:
    def deco(fn: Callable) -> Callable:
        key = registry.claim("witness", name, _WITNESSES, _OWNERS, fn)
        _WITNESSES[key] = fn
        return fn
    return deco


def registered() -> list[str]:
    return sorted(_WITNESSES)


def active_name() -> str:
    """Which witness to use. `auto` picks the first that reports itself usable."""
    return os.environ.get(WITNESS_ENV, "auto")


def vouch(*, cursor: dict | None) -> dict:
    """Return a proof dict, or raise NoWitness.

    `cursor` is the proof recorded for this run's previous gate (or None for the
    first). A witness uses it to answer "since when", so two gates in one turn cannot
    both claim the same single human reply — the second sees a cursor that has already
    consumed it.
    """
    name = active_name()
    if name == "auto":
        for candidate in ("transcript", "manual"):
            fn = _WITNESSES.get(candidate)
            if fn is None:
                continue
            try:
                return fn(cursor)
            except NoWitness:
                continue
        raise NoWitness(
            "no witness could vouch for a human affirmation.\n"
            f"  tried: transcript, manual\n"
            f"  point {TRANSCRIPT_ENV} at a transcript the agent does not write, or\n"
            f"  set {WITNESS_ENV}=manual to record an explicitly UNWITNESSED gate"
        )
    fn = _WITNESSES.get(name)
    if fn is None:
        raise NoWitness(
            f"unknown witness {name!r}; registered: {', '.join(registered())}"
        )
    return fn(cursor)


# ------------------------------------------------------------------ witnesses

def _dig(rec: dict, dotted: str):
    """Follow a declared dotted path through dicts. Anything else is "not found", not an error.

    No list indexing and no wildcards: a path expressive enough to need them is a parser, and a
    parser here would be the engine learning a layout instead of applying a declaration.
    """
    cur = rec
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur if isinstance(cur, (str, int, float)) else None


@witness("transcript")
def _transcript(cursor: dict | None) -> dict:
    """Count human turns in a JSONL transcript written by the runtime, not the agent.

    A turn is human when the record's side field holds a value that means a person. WHERE that
    field is, and WHICH values mean a person, are both declared from outside — see ROLE_PATH_ENV
    and HUMAN_ENV. We deliberately do not try to parse content: the only claim being made is
    "the count went up", and a count is hard to fake without write access to the file.

    Nothing here knows any runtime's layout, and that is not a nicety — the defaults were
    measured to be wrong for a real one, and the alternative to a declaration was a special case
    per runtime inside this function.
    """
    raw = os.environ.get(TRANSCRIPT_ENV)
    if not raw:
        raise NoWitness(f"{TRANSCRIPT_ENV} is not set")
    path = Path(raw).expanduser()
    if not path.is_file():
        raise NoWitness(f"{TRANSCRIPT_ENV} points at {path}, which is not a file")

    dotted = os.environ.get(ROLE_PATH_ENV, "").strip()
    declared = tuple(v.strip().lower() for v in os.environ.get(HUMAN_ENV, "").split(",")
                     if v.strip())
    human = declared or _DEFAULT_HUMAN

    count = 0
    named = 0          # records where the side field was FOUND at all
    seen: dict[str, int] = {}
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:  # line-wise: a fixed-size read can block on a live stream
                line = line.strip()
                if not line or not line.startswith("{"):
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(rec, dict):
                    continue
                if dotted:
                    got = _dig(rec, dotted)
                else:
                    got = next((rec[k] for k in _DEFAULT_ROLE_KEYS
                                if isinstance(rec.get(k), (str, int, float))), None)
                if got is None:
                    continue
                role = str(got)
                named += 1
                if len(seen) < _REPORT_VALUES or role[:_REPORT_CHARS] in seen:
                    seen[role[:_REPORT_CHARS]] = seen.get(role[:_REPORT_CHARS], 0) + 1
                if role.lower() in human:
                    count += 1
    except OSError as exc:
        raise NoWitness(f"cannot read {path}: {exc}") from None

    # "I COULD NOT FIND THE FIELD" IS NOT "THERE WERE NO PEOPLE", and conflating them is the
    # failure this whole engine keeps being corrected for. A wrong path produces a refusal on
    # every gate, so the refusal has to name the path it used rather than describe the session.
    where = f"{ROLE_PATH_ENV}={dotted!r}" if dotted else \
        f"the default field names {list(_DEFAULT_ROLE_KEYS)}"
    if named == 0:
        raise NoWitness(
            f"no record in {path.name} names a side.\n"
            f"  Looked at: {where}\n"
            f"  This is 'I cannot see who spoke', NOT 'nobody spoke'. Declare where this "
            f"runtime\n  names it, e.g. {ROLE_PATH_ENV}=kind or {ROLE_PATH_ENV}=data.role"
        )

    prior = int((cursor or {}).get("human_turns", -1))
    if prior >= 0 and count <= prior:
        raise NoWitness(
            f"no new human turn since the last gate "
            f"(transcript still shows {count} human turn(s)).\n"
            f"  A gate recorded in the same turn as the previous one is the exact\n"
            f"  shape of a fabricated affirmation. Yield, wait for a real reply, retry."
        )
    if count == 0:
        # The field was found and no value in it means a person. Report the values actually
        # there: with a declared vocabulary this is almost always a mismatch between what was
        # declared and what the runtime writes, and naming both sides is the whole remedy.
        got = ", ".join(f"{k!r}×{v}" for k, v in
                        sorted(seen.items(), key=lambda kv: -kv[1])[:_REPORT_VALUES])
        raise NoWitness(
            f"{named} record(s) name a side, and none of them means a person.\n"
            f"  Looked at: {where}\n"
            f"  Counted as human: {list(human)}"
            + ("  (default — declare " + HUMAN_ENV + " if this runtime spells it otherwise)"
               if not declared else "")
            + f"\n  Values present: {got}"
        )
    return {"witness": "transcript", "witnessed": True,
            "human_turns": count, "source": str(path),
            # What was applied, so a proof read back later says how the count was arrived at
            # rather than leaving it to be guessed from the environment of a past process.
            "role_path": dotted or list(_DEFAULT_ROLE_KEYS), "human_values": list(human)}


@witness("manual")
def _manual(cursor: dict | None) -> dict:
    """Deliberate, RECORDED downgrade — only reachable when asked for by name.

    `auto` will fall through to this, but only after `transcript` has declined, and the
    proof it writes says `witnessed: false` so the gate shows up in `harness audit`.
    """
    if active_name() not in ("manual", "auto"):
        raise NoWitness("manual witness must be selected explicitly")
    return {"witness": "manual", "witnessed": False,
            "note": "recorded without an independent witness"}
