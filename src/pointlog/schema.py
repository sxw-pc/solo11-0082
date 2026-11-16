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
    issue_count INTEGER NOT NULL DEFAULT 0
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
    engineering_value REAL,
    unit TEXT NOT NULL DEFAULT '',
    quality_code TEXT NOT NULL,
    enum_label TEXT,
    source TEXT NOT NULL DEFAULT '',
    calibration_version_id TEXT NOT NULL DEFAULT '',
    calibration_registered_at TEXT NOT NULL DEFAULT '',
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
    PRIMARY KEY (replay_path, replay_sha256),
    FOREIGN KEY (batch_id) REFERENCES batches(id) ON DELETE CASCADE
);

-- Append-only audit trail of recalculated conclusions. Rows are never
-- updated or deleted: a later registered revision inserts new rows keyed by
-- its version id while earlier derived rows remain as evidence.
CREATE TABLE IF NOT EXISTS derived_readings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL,
    device_id TEXT NOT NULL,
    point_key TEXT NOT NULL,
    ts TEXT NOT NULL,
    source_reading_id INTEGER NOT NULL,
    old_calibration_version_id TEXT NOT NULL DEFAULT '',
    new_calibration_version_id TEXT NOT NULL DEFAULT '',
    new_calibration_registered_at TEXT NOT NULL DEFAULT '',
    scale REAL,
    offset REAL,
    raw_value REAL NOT NULL,
    engineering_value REAL,
    quality_code TEXT NOT NULL,
    enum_label TEXT,
    input_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (batch_id, device_id, point_key, ts, new_calibration_version_id),
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
CREATE INDEX IF NOT EXISTS idx_derived_batch_point
    ON derived_readings(batch_id, device_id, point_key);
"""
