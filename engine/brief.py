"""Brief — the driving contract, rendered from the engine instead of written by hand.

WHY THIS IS GENERATED

An agent needs a short document telling it how to drive this engine: the call loop, what the
exit codes mean, what it must not do. That document was hand-written for a while, and it did
what hand-written descriptions of a live surface always do — it drifted. Within two days of
work it claimed a store location that had moved, listed exit codes one short, named a hard-coded
path that only existed on one machine, and quoted step and line counts that were both wrong.
Nothing objected, because nothing could: it was prose about the engine, sitting beside the
engine, with no relationship the engine could check.

Everything derivable is therefore derived here — the commands that exist, the codes that exist,
the mechanisms this INSTALLATION actually uses, the flows it can route to. Drift becomes
impossible rather than unlikely.

WHAT IS NOT DERIVABLE, AND IS THEREFORE DATA

Six rules below are judgment, not structure: no amount of reading the argparse tree yields "ask
for the affirmation in a separate turn". They live in `JUDGMENT` as data, which puts them inside
the engine — and therefore under the purity guard, which cannot be satisfied by a rule phrased
in any consumer's vocabulary. A rule that survives that check is one that holds for every
consumer, which is exactly the property a base needs its advice to have.

TAILORED, NOT UNIVERSAL

Sections are emitted only when the installed flows exercise the mechanism they describe. An
installation whose flows have no variants is not told about variants. That is not brevity for
its own sake: every paragraph an agent reads and cannot use is budget spent teaching it about
machinery it will never meet, and it makes the paragraphs that DO apply harder to find.
"""
from __future__ import annotations

# Exit codes, keyed by NAME rather than by number. The numbers live with the CLI; this holds
# what each one MEANS. A test asserts the two sets of names match exactly, so adding a code
# without describing it — the drift that happened — fails the build instead of shipping.
EXIT_MEANINGS: dict[str, tuple[str, str]] = {
    "OK": ("passed", "carry on"),
    "USAGE": ("bad usage, or an environment that cannot answer",
              "fix the command, or the environment it names"),
    "BAD_SPEC": ("the flow could not be loaded — invalid, or its extension code is not "
                 "approved on this machine",
                 "read stderr. If it names an unapproved extension file, that is a LOCAL "
                 "decision for whoever owns this machine (`harness trust`), not something to "
                 "report or work around. Otherwise report it upstream; either way do NOT edit "
                 "the spec to get past it"),
    "REFUSED": ("a rule is not satisfied",
                "read stderr — it names what is missing and the command that supplies it"),
    "BLOCKED": ("a protected action has no recorded authorisation",
                "get a real human affirmation. Do NOT reword the action to slip past"),
    "INTERNAL": ("the engine failed in a way it does not account for",
                 "report it with the traceback; this is a defect in the engine, not your input"),
}

# The non-derivable half. Each entry is (title, what to do, why it is not obvious).
#
# Phrased in ENGINE vocabulary throughout, because the purity guard scans this file. That
# constraint is the feature: a rule that can only be stated using one consumer's words is a rule
# about that consumer, and does not belong in a base.
JUDGMENT: tuple[tuple[str, str, str], ...] = (
    (
        "Saying a step is done is not what closes it",
        "Record the evidence the step asks for, then close it. If the close is refused, the "
        "work is not finished — read what the refusal names and do that.",
        "The predicate is evaluated against the store, not against your account of it. This is "
        "the whole difference between a flow and a list of instructions.",
    ),
    (
        "An affirmation takes two turns, never one",
        "Put what needs deciding in front of the human and END your turn. Record the "
        "affirmation in the turn AFTER they answer.",
        "The engine counts whether a human spoke since the last one. Asking and answering "
        "inside a single turn is detected as forged and refused — and every refusal is kept.",
    ),
    (
        "Quote the human, do not summarise them",
        "Put their own words in the evidence.",
        "A summary silently changes meaning: a conditional yes becomes an unconditional one, "
        "and the record then says something the human never said.",
    ),
    (
        "Do not read all the guidance up front",
        "The directive is enough to start. Fetch a guide or a topic only when you need it.",
        "A mature flow carries thousands of lines of guidance. Preloading it spends the context "
        "you need for the work, and the tiering exists precisely so you do not have to.",
    ),
    (
        "A spec that blocks you is not a spec to edit",
        "If a criterion cannot be met, say so. Never relax the criterion, and never rewrite a "
        "record so that a criterion passes.",
        "Both are the same move: turning 'the work is incomplete' into 'the record says it is "
        "complete'. The second is worse than the first, because it is invisible afterwards.",
    ),
    (
        "Nothing forces you to open a run",
        "When a task will change files or make an external effect, open a run FIRST.",
        "The runtime guard allows every action while no run is open — it runs before every tool "
        "call and must never brick a machine. So skipping this is not blocked, and afterwards "
        "nothing distinguishes a flow that was followed from one that was never started.",
    ),
)

