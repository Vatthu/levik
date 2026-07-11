package costledger

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

func TestOpenDB_CreatesSchema(t *testing.T) {
	dir := t.TempDir()
	dbPath := filepath.Join(dir, "cost_ledger.db")

	db, err := OpenDB(dbPath)
	require.NoError(t, err)
	defer db.Close()

	// Verify tables were created.
	tables := []string{"schema_version", "cost_records", "budget_strategies", "daily_ceilings"}
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
	dbPath := filepath.Join(dir, "cost_ledger.db")

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

func TestOpenDB_CostRecordsIndexes(t *testing.T) {
	dir := t.TempDir()
	dbPath := filepath.Join(dir, "cost_ledger.db")

	db, err := OpenDB(dbPath)
	require.NoError(t, err)
	defer db.Close()

	// Verify indexes exist.
	indexes := []string{"idx_cost_records_task_timestamp", "idx_cost_records_timestamp"}
	for _, idx := range indexes {
		var count int
		err := db.QueryRow(`SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND name=?`, idx).Scan(&count)
		require.NoError(t, err)
		assert.Equal(t, 1, count, "expected index %s to exist", idx)
	}
}

func TestOpenDB_WALMode(t *testing.T) {
	dir := t.TempDir()
	dbPath := filepath.Join(dir, "cost_ledger.db")

	db, err := OpenDB(dbPath)
	require.NoError(t, err)
	defer db.Close()

	var journalMode string
	err = db.QueryRow("PRAGMA journal_mode").Scan(&journalMode)
	require.NoError(t, err)
	assert.Equal(t, "wal", journalMode)
}

func TestOpenDB_InvalidPath(t *testing.T) {
	// Try to open a DB in a non-existent directory that can't be created.
	dbPath := filepath.Join("/nonexistent-path-xyz", "cost_ledger.db")
	_, err := OpenDB(dbPath)
	// On most systems this will fail because the directory doesn't exist.
	// The exact error depends on OS, but it should be non-nil.
	if _, statErr := os.Stat("/nonexistent-path-xyz"); os.IsNotExist(statErr) {
		assert.Error(t, err)
	}
}

func TestOpenDB_InsertAndQueryCostRecord(t *testing.T) {
	dir := t.TempDir()
	dbPath := filepath.Join(dir, "cost_ledger.db")

	db, err := OpenDB(dbPath)
	require.NoError(t, err)
	defer db.Close()

	// Insert a record.
	_, err = db.Exec(`
		INSERT INTO cost_records (record_id, task_id, role, model, provider, work_phase,
			input_tokens, output_tokens, cost_usd, estimated, duration_ms, invocation_id, timestamp)
		VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))`,
		"rec-001", "task-123", "planner", "claude-sonnet-4-20250514", "anthropic",
		"planning", 1000, 500, 0.0105, 0, 1500, "inv-001")
	require.NoError(t, err)

	// Query it back.
	var costUSD float64
	err = db.QueryRow("SELECT cost_usd FROM cost_records WHERE record_id = ?", "rec-001").Scan(&costUSD)
	require.NoError(t, err)
	assert.InDelta(t, 0.0105, costUSD, 1e-10)
}

func TestOpenDB_BudgetStrategiesDefaults(t *testing.T) {
	dir := t.TempDir()
	dbPath := filepath.Join(dir, "cost_ledger.db")

	db, err := OpenDB(dbPath)
	require.NoError(t, err)
	defer db.Close()

	// Insert a budget strategy using defaults.
	_, err = db.Exec(`
		INSERT INTO budget_strategies (task_type, created_at, updated_at)
		VALUES (?, datetime('now'), datetime('now'))`, "bugfix")
	require.NoError(t, err)

	var planning, implementation, verification, review float64
	err = db.QueryRow(`
		SELECT planning, implementation, verification, review
		FROM budget_strategies WHERE task_type = ?`, "bugfix").
		Scan(&planning, &implementation, &verification, &review)
	require.NoError(t, err)
	assert.Equal(t, 10.0, planning)
	assert.Equal(t, 60.0, implementation)
	assert.Equal(t, 20.0, verification)
	assert.Equal(t, 10.0, review)
}

func TestOpenDB_DailyCeilingSingleton(t *testing.T) {
	dir := t.TempDir()
	dbPath := filepath.Join(dir, "cost_ledger.db")

	db, err := OpenDB(dbPath)
	require.NoError(t, err)
	defer db.Close()

	// Insert the singleton ceiling record.
	_, err = db.Exec(`
		INSERT INTO daily_ceilings (id, max_daily_usd, reset_hour, created_at, updated_at)
		VALUES (1, 100.0, 0, datetime('now'), datetime('now'))`)
	require.NoError(t, err)

	// Attempting a second insert should fail (CHECK constraint).
	_, err = db.Exec(`
		INSERT INTO daily_ceilings (id, max_daily_usd, reset_hour, created_at, updated_at)
		VALUES (2, 200.0, 0, datetime('now'), datetime('now'))`)
	assert.Error(t, err)
}
