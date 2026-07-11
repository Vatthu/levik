package costledger

import (
	"database/sql"
	"fmt"

	_ "modernc.org/sqlite"
)

// SchemaVersion is the current version of the cost ledger database schema.
const SchemaVersion = 2

// createSchema contains the DDL statements for the cost ledger tables.
const createSchema = `
-- Schema version tracking
CREATE TABLE IF NOT EXISTS schema_version (
	version INTEGER NOT NULL
);

-- cost_records stores every LLM provider call with full cost attribution.
CREATE TABLE IF NOT EXISTS cost_records (
	record_id    TEXT PRIMARY KEY,
	task_id      TEXT NOT NULL,
	role         TEXT NOT NULL,
	model        TEXT NOT NULL,
	provider     TEXT NOT NULL,
	work_phase   TEXT NOT NULL,
	input_tokens INTEGER NOT NULL,
	output_tokens INTEGER NOT NULL,
	cost_usd     REAL NOT NULL,
	estimated    INTEGER NOT NULL DEFAULT 0,
	duration_ms  INTEGER NOT NULL DEFAULT 0,
	invocation_id TEXT NOT NULL,
	timestamp    DATETIME NOT NULL
);

-- Indexes for efficient lookups
CREATE INDEX IF NOT EXISTS idx_cost_records_task_timestamp
	ON cost_records(task_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_cost_records_timestamp
	ON cost_records(timestamp);

-- budget_strategies stores per-task-type budget allocation percentages.
CREATE TABLE IF NOT EXISTS budget_strategies (
	task_type       TEXT PRIMARY KEY,
	planning        REAL NOT NULL DEFAULT 10.0,
	implementation  REAL NOT NULL DEFAULT 60.0,
	verification    REAL NOT NULL DEFAULT 20.0,
	review          REAL NOT NULL DEFAULT 10.0,
	created_at      DATETIME NOT NULL,
	updated_at      DATETIME NOT NULL
);

-- daily_ceilings stores the system-wide daily spending limit configuration.
CREATE TABLE IF NOT EXISTS daily_ceilings (
	id            INTEGER PRIMARY KEY CHECK (id = 1),
	max_daily_usd REAL NOT NULL DEFAULT 50.0,
	reset_hour    INTEGER NOT NULL DEFAULT 0,
	created_at    DATETIME NOT NULL,
	updated_at    DATETIME NOT NULL
);

-- task_metadata stores complexity tier and target file count for tasks,
-- used by the forecast engine to group historical costs by similarity.
CREATE TABLE IF NOT EXISTS task_metadata (
	task_id         TEXT PRIMARY KEY,
	complexity_tier TEXT NOT NULL,
	target_files    INTEGER NOT NULL DEFAULT 0,
	created_at      DATETIME NOT NULL
);

-- Index for efficient forecast queries grouping by complexity tier.
CREATE INDEX IF NOT EXISTS idx_task_metadata_tier
	ON task_metadata(complexity_tier);
`

// OpenDB opens or creates the cost ledger SQLite database at the given path
// and applies schema migrations as needed.
func OpenDB(dbPath string) (*sql.DB, error) {
	db, err := sql.Open("sqlite", dbPath)
	if err != nil {
		return nil, fmt.Errorf("costledger: failed to open database: %w", err)
	}

	// Enable WAL mode for concurrent reads.
	if _, err := db.Exec("PRAGMA journal_mode=WAL"); err != nil {
		db.Close()
		return nil, fmt.Errorf("costledger: failed to enable WAL: %w", err)
	}

	// Enable foreign keys.
	if _, err := db.Exec("PRAGMA foreign_keys=ON"); err != nil {
		db.Close()
		return nil, fmt.Errorf("costledger: failed to enable foreign keys: %w", err)
	}

	if err := migrateSchema(db); err != nil {
		db.Close()
		return nil, err
	}

	return db, nil
}

// migrateSchema applies pending schema migrations.
func migrateSchema(db *sql.DB) error {
	// Check if schema_version table exists.
	var tableExists int
	err := db.QueryRow(`
		SELECT COUNT(*) FROM sqlite_master
		WHERE type='table' AND name='schema_version'
	`).Scan(&tableExists)
	if err != nil {
		return fmt.Errorf("costledger: failed to check schema_version: %w", err)
	}

	if tableExists == 0 {
		// Fresh database — apply full schema.
		if _, err := db.Exec(createSchema); err != nil {
			return fmt.Errorf("costledger: failed to create schema: %w", err)
		}
		if _, err := db.Exec("INSERT INTO schema_version (version) VALUES (?)", SchemaVersion); err != nil {
			return fmt.Errorf("costledger: failed to record schema version: %w", err)
		}
		return nil
	}

	// Check current version.
	var currentVersion int
	err = db.QueryRow("SELECT version FROM schema_version LIMIT 1").Scan(&currentVersion)
	if err != nil {
		return fmt.Errorf("costledger: failed to read schema version: %w", err)
	}

	if currentVersion < SchemaVersion {
		// Apply migrations incrementally.
		if currentVersion < 2 {
			if err := migrateV2(db); err != nil {
				return fmt.Errorf("costledger: migration to v2 failed: %w", err)
			}
		}
		if _, err := db.Exec("UPDATE schema_version SET version = ?", SchemaVersion); err != nil {
			return fmt.Errorf("costledger: failed to update schema version: %w", err)
		}
	}

	return nil
}

// migrateV2 adds the task_metadata table for associating tasks with complexity tiers
// and an index on cost_records for forecast queries by complexity tier.
func migrateV2(db *sql.DB) error {
	const v2DDL = `
-- task_metadata stores complexity tier and target file count for tasks,
-- used by the forecast engine to group historical costs by similarity.
CREATE TABLE IF NOT EXISTS task_metadata (
	task_id         TEXT PRIMARY KEY,
	complexity_tier TEXT NOT NULL,
	target_files    INTEGER NOT NULL DEFAULT 0,
	created_at      DATETIME NOT NULL
);

-- Index for efficient forecast queries grouping by complexity tier.
CREATE INDEX IF NOT EXISTS idx_task_metadata_tier
	ON task_metadata(complexity_tier);
`
	if _, err := db.Exec(v2DDL); err != nil {
		return fmt.Errorf("apply v2 DDL: %w", err)
	}
	return nil
}
