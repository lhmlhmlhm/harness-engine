"""The load-bearing test: the engine must contain NO ability vocabulary.

WHY THIS IS THE MOST IMPORTANT TEST IN THE REPO

A prior system in this space grew 575 references to 102 step ids spread across ~50
functions. Nobody added them maliciously — each one was locally reasonable ("this
check only applies to the commit step"). The aggregate outcome was an engine that
could not be separated from its first ability without a multi-day refactor, and a
generic-table scheme that stayed at zero rows because the real logic had already
fused to one consumer.

Domain vocabulary in engine code is a one-way door. This test is the door-stop: it
greps the engine for the shapes that vocabulary takes and fails the build on a hit.
It runs on every commit, so the fusion can never accumulate silently.

If this test fails, the fix is NEVER to add an exemption. It is to move the knowledge
into `abilities/<name>/flow.yaml` and reach it through a declared field or a
registered predicate.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ENGINE_DIR = Path(__file__).resolve().parent.parent / "engine"
ABILITIES_DIR = Path(__file__).resolve().parent.parent / "abilities"

# Every python + sql file that makes up the engine.
ENGINE_FILES = sorted(
    [p for p in ENGINE_DIR.glob("*.py") if p.name != "__init__.py"]
    + list(ENGINE_DIR.glob("*.sql"))
)

# A step id is a letter-cluster plus digits: C01, D02, K01, A1, E18b, CF09, U7.
# This is the shape that metastasised in the prior system, so it is the primary catch.
STEP_ID_RE = re.compile(r"\b(?:[A-Z]{1,3}\d{1,3}[a-z]?)\b")

# Step-id-shaped strings that are NOT step ids. Kept deliberately tiny — a growing
# allowlist is the same disease wearing a different hat.
STEP_ID_ALLOW = {
    "E501",   # flake8 code in a noqa comment
    "P0",     # not currently used, reserved to avoid a churn failure
    "UTC8",
}

# Distinctive domain nouns — banned as a plain substring anywhere, comments included.
# Each is a word that only exists because some ability exists.
#
# This list is about CONCEPT WORDS, and it is deliberately not the list of installed
# names: those are derived from disk by the test below, because a hardcoded roster goes
# stale silently. This one went stale exactly that way — the comment below used to read
# "the two installed abilities" while seven were installed, so five of them could have
# been named in engine code with nothing objecting.
FORBIDDEN_TOKENS = [
    "ship-check", "shipcheck", "ship_check",
    "delivery", "authoring",          # two of the installed names, kept as concept words
    "brazil", "taskei", "analyzer", "reviewer",
    "worktree", "plandoc",
]

# Words that are ordinary English AND could be domain vocabulary. Banning the bare word
# would fire on prose like "the plugin layer" and produce exactly the pressure to add
# exemptions that this file warns against. So they are banned only in IDENTIFIER form —
# joined to something by an underscore, which is what code looks like and prose does not.
#
# This distinction is the difference between a sharp detector and a noisy one. A noisy
# detector gets an allowlist; an allowlist grows; a grown allowlist is the fusion back.
FORBIDDEN_IDENTIFIER_STEMS = [
    # `layer` is one ability's NAME for a phase. `phase` itself is deliberately NOT on
    # this list: the engine defines an ordered grouping of steps and calls it a phase,
    # at the same level as run / step / gate. An ability may call its phases whatever it
    # likes in its own directives, but it maps them onto `phases:` in the spec. So
    # `steps_in_phase` is engine vocabulary and `current_layer` would be fusion.
    # Recorded because the next reader will otherwise wonder why one is banned and the
    # other is not, and will guess wrong.
    "layer",
    "commit",
    "merge",
    "plan",
    "task",
    "cr",
]


def _engine_sources() -> list[tuple[Path, str]]:
    return [(p, p.read_text(encoding="utf-8")) for p in ENGINE_FILES]


def _installed_names() -> list[str]:
    """The names actually installed, read from disk instead of listed in this file.

    Scoped to the in-tree root on purpose. The engine can now be pointed at other roots
    (`HARNESS_ABILITIES_PATH`), but this file guards THIS repository's sources, and a guard
    that asks the thing it is guarding where to look can be pointed away from the evidence.
    """
    return sorted(
        p.name for p in ABILITIES_DIR.iterdir()
        if p.is_dir() and (p / "flow.yaml").is_file()
    )


def test_engine_files_exist():
    """Guard the guard: if the glob silently matches nothing, the rest is vacuous.

    A test that passes because it examined zero files is the worst kind of green.
    """
    assert len(ENGINE_FILES) >= 4, f"expected the engine's sources, found {ENGINE_FILES}"
    names = {p.name for p in ENGINE_FILES}
    for required in ("harness.py", "flow.py", "store.py", "predicates.py", "schema.sql"):
        assert required in names, f"{required} missing from the engine source set"


@pytest.mark.parametrize("path", ENGINE_FILES, ids=lambda p: p.name)
def test_no_step_ids_in_engine(path: Path):
    """No step id may appear anywhere in engine code — including comments."""
    text = path.read_text(encoding="utf-8")
    hits: dict[str, list[int]] = {}
    for lineno, line in enumerate(text.splitlines(), 1):
        for m in STEP_ID_RE.finditer(line):
            tok = m.group(0)
            if tok in STEP_ID_ALLOW:
                continue
            hits.setdefault(tok, []).append(lineno)
    assert not hits, (
        f"{path.name} contains step-id-shaped tokens: "
        + "; ".join(f"{k} @ line(s) {v}" for k, v in sorted(hits.items()))
        + "\n\nMove this knowledge into abilities/<name>/flow.yaml. Do NOT add an "
          "exemption — an allowlist that grows is the fusion this test exists to stop."
    )


@pytest.mark.parametrize("path", ENGINE_FILES, ids=lambda p: p.name)
def test_no_domain_nouns_in_engine(path: Path):
    """No ability's distinctive domain noun may appear in engine code."""
    text = path.read_text(encoding="utf-8").lower()
    found = []
    for word in FORBIDDEN_TOKENS:
        m = re.search(re.escape(word), text)
        if m:
            found.append(f"{word!r} @ line {text[: m.start()].count(chr(10)) + 1}")
    assert not found, (
        f"{path.name} names ability-specific concepts: {', '.join(found)}\n"
        "The engine must be able to run an ability it has never heard of."
    )


