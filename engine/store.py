"""Store — the engine's own SQLite state.

Ability-agnostic by construction: every value that crosses this boundary is opaque
TEXT. The store never asks what a step id means.

Location: `$HARNESS_STATE_DIR/harness.db`, defaulting to `<repo>/state/harness.db`.
The engine owns it outright and it is never a symlink into a consumer's tree.
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ENGINE_DIR = Path(__file__).resolve().parent
REPO_ROOT = ENGINE_DIR.parent
SCHEMA_PATH = ENGINE_DIR / "schema.sql"

# The shape `schema.sql` currently describes. Bump it together with a migration below, never
# alone — `init()` refuses if the two disagree, because a bump without its migration turns
# every subsequent command into a version-mismatch refusal nobody can act on.
SCHEMA_VERSION = 1

# How to carry an OLDER store forward. Ordered, applied once each, one transaction apiece.
#
# WHAT BELONGS HERE: anything `CREATE ... IF NOT EXISTS` cannot do by itself. A NEW table needs
# no migration — re-running the baseline creates it. A dropped or altered one does, because
# re-running the baseline is silent about things that should no longer exist or should now look
# different, so the change would land on new machines and not on upgraded ones.
MIGRATIONS: tuple[tuple[int, str, tuple[str, ...]], ...] = (
    (
        1,
        "drop the open-runs view: it lost its only reader when the run listing began needing "
        "run metadata, and a view left behind is a shape the code no longer knows about",
        ("DROP VIEW IF EXISTS v_open_runs;",),
    ),
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def state_dir() -> Path:
    return Path(os.environ.get("HARNESS_STATE_DIR", REPO_ROOT / "state"))


def db_path() -> Path:
    return state_dir() / "harness.db"


def connect(*, read_only: bool = False) -> sqlite3.Connection:
    """Open the store. `init()` must have run first — no lazy table creation.

    Lazy creation is tempting and wrong: it turns "the DB was never initialised"
    (loud, fixable) into "the DB exists but is empty" (silent, and every read
    returns nothing as if the run legitimately did not exist).

    The schema version is checked here for the same reason, in both directions. A store
    whose shape does not match the code is the silent-divergence failure one level up: reads
    return plausible answers about a shape that is no longer the one in use.
    """
    path = db_path()
    if not path.is_file():
        raise StoreNotInitialised(path)
    if read_only:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
    else:
        conn = sqlite3.connect(path, timeout=5.0)
        conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    found = schema_version(conn)
    if found != SCHEMA_VERSION:
        conn.close()
        if found < SCHEMA_VERSION:
            raise StoreNeedsMigration(path, found)
        raise StoreFromNewerEngine(path, found)
    return conn


class StoreUnusable(RuntimeError):
    """The store cannot be used as it stands. Callers map every case to one exit code."""


class StoreNotInitialised(StoreUnusable):
    def __init__(self, path: Path):
        super().__init__(
            f"harness store not initialised at {path}\n"
            f"  run: harness init"
        )
        self.path = path


class StoreNeedsMigration(StoreUnusable):
    def __init__(self, path: Path, found: int):
        super().__init__(
            f"store at {path} is at schema {found}; this engine expects {SCHEMA_VERSION}\n"
            f"  run: harness init   (it creates OR migrates, and is safe to re-run)"
        )
        self.path, self.found = path, found


class StoreFromNewerEngine(StoreUnusable):
    def __init__(self, path: Path, found: int):
        super().__init__(
            f"store at {path} is at schema {found}, which is NEWER than this engine's "
            f"{SCHEMA_VERSION}.\n"
            f"  Refusing rather than migrating downwards: the newer engine wrote shapes this\n"
            f"  one does not know about, and operating on them would produce plausible\n"
            f"  answers about a store it is misreading.\n"
            f"  Upgrade the engine, or point HARNESS_STATE_DIR at a different store."
        )
        self.path, self.found = path, found


def schema_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def _apply_migrations(conn: sqlite3.Connection) -> list[int]:
    """Bring an EXISTING store up to date, one migration per exclusive transaction.

    Each one re-reads the version inside its own lock, so two processes running `init` at the
    same time cannot both apply the same step — the loser sees the winner's version and skips.
    """
    applied: list[int] = []
    prev = conn.isolation_level
    conn.isolation_level = None
    try:
        for version, _why, statements in MIGRATIONS:
            conn.execute("BEGIN IMMEDIATE")
            try:
                if schema_version(conn) >= version:
                    conn.execute("ROLLBACK")
                    continue
                for sql in statements:
                    conn.execute(sql)
                conn.execute(f"PRAGMA user_version = {int(version)}")
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            applied.append(version)
    finally:
        conn.isolation_level = prev
    return applied


def init() -> tuple[Path, list[int]]:
    """Create the store, or migrate an existing one. Returns (path, migrations applied).

    WAL is set outside a transaction on purpose: `PRAGMA journal_mode=WAL` cannot run
    inside one, and that failure only ever surfaces on a brand-new database — the
    exact path a fresh install takes and a developed-in-place one never does.

    WHY A FRESH STORE IS STAMPED RATHER THAN MIGRATED. `schema.sql` is kept at the LATEST
    shape, because it is also the readable explanation of that shape — every table there
    carries the reasoning for its columns. So a brand-new store gets the current schema and is
    stamped at the current version; migrations exist only to carry an OLD store forward.

    THE HAZARD THAT CREATES, STATED PLAINLY: a migration and an edit to `schema.sql` must have
    the same effect, and nothing about writing them enforces that. Divergence would be silent
    and would split the population in two — new machines correct, upgraded ones subtly not.
    A test closes it by building a store both ways and comparing the resulting schema; that
    test is the reason this arrangement is safe rather than merely convenient.
    """
    state_dir().mkdir(parents=True, exist_ok=True)
    path = db_path()
    fresh = not path.is_file()
    conn = sqlite3.connect(path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        if not fresh and not core_tables(conn):
            fresh = True  # the file exists but holds nothing; treat it as new, not as v0
        found = schema_version(conn)
        if not fresh and found > SCHEMA_VERSION:
            raise StoreFromNewerEngine(path, found)
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        conn.commit()
        if fresh:
            conn.execute(f"PRAGMA user_version = {int(SCHEMA_VERSION)}")
            conn.commit()
            return path, []
        applied = _apply_migrations(conn)
        if schema_version(conn) != SCHEMA_VERSION:
            # Reachable only if MIGRATIONS does not reach SCHEMA_VERSION — a bump without its
            # migration. Loud here beats every later command refusing with a version mismatch
            # nobody can act on.
            raise RuntimeError(
                f"schema {SCHEMA_VERSION} is declared but migrations only reach "
                f"{schema_version(conn)}; add the missing migration"
            )
        return path, applied
    finally:
        conn.close()


def core_tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {r["name"] for r in rows}


# ---------------------------------------------------------------- runs

def open_run(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    ability: str,
    flow_digest: str,
    title: str | None,
    scope_kind: str,
    scope_key: str,
    variant: str | None = None,
    metadata: dict | None = None,
    commit: bool = True,
) -> None:
    ts = now_iso()
    conn.execute(
        "INSERT INTO run (run_id, ability, flow_digest, title, scope_kind, scope_key,"
        " variant, status, opened_at, updated_at, metadata_json)"
        " VALUES (?,?,?,?,?,?,?,'open',?,?,?)",
        (run_id, ability, flow_digest, title, scope_kind, scope_key, variant, ts, ts,
         json.dumps(metadata or {}, ensure_ascii=False)),
    )
    if commit:
        conn.commit()


def get_run(conn: sqlite3.Connection, run_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM run WHERE run_id = ?", (run_id,)).fetchone()


def open_runs_in_scope(
    conn: sqlite3.Connection, scope_kind: str, scope_key: str
) -> list[sqlite3.Row]:
    """Every open run in one scope — plural on purpose.

    The caller must decide what to do when there is more than one. Collapsing that
    here with `LIMIT 1` and an ORDER BY is how a guard ends up confidently demanding
    the wrong run's gate, which is worse than demanding none: it pressures whoever
    hit it into forging the gate that would let them past.
    """
    return conn.execute(
        "SELECT * FROM run WHERE scope_kind = ? AND scope_key = ? AND status = 'open'"
        " ORDER BY updated_at DESC",
        (scope_kind, scope_key),
    ).fetchall()


def purge_run(conn: sqlite3.Connection, run_id: str, reason: str) -> dict:
    """Delete a CLOSED run and everything hanging off it, leaving a purge record.

    Closed-only, on purpose. An open run's violations are live evidence; being able to erase
    them by closing nothing and purging would make the ledger meaningless. And the purge itself
    is recorded, because "the row is gone" and "the row was never written" must not look alike.
    """
    row = conn.execute("SELECT * FROM run WHERE run_id = ?", (run_id,)).fetchone()
    if row is None:
        raise KeyError(run_id)
    if row["status"] != "closed":
        raise ValueError(f"run {run_id} is {row['status']}, not closed")
    counts = {}
    for table in ("step_log", "gate", "evidence", "violation", "obligation", "phase_summary"):
        counts[table] = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE run_id = ?", (run_id,)).fetchone()[0]
        conn.execute(f"DELETE FROM {table} WHERE run_id = ?", (run_id,))
    conn.execute("DELETE FROM run WHERE run_id = ?", (run_id,))
    conn.execute(
        "INSERT INTO purge_log (run_id, ability, scope_kind, scope_key, result, counts_json,"
        " reason, purged_at) VALUES (?,?,?,?,?,?,?,?)",
        (run_id, row["ability"], row["scope_kind"], row["scope_key"], row["result"],
         json.dumps(counts, sort_keys=True), reason, now_iso()),
    )
    conn.commit()
    return counts


def purges(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM purge_log ORDER BY id").fetchall()


def all_open_runs(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every open run, any scope. For a caller that knows a LOCATION but not a scope key.

    A runtime hook is exactly that caller: it is about to run a tool in some directory and has
    no idea which scope dimension any ability uses. Narrowing has to happen afterwards, using
    each ability's own declaration of how its scope relates to a location.
    """
    return conn.execute(
        "SELECT * FROM run WHERE status = 'open' ORDER BY updated_at DESC"
    ).fetchall()


