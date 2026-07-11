package costledger

import (
	"context"
	"database/sql"
	"fmt"
	"sync"
	"sync/atomic"
	"time"
)

// SQLiteLedger is the concrete implementation of the Ledger interface
// backed by SQLite for persistence with in-memory accumulators for
// fast reads on hot paths (task cumulative cost, daily total).
type SQLiteLedger struct {
	db   *sql.DB
	dbMu sync.Mutex // serializes database writes

	// taskTotals caches per-task cumulative cost for O(1) threshold checks.
	// Key: taskID (string), Value: *float64 (atomic-compatible via mutex)
	taskTotals sync.Map

	// dailyTotal tracks today's system-wide spend since the configured reset hour.
	dailyTotal   atomic.Int64 // stores float64 bits via math.Float64bits
	dailyTotalMu sync.Mutex   // protects daily total updates

	// resetHour is the UTC hour at which the daily accumulator resets (0-23).
	resetHour int

	// dailyStart is the timestamp at which the current daily window started.
	dailyStart time.Time

	// cbState holds the circuit breaker runtime state (notifier, task budgets, warnings).
	// Nil until SetCircuitBreakerConfig is called.
	cbState *circuitBreakerState
}

// LedgerConfig holds configuration options for creating a new SQLiteLedger.
type LedgerConfig struct {
	// ResetHour is the UTC hour (0-23) at which the daily accumulator resets.
	// Defaults to 0 (midnight UTC).
	ResetHour int
}

// NewSQLiteLedger creates a new SQLiteLedger backed by the given database.
// It rebuilds in-memory accumulators from existing records on startup.
func NewSQLiteLedger(db *sql.DB, cfg LedgerConfig) (*SQLiteLedger, error) {
	if cfg.ResetHour < 0 || cfg.ResetHour > 23 {
		cfg.ResetHour = 0
	}

	// Set busy timeout to avoid SQLITE_BUSY under concurrent access.
	if _, err := db.Exec("PRAGMA busy_timeout = 5000"); err != nil {
		return nil, fmt.Errorf("costledger: failed to set busy_timeout: %w", err)
	}

	l := &SQLiteLedger{
		db:        db,
		resetHour: cfg.ResetHour,
	}

	// Compute the current daily window start.
	l.dailyStart = l.computeDailyStart(time.Now().UTC())

	// Rebuild accumulators from database.
	if err := l.rebuildAccumulators(); err != nil {
		return nil, fmt.Errorf("costledger: failed to rebuild accumulators: %w", err)
	}

	return l, nil
}

// computeDailyStart returns the start time of the current daily window
// based on the configured reset hour.
func (l *SQLiteLedger) computeDailyStart(now time.Time) time.Time {
	today := time.Date(now.Year(), now.Month(), now.Day(), l.resetHour, 0, 0, 0, time.UTC)
	if now.Before(today) {
		// We haven't hit today's reset hour yet, so the window started yesterday.
		today = today.AddDate(0, 0, -1)
	}
	return today
}

// rebuildAccumulators loads per-task totals and daily total from the database.
func (l *SQLiteLedger) rebuildAccumulators() error {
	// Rebuild per-task totals.
	rows, err := l.db.Query(`SELECT task_id, SUM(cost_usd) FROM cost_records GROUP BY task_id`)
	if err != nil {
		return fmt.Errorf("query task totals: %w", err)
	}
	defer rows.Close()

	for rows.Next() {
		var taskID string
		var total float64
		if err := rows.Scan(&taskID, &total); err != nil {
			return fmt.Errorf("scan task total: %w", err)
		}
		l.taskTotals.Store(taskID, total)
	}
	if err := rows.Err(); err != nil {
		return fmt.Errorf("iterate task totals: %w", err)
	}

	// Rebuild daily total from records since the daily window start.
	var dailyTotal float64
	err = l.db.QueryRow(
		`SELECT COALESCE(SUM(cost_usd), 0) FROM cost_records WHERE timestamp >= ?`,
		l.dailyStart.Format(time.RFC3339),
	).Scan(&dailyTotal)
	if err != nil {
		return fmt.Errorf("query daily total: %w", err)
	}
	l.storeDailyTotal(dailyTotal)

	return nil
}