@pytest.mark.parametrize("path", ENGINE_FILES, ids=lambda p: p.name)
def test_no_domain_identifiers_in_engine(path: Path):
    """Generic-English domain words are banned in identifier form.

    `current_layer` / `layer_summary` / `commit_sha` are fusion; the sentence "the
    plugin layer" is not. Matching only the underscore-joined form keeps the detector
    sharp enough that it never needs an exemption.
    """
    text = path.read_text(encoding="utf-8").lower()
    found = []
    for stem in FORBIDDEN_IDENTIFIER_STEMS:
        pattern = rf"\b\w+_{re.escape(stem)}\b|\b{re.escape(stem)}_\w+\b"
        m = re.search(pattern, text)
        if m:
            found.append(
                f"{m.group(0)!r} @ line {text[: m.start()].count(chr(10)) + 1}"
            )
    assert not found, (
        f"{path.name} uses ability vocabulary as identifiers: {', '.join(found)}\n"
        "Reach it through a declared flow field or a registered predicate instead."
    )


def test_engine_does_not_import_abilities():
    """No engine module may import from an ability, and none may read a fixed path."""
    for path, text in _engine_sources():
        assert "abilities." not in text, f"{path.name} imports from abilities/"
        # The abilities ROOT is derived (and now relocatable); a hardcoded ability NAME in
        # a path is the smell.
        for ability in _installed_names():
            assert f"/{ability}/" not in text, (
                f"{path.name} hardcodes a path into ability {ability!r}"
            )