def touch_run(conn: sqlite3.Connection, run_id: str, *, current_step: str | None = None) -> None:
    if current_step is None:
        conn.execute("UPDATE run SET updated_at = ? WHERE run_id = ?", (now_iso(), run_id))
    else:
        conn.execute(
            "UPDATE run SET updated_at = ?, current_step = ? WHERE run_id = ?",
            (now_iso(), current_step, run_id),
        )
    conn.commit()


def close_run(conn: sqlite3.Connection, run_id: str, result: str) -> None:
    ts = now_iso()
    conn.execute(
        "UPDATE run SET status='closed', result=?, closed_at=?, updated_at=? WHERE run_id=?",
        (result, ts, ts, run_id),
    )
    conn.commit()


# ---------------------------------------------------------------- steps

def log_step(conn: sqlite3.Connection, run_id: str, step_id: str, event: str,
             detail: str | None = None) -> None:
    conn.execute(
        "INSERT INTO step_log (run_id, step_id, event, at, detail) VALUES (?,?,?,?,?)",
        (run_id, step_id, event, now_iso(), detail),
    )
    conn.commit()


def closed_steps(conn: sqlite3.Connection, run_id: str) -> set[str]:
    rows = conn.execute(
        "SELECT DISTINCT step_id FROM step_log WHERE run_id = ? AND event IN ('closed','skipped')",
        (run_id,),
    ).fetchall()
    return {r["step_id"] for r in rows}


