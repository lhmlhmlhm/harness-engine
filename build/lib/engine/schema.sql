-- harness-engine store schema.
--
-- The engine OWNS this database. It is never a symlink into an ability's directory:
-- an earlier framework attempt put its `framework.db` as a symlink pointing at its
-- first consumer's store, which inverted the dependency (base -> consumer) while the
-- consumer also called back into the base. A base that borrows its state from a
-- consumer is not a base.
--
-- Every table here is ability-agnostic. No column encodes a step name, a phase name,
-- or any domain vocabulary — those live in the ability's flow spec and arrive as
-- opaque TEXT. That is the invariant `tests/test_engine_purity.py` enforces.

PRAGMA foreign_keys = ON;

-- One row per run of an ability's flow.
-- "run" is the generic word: one ability may call it a session, another a revision.
CREATE TABLE IF NOT EXISTS run (
    run_id        TEXT PRIMARY KEY,
    ability       TEXT NOT NULL,          -- which flow spec this run follows
    flow_digest   TEXT NOT NULL,          -- sha256 of the spec at open time; detects
                                          -- a flow edited underneath a live run
    title         TEXT,
    -- Scope dimensions. A guard has to answer "does THIS action belong to THIS run?"
    -- and the only reliable answer is a scope key the action carries with it. Leaving
    -- this implicit is what makes a guard demand an unrelated run's gate.
    scope_kind    TEXT NOT NULL,          -- e.g. the name of the dimension being scoped
    scope_key     TEXT NOT NULL,          -- the value in that dimension
    -- Which VARIANT of the flow this run follows, pinned at open. Pinned, not re-derived:
    -- a flow whose shape changes mid-run cannot be analysed, and "which steps were owed"
    -- would depend on when you asked.
    variant       TEXT,
    status        TEXT NOT NULL,          -- open | closed
    result        TEXT,                   -- set when closed
    current_step  TEXT,
    opened_at     TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    closed_at     TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_run_open_scope
    ON run (scope_kind, scope_key) WHERE status = 'open';
CREATE INDEX IF NOT EXISTS idx_run_ability ON run (ability);

-- Append-only transition log. Never updated, never deleted while the run lives.
CREATE TABLE IF NOT EXISTS step_log (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id  TEXT NOT NULL REFERENCES run(run_id) ON DELETE CASCADE,
    step_id TEXT NOT NULL,
    event   TEXT NOT NULL,                -- entered | closed | refused | skipped
    at      TEXT NOT NULL,
    detail  TEXT
);

CREATE INDEX IF NOT EXISTS idx_step_log_run ON step_log (run_id, step_id);

-- Gate decisions. One per (run, step) — a gate is answered once.
-- `proof_json` holds the snapshot of whatever unfakeable signal authorised the
-- decision. A decision with no proof is refused at record time, not trusted and
-- audited later: prose asking an agent to be honest does not bind it, an exit code does.
CREATE TABLE IF NOT EXISTS gate (
    run_id      TEXT NOT NULL REFERENCES run(run_id) ON DELETE CASCADE,
    step_id     TEXT NOT NULL,
    decision    TEXT NOT NULL,            -- affirm | decline | preauth
    evidence    TEXT,
    proof_json  TEXT NOT NULL DEFAULT '{}',
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (run_id, step_id)
);

-- Evidence rows back completion predicates. The engine never interprets `value`;
-- the predicate named by the flow spec does.
CREATE TABLE IF NOT EXISTS evidence (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id  TEXT NOT NULL REFERENCES run(run_id) ON DELETE CASCADE,
    step_id TEXT NOT NULL,
    kind    TEXT NOT NULL,
    value   TEXT,
    at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_evidence_lookup ON evidence (run_id, step_id, kind);

-- Refusals and contract breaches, kept for audit. Recording a violation must never
-- be what STOPS the bad thing — the exit code does that. This is the ledger, not the lock.
-- Two natures share this ledger and must not be conflated:
--   'blocked' — an attempt was REFUSED. The guarantee held; the row is audit trail.
--   'breach'  — something got through, marked. The guarantee did NOT hold.
-- Without the distinction, a terminal criterion of "no violations" punishes a run for the
-- engine having WORKED, and creates a reason to avoid attempting rather than to comply. It
-- also let one code mean both at once: a refused gate and an accepted downgrade were both
-- filed as the same thing, in opposite directions.
CREATE TABLE IF NOT EXISTS violation (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id   TEXT REFERENCES run(run_id) ON DELETE CASCADE,
    step_id  TEXT,
    code     TEXT NOT NULL,
    detail   TEXT,
    severity TEXT NOT NULL DEFAULT 'breach',
    at       TEXT NOT NULL
);

-- Removing a run leaves THIS behind. The ledger is otherwise append-only, and the reason a
-- maintenance surface exists at all is that without one the only way to clear test residue is
-- to write to the database by hand — which bypasses every check and leaves no trace that it
-- happened. So the sanctioned path exists, is restricted to CLOSED runs (a live run's
-- violations must not be erasable), and records what it removed.
CREATE TABLE IF NOT EXISTS purge_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT NOT NULL,
    ability     TEXT NOT NULL,
    scope_kind  TEXT NOT NULL,
    scope_key   TEXT NOT NULL,
    result      TEXT,
    counts_json TEXT NOT NULL,      -- what was removed, per table
    reason      TEXT NOT NULL,
    purged_at   TEXT NOT NULL
);

-- Obligations raised by a hook at a boundary, and not yet discharged.
--
-- `facts_json` is the snapshot of the facts as they were AT THE MOMENT the obligation
-- arose. Kept because a discharge reviewed later is otherwise unauditable: the world has
-- moved on, and "why did this fire" becomes unanswerable. An unanswerable negative is how
-- a condition rots without anyone noticing.
CREATE TABLE IF NOT EXISTS obligation (
    run_id        TEXT NOT NULL REFERENCES run(run_id) ON DELETE CASCADE,
    hook_id       TEXT NOT NULL,
    trigger_kind  TEXT NOT NULL,
    selector      TEXT NOT NULL,
    body          TEXT NOT NULL,
    facts_json    TEXT NOT NULL DEFAULT '{}',
    raised_at     TEXT NOT NULL,
    discharged_at TEXT,
    evidence      TEXT,
    PRIMARY KEY (run_id, hook_id)
);

CREATE INDEX IF NOT EXISTS idx_obligation_open
    ON obligation (run_id) WHERE discharged_at IS NULL;

-- Per-phase rollup, written explicitly at a phase boundary.
--
-- Derivable facts (which steps closed) are recorded anyway, because the point is not the
-- derivation but the ATTESTATION that the phase was consciously wrapped up: a phase whose
-- steps all closed and which nobody summarised is a phase nobody looked back at.
-- `metrics_json` carries whatever observations the ability wants to keep — the engine
-- never interprets it.
CREATE TABLE IF NOT EXISTS phase_summary (
    run_id        TEXT NOT NULL REFERENCES run(run_id) ON DELETE CASCADE,
    phase         TEXT NOT NULL,
    closed_steps  TEXT NOT NULL DEFAULT '',
    skipped_steps TEXT NOT NULL DEFAULT '',
    duration_s    INTEGER,
    metrics_json  TEXT NOT NULL DEFAULT '{}',
    note          TEXT,
    at            TEXT NOT NULL,
    PRIMARY KEY (run_id, phase)
);

-- Who holds authority over a scope while more than one run is open in it.
--
-- WHY A LEASE AND NOT A PARENT POINTER. A parent pointer is a genealogy, and a genealogy
-- immediately demands answers this engine must not give: does closing the parent close the
-- child, does the child's progress count toward the parent's, does a child's breach surface
-- on the parent's ledger. Every answer is a consumer's decision, and one picked here would
-- be wrong for some consumer. It also mis-describes the common case: a flow that hands work
-- to another flow days later, in another process, possibly never, is not a containment.
--
-- What actually happens when one flow delegates is narrower: FOR A WHILE, A DIFFERENT RUN
-- HAS AUTHORITY OVER THIS SCOPE. That is a lease. It says nothing about progress, scoring or
-- cascade — only about who a guard should adjudicate against, which is the one question that
-- becomes unanswerable when two runs share a scope.
--
-- Declared, never inferred. Deducing the relation from timing ("this one opened while that
-- one was live, so it must be inside it") is the same mistake as resolving a guard with
-- ORDER BY ... LIMIT 1: it produces a confident answer with nothing behind it.
CREATE TABLE IF NOT EXISTS scope_lease (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    -- The scope is stored rather than read back through the grantor so that resolving "who
    -- holds this scope" is one indexed lookup on the columns below instead of a join. It also
    -- keeps the lease's subject explicit: a lease is ABOUT a scope, and deriving that from a
    -- run row would couple the two the moment either could disagree.
    scope_kind      TEXT NOT NULL,
    scope_key       TEXT NOT NULL,
    grantor_run_id  TEXT NOT NULL REFERENCES run(run_id) ON DELETE CASCADE,
    granted_at_step TEXT NOT NULL,          -- where in the grantor's flow this happened
    holder_run_id   TEXT NOT NULL REFERENCES run(run_id) ON DELETE CASCADE,
    granted_at      TEXT NOT NULL,
    released_at     TEXT,
    released_by     TEXT                    -- what ended it, for the ledger
);

CREATE INDEX IF NOT EXISTS idx_lease_active
    ON scope_lease (scope_kind, scope_key) WHERE released_at IS NULL;