def test_engine_does_not_name_an_installed_flow():
    """No engine source may name an installed flow — as a literal or in an identifier.

    WHY NOT A PLAIN SUBSTRING, WHICH WOULD BE STRICTER: because that is provably wrong
    here. `predicates.py` justifies a predicate's shape by quoting a real invariant — "the
    revision whose checks were verified must BE the latest revision pushed" — and one of
    the installed names is `push`. A bare-substring rule fails on English prose; a guard
    that fails on prose earns an exemption; an exemption list grows; and a grown exemption
    list is the fusion this whole file exists to prevent. So the two forms that mean CODE
    are banned instead: a quoted literal, and the name joined into an identifier.

    FORBIDDEN_TOKENS overlaps this on two names, and that is not redundancy — those
    entries ban a CONCEPT WORD anywhere including prose, which is the stronger claim, and
    it holds today. This test is the weaker claim applied to every installed name, so the
    roster can never again be five names short without anything noticing.
    """
    names = _installed_names()
    assert len(names) >= 2, "fewer than two flows installed; this test would be vacuous"
    failures = []
    for path, text in _engine_sources():
        low = text.lower()
        for name in names:
            stem = name.replace("-", "_")
            forms = (
                (rf"""['"]{re.escape(name)}['"]""", "a quoted literal"),
                (rf"\b\w+_{re.escape(stem)}\b|\b{re.escape(stem)}_\w+\b", "an identifier"),
            )
            for pattern, form in forms:
                m = re.search(pattern, low)
                if m:
                    line = low[: m.start()].count(chr(10)) + 1
                    failures.append(
                        f"{path.name}:{line} names {name!r} as {form}: {m.group(0)!r}"
                    )
    assert not failures, (
        "the engine names flows it must be able to run without having heard of:\n  "
        + "\n  ".join(failures)
        + "\n\nReach it through a declared flow field or a registered predicate."
    )


def test_no_fact_names_in_engine_logic():
    """The engine may not name a fact — same rule as step ids, one level up.

    A fact name is an ability's vocabulary for the world. `changed_files` is a reasonable
    thing for a *provider* to declare and an unreasonable thing for the condition
    evaluator to know: the moment it knows, an ability whose facts are shaped differently
    stops being expressible.

    The BUILT-IN providers are exempt for the obvious reason that declaring a schema is
    their entire job. They are checked instead by the disjoint-provider test in
    test_behaviour.py, which asserts the two shipped abilities share no fact name at all.
    """
    fact_names = set()
    for spec in ABILITIES_DIR.glob("*/flow.yaml"):
        # `fact:` appears in YAML flow style too — `{ fact: x, op: y }` — so anchoring
        # to line start made this collect nothing and the test passed vacuously.
        for m in re.finditer(r"\bfact:\s*([a-z_][a-z0-9_]*)", spec.read_text(encoding="utf-8")):
            fact_names.add(m.group(1))
    assert fact_names, "no ability declares a condition; this test would be vacuous"

    # Scoped to the CONDITION MACHINERY, and the scoping is the point:
    #   * facts.py hosts the built-in providers, whose entire job is to name a schema;
    #   * store.py / predicates.py / harness.py have their own long-standing concepts, and
    #     a provider over the engine's OWN state legitimately names a fact after them
    #     (`closed_steps` is the engine's function first and a fact second).
    # What must stay fact-blind is the part that EVALUATES conditions — the moment it
    # knows one fact's name, an ability whose facts are shaped differently stops being
    # expressible, which is the whole failure this file exists to prevent.
    # hooks.py is excluded for a specific reason: it BUILDS the context handed to a
    # provider, so it necessarily names the engine's own identity fields (the run, its
    # scope). A provider may then expose those as facts, which is a name collision rather
    # than knowledge leaking in. The line that matters is that EVALUATION is name-blind.
    machinery = ("conditions.py", "operators.py")
    logic_files = [p for p in ENGINE_FILES if p.name in machinery]
    assert len(logic_files) == len(machinery), "the condition machinery moved; re-scope this"
    for path in logic_files:
        text = path.read_text(encoding="utf-8").lower()
        for name in sorted(fact_names):
            assert name not in text, (
                f"{path.name} names the fact {name!r}, which belongs to an ability.\n"
                f"Reach it through the declared schema, never by name."
            )


def test_condition_operators_are_all_domain_free():
    """A built-in operator must be a generic comparison, not a domain question.

    `matches_any` compares globs; a hypothetical `workspace_in_scope` would be the engine
    quietly learning what a workspace is.
    """
    import sys as _s
    _s.path.insert(0, str(ENGINE_DIR.parent))
    from engine import operators as _ops
    for name in _ops.registered():
        for stem in FORBIDDEN_IDENTIFIER_STEMS + ["workspace", "file", "repo", "branch"]:
            assert stem not in name, (
                f"built-in operator {name!r} contains the domain stem {stem!r}; "
                f"a domain-specific comparison belongs in an ability's own registered operator"
            )


