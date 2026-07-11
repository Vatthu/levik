package telemetry

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

func TestOpenDB_CreatesSchema(t *testing.T) {
	dir := t.TempDir()
	dbPath := filepath.Join(dir, "telemetry.db")

	db, err := OpenDB(dbPath)
	require.NoError(t, err)
	defer db.Close()

	// Verify tables were created.
	tables := []string{"schema_version", "telemetry_events", "retention_config"}
	for _, table := range tables {
		var count int
		err := db.QueryRow(`SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?`, table).Scan(&count)
		require.NoError(t, err)
		assert.Equal(t, 1, count, "expected table %s to exist", table)
	}

	// Verify schema version.
	var version int
	err = db.QueryRow("SELECT version FROM schema_version").Scan(&version)
	require.NoError(t, err)
	assert.Equal(t, SchemaVersion, version)
}

func TestOpenDB_IdempotentMigration(t *testing.T) {
	dir := t.TempDir()
	dbPath := filepath.Join(dir, "telemetry.db")

	// First open creates schema.
	db1, err := OpenDB(dbPath)
	require.NoError(t, err)
	db1.Close()

	// Second open should not fail.
	db2, err := OpenDB(dbPath)
	require.NoError(t, err)
	defer db2.Close()

	var version int
	err = db2.QueryRow("SELECT version FROM schema_version").Scan(&version)
	require.NoError(t, err)
	assert.Equal(t, SchemaVersion, version)
}

func TestOpenDB_Indexes(t *testing.T) {
	dir := t.TempDir()
	dbPath := filepath.Join(dir, "telemetry.db")

	db, err := OpenDB(dbPath)
	require.NoError(t, err)
	defer db.Close()

	// Verify indexes exist.
	indexes := []string{
		"idx_telemetry_events_timestamp",
		"idx_telemetry_events_type_timestamp",
		"idx_telemetry_events_task_timestamp",
	}
	for _, idx := range indexes {
		var count int
		err := db.QueryRow(`SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND name=?`, idx).Scan(&count)
		require.NoError(t, err)
		assert.Equal(t, 1, count, "expected index %s to exist", idx)
	}
}

func TestOpenDB_WALMode(t *testing.T) {
	dir := t.TempDir()
	dbPath := filepath.Join(dir, "telemetry.db")

	db, err := OpenDB(dbPath)
	require.NoError(t, err)
	defer db.Close()

	var journalMode string
	err = db.QueryRow("PRAGMA journal_mode").Scan(&journalMode)
	require.NoError(t, err)
	assert.Equal(t, "wal", journalMode)
}

func TestOpenDB_InvalidPath(t *testing.T) {
	dbPath := filepath.Join("/nonexistent-path-xyz", "telemetry.db")
	_, err := OpenDB(dbPath)
	if _, statErr := os.Stat("/nonexistent-path-xyz"); os.IsNotExist(statErr) {
		assert.Error(t, err)
	}
}

func TestOpenDB_InsertAndQueryEvent(t *testing.T) {
	dir := t.TempDir()
	dbPath := filepath.Join(dir, "telemetry.db")

	db, err := OpenDB(dbPath)
	require.NoError(t, err)
	defer db.Close()

	// Insert an event.
	_, err = db.Exec(`
		INSERT INTO telemetry_events (event_id, event_type, task_id, timestamp, attributes)
		VALUES (?, ?, ?, datetime('now'), ?)`,
		"evt-001", "agent_call_start", "task-123", `{"role":"planner","model":"claude-sonnet-4-20250514"}`)
	require.NoError(t, err)

	// Query it back.
	var eventType string
	err = db.QueryRow("SELECT event_type FROM telemetry_events WHERE event_id = ?", "evt-001").Scan(&eventType)
	require.NoError(t, err)
	assert.Equal(t, "agent_call_start", eventType)
}

func TestOpenDB_RetentionConfigSingleton(t *testing.T) {
	dir := t.TempDir()
	dbPath := filepath.Join(dir, "telemetry.db")

	db, err := OpenDB(dbPath)
	require.NoError(t, err)
	defer db.Close()

	// Insert the singleton retention config.
	_, err = db.Exec(`
		INSERT INTO retention_config (id, retention_days, updated_at)
		VALUES (1, 90, datetime('now'))`)
	require.NoError(t, err)

	// Attempting a second insert with id=2 should fail (CHECK constraint).
	_, err = db.Exec(`
		INSERT INTO retention_config (id, retention_days, updated_at)
		VALUES (2, 30, datetime('now'))`)
	assert.Error(t, err)
}

func TestOpenDB_EventTypeFiltering(t *testing.T) {
	dir := t.TempDir()
	dbPath := filepath.Join(dir, "telemetry.db")

	db, err := OpenDB(dbPath)
	require.NoError(t, err)
	defer db.Close()

	// Insert multiple event types.
	events := []struct {
		id        string
		eventType string
		taskID    string
	}{
		{"evt-001", "agent_call_start", "task-1"},
		{"evt-002", "agent_call_end", "task-1"},
		{"evt-003", "phase_transition", "task-1"},
		{"evt-004", "agent_call_start", "task-2"},
		{"evt-005", "host_action", "task-2"},
	}

	for _, e := range events {
		_, err = db.Exec(`
			INSERT INTO telemetry_events (event_id, event_type, task_id, timestamp, attributes)
			VALUES (?, ?, ?, datetime('now'), '{}')`,
			e.id, e.eventType, e.taskID)
		require.NoError(t, err)
	}

	// Query by event type.
	var count int
	err = db.QueryRow(`SELECT COUNT(*) FROM telemetry_events WHERE event_type = ?`, "agent_call_start").Scan(&count)
	require.NoError(t, err)
	assert.Equal(t, 2, count)

	// Query by task_id.
	err = db.QueryRow(`SELECT COUNT(*) FROM telemetry_events WHERE task_id = ?`, "task-2").Scan(&count)
	require.NoError(t, err)
	assert.Equal(t, 2, count)
}
