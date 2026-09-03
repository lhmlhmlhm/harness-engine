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

_WITNESSES: dict[str, Callable] = {}
# Who claimed each name, so a collision can name BOTH sides rather than only the loser.
_OWNERS: dict[str, str] = {}


class NoWitness(RuntimeError):
    """No witness could vouch for a human affirmation."""


def witness(name: str) -> Callable:
    def deco(fn: Callable) -> Callable:
        registry.claim("witness", name, _WITNESSES, _OWNERS, fn)
        _WITNESSES[name] = fn
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

@witness("transcript")
def _transcript(cursor: dict | None) -> dict:
    """Count human turns in a JSONL transcript written by the runtime, not the agent.

    A turn is human when its record carries a role/author field naming the human side.
    We deliberately do not try to parse content: the only claim being made is "the
    count went up", and a count is hard to fake without write access to the file.

    The path must come from the environment because the engine has no business knowing
    a runtime's on-disk layout.
    """
    raw = os.environ.get(TRANSCRIPT_ENV)
    if not raw:
        raise NoWitness(f"{TRANSCRIPT_ENV} is not set")
    path = Path(raw).expanduser()
    if not path.is_file():
        raise NoWitness(f"{TRANSCRIPT_ENV} points at {path}, which is not a file")

    count = 0
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
                role = str(rec.get("role") or rec.get("author") or rec.get("from") or "")
                if role.lower() in ("user", "human"):
                    count += 1
    except OSError as exc:
        raise NoWitness(f"cannot read {path}: {exc}") from None

    prior = int((cursor or {}).get("human_turns", -1))
    if prior >= 0 and count <= prior:
        raise NoWitness(
            f"no new human turn since the last gate "
            f"(transcript still shows {count} human turn(s)).\n"
            f"  A gate recorded in the same turn as the previous one is the exact\n"
            f"  shape of a fabricated affirmation. Yield, wait for a real reply, retry."
        )
    if count == 0:
        raise NoWitness("transcript contains no human turns at all")
    return {"witness": "transcript", "witnessed": True,
            "human_turns": count, "source": str(path)}


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