# Sections that only make sense when the installation actually uses the mechanism. Keyed by the
# capability name the engine detects in a flow (see flow.ENGINE_CAPABILITIES).
_CONDITIONAL: dict[str, tuple[str, str]] = {
    "gates": (
        "Gates",
        "A step whose gate is `affirm` needs an authorisation you cannot produce yourself — see\n"
        "the judgment rule on two turns. A gate of the form `preauth:<key>` may be satisfied by\n"
        "configuration instead: set the key on the run, then record the decision as `preauth`.\n"
        "Recording a preauth while its key is unset is refused; pre-authorisation is never\n"
        "inferred.",
    ),
    "variants": (
        "Variants",
        "Some flows have more than one shape. It is pinned when the run opens and does not\n"
        "change. Steps belonging to the other shape are not 'skippable' — they DO NOT EXIST, and\n"
        "entering or skipping them is refused.\n\n"
        "If a recorded value conflicts with the run's shape, the run was opened in the wrong\n"
        "shape: close it and open a new one with the shape stated explicitly. Do NOT edit the\n"
        "recorded value to match — that converts 'opened wrong' into 'the record says I did not'.",
    ),
    "obligations": (
        "Obligations",
        "A hook whose condition matches raises an obligation. Closing the run is refused while\n"
        "any remain undischarged. List them, do what the body says, then discharge each with\n"
        "evidence of what you did. The body is in the store, so a truncated output cannot lose it.",
    ),
    "prose": (
        "Guidance comes in tiers",
        "`next` prints the directive unconditionally — a few lines, enough to act. It also prints\n"
        "POINTERS to a longer guide and to named topics. Fetch those on demand.",
    ),
    "optional_steps": (
        "Optional steps",
        "Some steps may legitimately never run. Record that decision rather than leaving them\n"
        "open, or the run cannot be closed.",
    ),
    "repeatable": (
        "Repeatable steps",
        "Some steps may run more than once — a fix-and-recheck cycle. Some carry a budget; when\n"
        "it is exhausted the engine records that fact rather than letting the loop continue\n"
        "silently.",
    ),
    "exclusive_groups": (
        "Mutually exclusive steps",
        "Some steps come in groups where exactly one member ever runs. Closing one satisfies the\n"
        "group; its siblings are then not owed.",
    ),
    "phase_goals": (
        "Phase acceptance",
        "A phase can carry a criterion of its own. Summarising a phase is refused when it is not\n"
        "met. There is a read-only form of the same question — ask that first; it is faster than\n"
        "attempting the summary and reading the refusal.",
    ),
    "guards": (
        "Guards run outside your control",
        "Certain tool calls are checked before they happen, and a missing authorisation stops\n"
        "them with the BLOCKED code. That check is not something you invoke; it is wired into the\n"
        "runtime hosting you.",
    ),
}


def _fence(lines: list[str]) -> str:
    return "```sh\n" + "\n".join(lines) + "\n```"