// Record persists a cost record and updates in-memory accumulators.
func (l *SQLiteLedger) Record(ctx context.Context, rec CostRecord) error {
	// Check if daily window needs rotation.
	l.rotateDailyIfNeeded()

	// Serialize database writes to avoid SQLITE_BUSY under concurrent access.
	l.dbMu.Lock()
	_, err := l.db.ExecContext(ctx, `
		INSERT INTO cost_records (record_id, task_id, role, model, provider, work_phase,
			input_tokens, output_tokens, cost_usd, estimated, duration_ms, invocation_id, timestamp)
		VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
		rec.RecordID, rec.TaskID, rec.Role, rec.Model, rec.Provider, rec.WorkPhase,
		rec.InputTokens, rec.OutputTokens, rec.CostUSD, boolToInt(rec.Estimated),
		rec.DurationMS, rec.InvocationID, rec.Timestamp.UTC().Format(time.RFC3339Nano),
	)
	l.dbMu.Unlock()
	if err != nil {
		return fmt.Errorf("costledger: failed to insert record: %w", err)
	}

	// Update per-task accumulator.
	l.addToTaskTotal(rec.TaskID, rec.CostUSD)

	// Update daily accumulator.
	l.addToDailyTotal(rec.CostUSD)

	return nil
}

// TaskCumulative returns the total cost incurred by a task across all calls.
func (l *SQLiteLedger) TaskCumulative(ctx context.Context, taskID string) (float64, error) {
	val, ok := l.taskTotals.Load(taskID)
	if !ok {
		return 0, nil
	}
	return val.(float64), nil
}

// DailyTotal returns the system-wide daily spend since the last reset.
func (l *SQLiteLedger) DailyTotal(ctx context.Context) (float64, error) {
	l.rotateDailyIfNeeded()
	return l.loadDailyTotal(), nil
}

// PhaseBudgetRemaining returns the remaining budget for a specific work phase
// within a task, based on the task's BudgetStrategy and total budget.
func (l *SQLiteLedger) PhaseBudgetRemaining(ctx context.Context, taskID, phase string) (float64, error) {
	// Look up the task's budget strategy. We derive the task type from the budget_strategies table.
	// For now, we use "default" as the task type if no specific entry is found.
	strategy, err := l.getTaskBudgetStrategy(ctx, taskID)
	if err != nil {
		return 0, fmt.Errorf("costledger: get budget strategy: %w", err)
	}

	// Get the task's total budget from its constraints.
	totalBudget, err := l.getTaskTotalBudget(ctx, taskID)
	if err != nil {
		return 0, fmt.Errorf("costledger: get task budget: %w", err)
	}
	if totalBudget <= 0 {
		// No budget cap means unlimited.
		return 0, nil
	}

	// Calculate allocated budget for this phase.
	phasePercentage := l.phasePercentage(strategy, phase)
	phaseAllocation := totalBudget * (phasePercentage / 100.0)

	// Get actual spend for this phase.
	var phaseSpend float64
	err = l.db.QueryRowContext(ctx,
		`SELECT COALESCE(SUM(cost_usd), 0) FROM cost_records WHERE task_id = ? AND work_phase = ?`,
		taskID, phase,
	).Scan(&phaseSpend)
	if err != nil {
		return 0, fmt.Errorf("costledger: query phase spend: %w", err)
	}

	remaining := phaseAllocation - phaseSpend
	if remaining < 0 {
		remaining = 0
	}
	return remaining, nil
}

// Forecast is implemented in forecast.go

// CheckCircuitBreaker is implemented in circuit_breaker.go.

// --- Internal helpers ---

// addToTaskTotal atomically adds cost to a task's running total.
func (l *SQLiteLedger) addToTaskTotal(taskID string, cost float64) {
	for {
		val, loaded := l.taskTotals.LoadOrStore(taskID, cost)
		if !loaded {
			// Successfully stored initial value.
			return
		}
		current := val.(float64)
		if l.taskTotals.CompareAndSwap(taskID, current, current+cost) {
			return
		}
		// CAS failed, retry.
	}
}

// storeDailyTotal stores the daily total using atomic int64 (bit-level float64 storage).
func (l *SQLiteLedger) storeDailyTotal(total float64) {
	l.dailyTotalMu.Lock()
	defer l.dailyTotalMu.Unlock()
	l.dailyTotal.Store(int64(total * 1e9)) // store as nano-dollars for precision
}

// loadDailyTotal loads the current daily total.
func (l *SQLiteLedger) loadDailyTotal() float64 {
	return float64(l.dailyTotal.Load()) / 1e9
}

// addToDailyTotal adds cost to the daily total.
func (l *SQLiteLedger) addToDailyTotal(cost float64) {
	l.dailyTotalMu.Lock()
	defer l.dailyTotalMu.Unlock()
	current := float64(l.dailyTotal.Load()) / 1e9
	l.dailyTotal.Store(int64((current + cost) * 1e9))
}

// rotateDailyIfNeeded resets the daily accumulator if the window has elapsed.
func (l *SQLiteLedger) rotateDailyIfNeeded() {
	now := time.Now().UTC()
	newStart := l.computeDailyStart(now)
	if newStart.After(l.dailyStart) {
		l.dailyTotalMu.Lock()
		defer l.dailyTotalMu.Unlock()
		// Double-check after acquiring lock.
		newStart = l.computeDailyStart(now)
		if newStart.After(l.dailyStart) {
			l.dailyStart = newStart
			l.dailyTotal.Store(0)
		}
	}
}

// getTaskBudgetStrategy retrieves the budget strategy for a task.
// It first checks for a task-type-specific strategy, then falls back to "default".
func (l *SQLiteLedger) getTaskBudgetStrategy(ctx context.Context, taskID string) (BudgetStrategy, error) {
	// For now, try to find a "default" budget strategy in the table.
	// Future: look up the task's type and use that.
	var strategy BudgetStrategy
	err := l.db.QueryRowContext(ctx,
		`SELECT planning, implementation, verification, review FROM budget_strategies WHERE task_type = ?`,
		"default",
	).Scan(&strategy.Planning, &strategy.Implementation, &strategy.Verification, &strategy.Review)
	if err == sql.ErrNoRows {
		// Return the standard default: 10/60/20/10
		return BudgetStrategy{
			Planning:       10.0,
			Implementation: 60.0,
			Verification:   20.0,
			Review:         10.0,
		}, nil
	}
	if err != nil {
		return BudgetStrategy{}, err
	}
	return strategy, nil
}

// getTaskTotalBudget retrieves the total budget for a task.
// It first checks the circuit breaker's TaskBudgetStore (if configured),
// then returns 0 (unlimited) if no budget is registered.
func (l *SQLiteLedger) getTaskTotalBudget(ctx context.Context, taskID string) (float64, error) {
	if l.cbState != nil {
		if budget, ok := l.cbState.taskBudgets.Lookup(taskID); ok && budget > 0 {
			return budget, nil
		}
	}
	return 0, nil
}

// phasePercentage returns the budget percentage for a given phase.
func (l *SQLiteLedger) phasePercentage(strategy BudgetStrategy, phase string) float64 {
	switch phase {
	case "planning":
		return strategy.Planning
	case "implementation":
		return strategy.Implementation
	case "verification":
		return strategy.Verification
	case "review":
		return strategy.Review
	default:
		return 0
	}
}

// boolToInt converts a boolean to SQLite integer (0/1).
func boolToInt(b bool) int {
	if b {
		return 1
	}
	return 0
}
