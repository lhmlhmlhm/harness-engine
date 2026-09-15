"""Guards for RELOCATABILITY: this tree gets copied to another machine and handed to another person.

WHY THIS FILE EXISTS

A literal path under a macOS `/Users` home, written into a doc, is wrong on Linux. A literal path under a home directory is
wrong for everyone except the one person whose home it is. Neither of those fails loudly — the
reader just follows an instruction that silently does not apply, or copies a script that resolves
to nothing. And the author cannot notice either, because on the author's machine both are correct.

That is the same shape as the eight numbers that drifted in README while WIRING.md drifted in none:
correct-when-written, unwatched, and wrong later. So it gets a mechanism rather than a habit.

WHAT IS BANNED, AND WHAT DELIBERATELY IS NOT

  banned    a path rooted at a home or a vendor directory — `/Users`, `/home`, `/opt`,
            `/Library` and friends — because those encode an operating system and usually a user

            (written here without their trailing slash on purpose: with it, this file would name
            the very shape it bans and fail its own check. An exemption would have been the
            easier fix and the wrong one — the first entry on an exemption list is how a rule
            starts becoming advisory.)
  banned    this checkout's own absolute location, derived from disk so the rule cannot go stale
  banned    the current user's login name, derived from the environment for the same reason
  ALLOWED   `~/` and `$HOME/` — they relocate correctly. This engine relies on that on purpose:
            `harness require` stores a literal `~` and expands it only at comparison time, which
            is what lets one policy record work across machines.
  ALLOWED   placeholders that merely LOOK absolute — `/path/to/your/flows`, `<tree>/abilities/x`,
            `…/delivery/providers.py`. A blanket "no leading slash in docs" rule would flag these,
            and they are the correct way to write a doc. A guard that fires on good practice earns
            an exemption list, and a grown exemption list is the rule quietly leaving.

WHAT IS NOT SCANNED, AND WHY THAT IS CORRECT

Abilities that are not `role: fixture`. Those are private flows whose providers legitimately point
at real machine-local tools (`~/.kiro/...`), they are not part of what ships, and genericising them
would break them. The shipped surface is derived from disk — the engine, the entry point, the
integrations, the tests, the root docs, and the fixture abilities that serve as samples.
"""
from __future__ import annotations

import getpass
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

# A path rooted somewhere that belongs to one machine or one person. `/tmp` is absent on purpose:
# it exists on every unix and carries no identity, and banning it would force churn in test
# fixtures for no portability gain.
HOME_OR_VENDOR = re.compile(r"/(Users|home|root|Applications|Library|private|opt|usr/local)/")


# Directories that hold generated output, and suffixes that are not source. Both named rather than
# guessed: the first version of this scan used `rglob("*")` and swept up `__pycache__/*.pyc`, whose
# embedded compile-time paths tripped three guards at once — and, because this file parametrises over
# the result, changed the COLLECTED TEST COUNT depending on whether anything had run first. The
# README's generated block records that count, so its byte guard went non-deterministic with it. A
# guard that is green after a purge and red otherwise teaches people that red means "try again".
BUILD_DIRS = {"__pycache__", "build", "dist", ".egg-info", ".pytest_cache", ".mypy_cache"}
SOURCE_SUFFIXES = {".py", ".sql", ".yaml", ".yml", ".md", ".toml", ".json", ".sh", ".cfg", ""}


def _is_source(p: Path) -> bool:
    if any(part in BUILD_DIRS or part.endswith(".egg-info") for part in p.parts):
        return False
    return p.suffix in SOURCE_SUFFIXES


def _shipped() -> list[Path]:
    """The files that travel. Derived, not listed: a new engine module or a new fixture ability
    joins the scan by existing, which is the only way a guard like this keeps up with the tree.

    Source only — see BUILD_DIRS above for what that excludes and why it is spelled out.
    """
    out: list[Path] = []
    for pat in ("engine/*.py", "engine/*.sql", "bin/*", "integrations/*",
                "tests/*.py", "README.md", "pyproject.toml"):
        out += [p for p in REPO.glob(pat) if p.is_file() and _is_source(p)]
    for spec in sorted((REPO / "abilities").glob("*/flow.yaml")):
        if "role: fixture" in spec.read_text(encoding="utf-8"):
            out += [p for p in sorted(spec.parent.rglob("*")) if p.is_file() and _is_source(p)]
    assert len(out) > 20, f"the shipped-surface scan found only {len(out)} files — it is not looking"
    return sorted(set(out))


