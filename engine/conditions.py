"""Conditions — nested boolean expressions over declared facts.

SHAPE

    leaf:      { fact: <name>, <operator>: <arg> }
    all_of:    [ cond, ... ]      # every one must hold
    any_of:    [ cond, ... ]      # at least one must hold
    not:       cond

Composable to any depth, and the whole thing is checkable before it ever runs: every
`fact` must be in the provider's schema and every operator must be legal for that fact's
type. That is deliberate — see `facts.py` on why a silently-false condition is the worst
outcome available.

WHY NOT A STRING EXPRESSION LANGUAGE

Two reasons, and the second is the one that decided it:

1. A mini-language always grows. First string functions, then arithmetic, then variables —
   and each addition is a parser change plus a new way for a spec to be subtly wrong.
2. In YAML flow style the structured form is already compact for the common case:
       when: { fact: <declared-fact>, matches_any: ["<glob>", ...] }
   The characters saved by a bespoke syntax do not pay for a parser, and the structured
   form is what makes the load-time key/type check straightforward.

The escape hatch for genuinely novel logic is a registered operator or a registered fact
provider — python, reviewed as code — never an expression embedded in data.
"""
from __future__ import annotations

import difflib

from . import operators

COMBINATORS = ("all_of", "any_of", "not")


class ConditionError(ValueError):
    pass


def validate(cond, schema: dict, where: str) -> None:
    """Check a condition tree against a fact schema. Raises ConditionError."""
    if cond is None:
        return
    if not isinstance(cond, dict):
        raise ConditionError(
            f"{where}: a condition must be a mapping, got {type(cond).__name__}"
        )

    combos = [k for k in COMBINATORS if k in cond]
    if combos:
        if len(combos) > 1 or len(cond) > 1:
            raise ConditionError(
                f"{where}: a condition may use exactly ONE of "
                f"{', '.join(COMBINATORS)} and nothing else alongside it; got "
                f"{', '.join(sorted(cond))}"
            )
        key = combos[0]
        body = cond[key]
        if key == "not":
            validate(body, schema, f"{where} → not")
            return
        if not isinstance(body, list) or not body:
            raise ConditionError(f"{where}: '{key}' must be a non-empty list of conditions")
        for i, sub in enumerate(body):
            validate(sub, schema, f"{where} → {key}[{i}]")
        return

    if "fact" not in cond:
        raise ConditionError(
            f"{where}: a leaf condition needs a 'fact' key (or use one of "
            f"{', '.join(COMBINATORS)}); got {', '.join(sorted(cond)) or '(empty)'}"
        )
    name = str(cond["fact"])
    if name not in schema:
        hint = difflib.get_close_matches(name, list(schema), n=1)
        suffix = f"  did you mean '{hint[0]}'?" if hint else ""
        raise ConditionError(
            f"{where}: fact '{name}' is not in the provider's schema.{suffix}\n"
            f"  declared: {', '.join(sorted(schema)) or '(none)'}"
        )

    ops = [k for k in cond if k != "fact"]
    if len(ops) != 1:
        raise ConditionError(
            f"{where}: a leaf condition needs exactly one operator alongside 'fact'; "
            f"got {', '.join(sorted(ops)) or '(none)'}\n"
            f"  registered operators: {', '.join(operators.registered())}"
        )
    op = ops[0]
    if not operators.is_registered(op):
        hint = difflib.get_close_matches(op, operators.registered(), n=1)
        suffix = f"  did you mean '{hint[0]}'?" if hint else ""
        raise ConditionError(
            f"{where}: unknown operator '{op}'.{suffix}\n"
            f"  registered: {', '.join(operators.registered())}"
        )
    try:
        operators.check_type(op, schema[name], where)
    except operators.OperatorError as exc:
        raise ConditionError(str(exc)) from None


def facts_referenced(cond) -> set[str]:
    """Every fact name a condition tree touches. Used to report what a hook depends on."""
    out: set[str] = set()
    if not isinstance(cond, dict):
        return out
    for key in COMBINATORS:
        if key in cond:
            body = cond[key]
            if key == "not":
                return facts_referenced(body)
            for sub in body:
                out |= facts_referenced(sub)
            return out
    if "fact" in cond:
        out.add(str(cond["fact"]))
    return out


def evaluate(cond, values: dict) -> bool:
    """Evaluate a validated condition tree. No condition means "always"."""
    if cond is None:
        return True
    for key in COMBINATORS:
        if key in cond:
            body = cond[key]
            if key == "not":
                return not evaluate(body, values)
            if key == "all_of":
                return all(evaluate(s, values) for s in body)
            return any(evaluate(s, values) for s in body)
    name = str(cond["fact"])
    op = next(k for k in cond if k != "fact")
    return operators.apply(op, values.get(name), cond[op])


def explain(cond, values: dict, indent: int = 0) -> list[str]:
    """Render a per-leaf trace of why a condition came out as it did.

    Exists because "the hook did not fire" is otherwise unanswerable, and an unanswerable
    negative is how a condition rots unnoticed.
    """
    pad = "  " * indent
    if cond is None:
        return [f"{pad}(no condition — always true)"]
    for key in COMBINATORS:
        if key in cond:
            body = cond[key]
            got = evaluate(cond, values)
            lines = [f"{pad}{key}: {'✔' if got else '✘'}"]
            subs = [body] if key == "not" else body
            for sub in subs:
                lines += explain(sub, values, indent + 1)
            return lines
    name = str(cond["fact"])
    op = next(k for k in cond if k != "fact")
    got = evaluate(cond, values)
    val = values.get(name)
    shown = val if not isinstance(val, list) else f"[{len(val)} item(s)]"
    return [f"{pad}{'✔' if got else '✘'} {name} {op} {cond[op]!r}   (actual: {shown!r})"]
