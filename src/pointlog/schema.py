"""SQLite schema for the capture ledger."""

SCHEMA_VERSION = 2

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS batches (
    id TEXT PRIMARY KEY,
    replay_path TEXT NOT NULL,
    replay_sha256 TEXT NOT NULL,
    contract_path TEXT NOT NULL,
    contract_sha256 TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL CHECK (status IN ('partial', 'completed', 'failed')),
    total_lines INTEGER NOT NULL DEFAULT 0,
    processed_lines INTEGER NOT NULL DEFAULT 0,
    reading_count INTEGER NOT NULL DEFAULT 0,
    valid_readings INTEGER NOT NULL DEFAULT 0,
    issue_count INTEGER NOT NULL DEFAULT 0,
    replay_mode TEXT NOT NULL DEFAULT 'pairs',
    pairing_route TEXT NOT NULL DEFAULT '',
    pair_timeout_ms INTEGER NOT NULL DEFAULT 0,
    mode_fingerprint TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS frames (
    batch_id TEXT NOT NULL,
    line_no INTEGER NOT NULL,
    ts TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    request_hex TEXT NOT NULL DEFAULT '',
    response_hex TEXT NOT NULL DEFAULT '',
    request_crc_ok INTEGER,
    response_crc_ok INTEGER,
    slave_id INTEGER,
    function_code INTEGER,
    start_address INTEGER,
    register_count INTEGER,
    device_id TEXT NOT NULL DEFAULT '',
    point_key TEXT NOT NULL DEFAULT '',
    contract_matched INTEGER NOT NULL DEFAULT 0,
    quality_code TEXT NOT NULL,
    issue_code TEXT,
    detail TEXT NOT NULL DEFAULT '',
    channel TEXT NOT NULL DEFAULT '',
    direction TEXT NOT NULL DEFAULT '',
    pair_id TEXT NOT NULL DEFAULT '',
    pair_route TEXT NOT NULL DEFAULT '',
    request_line_no INTEGER,
    response_line_no INTEGER,
    request_sha256 TEXT NOT NULL DEFAULT '',
    response_sha256 TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (batch_id, line_no),
    FOREIGN KEY (batch_id) REFERENCES batches(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS pair_events (
    batch_id TEXT NOT NULL,
    line_no INTEGER NOT NULL,
    ts TEXT NOT NULL DEFAULT '',
    channel TEXT NOT NULL DEFAULT '',
    direction TEXT NOT NULL DEFAULT '',
    frame_hex TEXT NOT NULL DEFAULT '',
    pair_id TEXT NOT NULL DEFAULT '',
    timeout_ms INTEGER NOT NULL DEFAULT 0,
    event_sha256 TEXT NOT NULL DEFAULT '',
    outcome TEXT NOT NULL DEFAULT 'open',
    paired_line_no INTEGER,
    detail TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (batch_id, line_no),
    FOREIGN KEY (batch_id) REFERENCES batches(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS readings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL,
    device_id TEXT NOT NULL,
    point_key TEXT NOT NULL,
    ts TEXT NOT NULL,
    line_no INTEGER NOT NULL,
    raw_value REAL NOT NULL,
    engineering_value REAL NOT NULL,
    unit TEXT NOT NULL DEFAULT '',
    quality_code TEXT NOT NULL,
    enum_label TEXT,
    source TEXT NOT NULL DEFAULT '',
    UNIQUE (batch_id, device_id, point_key, ts),
    FOREIGN KEY (batch_id) REFERENCES batches(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS issues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL,
    line_no INTEGER NOT NULL DEFAULT 0,
    device_id TEXT NOT NULL DEFAULT '',
    point_key TEXT NOT NULL DEFAULT '',
    issue_code TEXT NOT NULL,
    quality_code TEXT NOT NULL,
    severity TEXT NOT NULL,
    frame_summary TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL,
    ts TEXT NOT NULL DEFAULT '',
    pending_collect INTEGER NOT NULL DEFAULT 0,
    UNIQUE (batch_id, line_no, point_key, issue_code, detail),
    FOREIGN KEY (batch_id) REFERENCES batches(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS resume_state (
    replay_path TEXT NOT NULL,
    replay_sha256 TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    line_offset INTEGER NOT NULL DEFAULT 0,
    total_lines INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    mode_fingerprint TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (replay_path, replay_sha256),
    FOREIGN KEY (batch_id) REFERENCES batches(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_frames_batch_quality
    ON frames(batch_id, quality_code);
CREATE INDEX IF NOT EXISTS idx_readings_batch_point
    ON readings(batch_id, device_id, point_key);
CREATE INDEX IF NOT EXISTS idx_issues_batch_pending
    ON issues(batch_id, pending_collect, device_id, point_key);
CREATE INDEX IF NOT EXISTS idx_batches_fingerprint
    ON batches(replay_sha256, contract_sha256);
CREATE INDEX IF NOT EXISTS idx_resume_path
    ON resume_state(replay_path, updated_at);
CREATE INDEX IF NOT EXISTS idx_pair_events_batch
    ON pair_events(batch_id, outcome);
"""

# Columns added after schema version 1; init_schema applies these idempotently
# so databases created by older versions keep working.
MIGRATIONS = (
    ("batches", "replay_mode", "TEXT NOT NULL DEFAULT 'pairs'"),
    ("batches", "pairing_route", "TEXT NOT NULL DEFAULT ''"),
    ("batches", "pair_timeout_ms", "INTEGER NOT NULL DEFAULT 0"),
    ("batches", "mode_fingerprint", "TEXT NOT NULL DEFAULT ''"),
    ("frames", "channel", "TEXT NOT NULL DEFAULT ''"),
    ("frames", "direction", "TEXT NOT NULL DEFAULT ''"),
    ("frames", "pair_id", "TEXT NOT NULL DEFAULT ''"),
    ("frames", "pair_route", "TEXT NOT NULL DEFAULT ''"),
    ("frames", "request_line_no", "INTEGER"),
    ("frames", "response_line_no", "INTEGER"),
    ("frames", "request_sha256", "TEXT NOT NULL DEFAULT ''"),
    ("frames", "response_sha256", "TEXT NOT NULL DEFAULT ''"),
    ("resume_state", "mode_fingerprint", "TEXT NOT NULL DEFAULT ''"),
)
