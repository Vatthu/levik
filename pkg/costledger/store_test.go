package costledger

import (
	"context"
	"fmt"
	"path/filepath"
	"sync"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

func setupTestLedger(t *testing.T) *SQLiteLedger {
	t.Helper()
	dir := t.TempDir()
	dbPath := filepath.Join(dir, "test_ledger.db")
	db, err := OpenDB(dbPath)
	require.NoError(t, err)
	t.Cleanup(func() { db.Close() })

	ledger, err := NewSQLiteLedger(db, LedgerConfig{ResetHour: 0})
	require.NoError(t, err)
	return ledger
}

func TestNewSQLiteLedger_EmptyDB(t *testing.T) {
	ledger := setupTestLedger(t)
	assert.NotNil(t, ledger)
}

func TestRecord_InsertsAndUpdatesAccumulators(t *testing.T) {
	ledger := setupTestLedger(t)
	ctx := context.Background()

	rec := CostRecord{
		RecordID:     "rec-001",
		TaskID:       "task-abc",
		Role:         "planner",
		Model:        "claude-sonnet-4-20250514",
		Provider:     "anthropic",
		WorkPhase:    "planning",
		InputTokens:  1000,
		OutputTokens: 500,
		CostUSD:      0.0105,
		Estimated:    false,
		DurationMS:   1500,
		InvocationID: "inv-001",
		Timestamp:    time.Now().UTC(),
	}

	err := ledger.Record(ctx, rec)
	require.NoError(t, err)

	// Verify task cumulative.
	cumulative, err := ledger.TaskCumulative(ctx, "task-abc")
	require.NoError(t, err)
	assert.InDelta(t, 0.0105, cumulative, 1e-10)

	// Verify daily total.
	daily, err := ledger.DailyTotal(ctx)
	require.NoError(t, err)
	assert.InDelta(t, 0.0105, daily, 1e-6)
}

func TestRecord_MultipleRecordsSameTask(t *testing.T) {
	ledger := setupTestLedger(t)
	ctx := context.Background()

	for i := 0; i < 3; i++ {
		rec := CostRecord{
			RecordID:     fmt.Sprintf("rec-%03d", i),
			TaskID:       "task-xyz",
			Role:         "implementer",
			Model:        "gpt-4o",
			Provider:     "openai",
			WorkPhase:    "implementation",
			InputTokens:  2000,
			OutputTokens: 1000,
			CostUSD:      0.015,
			Estimated:    false,
			DurationMS:   2000,
			InvocationID: fmt.Sprintf("inv-%03d", i),
			Timestamp:    time.Now().UTC(),
		}
		err := ledger.Record(ctx, rec)
		require.NoError(t, err)
	}

	cumulative, err := ledger.TaskCumulative(ctx, "task-xyz")
	require.NoError(t, err)
	assert.InDelta(t, 0.045, cumulative, 1e-10)
}

func TestTaskCumulative_UnknownTask(t *testing.T) {
	ledger := setupTestLedger(t)
	ctx := context.Background()

	cumulative, err := ledger.TaskCumulative(ctx, "nonexistent-task")
	require.NoError(t, err)
	assert.Equal(t, 0.0, cumulative)
}

func TestDailyTotal_AcrossMultipleTasks(t *testing.T) {
	ledger := setupTestLedger(t)
	ctx := context.Background()

	tasks := []string{"task-1", "task-2", "task-3"}
	for i, taskID := range tasks {
		rec := CostRecord{
			RecordID:     fmt.Sprintf("rec-%d", i),
			TaskID:       taskID,
			Role:         "planner",
			Model:        "gpt-4o-mini",
			Provider:     "openai",
			WorkPhase:    "planning",
			InputTokens:  500,
			OutputTokens: 200,
			CostUSD:      0.01,
			Estimated:    false,
			DurationMS:   800,
			InvocationID: fmt.Sprintf("inv-%d", i),
			Timestamp:    time.Now().UTC(),
		}
		err := ledger.Record(ctx, rec)
		require.NoError(t, err)
	}

	daily, err := ledger.DailyTotal(ctx)
	require.NoError(t, err)
	assert.InDelta(t, 0.03, daily, 1e-6)
}

func TestPhaseBudgetRemaining_DefaultStrategy(t *testing.T) {
	ledger := setupTestLedger(t)
	ctx := context.Background()

	// With no task budget stored, PhaseBudgetRemaining returns 0 (unlimited).
	remaining, err := ledger.PhaseBudgetRemaining(ctx, "task-abc", "planning")
	require.NoError(t, err)
	assert.Equal(t, 0.0, remaining)
}

func TestPhaseBudgetRemaining_WithBudgetStrategy(t *testing.T) {
	ledger := setupTestLedger(t)
	ctx := context.Background()

	// Insert a "default" budget strategy.
	_, err := ledger.db.Exec(`
		INSERT INTO budget_strategies (task_type, planning, implementation, verification, review, created_at, updated_at)
		VALUES ('default', 10.0, 60.0, 20.0, 10.0, datetime('now'), datetime('now'))`)
	require.NoError(t, err)

	// Without a task budget, remaining is still 0.
	remaining, err := ledger.PhaseBudgetRemaining(ctx, "task-test", "implementation")
	require.NoError(t, err)
	assert.Equal(t, 0.0, remaining)
}

func TestRecord_EstimatedFlag(t *testing.T) {
	ledger := setupTestLedger(t)
	ctx := context.Background()

	rec := CostRecord{
		RecordID:     "rec-est",
		TaskID:       "task-est",
		Role:         "reviewer",
		Model:        "claude-haiku-3-5-20241022",
		Provider:     "anthropic",
		WorkPhase:    "review",
		InputTokens:  800,
		OutputTokens: 200,
		CostUSD:      0.001,
		Estimated:    true,
		DurationMS:   500,
		InvocationID: "inv-est",
		Timestamp:    time.Now().UTC(),
	}

	err := ledger.Record(ctx, rec)
	require.NoError(t, err)

	// Verify estimated flag is stored.
	var estimated int
	err = ledger.db.QueryRow(
		`SELECT estimated FROM cost_records WHERE record_id = ?`, "rec-est",
	).Scan(&estimated)
	require.NoError(t, err)
	assert.Equal(t, 1, estimated)
}

func TestForecast_NoHistory(t *testing.T) {
	ledger := setupTestLedger(t)
	ctx := context.Background()

	forecast, err := ledger.Forecast(ctx, "moderate", 5)
	require.NoError(t, err)
	// Without historical data, falls back to heuristic.
	assert.Equal(t, 0, forecast.BasisTaskCount)
	assert.Equal(t, 0.0, forecast.ConfidenceLevel)
	assert.Greater(t, forecast.ExpectedCostUSD, 0.0)
}

func TestCheckCircuitBreaker_Stub(t *testing.T) {
	ledger := setupTestLedger(t)
	ctx := context.Background()

	shouldBreak, reason, err := ledger.CheckCircuitBreaker(ctx, "task-abc")
	require.NoError(t, err)
	assert.False(t, shouldBreak)
	assert.Empty(t, reason)
}

func TestConcurrentRecords(t *testing.T) {
	ledger := setupTestLedger(t)
	ctx := context.Background()

	var wg sync.WaitGroup
	numGoroutines := 10

	for i := 0; i < numGoroutines; i++ {
		wg.Add(1)
		go func(idx int) {
			defer wg.Done()
			rec := CostRecord{
				RecordID:     fmt.Sprintf("rec-concurrent-%d", idx),
				TaskID:       "task-concurrent",
				Role:         "implementer",
				Model:        "gpt-4o",
				Provider:     "openai",
				WorkPhase:    "implementation",
				InputTokens:  1000,
				OutputTokens: 500,
				CostUSD:      0.01,
				Estimated:    false,
				DurationMS:   1000,
				InvocationID: fmt.Sprintf("inv-concurrent-%d", idx),
				Timestamp:    time.Now().UTC(),
			}
			err := ledger.Record(ctx, rec)
			assert.NoError(t, err)
		}(i)
	}

	wg.Wait()

	cumulative, err := ledger.TaskCumulative(ctx, "task-concurrent")
	require.NoError(t, err)
	assert.InDelta(t, 0.1, cumulative, 1e-6)

	daily, err := ledger.DailyTotal(ctx)
	require.NoError(t, err)
	assert.InDelta(t, 0.1, daily, 1e-6)
}

func TestRebuildAccumulators(t *testing.T) {
	dir := t.TempDir()
	dbPath := filepath.Join(dir, "rebuild_test.db")

	db, err := OpenDB(dbPath)
	require.NoError(t, err)

	// Insert records directly into DB.
	now := time.Now().UTC()
	for i := 0; i < 5; i++ {
		_, err = db.Exec(`
			INSERT INTO cost_records (record_id, task_id, role, model, provider, work_phase,
				input_tokens, output_tokens, cost_usd, estimated, duration_ms, invocation_id, timestamp)
			VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
			fmt.Sprintf("rec-rebuild-%d", i), "task-rebuild", "planner", "gpt-4o", "openai",
			"planning", 1000, 500, 0.02, 0, 1000, fmt.Sprintf("inv-rebuild-%d", i),
			now.Format(time.RFC3339Nano),
		)
		require.NoError(t, err)
	}

	// Create ledger — should rebuild accumulators from existing records.
	ledger, err := NewSQLiteLedger(db, LedgerConfig{ResetHour: 0})
	require.NoError(t, err)
	defer db.Close()

	ctx := context.Background()

	cumulative, err := ledger.TaskCumulative(ctx, "task-rebuild")
	require.NoError(t, err)
	assert.InDelta(t, 0.1, cumulative, 1e-10)

	daily, err := ledger.DailyTotal(ctx)
	require.NoError(t, err)
	assert.InDelta(t, 0.1, daily, 1e-6)
}

func TestComputeDailyStart(t *testing.T) {
	ledger := &SQLiteLedger{resetHour: 6}

	// At 10:00 UTC, the daily window started at 06:00 today.
	now := time.Date(2024, 1, 15, 10, 0, 0, 0, time.UTC)
	start := ledger.computeDailyStart(now)
	expected := time.Date(2024, 1, 15, 6, 0, 0, 0, time.UTC)
	assert.Equal(t, expected, start)

	// At 04:00 UTC (before reset hour), the daily window started at 06:00 yesterday.
	now = time.Date(2024, 1, 15, 4, 0, 0, 0, time.UTC)
	start = ledger.computeDailyStart(now)
	expected = time.Date(2024, 1, 14, 6, 0, 0, 0, time.UTC)
	assert.Equal(t, expected, start)
}

func TestPhasePercentage(t *testing.T) {
	ledger := &SQLiteLedger{}
	strategy := BudgetStrategy{
		Planning:       10.0,
		Implementation: 60.0,
		Verification:   20.0,
		Review:         10.0,
	}

	assert.Equal(t, 10.0, ledger.phasePercentage(strategy, "planning"))
	assert.Equal(t, 60.0, ledger.phasePercentage(strategy, "implementation"))
	assert.Equal(t, 20.0, ledger.phasePercentage(strategy, "verification"))
	assert.Equal(t, 10.0, ledger.phasePercentage(strategy, "review"))
	assert.Equal(t, 0.0, ledger.phasePercentage(strategy, "unknown"))
}
