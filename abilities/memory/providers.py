"""Accumulated-lessons facts — what the memory store actually holds right now.

Borrowed, not run. This ability owns one question — "was the store consulted, and what did it
say" — so any flow with a load-context-first step can ask it with `requires: [memory]` instead of
carrying its own reader.

WHY THE TOOL IS NOT COPIED, unlike the scripts other abilities vendor. The others are algorithms,
and an algorithm travels. This one's value is a LIVE LOCAL STORE — a database deliberately kept out
of version control because it accumulates per-machine usage signal. Copying the reader would produce
a reader pointed at nothing. So the capability names the path where it lives, and absence there is
reported rather than papered over. That is the general rule this case establishes: **a tool whose
value is its own accumulated state cannot be vendored**, and the capability declaration is what
keeps its absence honest.

WHY IT IS A SEPARATE ABILITY. The knowledge here is about a store — where it lives, which of its
subcommands are reads, how to tell "empty" from "unreadable" — none of which is about shipping. The
STEP that consults it (open by declaring what was loaded) stays with the flow that owns the
lifecycle: it is one point in a ship, not a lifecycle of its own.

WHY `--format json`. The reader used to regex `└─ [id]` lines out of a rendering meant for a person,
because `--format json` was accepted and ignored — it printed the same banner bytes and was not
parseable. That was fixed in the store first (2026-09-15), precisely so this side could stop
parsing prose. The banner remains the store's default because ship-check pastes it verbatim; a
machine reader asks for the machine surface.
"""
import json
import subprocess
import sys
from pathlib import Path

from engine import facts, operators

# Overridable because a test must be able to point this at a scratch store instead of the live one.
# Unlike a data directory this is a CLI, so a wrong value fails loudly on exec rather than quietly
# reading someone else's data.
MEMORY_CLI_DEFAULT = "${HARNESS_MEMORY_CLI:-~/.kiro/skills/shared-kb/memory/memory.py}"


@facts.provider("hot_set",
                requires=({"file": MEMORY_CLI_DEFAULT},),
                schema={
    # Whether the store could be READ. Paired with the count on purpose: "consulted and genuinely
    # empty" and "never consulted" both render as zero, and the standing instruction in this space
    # is explicitly not to let the second become the first.
    "hot_banner_readable": operators.T_BOOL,
    "hot_set_count": operators.T_INT,
    "hot_set_ids": operators.T_LIST,
    # WHICH store answered. A count on its own cannot tell one store from another, and the moment
    # `MEMORY_DB_PATH` is used to partition by user, "the lessons were loaded" is satisfiable by a
    # reading of somebody ELSE'S store — a criterion met by the wrong evidence, which is the failure
    # this engine treats as worse than a missing check. Reported by the store, not guessed here: the
    # CLI resolves the database, so this side cannot know it without being told.
    #
    # Empty when the reading failed, for the same reason the count is zero there — an unreadable
    # store has no path to name, and inventing the one it WOULD have used would be a fact about
    # configuration dressed as a fact about the world.
    "hot_store_path": operators.T_STR,
})
def _hot_set(ctx: dict) -> dict:
    """The always-resident slice of accumulated lessons, read from wherever it actually lives.

    The command is a pure read (SELECTs only, no counter or timestamp touched), so calling it
    repeatedly to evaluate a criterion is safe. Its sibling `recall` is NOT — it records usage
    unless told otherwise — which is exactly why this provider calls the one that does not.
    """
    empty = {"hot_banner_readable": False, "hot_set_count": 0, "hot_set_ids": [],
             "hot_store_path": ""}
    # `facts.expand_env` rather than a local expander: it is the same function the capability probe
    # uses, so `validate` reporting this file present and this provider finding it cannot disagree.
    # Resolved at call time, so an override set after import still applies.
    cli = Path(facts.expand_env(MEMORY_CLI_DEFAULT)).expanduser()
    try:
        proc = subprocess.run([sys.executable, str(cli), "hot-banner", "--format", "json"],
                              capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return empty
    # exit 3 = the store exists but was never migrated; exit 1 = locked or crashed. Both are
    # "could not look", and the count they imply is an artefact of failure, not a result.
    if proc.returncode != 0:
        return empty
    try:
        payload = json.loads(proc.stdout or "")
    except json.JSONDecodeError:
        # Which is what an older store does: `--format json` printed the banner there. Unreadable,
        # not empty — the distinction this provider exists to keep.
        return empty
    count, ids = payload.get("count"), payload.get("ids")
    if not isinstance(count, int) or not isinstance(ids, list):
        return empty
    if count != len(ids):
        # The two halves of one answer disagree, so there is no coherent reading to report. Saying
        # "unreadable" is the honest answer; reporting either number would present a store that
        # contradicted itself as a store that was consulted successfully.
        return empty
    # A reading with no store named is not a coherent one either: the count would be reportable
    # while the thing that makes it checkable is absent, which is the state this field exists to
    # end. An older store that answers without `db` is therefore unreadable, not empty.
    store = payload.get("db")
    if not isinstance(store, str) or not store.strip():
        return empty
    return {"hot_banner_readable": True,
            "hot_set_count": count,
            "hot_set_ids": [str(i) for i in ids],
            "hot_store_path": store}