def test_abilities_carry_the_vocabulary():
    """The mirror of the above: vocabulary must actually live in the specs.

    Without this, the purity tests could be satisfied by an engine that does nothing.
    """
    specs = list(ABILITIES_DIR.glob("*/flow.yaml"))
    assert len(specs) >= 2, (
        "at least two unlike abilities must be installed — a framework validated "
        "against one consumer collapses into that consumer"
    )
    total_steps = 0
    for spec in specs:
        text = spec.read_text(encoding="utf-8")
        ids = {m.group(0) for m in STEP_ID_RE.finditer(text)}
        assert ids, f"{spec} declares no step ids; the vocabulary went somewhere else"
        total_steps += len(ids)
    assert total_steps >= 8, f"only {total_steps} step ids across all abilities"


# Named external tools. NOT checked as plain substrings, and the reason is measurable: `git`
# occurs inside `legitimately` (8 times in the engine), `legitimate`, and `isdigit`. A substring
# rule would fire on all of them, and a guard that fires on ordinary prose earns an exemption —
# which is the disease this file exists to prevent. So the two forms that mean CODE are banned:
# a quoted literal, and the name joined into an identifier.
DOMAIN_TOOLS = ["git", "svn", "hg", "npm", "docker", "kubectl", "brew"]

# Where the built-in fact providers live. Scoped deliberately, the same way the fact-name check
# is scoped to the condition machinery: the rule is about what a BUILT-IN provider may depend on,
# not about the engine never starting a process — it legitimately starts one to run an ability's
# provider, and a hook in command mode runs whatever its flow declares.
BUILTIN_PROVIDER_FILE = ENGINE_DIR / "facts.py"


def test_no_builtin_provider_shells_out():
    """A built-in fact provider may only report what the engine ALREADY holds.

    One used to invoke a specific version-control tool, and it survived every guard here. That
    is the case worth remembering: the no-fact-names rule EXEMPTS this file, for the sound reason
    that declaring a schema is a provider's whole job — but a tool name is not a fact name, and
    the exemption silently covered it.

    It also bypassed the engine's own seam. A provider needing an external tool declares
    `requires={"cmd": ...}`, which is what makes "I could not look" distinguishable from "nothing
    found". A built-in cannot state that meaningfully, because the engine would then depend on a
    tool for one of its own facts — so a built-in that needs one is misplaced by construction.

    Checked structurally rather than by name, so it holds for every tool and not only the one
    that happened.
    """
    text = BUILTIN_PROVIDER_FILE.read_text(encoding="utf-8")
    assert "@provider(" in text, "the built-in providers moved; re-scope this guard"
    for banned in ("subprocess", "os.system", "os.popen", "Popen"):
        assert banned not in text, (
            f"{BUILTIN_PROVIDER_FILE.name} uses {banned!r}. A built-in provider that needs an "
            f"external tool belongs to the flow that needs it, declaring requires={{'cmd': ...}} "
            f"so an absent tool is reported instead of read as an empty answer."
        )


@pytest.mark.parametrize("path", ENGINE_FILES, ids=lambda p: p.name)
def test_no_named_external_tool_in_engine(path: Path):
    """The engine may not name a specific external tool, in either code-shaped form.

    Substring matching is measurably wrong here — see the comment on DOMAIN_TOOLS — so this looks
    for a quoted literal or an underscore-joined identifier, the same discrimination the
    installed-flow-name check had to make for exactly the same reason.
    """
    low = path.read_text(encoding="utf-8").lower()
    found = []
    for tool in DOMAIN_TOOLS:
        for pattern, form in (
            (rf"""['"]{re.escape(tool)}['"]""", "a quoted literal"),
            (rf"\b\w+_{re.escape(tool)}\b|\b{re.escape(tool)}_\w+\b", "an identifier"),
        ):
            m = re.search(pattern, low)
            if m:
                line = low[: m.start()].count(chr(10)) + 1
                found.append(f"{tool!r} as {form} @ line {line}: {m.group(0)!r}")
    assert not found, (
        f"{path.name} names an external tool:\n  " + "\n  ".join(found)
        + "\n\nA flow that needs a tool declares it; the engine does not know the tool exists."
    )
