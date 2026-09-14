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


def _shipped() -> list[Path]:
    """The files that travel. Derived, not listed: a new engine module or a new fixture ability
    joins the scan by existing, which is the only way a guard like this keeps up with the tree."""
    out: list[Path] = []
    for pat in ("engine/*.py", "engine/*.sql", "bin/*", "integrations/*",
                "tests/*.py", "README.md", "pyproject.toml"):
        out += [p for p in REPO.glob(pat) if p.is_file()]
    for spec in sorted((REPO / "abilities").glob("*/flow.yaml")):
        if "role: fixture" in spec.read_text(encoding="utf-8"):
            out += [p for p in sorted(spec.parent.rglob("*")) if p.is_file()]
    assert len(out) > 20, f"the shipped-surface scan found only {len(out)} files — it is not looking"
    return sorted(set(out))


SHIPPED = _shipped()


def _lines(p: Path):
    return enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1)


@pytest.mark.parametrize("path", SHIPPED, ids=lambda p: str(p.relative_to(REPO)))
def test_no_shipped_file_names_a_home_or_vendor_rooted_path(path: Path):
    """A macOS home path reads as an instruction, and is simply false on a Linux cloud desktop."""
    bad = [(i, l.strip()) for i, l in _lines(path) if HOME_OR_VENDOR.search(l)]
    assert not bad, (
        f"{path.relative_to(REPO)} names a path rooted at a home or vendor directory:\n"
        + "\n".join(f"  line {i}: {t[:110]}" for i, t in bad)
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