def render(
    *,
    invocation: str,
    version: str,
    portable: bool = False,
    write_hint: str = "",
    exit_codes: dict[str, int],
    subcommands: dict[str, str],
    routable: list[tuple[str, str]],
    capabilities: set[str],
    env: list[tuple[str, str]],
    guard_tools: list[str],
    example_step: str | None,
) -> str:
    """Assemble the brief. Every argument is a fact the caller read off the engine."""
    out: list[str] = []
    a = out.append

    a(f"<!-- GENERATED by `harness brief` from engine {version}. Do not edit: regenerate. -->")
    a("")
    a("# Driving this engine")
    a("")
    a("A flow is not a list of instructions you follow — it is a state machine. Each step asks")
    a("for specific evidence, and the engine decides from the store whether it may close.")
    a("")
    short = "harness"
    if invocation != short:
        # A source checkout has no such command on PATH. Naming the path once and aliasing it
        # keeps every later line readable — a fence where each line begins with an absolute path
        # is technically correct and unreadable, and unread advice enforces nothing.
        if portable:
            # Say what this copy IS. A reader who does not know it is a reference will try the
            # placeholder — which is exactly what happened when a consumer's configuration was
            # pointed at this file.
            a("**This is the portable reference copy**: the path below is a placeholder, and a")
            a("machine-local copy with a working path is what an agent should actually read.")
            if write_hint:
                a("")
                a(f"Produce one with `{write_hint}`.")
            a("")
        a("This installation is a source checkout, so the command is not on your PATH:")
        a("")
        a(_fence([f"alias {short}='{invocation}'"]))
        a("")
        a(f"Everything below uses `{short}`.")
        a("")

    a("## Exit codes are the contract")
    a("")
    a("| code | means | what you do |")
    a("|---|---|---|")
    for name, value in sorted(exit_codes.items(), key=lambda kv: kv[1]):
        meaning, action = EXIT_MEANINGS[name]
        a(f"| **{value}** | {meaning} | {action} |")
    a("")
    a("The refusal codes are products, not errors: their stderr names the exact remedy. Follow")
    a("it rather than inventing one.")
    a("")

    a("## The loop")
    a("")
    steps = [
        ("init", "once; safe to repeat"),
        ("abilities", "what is installed here"),
        ("open <ability> --scope <key> --run <id>", "start a run"),
        ("next --run <id>", "the next step, and what it requires"),
        ("evidence --run <id> --step <s> --kind <k> --value <v>", "record what the step asks for"),
        ("close-step --run <id> --step <s>", "refused if the criterion is unmet"),
        ("close-run --run <id> --result <r>", "refused while anything is owed"),
    ]
    a(_fence([f"{short} {cmd:<52} # {why}" for cmd, why in steps
              if cmd.split()[0] in subcommands]))
    a("")
    a("Diagnosis, all read-only:")
    a("")
    diag = [c for c in ("status", "obligations", "assert-goal", "show", "validate", "audit",
                        "leases", "history") if c in subcommands]
    a(_fence([f"{short} {c:<14} # {subcommands[c]}" for c in diag]))
    a("")

    if example_step:
        a("## What `next` gives you")
        a("")
        a("Read the requirement lines and the directive. The guide and topic lines are POINTERS")
        a(f"— fetch them only when you need them (`{short} show --run <id> --step "
          f"{example_step}`).")
        a("")

    for cap, (title, body) in _CONDITIONAL.items():
        if cap in capabilities:
            a(f"## {title}")
            a("")
            a(body)
            a("")

    if routable:
        a("## What is installed, and when to reach for it")
        a("")
        for name, when in routable:
            a(f"- **`{name}`** — {when}")
        a("")

    a("## Judgment")
    a("")
    a("These do not follow from the command surface. Each one is here because its absence")
    a("produced a specific failure.")
    a("")
    for title, rule, why in JUDGMENT:
        a(f"### {title}")
        a("")
        a(rule)
        a("")
        a(f"*Why:* {why}")
        a("")

    a("## Never")
    a("")
    a("- Force past owed work or undischarged obligations without asking the human first. Each")
    a("  force is recorded as a breach, and the two are separate authorisations — one does not")
    a("  cover the other.")
    a("- Edit the store directly. Every state change has a command; writing to it bypasses every")
    a("  check and leaves no trace that it happened.")
    a("- Reword an action to get past a BLOCKED result.")
    a("")

    if env:
        a("## Environment")
        a("")
        a("| variable | effect |")
        a("|---|---|")
        for var, effect in env:
            a(f"| `{var}` | {effect} |")
        a("")

    if guard_tools:
        a("## Tools the runtime hook must cover")
        a("")
        a("Derived from what the installed flows declare. A tool absent from the host's matcher")
        a("list is never checked, and that failure is silent — the declaration looks fine and the")
        a("hook is simply never asked.")
        a("")
        a(_fence(sorted(guard_tools)))
        a("")

    return "\n".join(out).rstrip() + "\n"