def step_events(conn: sqlite3.Connection, run_id: str, step_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM step_log WHERE run_id = ? AND step_id = ? ORDER BY id",
        (run_id, step_id),
    ).fetchall()


# ---------------------------------------------------------------- gates

def record_gate(conn: sqlite3.Connection, run_id: str, step_id: str, decision: str,
                evidence: str | None, proof: dict) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO gate (run_id, step_id, decision, evidence, proof_json,"
        " recorded_at) VALUES (?,?,?,?,?,?)",
        (run_id, step_id, decision, evidence,
         json.dumps(proof, ensure_ascii=False), now_iso()),
    )
    conn.commit()


def get_gate(conn: sqlite3.Connection, run_id: str, step_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM gate WHERE run_id = ? AND step_id = ?", (run_id, step_id)
    ).fetchone()


# ---------------------------------------------------------------- evidence

def add_evidence(conn: sqlite3.Connection, run_id: str, step_id: str, kind: str,
                 value: str | None) -> None:
    conn.execute(
        "INSERT INTO evidence (run_id, step_id, kind, value, at) VALUES (?,?,?,?,?)",
        (run_id, step_id, kind, value, now_iso()),
    )
    conn.commit()


def find_evidence(conn: sqlite3.Connection, run_id: str, step_id: str | None,
                  kind: str | None = None) -> list[sqlite3.Row]:
    """Evidence rows, oldest first.

    `step_id=None` searches the WHOLE run rather than one step. That is not a convenience:
    a phase-level acceptance criterion ("the run holds a terminal-clean external status")
    must not care which step recorded the value. Scoping it to one step would force an
    ability to nominate a bookkeeping step, and the criterion would then be satisfiable by
    recording the value against the wrong one.
    """
    where = ["run_id = ?"]
    params: list = [run_id]
    if step_id is not None:
        where.append("step_id = ?"); params.append(step_id)
    if kind is not None:
        where.append("kind = ?"); params.append(kind)
    return conn.execute(
        f"SELECT * FROM evidence WHERE {' AND '.join(where)} ORDER BY id", params,
    ).fetchall()