SHIPPED = _shipped()


def _lines(p: Path):
    return enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1)


def test_no_shipped_file_names_a_home_or_vendor_rooted_path():
    """A macOS home path reads as an instruction, and is simply false on a Linux cloud desktop.

    Deliberately NOT parametrised over the file list. It was, and that made the number of collected
    tests a function of the working tree — which the README's generated block records, so a stray
    file turned that block's byte guard red for a reason unrelated to the block. One test, every
    offender in the message: the per-file id read better in a failure, and a collected count that
    does not move is worth more than an id.
    """
    bad = [(p.relative_to(REPO), i, l.strip()) for p in SHIPPED for i, l in _lines(p)
           if HOME_OR_VENDOR.search(l)]
    assert not bad, (
        "these shipped files name a path rooted at a home or vendor directory:\n"
        + "\n".join(f"  {rel}:{i}  {t[:100]}" for rel, i, t in bad)
        + "\n  Use `~/` or `$HOME/` (they relocate), or a placeholder like `/path/to/x`."
    )


def test_no_shipped_file_hardcodes_this_checkouts_own_location():
    """Derived from disk, so this cannot be satisfied by moving the checkout and forgetting.

    A script that hardcodes where it lives works exactly once — on the machine it was written on.
    Everything here resolves its root from `__file__` instead, and this keeps it that way.
    """
    me = str(REPO)
    bad = [(p.relative_to(REPO), i) for p in SHIPPED for i, l in _lines(p) if me in l]
    assert not bad, (
        f"these files contain this checkout's absolute path ({me}):\n"
        + "\n".join(f"  {rel}:{i}" for rel, i in bad)
        + "\n  Resolve the root from __file__ (see integrations/render-readme.py)."
    )


def test_no_shipped_file_contains_the_authors_login_name():
    """The precise form of "shared with someone else": a name that is right for exactly one person.

    Read from the environment rather than written down — a hardcoded login would itself be the
    thing this file bans. Skipped for very short or generic logins, where the string would collide
    with ordinary words and the guard would fire on prose instead of on a path.
    """
    login = getpass.getuser()
    if len(login) < 4 or login in {"user", "root", "admin", "test", "build", "runner", "ubuntu"}:
        pytest.skip(f"login {login!r} is too generic to search for without false positives")

    # As a PATH SEGMENT, not anywhere. The first version of this check searched the whole line and
    # caught `pyproject.toml`'s author field — but a package is supposed to be signed, and a name in
    # `authors = [...]` costs the reader nothing. The harm is a name standing in for a directory.
    seg = re.compile(rf"(?:/|\bfile://|~/){re.escape(login)}(?:/|\b(?=[/\"'\s]))")
    bad = [(p.relative_to(REPO), i, l.strip()[:90]) for p in SHIPPED for i, l in _lines(p)
           if seg.search(l)]
    assert not bad, (
        f"these files use the current user ({login!r}) as a path segment, so the path resolves "
        f"for one person only:\n"
        + "\n".join(f"  {rel}:{i}  {t}" for rel, i, t in bad)
        + "\n  Use `~/` — it expands to whoever is running it."
    )


def test_a_fixture_ability_reaches_outside_its_own_directory_for_nothing():
    """What makes a fixture usable as the SAMPLE that ships: it is self-contained.

    A sample that quietly needs a file from the author's home is worse than no sample — it
    validates here and fails for the reader, at the moment they are deciding whether this engine
    works at all.
    """
    fixtures = [s.parent for s in sorted((REPO / "abilities").glob("*/flow.yaml"))
                if "role: fixture" in s.read_text(encoding="utf-8")]
    assert fixtures, "no `role: fixture` ability on disk — there is no sample to ship"
    outward = re.compile(r"~/|\$HOME|\.\./\.\.")
    bad = []
    for d in fixtures:
        for p in sorted(d.rglob("*")):
            if p.is_file():
                bad += [(p.relative_to(REPO), i, l.strip()[:90])
                        for i, l in _lines(p) if outward.search(l)]
    assert not bad, (
        "a fixture ability reaches outside its own directory:\n"
        + "\n".join(f"  {rel}:{i}  {t}" for rel, i, t in bad)
        + "\n  A shipped sample has to run for someone who has none of the author's files."
    )


