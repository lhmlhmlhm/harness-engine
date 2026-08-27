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
    """
    path = db_path()
    if read_only:
        if not path.is_file():
            raise StoreNotInitialised(path)
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
    else:
        if not path.is_file():
            raise StoreNotInitialised(path)
        conn = sqlite3.connect(path, timeout=5.0)
        conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    return conn


class StoreNotInitialised(RuntimeError):
    def __init__(self, path: Path):
        super().__init__(
            f"harness store not initialised at {path}\n"
            f"  run: harness init"
        )
        self.path = path


def init() -> Path:
    """Create the store. Idempotent (the schema is all IF NOT EXISTS).

    WAL is set outside a transaction on purpose: `PRAGMA journal_mode=WAL` cannot run
    inside one, and that failure only ever surfaces on a brand-new database — the
    exact path a fresh install takes and a developed-in-place one never does.
    """
    state_dir().mkdir(parents=True, exist_ok=True)
    path = db_path()
    conn = sqlite3.connect(path, timeout=5.0)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        conn.commit()
    finally:
        conn.close()
    return path


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
) -> None:
    ts = now_iso()
    conn.execute(
        "INSERT INTO run (run_id, ability, flow_digest, title, scope_kind, scope_key,"
        " variant, status, opened_at, updated_at, metadata_json)"
        " VALUES (?,?,?,?,?,?,?,'open',?,?,?)",
        (run_id, ability, flow_digest, title, scope_kind, scope_key, variant, ts, ts,
         json.dumps(metadata or {}, ensure_ascii=False)),
    )
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