# ---------------------------------------------------------------- violations

def record_violation(conn: sqlite3.Connection, run_id: str | None, step_id: str | None,
                     code: str, detail: str, *, severity: str = "breach") -> None:
    """Record a violation. `severity` is 'blocked' (refused, guarantee held) or 'breach'.

    Defaults to 'breach' so a caller that forgets records the SERIOUS one — the safe default
    is over-reporting a breach, never silently downgrading one to an audit note.
    """
    if severity not in ("blocked", "breach"):
        raise ValueError(f"severity must be 'blocked' or 'breach', got {severity!r}")
    conn.execute(
        "INSERT INTO violation (run_id, step_id, code, detail, severity, at) "
        "VALUES (?,?,?,?,?,?)",
        (run_id, step_id, code, detail, severity, now_iso()),
    )
    conn.commit()


def violations(conn: sqlite3.Connection, run_id: str,
               *, severity: str | None = None) -> list[sqlite3.Row]:
    if severity is None:
        return conn.execute(
            "SELECT * FROM violation WHERE run_id = ? ORDER BY id", (run_id,)
        ).fetchall()
    return conn.execute(
        "SELECT * FROM violation WHERE run_id = ? AND severity = ? ORDER BY id",
        (run_id, severity),
    ).fetchall()


# ---------------------------------------------------------------- obligations

