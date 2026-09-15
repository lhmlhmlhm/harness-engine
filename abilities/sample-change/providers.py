"""The one fact this sample derives for itself: which mode the run is in.

WHY IT IS DERIVED AND NOT ASKED. The variant decides which steps apply, and a variant the caller
types is a value that can disagree with the world silently — every step of the other shape then
reports "not part of this flow", and that refusal reads as if it were correct. So the mode comes from
the scope, which for this ability IS the thing the mode depends on.

The rule is deliberately trivial and stated rather than clever: a scope whose name ends in `-full` is
the full path, anything else is quick. A sample that inferred the mode from file contents would be
demonstrating inference, not the seam.
"""
from engine import facts, operators


@facts.provider("sample_mode", schema={
    # The variant selector. A plain string, because the engine compares it against the values the
    # spec declares and has no idea what either of them means.
    "sample_mode": operators.T_STR,
    # WHY it came out that way — recorded so a reader of the run can tell a derivation from a default.
    # Without it, "quick" from a real rule and "quick" because nothing matched are the same word.
    "sample_mode_reason": operators.T_STR,
})
def _sample_mode(ctx: dict) -> dict:
    scope = str(ctx.get("scope") or "")
    if scope.rstrip("/").endswith("-full"):
        return {"sample_mode": "full",
                "sample_mode_reason": "the scope name ends in '-full'"}
    return {"sample_mode": "quick",
            "sample_mode_reason": "the scope name does not end in '-full', so the default applies"}
