"""Test-wide safety: an in-process `store.connect()` must never reach the real store.

WHY THIS FILE EXISTS. Every test drives the CLI as a subprocess and passes HARNESS_STATE_DIR
in that subprocess's environment, so nothing leaks. A test that instead imports the engine and
calls the store IN-PROCESS reads `os.environ`, which those fixtures never touched — so it
silently connected to the developer's own `state/harness.db` and wrote rows into it. That
happened while adding the concurrency tests: three junk runs landed in the real ledger and were
only noticed because the schema there was older and the insert failed.

So every test starts with HARNESS_STATE_DIR pointing at a per-test scratch directory that is
deliberately NOT initialised. An accidental in-process use therefore fails loudly with
`StoreNotInitialised` instead of quietly succeeding somewhere real, and a test that means to
use the store in-process has to say so by pointing this at its own initialised directory.

Failing loudly rather than silently redirecting to a working temp DB is the point: a test that
passes against a store nobody chose is a test whose subject is unknown.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _never_the_real_store(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "unchosen-store"))
    # Also pin the flow root, so an in-process load cannot be steered by an outer shell.
    monkeypatch.delenv("HARNESS_ABILITIES_PATH", raising=False)
