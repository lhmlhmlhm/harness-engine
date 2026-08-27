"""Hooks — boundary triggers, and obligations that must be discharged.

TRIGGERS

Four boundaries, each with a selector naming what it watches:

    phase_start     phase: <id>    the phase's first step is entered
    phase_end       phase: <id>    the phase's last required step closes
    step_close      step:  <id>    that step closes
    gate_recorded   step:  <id>    that step's gate is answered

MODES

    contract   print text for the caller to fulfil — the engine executes NOTHING
    command    run a shell command and read its exit code

`contract` is the important one and it is not a weaker form of `command`. A real system in
this space runs 14 of its 15 hooks this way, because the work (call this API, report that
milestone) needs tools the flow engine has no business holding. So the engine's job is not
to do it but to make sure it is not forgotten.

OBLIGATIONS: HOW "NOT FORGOTTEN" IS ENFORCED

A hook with `obligation: true` writes a row when it fires, and `close-run` REFUSES while
any row is outstanding. Discharge is explicit: `harness discharge --hook <id>`.

That row replaces what the reference system does with sentinel files — it has 1,027 of them
on disk right now. Files exist there for a good reason: the printed contract can be lost to
output truncation, so it must also live somewhere durable. A database row is the same idea
with less machinery, and it lets `close-run` ask the question with a query instead of a
directory walk.

FACTS ARE SNAPSHOTTED PER TRIGGER

Facts are gathered once per trigger firing (not per hook, not once per run) and the values
are stored on the obligation. Per-run caching would go stale across a long flow — a
a fact about what has changed, read at the start, says nothing by the end. Storing the snapshot means
a discharge can be audited later against the world as it was when the obligation arose.
"""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass

from . import conditions, facts, store

TRIGGERS = ("phase_start", "phase_end", "step_close", "gate_recorded")
PHASE_TRIGGERS = ("phase_start", "phase_end")
STEP_TRIGGERS = ("step_close", "gate_recorded")

MODE_CONTRACT = "contract"
MODE_COMMAND = "command"
MODES = (MODE_CONTRACT, MODE_COMMAND)


class HookError(ValueError):
    pass


@dataclass(frozen=True)
class Hook:
    id: str
    on: str
    selector: str          # the phase id or step id this watches
    mode: str
    body: str              # contract text, or the command line
    when: dict | None
    obligation: bool
    fail_closed: bool      # command mode: a non-zero exit blocks instead of warning


@dataclass
class Fired:
    hook: Hook
    matched: bool
    trace: list[str]
    obligation_created: bool = False
    command_rc: int | None = None
    command_out: str = ""
    # The body with templates already expanded. Carried on the result rather than left to
    # the caller, because a caller that printed the RAW body would show `{{run_id}}` to
    # whoever has to act on the contract — which happened, and only a hook that also
    # raised an obligation escaped it (that path substituted separately).
    rendered: str = ""


def substitute(text: str, run_id: str, selector: str, on: str, values: dict,
               config: dict | None = None) -> str:
    """Expand `{{run_id}}`, `{{selector}}`, `{{trigger}}`, `{{fact.<key>}}`, `{{config.<key>}}`.

    Only these. A template that could reach anywhere would be a second, undeclared way for
    an ability to depend on the engine's internals; facts are the declared channel, so
    domain values arrive as `{{fact.<key>}}` and the provider owns supplying them.

    `{{config.<key>}}` is what makes ARTIFACT VERIFICATION expressible. A gate answers
    "may this proceed"; it cannot answer "did what got produced actually match what was
    configured" — that needs to read the artefact afterwards. With the config value in the
    template, a `fail_closed` command hook on a step boundary does exactly that:

        command: |
          <read the artefact> | grep -q "{{config.<some-key>}}" \
            || { echo "output disagrees with the configured value" >&2; exit 1; }

    The verification lives in the ability (it knows what its artefacts look like); the
    engine only supplies the configured value and honours the exit code.
    """
    out = (text.replace("{{run_id}}", run_id)
               .replace("{{selector}}", selector)
               .replace("{{trigger}}", on))
    for key, val in values.items():
        shown = ", ".join(str(v) for v in val) if isinstance(val, list) else str(val)
        out = out.replace(f"{{{{fact.{key}}}}}", shown)
    for key, val in (config or {}).items():
        out = out.replace(f"{{{{config.{key}}}}}", str(val))
    return out


_TEMPLATE_FACT = re.compile(r"\{\{\s*fact\.([A-Za-z0-9_]+)\s*\}\}")


def hooks_for(flow, on: str, selector: str) -> list[Hook]:
    return [h for h in flow.hooks if h.on == on and h.selector == selector]


