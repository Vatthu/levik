package costledger

import (
	"context"
	"fmt"
	"path/filepath"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// mockNotifier records all notifications for test verification.
type mockNotifier struct {
	notifications []notification
}

type notification struct {
	taskID  string
	message string
}

func (m *mockNotifier) Notify(_ context.Context, taskID, message string) error {
	m.notifications = append(m.notifications, notification{taskID: taskID, message: message})
	return nil
}

func setupCircuitBreakerLedger(t *testing.T, ceiling float64) (*SQLiteLedger, *mockNotifier) {
	t.Helper()
	dir := t.TempDir()
	dbPath := filepath.Join(dir, "cb_test.db")
	db, err := OpenDB(dbPath)
	require.NoError(t, err)
	t.Cleanup(func() { db.Close() })

	ledger, err := NewSQLiteLedger(db, LedgerConfig{ResetHour: 0})
	require.NoError(t, err)

	// Insert a daily ceiling if configured.
	if ceiling > 0 {
		_, err = db.Exec(`INSERT INTO daily_ceilings (id, max_daily_usd, reset_hour, created_at, updated_at)
			VALUES (1, ?, 0, datetime('now'), datetime('now'))`, ceiling)
		require.NoError(t, err)
	}

	notifier := &mockNotifier{}
	budgets := NewTaskBudgetStore()

	ledger.SetCircuitBreakerConfig(CircuitBreakerConfig{
		WarningThreshold: 0.8,
		Notifier:         notifier,
		TaskBudgets:      budgets,
	})

	return ledger, notifier
}

func TestCheckCircuitBreaker_NoBudget_NoBreak(t *testing.T) {
	ledger, _ := setupCircuitBreakerLedger(t, 0)
	ctx := context.Background()

	// No budget registered, no daily ceiling → no break.
	shouldBreak, reason, err := ledger.CheckCircuitBreaker(ctx, "task-no-budget")
	require.NoError(t, err)
	assert.False(t, shouldBreak)
	assert.Empty(t, reason)
}

func TestCheckCircuitBreaker_BelowWarningThreshold(t *testing.T) {
	ledger, notifier := setupCircuitBreakerLedger(t, 0)
	ctx := context.Background()

	// Register a $1.00 budget.
	ledger.cbState.taskBudgets.Register("task-1", 1.00)

	// Record $0.50 worth (50% of budget, below 80%).
	err := ledger.Record(ctx, CostRecord{
		RecordID: "r1", TaskID: "task-1", Role: "planner", Model: "gpt-4o",
		Provider: "openai", WorkPhase: "planning", InputTokens: 1000, OutputTokens: 500,
		CostUSD: 0.50, InvocationID: "inv-1", Timestamp: time.Now().UTC(),
	})
	require.NoError(t, err)

	shouldBreak, reason, err := ledger.CheckCircuitBreaker(ctx, "task-1")
	require.NoError(t, err)
	assert.False(t, shouldBreak)
	assert.Empty(t, reason)
	assert.Empty(t, notifier.notifications, "no warning expected below 80%")
}

func TestCheckCircuitBreaker_WarningAt80Percent(t *testing.T) {
	ledger, notifier := setupCircuitBreakerLedger(t, 0)
	ctx := context.Background()

	// Register a $1.00 budget.
	ledger.cbState.taskBudgets.Register("task-warn", 1.00)

	// Record $0.85 (85% of budget, above 80% threshold).
	err := ledger.Record(ctx, CostRecord{
		RecordID: "r1", TaskID: "task-warn", Role: "implementer", Model: "gpt-4o",
		Provider: "openai", WorkPhase: "implementation", InputTokens: 2000, OutputTokens: 1000,
		CostUSD: 0.85, InvocationID: "inv-1", Timestamp: time.Now().UTC(),
	})
	require.NoError(t, err)

	shouldBreak, reason, err := ledger.CheckCircuitBreaker(ctx, "task-warn")
	require.NoError(t, err)
	assert.False(t, shouldBreak, "should not break at 85%, only warn")
	assert.Empty(t, reason)
	require.Len(t, notifier.notifications, 1, "should have sent one warning")
	assert.Equal(t, "task-warn", notifier.notifications[0].taskID)
	assert.Contains(t, notifier.notifications[0].message, "80%")
}

func TestCheckCircuitBreaker_WarningNotRepeated(t *testing.T) {
	ledger, notifier := setupCircuitBreakerLedger(t, 0)
	ctx := context.Background()

	ledger.cbState.taskBudgets.Register("task-once", 1.00)

	// Record $0.85 to trigger warning.
	err := ledger.Record(ctx, CostRecord{
		RecordID: "r1", TaskID: "task-once", Role: "implementer", Model: "gpt-4o",
		Provider: "openai", WorkPhase: "implementation", InputTokens: 2000, OutputTokens: 1000,
		CostUSD: 0.85, InvocationID: "inv-1", Timestamp: time.Now().UTC(),
	})
	require.NoError(t, err)

	// First check — fires warning.
	_, _, err = ledger.CheckCircuitBreaker(ctx, "task-once")
	require.NoError(t, err)
	require.Len(t, notifier.notifications, 1)

	// Second check — should NOT repeat.
	_, _, err = ledger.CheckCircuitBreaker(ctx, "task-once")
	require.NoError(t, err)
	assert.Len(t, notifier.notifications, 1, "warning should not repeat")
}

func TestCheckCircuitBreaker_BudgetExhausted(t *testing.T) {
	ledger, _ := setupCircuitBreakerLedger(t, 0)
	ctx := context.Background()

	ledger.cbState.taskBudgets.Register("task-break", 1.00)

	// Record $1.00 exactly (100% of budget).
	err := ledger.Record(ctx, CostRecord{
		RecordID: "r1", TaskID: "task-break", Role: "implementer", Model: "gpt-4o",
		Provider: "openai", WorkPhase: "implementation", InputTokens: 5000, OutputTokens: 2000,
		CostUSD: 1.00, InvocationID: "inv-1", Timestamp: time.Now().UTC(),
	})
	require.NoError(t, err)

	shouldBreak, reason, err := ledger.CheckCircuitBreaker(ctx, "task-break")
	require.NoError(t, err)
	assert.True(t, shouldBreak)
	assert.Equal(t, "budget_exceeded", reason)
}

func TestCheckCircuitBreaker_BudgetExceeded(t *testing.T) {
	ledger, _ := setupCircuitBreakerLedger(t, 0)
	ctx := context.Background()

	ledger.cbState.taskBudgets.Register("task-over", 0.50)

	// Record $0.75 (over the $0.50 limit).
	err := ledger.Record(ctx, CostRecord{
		RecordID: "r1", TaskID: "task-over", Role: "implementer", Model: "gpt-4o",
		Provider: "openai", WorkPhase: "implementation", InputTokens: 5000, OutputTokens: 2000,
		CostUSD: 0.75, InvocationID: "inv-1", Timestamp: time.Now().UTC(),
	})
	require.NoError(t, err)

	shouldBreak, reason, err := ledger.CheckCircuitBreaker(ctx, "task-over")
	require.NoError(t, err)
	assert.True(t, shouldBreak)
	assert.Equal(t, "budget_exceeded", reason)
}

func TestCheckCircuitBreaker_DailyCeilingExceeded(t *testing.T) {
	ledger, _ := setupCircuitBreakerLedger(t, 5.00) // $5 daily ceiling
	ctx := context.Background()

	// Record multiple tasks totaling > $5.
	for i := range 6 {
		taskID := fmt.Sprintf("task-%d", i)
		err := ledger.Record(ctx, CostRecord{
			RecordID: fmt.Sprintf("r-%d", i), TaskID: taskID, Role: "planner", Model: "gpt-4o",
			Provider: "openai", WorkPhase: "planning", InputTokens: 1000, OutputTokens: 500,
			CostUSD: 1.00, InvocationID: fmt.Sprintf("inv-%d", i), Timestamp: time.Now().UTC(),
		})
		require.NoError(t, err)
	}

	// Check any task — daily ceiling should trip.
	shouldBreak, reason, err := ledger.CheckCircuitBreaker(ctx, "task-no-budget-registered")
	require.NoError(t, err)
	assert.True(t, shouldBreak)
	assert.Equal(t, "daily_ceiling_exceeded", reason)
}

func TestCheckCircuitBreaker_TaskBudgetBeforeDailyCeiling(t *testing.T) {
	// When both task budget AND daily ceiling would trigger, task budget takes priority.
	ledger, _ := setupCircuitBreakerLedger(t, 2.00) // $2 daily ceiling
	ctx := context.Background()

	ledger.cbState.taskBudgets.Register("task-both", 0.50)

	// Record $0.60 for task-both (exceeds $0.50 budget AND contributes to daily).
	err := ledger.Record(ctx, CostRecord{
		RecordID: "r1", TaskID: "task-both", Role: "implementer", Model: "gpt-4o",
		Provider: "openai", WorkPhase: "implementation", InputTokens: 5000, OutputTokens: 2000,
		CostUSD: 0.60, InvocationID: "inv-1", Timestamp: time.Now().UTC(),
	})
	require.NoError(t, err)

	shouldBreak, reason, err := ledger.CheckCircuitBreaker(ctx, "task-both")
	require.NoError(t, err)
	assert.True(t, shouldBreak)
	assert.Equal(t, "budget_exceeded", reason, "task budget should trigger before daily ceiling")
}

func TestCheckCircuitBreaker_NoCBState(t *testing.T) {
	// Without calling SetCircuitBreakerConfig, the circuit breaker is disabled.
	ledger := setupTestLedger(t)
	ctx := context.Background()

	shouldBreak, reason, err := ledger.CheckCircuitBreaker(ctx, "any-task")
	require.NoError(t, err)
	assert.False(t, shouldBreak)
	assert.Empty(t, reason)
}

func TestResetTaskWarning(t *testing.T) {
	ledger, notifier := setupCircuitBreakerLedger(t, 0)
	ctx := context.Background()

	ledger.cbState.taskBudgets.Register("task-reset", 1.00)

	// Spend 85% to trigger warning.
	err := ledger.Record(ctx, CostRecord{
		RecordID: "r1", TaskID: "task-reset", Role: "implementer", Model: "gpt-4o",
		Provider: "openai", WorkPhase: "implementation", InputTokens: 2000, OutputTokens: 1000,
		CostUSD: 0.85, InvocationID: "inv-1", Timestamp: time.Now().UTC(),
	})
	require.NoError(t, err)

	_, _, err = ledger.CheckCircuitBreaker(ctx, "task-reset")
	require.NoError(t, err)
	require.Len(t, notifier.notifications, 1)

	// Founder increases budget to $2.00 and resets warning.
	ledger.cbState.taskBudgets.Update("task-reset", 2.00)
	ledger.ResetTaskWarning("task-reset")

	// Now spend more to trigger the new 80% threshold ($1.60).
	err = ledger.Record(ctx, CostRecord{
		RecordID: "r2", TaskID: "task-reset", Role: "implementer", Model: "gpt-4o",
		Provider: "openai", WorkPhase: "implementation", InputTokens: 2000, OutputTokens: 1000,
		CostUSD: 0.80, InvocationID: "inv-2", Timestamp: time.Now().UTC(),
	})
	require.NoError(t, err)

	// Total is now $1.65, new 80% is $1.60 → should warn again.
	_, _, err = ledger.CheckCircuitBreaker(ctx, "task-reset")
	require.NoError(t, err)
	assert.Len(t, notifier.notifications, 2, "should have sent a second warning after reset")
}

func TestTaskBudgetStore_CRUD(t *testing.T) {
	store := NewTaskBudgetStore()

	// Initially empty.
	_, ok := store.Lookup("task-1")
	assert.False(t, ok)

	// Register.
	store.Register("task-1", 5.00)
	budget, ok := store.Lookup("task-1")
	assert.True(t, ok)
	assert.Equal(t, 5.00, budget)

	// Update.
	store.Update("task-1", 10.00)
	budget, ok = store.Lookup("task-1")
	assert.True(t, ok)
	assert.Equal(t, 10.00, budget)

	// Remove.
	store.Remove("task-1")
	_, ok = store.Lookup("task-1")
	assert.False(t, ok)
}

func TestCheckCircuitBreaker_DailyResetClearsCeiling(t *testing.T) {
	// Verify that rotateDailyIfNeeded() resets the daily total, which would
	// clear a previously triggered daily ceiling.
	dir := t.TempDir()
	dbPath := filepath.Join(dir, "daily_reset_test.db")
	db, err := OpenDB(dbPath)
	require.NoError(t, err)
	t.Cleanup(func() { db.Close() })

	// Use a reset hour that's already passed for the current day.
	ledger, err := NewSQLiteLedger(db, LedgerConfig{ResetHour: 0})
	require.NoError(t, err)

	// Insert a daily ceiling of $1.
	_, err = db.Exec(`INSERT INTO daily_ceilings (id, max_daily_usd, reset_hour, created_at, updated_at)
		VALUES (1, 1.0, 0, datetime('now'), datetime('now'))`)
	require.NoError(t, err)

	notifier := &mockNotifier{}
	ledger.SetCircuitBreakerConfig(CircuitBreakerConfig{
		WarningThreshold: 0.8,
		Notifier:         notifier,
		TaskBudgets:      NewTaskBudgetStore(),
	})

	ctx := context.Background()

	// Record $1.50 (exceeds $1 daily ceiling).
	err = ledger.Record(ctx, CostRecord{
		RecordID: "r1", TaskID: "task-daily", Role: "planner", Model: "gpt-4o",
		Provider: "openai", WorkPhase: "planning", InputTokens: 1000, OutputTokens: 500,
		CostUSD: 1.50, InvocationID: "inv-1", Timestamp: time.Now().UTC(),
	})
	require.NoError(t, err)

	shouldBreak, reason, err := ledger.CheckCircuitBreaker(ctx, "task-daily")
	require.NoError(t, err)
	assert.True(t, shouldBreak)
	assert.Equal(t, "daily_ceiling_exceeded", reason)

	// Simulate daily rotation by manually moving the dailyStart backward
	// and resetting the daily total.
	ledger.dailyTotalMu.Lock()
	ledger.dailyStart = time.Now().UTC().AddDate(0, 0, -2)
	ledger.dailyTotal.Store(0)
	ledger.dailyTotalMu.Unlock()

	// After reset, the circuit breaker should not trip for daily ceiling.
	shouldBreak, reason, err = ledger.CheckCircuitBreaker(ctx, "task-daily")
	require.NoError(t, err)
	assert.False(t, shouldBreak)
	assert.Empty(t, reason)
}