def add_obligation(conn: sqlite3.Connection, run_id: str, hook_id: str, trigger_kind: str,
                   selector: str, body: str, facts_json: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO obligation (run_id, hook_id, trigger_kind, selector, body,"
        " facts_json, raised_at) VALUES (?,?,?,?,?,?,?)",
        (run_id, hook_id, trigger_kind, selector, body, facts_json, now_iso()),
    )
    conn.commit()


def get_obligation(conn: sqlite3.Connection, run_id: str, hook_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM obligation WHERE run_id = ? AND hook_id = ?", (run_id, hook_id)
    ).fetchone()


def open_obligations(conn: sqlite3.Connection, run_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM obligation WHERE run_id = ? AND discharged_at IS NULL"
        " ORDER BY raised_at",
        (run_id,),
    ).fetchall()


def all_obligations(conn: sqlite3.Connection, run_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM obligation WHERE run_id = ? ORDER BY raised_at", (run_id,)
    ).fetchall()


def discharge_obligation(conn: sqlite3.Connection, run_id: str, hook_id: str,
                         evidence: str | None) -> None:
    conn.execute(
        "UPDATE obligation SET discharged_at = ?, evidence = ?"
        " WHERE run_id = ? AND hook_id = ?",
        (now_iso(), evidence, run_id, hook_id),
    )
    conn.commit()


# ---------------------------------------------------------------- phase summaries

def add_phase_summary(conn: sqlite3.Connection, run_id: str, phase: str, *,
                      closed: str, skipped: str, duration_s: int | None,
                      metrics_json: str, note: str | None) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO phase_summary (run_id, phase, closed_steps, skipped_steps,"
        " duration_s, metrics_json, note, at) VALUES (?,?,?,?,?,?,?,?)",
        (run_id, phase, closed, skipped, duration_s, metrics_json, note, now_iso()),
    )
    conn.commit()


def summarized_phases(conn: sqlite3.Connection, run_id: str) -> set[str]:
    return {r["phase"] for r in conn.execute(
        "SELECT phase FROM phase_summary WHERE run_id = ?", (run_id,)).fetchall()}


def phase_summaries(conn: sqlite3.Connection, run_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM phase_summary WHERE run_id = ? ORDER BY at", (run_id,)
    ).fetchall()


def step_attempts(conn: sqlite3.Connection, run_id: str, step_id: str) -> int:
    """How many times a step has been ENTERED. The attempt counter a budget reads."""
    row = conn.execute(
        "SELECT COUNT(*) n FROM step_log WHERE run_id = ? AND step_id = ? AND event = 'entered'",
        (run_id, step_id),
    ).fetchone()
    return int(row["n"])


# ---------------------------------------------------------------- scope leases

def active_leases(conn: sqlite3.Connection, scope_kind: str, scope_key: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM scope_lease WHERE scope_kind = ? AND scope_key = ?"
        " AND released_at IS NULL ORDER BY id",
        (scope_kind, scope_key),
    ).fetchall()


def outgoing_lease(conn: sqlite3.Connection, run_id: str,
                   scope_kind: str, scope_key: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM scope_lease WHERE grantor_run_id = ? AND scope_kind = ?"
        " AND scope_key = ? AND released_at IS NULL",
        (run_id, scope_kind, scope_key),
    ).fetchone()


def held_lease(conn: sqlite3.Connection, run_id: str,
               scope_kind: str, scope_key: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM scope_lease WHERE holder_run_id = ? AND scope_kind = ?"
        " AND scope_key = ? AND released_at IS NULL",
        (run_id, scope_kind, scope_key),
    ).fetchone()


def grant_lease(conn: sqlite3.Connection, *, scope_kind: str, scope_key: str,
                grantor_run_id: str, granted_at_step: str, holder_run_id: str,
                commit: bool = True) -> None:
    conn.execute(
        "INSERT INTO scope_lease (scope_kind, scope_key, grantor_run_id, granted_at_step,"
        " holder_run_id, granted_at) VALUES (?,?,?,?,?,?)",
        (scope_kind, scope_key, grantor_run_id, granted_at_step, holder_run_id, now_iso()),
    )
    if commit:
        conn.commit()


def release_lease(conn: sqlite3.Connection, lease_id: int, reason: str) -> None:
    conn.execute(
        "UPDATE scope_lease SET released_at = ?, released_by = ? WHERE id = ?"
        " AND released_at IS NULL",
        (now_iso(), reason, lease_id),
    )
    conn.commit()


def release_leases_touching(conn: sqlite3.Connection, run_id: str, reason: str) -> dict:
    """End every active lease this run holds OR granted. Returns what was ended.

    Both directions, because a lease naming a closed run is worse than no lease: resolution
    would keep answering with a run that can no longer act, and the answer would look
    authoritative. Leaving the row active until someone notices is the failure mode where a
    guard adjudicates against a finished run forever.
    """
    held = conn.execute(
        "SELECT id FROM scope_lease WHERE holder_run_id = ? AND released_at IS NULL",
        (run_id,),
    ).fetchall()
    granted = conn.execute(
        "SELECT id FROM scope_lease WHERE grantor_run_id = ? AND released_at IS NULL",
        (run_id,),
    ).fetchall()
    ts = now_iso()
    for r in held:
        conn.execute("UPDATE scope_lease SET released_at = ?, released_by = ? WHERE id = ?",
                     (ts, f"{reason}:holder_closed", r["id"]))
    for r in granted:
        conn.execute("UPDATE scope_lease SET released_at = ?, released_by = ? WHERE id = ?",
                     (ts, f"{reason}:grantor_closed", r["id"]))
    conn.commit()
    return {"held": len(held), "granted": len(granted)}


def resolve_scope_chain(conn: sqlite3.Connection, scope_kind: str, scope_key: str,
                        open_run_ids: set[str]) -> tuple[list[str], str | None]:
    """Order the open runs in one scope into a delegation chain, or say why you cannot.

    Returns `(chain, None)` on success — grantor first, current holder last — or
    `([], reason)` when the situation is not adjudicable.

    THE POINT OF RETURNING A REASON. Every rejection here sends the caller back to allowing
    the action without enforcement, which is a real loss of a guarantee. So the caller has to
    be able to SAY which of these it hit; "could not resolve" recorded without the reason is
    an audit note nobody can act on.

    Three ways it fails, and none of them is guessable around:

    * `no_lease` — two or more runs share the scope and nothing declares a relation. This is
      the ordinary concurrent case, and the engine has no business inventing an order for it.
    * `partial` — a chain exists but does not cover every open run in the scope. The runs
      outside it are unrelated, so the action may belong to one of them, and demanding a gate
      from a run that does not own the work is what makes forging the gate the only way out.
    * `forked` — a run granted the same scope twice, or two runs claim to hold it. Authority
      that is in two places at once is not authority.

    THERE IS NO CYCLE CHECK, AND THAT IS PROVEN RATHER THAN ASSUMED. Rejecting a repeated
    grantor or a repeated holder makes the declarations injective on both sides, so the graph
    is a set of disjoint paths and cycles. Every node in a cycle appears as both a grantor and
    a holder, so no cycle contains a root — with exactly one root, its component is a path,
    and cycles elsewhere are simply unreachable from the walk and fall out as `partial`. A
    guard against them would be a branch that cannot run, which is worse than absent: it reads
    as a hazard being handled.
    """
    leases = active_leases(conn, scope_kind, scope_key)
    if not leases:
        return [], "no_lease"
    forward: dict[str, str] = {}
    for row in leases:
        g, h = row["grantor_run_id"], row["holder_run_id"]
        if g in forward or h in forward.values():
            return [], "forked"
        forward[g] = h
    roots = [g for g in forward if g not in forward.values()]
    if len(roots) != 1:
        return [], "forked"
    chain = [roots[0]]
    while chain[-1] in forward:
        chain.append(forward[chain[-1]])
    if set(chain) != open_run_ids:
        return [], "partial"
    return chain, None


def claim_scope_and_open(conn: sqlite3.Connection, *, require_free_scope: bool,
                        lease: dict | None, **run_kw) -> tuple[str | None, list[sqlite3.Row]]:
    """Take the scope and insert the run in ONE exclusive transaction.

    Returns `(None, [])` on success, or `(reason, blockers)` — where `reason` is
    `scope_taken` (with the rows that blocked it), `grantor_gone` or
    `grantor_already_delegated`.

    WHY THIS IS ONE CALL AND NOT TWO STATEMENTS. Asking "is this scope free" and then
    inserting were separate, with a gap in between, and concurrent sessions both read zero and
    both inserted. That broke the invariant this engine leans on hardest — one open run per
    scope, so a guard knows whose gate applies — and it broke it in the worst direction:
    LOSING the race lands you in the state where the guard stops adjudicating, so the failure
    took enforcement away rather than leaving a duplicate row somebody would notice.
    Measured before this existed: six concurrent opens on one scope produced two runs in one
    of three trials, intermittently, which is why 200-odd sequential tests never saw it.

    BEGIN IMMEDIATE, not BEGIN. A deferred transaction acquires its write lock at the first
    WRITE, which is after the read — the same gap, relocated. IMMEDIATE acquires it up front,
    so a second process waits on `busy_timeout` and then reads the first one's row.

    NOTHING SLOW MAY MOVE IN HERE. The caller resolves the flow variant before calling, and
    that can spawn provider subprocesses; doing it inside would hold a write lock for the
    lifetime of a subprocess. That is precisely the lock incident a neighbouring system spent
    a day diagnosing, and its fix was this same separation — the transaction covers the check
    and the inserts, and nothing else.

    The lease is inserted here too, rather than after. A run that exists with its delegation
    still missing is indistinguishable from an unrelated concurrent run, so a crash between
    two separate writes would look exactly like the ambiguity the lease exists to remove.
    """
    prev = conn.isolation_level
    conn.isolation_level = None  # take manual control; the module's implicit BEGIN is deferred
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if require_free_scope:
                blockers = conn.execute(
                    "SELECT * FROM run WHERE scope_kind = ? AND scope_key = ?"
                    " AND status = 'open' ORDER BY updated_at DESC",
                    (run_kw["scope_kind"], run_kw["scope_key"]),
                ).fetchall()
                if blockers:
                    conn.execute("ROLLBACK")
                    return "scope_taken", blockers
            if lease is not None:
                # Re-checked inside the lock, because the grantor was validated before it was
                # taken: it could have closed, or delegated elsewhere, in between. A lease
                # naming a closed grantor would make the chain unresolvable, which silently
                # returns the scope to unenforced.
                g = conn.execute("SELECT status FROM run WHERE run_id = ?",
                                 (lease["grantor_run_id"],)).fetchone()
                if g is None or g["status"] != "open":
                    conn.execute("ROLLBACK")
                    return "grantor_gone", []
                dup = conn.execute(
                    "SELECT 1 FROM scope_lease WHERE grantor_run_id = ? AND scope_kind = ?"
                    " AND scope_key = ? AND released_at IS NULL",
                    (lease["grantor_run_id"], run_kw["scope_kind"], run_kw["scope_key"]),
                ).fetchone()
                if dup is not None:
                    conn.execute("ROLLBACK")
                    return "grantor_already_delegated", []
            open_run(conn, commit=False, **run_kw)
            if lease is not None:
                grant_lease(conn, scope_kind=run_kw["scope_kind"],
                            scope_key=run_kw["scope_key"],
                            grantor_run_id=lease["grantor_run_id"],
                            granted_at_step=lease["granted_at_step"],
                            holder_run_id=run_kw["run_id"], commit=False)
            conn.execute("COMMIT")
            return None, []
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.isolation_level = prev
