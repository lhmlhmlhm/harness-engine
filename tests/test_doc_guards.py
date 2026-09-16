"""Guards for the hand-written integration docs.

WHY THIS FILE EXISTS, AND WHY IT DID NOT BEFORE

`integrations/CAPABILITIES.md` had assertions in the suite. `integrations/WIRING.md` had none — and
WIRING.md is the one that drifted, in four places, every one of them a number that was correct when
written. The document that nothing was watching is the document that aged. That is the same shape
this project corrects everywhere else, so it is corrected here rather than noted.

TWO KINDS OF GUARD, BECAUSE THERE ARE TWO KINDS OF CLAIM

  * DERIVABLE — a count, a size, a path the engine can produce. Those are not guarded, they are
    GENERATED: `integrations/render-wiring.py` renders them into a marked block, and the first test
    below asserts the file matches what re-rendering produces. A generated fact cannot be stale
    without failing; a guarded one can only be caught being stale.
  * JUDGMENT — why the hook is the only enforcement point, why the isolation axis is a scope. Not
    derivable, must stay prose, and nothing here tries to check it.

Between those sits a third kind, which is what the middle tests cover: a claim that is neither a
number nor judgment, but a REFERENCE — a command, a flag, a path, an env var, a constant, a quoted
message. Those are checkable by existence, and a doc naming something that no longer exists is the
most misleading kind of stale, because it reads as instruction.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
HARNESS = REPO / "bin" / "harness"
# `integrations/CAPABILITIES.md` used to be here. It left the engine's tree: its content was one
# person's routing map, and the path four agent configs read was a symlink INTO this repository — so
# publishing the engine published that map. What replaced it is `agents/<name>.yaml`, which is checked
# by `harness onboard` rather than by a doc guard, because a schema the engine resolves against the
# installed set is a stronger check than any assertion about prose could be.
DOCS = ("README.md", "integrations/WIRING.md")
RENDERER = REPO / "integrations" / "render-wiring.py"
BEGIN = "<!-- BEGIN GENERATED"
END = "<!-- END GENERATED -->"

# Every doc that has a generated block, with the renderer that owns it. Parametrised rather than
# duplicated: WIRING.md got these two guards first and README.md then drifted in 7 places while
# WIRING.md drifted in 0 — so the guards are the asset, and a third generated doc should inherit
# them by adding one line here, not by someone remembering to copy two tests.
GENERATED = (
    ("integrations/WIRING.md", "integrations/render-wiring.py"),
    ("README.md", "integrations/render-readme.py"),
)

# References a doc names that are NOT this project's, with the reason. Declared rather than
# skipped: an unexplained exemption is how a check stops meaning anything, and a NEW unknown
# reference must fail rather than join a silent allowance.
FOREIGN = {
    "CANONICAL_STEPS": "a registry in the SOURCE system this flow was transcribed from; the doc "
                       "names it as that system's, and quotes its file name in the same sentence",
    "LAYER_MANDATORY_GATE": "same source system — one of the three tables its gates were spread "
                            "across, which is the point being made",
    "LIFECYCLE_TRANSITION_GATES": "same source system, second of those three tables",
    "STEP_PRECONDITIONS": "same source system, where its deps came from",
    "HARNESS_ENGINE_HOME": "belongs to the MACHINE's wiring, not the engine: it tells a PATH "
                           "wrapper which checkout to hand a call to, and the engine resolves its "
                           "own root from __file__ and must never read it. Adding it to engine "
                           "source purely to satisfy this guard would be the doc wagging the code",
    "HARNESS_WORKTREE_ROOT": "belongs to a PERSONAL ability set, not the engine. The tool that "
                             "reads it provisions brazil-flavoured worktrees and lives in "
                             "`workspace/<name>/abilities/`, outside this repository — which is the "
                             "whole point of the split. The doc names it because the wiring it "
                             "describes is the machine's, and a reader wiring their own set needs "
                             "the name; adding it to engine source to satisfy this guard would put "
                             "a personal concern back into the engine",
    "HARNESS_WIRE_NO_POLICY": "belongs to the MACHINE's wiring too: it opts the wiring command out "
                              "of maintaining the mandatory-flow record. The engine only READS that "
                              "record and must not know that something else writes it, so this name "
                              "cannot live in engine source without inverting the ownership",
}


def _cli(*args, cwd=None, env_extra=None):
    env = {**os.environ, "HARNESS_STATE_DIR": str(REPO / "build" / "doc-guard-state")}
    env.update(env_extra or {})
    return subprocess.run([sys.executable, str(HARNESS), *args],
                          capture_output=True, text=True, cwd=str(cwd or REPO), env=env)


# ------------------------------------------------------------------ derivable → generated

@pytest.mark.parametrize("doc,renderer", GENERATED)
def test_the_generated_block_matches_what_re_rendering_produces(doc, renderer):
    """The facts that drifted are now generated, and this is what makes that mean something.

    Without it, "generated" would only describe how the text got there the first time.
    """
    proc = subprocess.run([sys.executable, str(REPO / renderer)],
                          capture_output=True, text=True, cwd=str(REPO))
    assert proc.returncode == 0, proc.stderr
    rendered = proc.stdout.strip()

    text = (REPO / doc).read_text(encoding="utf-8")
    assert BEGIN in text and END in text, f"{doc}'s generated block markers are gone"
    start = text.index(BEGIN)
    in_file = text[start:text.index(END, start) + len(END)].strip()

    assert in_file == rendered, (
        f"{doc}'s generated block is not what the renderer produces.\n"
        f"  regenerate: python3 {renderer} --write"
    )


@pytest.mark.parametrize("doc,renderer", GENERATED)
def test_re_rendering_is_idempotent(doc, renderer):
    """A generator that changes the file every run makes the guard above unusable — it would fail
    on a clean tree, and a guard that cries wolf is turned off."""
    path = REPO / doc
    before = path.read_text(encoding="utf-8")
    try:
        proc = subprocess.run([sys.executable, str(REPO / renderer), "--write"],
                              capture_output=True, text=True, cwd=str(REPO))
        assert proc.returncode == 0, proc.stderr
        assert path.read_text(encoding="utf-8") == before, f"{renderer} --write is not idempotent"
    finally:
        path.write_text(before, encoding="utf-8")


def test_the_readme_block_survives_the_tree_being_published(tmp_path):
    """README's block promises every number still holds once `abilities/` is cut to a sample.

    Rehearsed rather than trusted: stage the tree as it would ship — engine, tests, integrations,
    and ONLY the `role: fixture` abilities — render there, and require the same bytes. A docstring
    saying "engine facts only" would not catch the first row that quietly reads the installed set,
    and that mistake is one copy-paste away: `render-wiring.py` has such a row BY DESIGN (its
    "biggest installed flow"). Publishing would silently turn that kind of row into a confident
    wrong number, which is worse than an absent one.
    """
    stage = tmp_path / "as-published"
    for rel in ("engine", "bin", "tests", "integrations"):
        shutil.copytree(REPO / rel, stage / rel)
    # README.md and LICENSE are part of what ships, and leaving them out was a real bug in an
    # earlier version of this rehearsal: `test_portability.py` parametrises over the shipped
    # surface, so an absent README.md removed one test case, changed the collected count, and
    # broke the byte comparison for a reason that had nothing to do with abilities.
    for name in ("pyproject.toml", "README.md", "LICENSE"):
        shutil.copy2(REPO / name, stage / name)

    (stage / "abilities").mkdir()
    fixtures = [p.parent for p in sorted((REPO / "abilities").glob("*/flow.yaml"))
                if "role: fixture" in p.read_text(encoding="utf-8")]
    assert fixtures, "no `role: fixture` ability on disk — this rehearsal needs a sample to keep"
    for d in fixtures:
        shutil.copytree(d, stage / "abilities" / d.name)

    here = subprocess.run([sys.executable, str(REPO / "integrations/render-readme.py")],
                          capture_output=True, text=True, cwd=str(REPO))
    there = subprocess.run([sys.executable, str(stage / "integrations/render-readme.py")],
                           capture_output=True, text=True, cwd=str(stage))
    assert there.returncode == 0, f"the renderer does not even run on a published tree:\n{there.stderr}"
    assert there.stdout == here.stdout, (
        "README's generated block changes when the tree is cut down to its sample abilities, so at "
        "least one row is derived from the installed ability set. Move that row out of README — "
        "`harness abilities` is where an installation-specific count belongs."
    )


def test_the_readme_block_still_carries_every_row_it_is_meant_to():
    """Guard the block's INVENTORY, not just its bytes.

    The byte guard above cannot see a row being deleted — remove a row and re-render, and both
    files agree again. But a deleted row is exactly how a number goes back to being hand-copied
    into prose. So each source of truth the block cites is asserted present by name.
    """
    text = (REPO / "README.md").read_text(encoding="utf-8")
    start = text.index(BEGIN)
    block = text[start:text.index(END, start)]
    for cited in ("engine/*.py", "bin/harness", "engine/schema.sql", "flow.SPEC_MAJOR",
                  "predicates._REGISTRY", "proof._WITNESSES", "facts._PROVIDERS",
                  "operators._OPERATORS", "flow.ENGINE_CAPABILITIES", "flow.SCOPE_MATCH_MODES",
                  "harness.GUARD_VERDICTS", "harness.PROSE_ONLY", "harness.NO_JSON_YET",
                  "pytest --collect-only"):
        assert cited in block, (
            f"README's generated block no longer cites `{cited}`. If that fact stopped being "
            f"derivable, say so; if it just moved, it moved into prose, which is where the drift "
            f"came from."
        )


def test_a_hand_written_total_test_count_carries_the_date_it_was_measured():
    """The one test count that must NOT be generated, and why it must carry a date instead.

    "The whole suite passes on 3.10.16" cannot be regenerated: the suite runs on ONE interpreter
    and never verifies another, so re-rendering that number would fabricate a run that did not
    happen. It is a record of a past measurement. But an undated "passes and all green" is
    unfalsifiable — a reader cannot tell whether it is from this week or last quarter. So the
    rule is: a hand-written TOTAL goes with a date, or it does not go in.

    Subset counts ("the 5 mcp tests") are not totals and are not asked for a date.
    """
    text = (REPO / "README.md").read_text(encoding="utf-8")
    start, stop = text.index(BEGIN), text.index(END) + len(END)
    outside = text[:start] + text[stop:]

    totals = [m for m in re.finditer(r"整套\s*(\d+)\s*个测试", outside)]
    assert totals, ("no hand-written total test count found — if the 3.10 sentence was reworded "
                    "away, drop this guard deliberately rather than leaving it to pass on nothing")
    for m in totals:
        window = outside[m.start():m.start() + 400]
        assert re.search(r"20\d\d-\d\d-\d\d", window), (
            f"{m.group(0)!r} states a whole-suite result with no measurement date within the "
            f"following sentences. Add the date you actually ran it, or remove the number."
        )


def test_the_facts_that_are_generated_are_not_also_restated_in_prose():
    """The document's OWN rule, made binding: it does not restate what a command produces.

    Each of these is one of the four that aged. A number carrying the same unit outside the block
    is a second copy, and a second copy is exactly what generating one was supposed to end.
    """
    text = (REPO / "integrations/WIRING.md").read_text(encoding="utf-8")
    start, stop = text.index(BEGIN), text.index(END) + len(END)
    outside = text[:start] + text[stop:]

    for pattern, what in [
        (r"\d+\s*条判断规则", "the judgment-rule count"),
        (r"\d+\s*KB\s*spec", "the spec size"),
        (r"\d{3,}\s*行\s*\n?散文", "the prose line count"),
        (r"(translation|resilience)[^\n]{0,40}?\d+\s*条", "an adapter-contract case count"),
    ]:
        m = re.search(pattern, outside)
        assert m is None, (
            f"{what} is restated in prose: {m.group(0)!r}\n"
            f"  It is in the generated block already — point at the table instead."
        )


# ------------------------------------------------------------------ references → existence

def _ground_truth():
    sys.path.insert(0, str(REPO))
    from engine.harness import build_parser
    p = build_parser()
    subs = [a for a in p._actions if isinstance(a, argparse._SubParsersAction)][0]
    flags = {name: {o for a in sub._actions for o in getattr(a, "option_strings", [])}
             for name, sub in subs.choices.items()}
    # Python AND the shell tools that ship with it. The scan used to be python-only, so a variable
    # read by `abilities/*/tools/*.sh` looked absent and the only way to green the doc was to declare
    # it FOREIGN — which would have been false, and a false exemption is how an exemption list stops
    # meaning anything. Derived by existing, like the shipped-surface scan in test_portability.
    src = "\n".join(f.read_text(encoding="utf-8", errors="replace") for f in sorted(
        list((REPO / "engine").glob("*.py"))
        + list((REPO / "integrations").glob("*.py"))
        + list((REPO / "abilities").glob("*/*.py"))
        + [f for f in (REPO / "bin").glob("*") if f.is_file()]
        + [f for f in (REPO / "abilities").glob("*/tools/*") if f.is_file()]))
    return set(subs.choices), flags, src


@pytest.mark.parametrize("doc", DOCS)
def test_every_command_and_flag_a_doc_names_exists(doc):
    """A doc naming a command that was renamed reads as an instruction and fails as one."""
    cmds, flags, _ = _ground_truth()
    bad = []
    for lineno, line in enumerate((REPO / doc).read_text(encoding="utf-8").splitlines(), 1):
        for m in re.finditer(r"harness\s+([a-z][a-z-]{2,})\b((?:\s+--[a-z-]+)*)", line):
            cmd, tail = m.group(1), m.group(2)
            if cmd not in cmds:
                bad.append(f"{doc}:{lineno} no such subcommand: {cmd}")
                continue
            for fm in re.finditer(r"--[a-z-]+", tail):
                if fm.group(0) not in flags[cmd]:
                    bad.append(f"{doc}:{lineno} {cmd} has no flag {fm.group(0)}")
    assert not bad, "\n".join(bad)


@pytest.mark.parametrize("doc", DOCS)
def test_every_path_env_and_constant_a_doc_names_exists(doc):
    _, _, src = _ground_truth()
    bad = []
    for lineno, line in enumerate((REPO / doc).read_text(encoding="utf-8").splitlines(), 1):
        for m in re.finditer(r"`((?:engine|integrations|tests|abilities|bin)/[\w./*-]+)`", line):
            raw = m.group(1)
            ok = bool(list(REPO.glob(raw))) if "*" in raw else (REPO / raw).exists()
            if not ok:
                bad.append(f"{doc}:{lineno} path does not exist: {raw}")
        for m in re.finditer(r"\bHARNESS_[A-Z_]+\b", line):
            # `FOREIGN` applies here for the same reason it applies to constants below: this doc's
            # subject includes wiring that is deliberately NOT the engine's, and a variable owned by
            # that wiring cannot be satisfied by adding it to engine source. Undeclared still fails.
            if m.group(0) in FOREIGN or m.group(0) in src:
                continue
            bad.append(f"{doc}:{lineno} env var in no source file and not declared foreign: "
                       f"{m.group(0)}")
        for m in re.finditer(r"`([A-Z][A-Z0-9_]{4,})`", line):
            name = m.group(1)
            if name in FOREIGN or re.search(rf"\b{re.escape(name)}\b", src):
                continue
            bad.append(f"{doc}:{lineno} constant in no source file and not declared foreign: "
                       f"{name}")
    assert not bad, "\n".join(bad)


def test_every_foreign_reference_is_declared_with_a_reason():
    """An exemption without a reason is how a check quietly stops applying."""
    for name, why in FOREIGN.items():
        assert len(why) > 40, (name, why)


# ------------------------------------------------------------------ quoted behaviour

def test_the_section_the_doc_tells_you_to_slice_out_of_brief_exists():
    """WIRING.md hands the reader a `sed` range. If that heading is renamed the command silently
    prints nothing, which reads as "no tools need covering" — the worst possible wrong answer,
    because an uncovered tool is never checked and that failure is already silent."""
    doc = (REPO / "integrations/WIRING.md").read_text(encoding="utf-8")
    m = re.search(r"sed -n '/([^/]+)/,\$p'", doc)
    assert m, "the doc no longer slices a section out of brief; drop this guard or fix it"
    heading = m.group(1)
    out = _cli("brief")
    assert out.returncode == 0, out.stderr
    assert heading in out.stdout, f"`harness brief` has no section named {heading!r}"


def test_the_adapter_contract_keys_the_doc_names_are_really_there():
    out = _cli("adapter-contract")
    assert out.returncode == 0, out.stderr
    got = json.loads(out.stdout)
    doc = (REPO / "integrations/WIRING.md").read_text(encoding="utf-8")
    for key in ("io", "translation", "resilience", "surface", "end_to_end"):
        if f"`{key}`" in doc or f"{key} ——" in doc:
            assert key in got, f"the doc names adapter-contract's {key!r}; it is not in the output"
    # The doc's specific claim about where guard-tool lives, and why.
    assert "surface.not_a_tool" in doc
    assert "guard-tool" in got["surface"]["not_a_tool"]


def test_only_one_exit_code_becomes_a_block():
    """The doc states it as the rule an adapter must implement, and it is the one an adapter is
    most likely to get wrong in the dangerous direction."""
    got = json.loads(_cli("adapter-contract").stdout)
    # On the explicit field, not on the word appearing somewhere in the case: a substring search
    # would also pass on a case whose PROSE mentions blocking, which is how a guard ends up
    # asserting nothing.
    blocking = [c for c in got["translation"] if c["expect"] == "block"]
    assert len(blocking) == 1, f"expected exactly one blocking translation case, got {blocking}"
    assert blocking[0]["engine_exit"] == 4, blocking[0]
    assert all(c["expect"] == "allow" for c in got["translation"] if c is not blocking[0])


@pytest.mark.parametrize("quoted", [
    "has no harness.db",                       # the adapter's state-dir warning
    "guard NOT enforced",
])
def test_the_adapter_message_the_doc_quotes_is_really_in_the_adapter(quoted):
    """A quoted message is a promise about what the operator will see."""
    doc = (REPO / "integrations/WIRING.md").read_text(encoding="utf-8")
    assert quoted in doc, f"the doc no longer quotes {quoted!r}; drop this parameter or fix it"
    adapter = (REPO / "integrations/kiro-pretooluse.py").read_text(encoding="utf-8")
    assert quoted in adapter, f"the doc quotes {quoted!r} but the adapter does not print it"


@pytest.mark.parametrize("quoted", [
    "whose flow it CANNOT READ",
    "there is no guard",
])
def test_the_engine_message_the_doc_quotes_is_really_in_the_engine(quoted):
    doc = (REPO / "integrations/WIRING.md").read_text(encoding="utf-8")
    assert quoted in doc, f"the doc no longer quotes {quoted!r}; drop this parameter or fix it"
    src = (REPO / "engine/harness.py").read_text(encoding="utf-8")
    assert quoted in src, f"the doc quotes {quoted!r} but the engine does not print it"


def test_the_two_brief_renderings_really_refuse_to_be_asked_for_together():
    """The doc says asking for both is refused, and gives the reason: the two have different
    readers, so letting one win silently produces the unusable file."""
    doc = (REPO / "integrations/WIRING.md").read_text(encoding="utf-8")
    assert "同时给会被拒" in doc
    out = _cli("brief", "--write", "--portable")
    assert out.returncode != 0, "asking for both renderings was accepted"


def test_the_default_state_dir_the_doc_states_is_the_one_the_engine_uses():
    """This is the exact claim the doc admits having got wrong once, when the default moved."""
    sys.path.insert(0, str(REPO))
    from engine import store
    doc = (REPO / "integrations/WIRING.md").read_text(encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k != "HARNESS_STATE_DIR"}
    env.pop("XDG_STATE_HOME", None)
    proc = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0,'.'); from engine import store; print(store.state_dir())"],
        capture_output=True, text=True, cwd=str(REPO), env=env)
    actual = proc.stdout.strip()
    assert actual, proc.stderr
    assert actual.replace(str(Path.home()), "~") in doc.replace("`", ""), (
        f"the engine's default state dir is {actual}, which the doc does not state"
    )


def test_every_env_var_the_engine_reads_is_named_in_some_doc():
    """The other direction, and it was missing — which is how a new one got in unmentioned.

    Its sibling above checks that a doc naming `HARNESS_X` is not naming something imaginary. That is
    one of two ways this can go wrong, and the docstring of this whole file only anticipated that one.

    The symmetric failure is quieter and arguably worse: a variable the engine READS that no document
    mentions. A doc pointing at something absent is caught by the first reader who tries it; a knob
    nobody wrote down is found only by whoever needed it, after they have failed to find it. Measured:
    `HARNESS_AGENTS_PATH` was added to the engine and no test said a word, and two older ones had been
    undocumented for longer.

    Deliberately satisfied by ANY doc rather than by a specific one. Which document a variable belongs
    in is an editorial judgment, and pinning it here would turn a coverage check into a filing rule —
    the second one gets worked around by moving a line, and stops meaning anything.
    """
    # COMMENTS ARE STRIPPED FIRST, because a variable NAMED in an explanation is not a variable the
    # engine reads. Measured: a comment quoting a sample's own knob as an example of an error message
    # made this guard demand that the engine's docs adopt it. A check that fires on prose gets
    # answered by editing prose, and the next real omission would land in a test everyone had already
    # learned to argue with.
    #
    # String literals are KEPT — `os.environ.get("HARNESS_X")` is exactly how one is read, and the
    # name lives in a literal there.
    src = "\n".join(
        re.sub(r"#.*", "", (REPO / "engine" / f.name).read_text(encoding="utf-8"))
        for f in sorted((REPO / "engine").glob("*.py")))
    read = set(re.findall(r"\bHARNESS_[A-Z_]+\b", src))
    assert read, "no env vars found in engine source; this test asserted nothing"

    documented: set = set()
    for doc in (*DOCS, "integrations/DRIVING.md"):
        documented |= set(re.findall(r"\bHARNESS_[A-Z_]+\b",
                                     (REPO / doc).read_text(encoding="utf-8")))

    missing = sorted(read - documented)
    assert not missing, (
        f"the engine reads these and no doc names them: {missing}\n"
        f"  A variable nobody wrote down is found only by whoever needed it, after failing to.\n"
        f"  Name it in one of: {', '.join((*DOCS, 'integrations/DRIVING.md'))}")