def test_a_vendored_tool_does_not_point_back_at_where_it_came_from():
    """A copy that still names its ORIGIN is a copy in name only.

    Measured, on the copy this test was written for: three vendored shell scripts carried
    `DIR="$HOME/.kiro/tools/cr-watch"` and a python helper read the origin's `resolved.jsonl`. The
    vendored poller would therefore have executed the ORIGINAL scripts and written the ORIGINAL
    state — every path resolved, nothing errored, and the move would have been cosmetic with nothing
    saying so. `state/` under an ability is git-ignored, so not even a stray file would have shown.

    Scoped to `~/.kiro/tools/` and `~/.kiro/skills/` — the two places tools get vendored FROM. Other
    `~/.kiro/` reads are legitimate and declared (a runtime's session store, its MCP settings), and
    banning those would turn this into an exemption list. Comments are exempt: a line recording
    where a file came from is history, and deleting history to green a guard is the wrong trade.
    """
    import re
    origin = re.compile(r"(~|\$HOME|\$\{HOME\})/\.kiro/(tools|skills)/")
    bad = []
    for tools in sorted((REPO / "abilities").glob("*/tools")):
        for f in sorted(tools.rglob("*")):
            if not f.is_file() or not _is_source(f):
                continue
            for i, line in _lines(f):
                if line.lstrip().startswith(("#", "//")):
                    continue
                m = origin.search(line)
                if not m:
                    continue
                # Backticked = this tree's convention for TALKING ABOUT a path (docstrings explain
                # the layout that made a bug possible). Bare = using it. Checking the convention
                # rather than listing exemptions is what keeps the guard from becoming a list.
                before, after = line[:m.start()], line[m.end():]
                if before.count("`") % 2 == 1 and "`" in after:
                    continue
                # An OVERRIDABLE default is declared, not baked in: `${VAR:-<path>}` is the form the
                # rest of this tree uses for "here unless you say otherwise", and a copy that can be
                # pointed elsewhere is not a copy that resolves back to its origin.
                if re.search(r"\$\{[A-Z_]+:-[^}]*$", before):
                    continue
                bad.append(f"  {f.relative_to(REPO)}:{i}  {line.strip()[:74]}")
    assert not bad, (
        "a vendored tool resolves back to the tree it was copied from:\n" + "\n".join(bad)
        + "\n  Resolve from `$0` / `__file__` instead — otherwise the copy runs the original."
    )


def test_no_personal_ability_set_is_tracked():
    """`workspace/<name>/` holds somebody's OWN abilities. This repository ships the engine.

    The two live in one checkout deliberately — the trust boundary is an `ENGINE_TREE` prefix check,
    so an ability set nested here is implicitly trusted (it is its owner's own code) while the same
    files in a sibling directory must be approved one at a time and re-approved on every edit.
    Measured before choosing the layout: nine of twelve abilities failed to load out of tree.

    The cost of that convenience is exactly one accidental `git add -A`, which is what this catches.
    Checked through git rather than by reading `.gitignore`, because an ignore rule added AFTER a
    file was tracked does not untrack it — and that is the state this would be silent in.
    """
    import subprocess
    out = subprocess.run(["git", "ls-files", "workspace"], cwd=str(REPO),
                         capture_output=True, text=True)
    tracked = [l for l in out.stdout.splitlines() if l.strip()]
    assert not tracked, (
        "this repository is tracking somebody's personal ability set:\n"
        + "\n".join("  " + t for t in tracked[:20])
        + "\n  `git rm --cached` them; `workspace/` is ignored for this reason."
    )


def test_the_public_suite_does_not_collect_a_personal_one():
    """pytest recurses from the rootdir, so `workspace/<name>/tests/` joins the run unless scoped.

    Measured the day the split landed: twenty tests whose subject is a personal flow ran inside the
    engine's suite, and the engine's own pass/fail number silently included them. That is the exact
    coupling the split exists to remove, and it comes back with one deleted line of config.

    Asserted by ASKING pytest what it would collect, not by reading the config: `testpaths` can be
    overridden on the command line, and a suite that only checks the file has checked the wrong thing.
    """
    import subprocess
    import sys
    out = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q",
                          "-p", "no:cacheprovider"],
                         cwd=str(REPO), capture_output=True, text=True)
    leaked = sorted({l.split("::")[0] for l in out.stdout.splitlines()
                     if l.startswith("workspace/")})
    assert not leaked, (
        "the engine's suite is collecting a personal ability set's tests:\n"
        + "\n".join("  " + f for f in leaked)
        + "\n  `testpaths` in pyproject.toml scopes this; see the comment there for what it cost."
    )
