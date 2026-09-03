"""Operators — the fourth registry, and where condition extensibility lives.

An ability's conditions are built from operators named in its spec. The engine ships a
small closed set of DOMAIN-FREE comparisons; anything else is registered, exactly like a
completion predicate or a witness.

This is the answer to "how do I express a comparison the engine does not have" — you
register an operator, which is python reviewed as code. You do NOT put an expression
language or an eval into the spec. That distinction matters more than it looks:

    An ability is a MOVABLE, OFFLINE-VALIDATABLE unit. `cp -r abilities/foo` elsewhere and
    `harness validate foo` tells you whether it is correct.

The moment a spec can carry code, you can no longer judge an ability without running it —
somebody handing you an ability would have to be trusted, not just validated. The same
reasoning already forbids an ability's prose from reaching outside its own directory.

So: extend the registry, never embed code in data.

TYPE CHECKING IS THE POINT. Each operator declares the fact types it accepts, and the
fact provider declares each fact's type, so `count_gte` applied to a string is caught at
LOAD time rather than evaluating to something arbitrary at run time. A condition that
silently evaluates false is the worst failure available here: the hook simply never fires
and nobody notices for a year.
"""
from __future__ import annotations

from . import registry

import fnmatch
from typing import Callable

# The type vocabulary a fact provider may declare. Deliberately tiny — a rich type system
# here would be a second language to learn for no gain.
T_STR = "str"
T_INT = "int"
T_BOOL = "bool"
T_LIST = "list[str]"
TYPES = (T_STR, T_INT, T_BOOL, T_LIST)

ANY = TYPES  # shorthand for "accepts any declared type"

_OPERATORS: dict[str, dict] = {}
# Who claimed each name, so a collision can name BOTH sides rather than only the loser.
_OWNERS: dict[str, str] = {}


class OperatorError(ValueError):
    pass


def operator(name: str, *, accepts: tuple[str, ...], arg: str) -> Callable:
    """Register a comparison.

    `accepts` = the fact types this operator can be applied to.
    `arg`     = a human description of the right-hand side, used in error messages.
    """
    def deco(fn: Callable) -> Callable:
        registry.claim("operator", name, _OPERATORS, _OWNERS, fn)
        _OPERATORS[name] = {"fn": fn, "accepts": tuple(accepts), "arg": arg}
        return fn
    return deco


def is_registered(name: str) -> bool:
    return name in _OPERATORS


def registered() -> list[str]:
    return sorted(_OPERATORS)


def accepts(name: str) -> tuple[str, ...]:
    return _OPERATORS[name]["accepts"]


def arg_shape(name: str) -> str:
    return _OPERATORS[name]["arg"]


def check_type(name: str, fact_type: str, where: str) -> None:
    ok = _OPERATORS[name]["accepts"]
    if fact_type not in ok:
        raise OperatorError(
            f"{where}: operator '{name}' cannot be applied to a fact of type "
            f"'{fact_type}' (it accepts {', '.join(ok)})"
        )


def apply(name: str, value, arg) -> bool:
    entry = _OPERATORS.get(name)
    if entry is None:  # unreachable: validated at load time
        raise OperatorError(f"no operator '{name}'")
    return bool(entry["fn"](value, arg))


# ------------------------------------------------------------------ built-ins
# Every one of these is a comparison over generic shapes. None of them knows what any
# particular fact MEANS — that is the line this file must not cross, and
# `tests/test_engine_purity.py` greps for it.

def _as_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    return [str(value)]


def _glob_match(path: str, glob: str) -> bool:
    """fnmatch, plus a leading `**/` also matches a root-level path.

    Plain fnmatch treats `/` literally, so `**/*.md` would miss `a.md` at the root — a
    surprise sharp enough that every system that has hit it special-cases it.
    """
    if fnmatch.fnmatchcase(path, glob):
        return True
    if glob.startswith("**/") and fnmatch.fnmatchcase(path, glob[3:]):
        return True
    return False


@operator("equals", accepts=ANY, arg="a scalar")
def _equals(value, arg) -> bool:
    return value == arg


@operator("matches_any", accepts=(T_STR, T_LIST), arg="a list of globs")
def _matches_any(value, arg) -> bool:
    globs = _as_list(arg)
    return any(_glob_match(v, g) for v in _as_list(value) for g in globs)


@operator("matches_none", accepts=(T_STR, T_LIST), arg="a list of globs")
def _matches_none(value, arg) -> bool:
    return not _matches_any(value, arg)


@operator("contains", accepts=(T_STR, T_LIST), arg="a substring or member")
def _contains(value, arg) -> bool:
    if isinstance(value, (list, tuple)):
        return str(arg) in [str(v) for v in value]
    return str(arg) in str(value or "")


@operator("in", accepts=(T_STR, T_INT, T_BOOL), arg="a list of allowed values")
def _in(value, arg) -> bool:
    return value in (arg if isinstance(arg, (list, tuple)) else [arg])


@operator("exists", accepts=ANY, arg="true or false")
def _exists(value, arg) -> bool:
    present = value is not None and value != [] and value != ""
    return present if arg in (True, None) else not present


@operator("count_gte", accepts=(T_LIST, T_INT), arg="an integer")
def _count_gte(value, arg) -> bool:
    n = value if isinstance(value, int) else len(_as_list(value))
    return n >= int(arg)


@operator("count_lte", accepts=(T_LIST, T_INT), arg="an integer")
def _count_lte(value, arg) -> bool:
    n = value if isinstance(value, int) else len(_as_list(value))
    return n <= int(arg)