def fire(conn, flow, run_row, on: str, selector: str) -> list[Fired]:
    """Evaluate and fire every hook watching this boundary.

    Facts are gathered ONCE here and shared by every hook on this boundary — the point of
    per-trigger evaluation. Gathering is skipped entirely when no hook on this boundary
    declares a condition, so an unconditional flow never pays for a provider call.
    """
    candidates = hooks_for(flow, on, selector)
    if not candidates:
        return []

    meta = json.loads(run_row["metadata_json"] or "{}")
    cfg = {**flow.config_defaults, **(meta.get("config") or {})}

    # Gather only the providers whose facts are actually referenced on THIS boundary. With
    # several providers wired — and a real one shelling out to domain tooling — gathering all
    # of them at every boundary makes most of the work waste, and an unused provider's cost
    # then argues against declaring it, which is backwards.
    needed: set[str] = set()
    for h in candidates:
        if h.when is not None:
            needed |= conditions.facts_referenced(h.when)
        needed |= set(_TEMPLATE_FACT.findall(h.body or ""))
    use = tuple(p for p in flow.facts_providers
                if any(flow.facts_owner.get(k) == p for k in needed))
    values: dict = {}
    # The engine-owned fact costs nothing to supply and must be available even when no provider
    # is called — otherwise conditioning on the run's own shape would drag in a provider run.
    engine_facts = {"run_variant": run_row["variant"] or ""}
    if use:
        values = facts.gather_all(use, {
            "run_id": run_row["run_id"],
            "scope": run_row["scope_key"],
            "scope_kind": run_row["scope_kind"],
            "ability": run_row["ability"],
        })

    values.update(engine_facts)

    out: list[Fired] = []
    for h in candidates:
        matched = conditions.evaluate(h.when, values)
        trace = conditions.explain(h.when, values)
        rendered = substitute(h.body, run_row["run_id"], selector, on, values, cfg)
        f = Fired(hook=h, matched=matched, trace=trace, rendered=rendered)
        if not matched:
            out.append(f)
            continue
        if h.obligation:
            existing = store.get_obligation(conn, run_row["run_id"], h.id)
            if existing is None:
                store.add_obligation(
                    conn, run_row["run_id"], h.id, on, selector, rendered,
                    json.dumps(values, ensure_ascii=False, sort_keys=True),
                )
                f.obligation_created = True
        if h.mode == MODE_COMMAND:
            try:
                r = subprocess.run(rendered, shell=True, capture_output=True,
                                   text=True, timeout=120)
                f.command_rc = r.returncode
                f.command_out = (r.stdout + r.stderr).strip()
            except (OSError, subprocess.SubprocessError) as exc:
                f.command_rc = -1
                f.command_out = str(exc)
        out.append(f)
    return out


# ------------------------------------------------------------------ spec parsing

HOOK_KEYS = {"id", "trigger", "phase", "step", "mode", "contract", "command", "when",
             "obligation", "fail_closed"}


def parse(raw_list, flow_steps: dict, flow_phases: tuple, schema: dict,
          reject_unknown, path) -> tuple[Hook, ...]:
    """Build and validate the hook list. Every failure is fatal at load time."""
    if not isinstance(raw_list, list):
        raise HookError(f"{path}: 'hooks' must be a list")
    hooks: list[Hook] = []
    seen: set[str] = set()
    for i, raw in enumerate(raw_list):
        if not isinstance(raw, dict) or "id" not in raw:
            raise HookError(f"{path}: hooks[{i}] must be a mapping with an 'id'")
        reject_unknown(raw, HOOK_KEYS, f"hooks[{i}]", path)
        hid = str(raw["id"])
        if hid in seen:
            raise HookError(f"{path}: duplicate hook id '{hid}'")
        seen.add(hid)

        on = str(raw.get("trigger", ""))
        if on not in TRIGGERS:
            raise HookError(
                f"{path}: hook '{hid}' has trigger='{on}'; expected one of {', '.join(TRIGGERS)}"
            )

        has_phase, has_step = "phase" in raw, "step" in raw
        if has_phase == has_step:
            raise HookError(
                f"{path}: hook '{hid}' must name exactly one of 'phase' or 'step'"
            )
        if on in PHASE_TRIGGERS and not has_phase:
            raise HookError(f"{path}: hook '{hid}' watches '{on}' so it needs 'phase'")
        if on in STEP_TRIGGERS and not has_step:
            raise HookError(f"{path}: hook '{hid}' watches '{on}' so it needs 'step'")
        selector = str(raw["phase"] if has_phase else raw["step"])
        if has_phase and selector not in flow_phases:
            raise HookError(
                f"{path}: hook '{hid}' watches phase '{selector}', which is not declared"
            )
        if has_step and selector not in flow_steps:
            raise HookError(
                f"{path}: hook '{hid}' watches step '{selector}', which is not declared"
            )
        if on == "gate_recorded" and flow_steps[selector].gate == "none":
            # A gate that cannot happen means a hook that can never fire — inert, and
            # inert-but-looks-active is the shape this engine refuses everywhere else.
            raise HookError(
                f"{path}: hook '{hid}' watches gate_recorded on step '{selector}', whose "
                f"gate is 'none' — it could never fire"
            )

        mode = str(raw.get("mode", MODE_CONTRACT))
        if mode not in MODES:
            raise HookError(
                f"{path}: hook '{hid}' has mode='{mode}'; expected {', '.join(MODES)}"
            )
        body_key = "contract" if mode == MODE_CONTRACT else "command"
        if not raw.get(body_key):
            raise HookError(f"{path}: hook '{hid}' is mode '{mode}' so it needs '{body_key}'")
        other = "command" if mode == MODE_CONTRACT else "contract"
        if raw.get(other):
            raise HookError(
                f"{path}: hook '{hid}' is mode '{mode}' but also sets '{other}'; "
                f"which one runs would be arbitrary"
            )

        when = raw.get("when")
        if when is not None:
            conditions.validate(when, schema, f"{path}: hook '{hid}' when")

        obligation = bool(raw.get("obligation", False))
        fail_closed = bool(raw.get("fail_closed", False))
        if fail_closed and mode != MODE_COMMAND:
            raise HookError(
                f"{path}: hook '{hid}' sets fail_closed but is mode '{mode}'; only a "
                f"command has an exit code to fail on"
            )
        hooks.append(Hook(id=hid, on=on, selector=selector, mode=mode,
                          body=str(raw[body_key]).rstrip(), when=when,
                          obligation=obligation, fail_closed=fail_closed))
    return tuple(hooks)
