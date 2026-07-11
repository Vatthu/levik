package telemetry

import (
	"database/sql"
	"fmt"

	_ "modernc.org/sqlite"
)

// SchemaVersion is the current version of the telemetry database schema.
const SchemaVersion = 1

// DefaultRetentionDays is the default number of days telemetry events are retained.
const DefaultRetentionDays = 90

// createSchema contains the DDL statements for the telemetry tables.
const createSchema = `
-- Schema version tracking
CREATE TABLE IF NOT EXISTS schema_version (
	version INTEGER NOT NULL
);

-- telemetry_events stores all structured telemetry events emitted by the platform.
CREATE TABLE IF NOT EXISTS telemetry_events (
	event_id   TEXT PRIMARY KEY,
	event_type TEXT NOT NULL,
	task_id    TEXT NOT NULL DEFAULT '',
	timestamp  DATETIME NOT NULL,
	attributes TEXT NOT NULL DEFAULT '{}'
);

-- Indexes for efficient time-range and filtered queries.
CREATE INDEX IF NOT EXISTS idx_telemetry_events_timestamp
	ON telemetry_events(timestamp);
CREATE INDEX IF NOT EXISTS idx_telemetry_events_type_timestamp
	ON telemetry_events(event_type, timestamp);
CREATE INDEX IF NOT EXISTS idx_telemetry_events_task_timestamp
	ON telemetry_events(task_id, timestamp);

-- retention_config stores the configurable retention period.
CREATE TABLE IF NOT EXISTS retention_config (
	id             INTEGER PRIMARY KEY CHECK (id = 1),
	retention_days INTEGER NOT NULL DEFAULT 90,
	updated_at     DATETIME NOT NULL
);
`

// OpenDB opens or creates the telemetry SQLite database at the given path
// and applies schema migrations as needed. The database uses WAL mode for
// concurrent read access.
func OpenDB(dbPath string) (*sql.DB, error) {
	db, err := sql.Open("sqlite", dbPath)
	if err != nil {
		return nil, fmt.Errorf("telemetry: failed to open database: %w", err)
	}

	// Enable WAL mode for concurrent reads during WebSocket streaming.
	if _, err := db.Exec("PRAGMA journal_mode=WAL"); err != nil {
		db.Close()
		return nil, fmt.Errorf("telemetry: failed to enable WAL: %w", err)
	}

	// Enable foreign keys.
	if _, err := db.Exec("PRAGMA foreign_keys=ON"); err != nil {
		db.Close()
		return nil, fmt.Errorf("telemetry: failed to enable foreign keys: %w", err)
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
		return fmt.Errorf("telemetry: failed to check schema_version: %w", err)
	}

	if tableExists == 0 {
		// Fresh database — apply full schema.
		if _, err := db.Exec(createSchema); err != nil {
			return fmt.Errorf("telemetry: failed to create schema: %w", err)
		}
		if _, err := db.Exec("INSERT INTO schema_version (version) VALUES (?)", SchemaVersion); err != nil {
			return fmt.Errorf("telemetry: failed to record schema version: %w", err)
		}
		return nil
	}

	// Check current version.
	var currentVersion int
	err = db.QueryRow("SELECT version FROM schema_version LIMIT 1").Scan(&currentVersion)
	if err != nil {
		return fmt.Errorf("telemetry: failed to read schema version: %w", err)
	}

	if currentVersion < SchemaVersion {
		// Future migrations go here.
		if _, err := db.Exec("UPDATE schema_version SET version = ?", SchemaVersion); err != nil {
			return fmt.Errorf("telemetry: failed to update schema version: %w", err)
		}
	}

	return nil
}
