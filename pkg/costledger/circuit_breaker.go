package costledger

import (
	"context"
	"database/sql"
	"fmt"
	"sync"
)

// Notifier defines the interface for sending budget-related notifications
// to the founder via configured operator channels.
type Notifier interface {
	// Notify sends a notification message associated with a specific task.
	Notify(ctx context.Context, taskID, message string) error
}

// noopNotifier is a default no-op implementation of Notifier used when
// no notifier is configured.
type noopNotifier struct{}

func (n *noopNotifier) Notify(_ context.Context, _, _ string) error { return nil }

// TaskBudgetStore manages the mapping from task IDs to their maximum budget (max_cost_usd).
// Budgets can be registered when a task is created and looked up during circuit breaker checks.
type TaskBudgetStore struct {
	mu      sync.RWMutex
	budgets map[string]float64 // taskID -> max_cost_usd
}

// NewTaskBudgetStore creates a new in-memory task budget store.
func NewTaskBudgetStore() *TaskBudgetStore {
	return &TaskBudgetStore{
		budgets: make(map[string]float64),
	}
}

// Register stores the max budget for a task. If maxCostUSD <= 0, the task
// has no budget cap (unlimited).
func (s *TaskBudgetStore) Register(taskID string, maxCostUSD float64) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.budgets[taskID] = maxCostUSD
}

// Update changes the budget cap for a task. This is used when a founder
// resumes a budget-exceeded task with an increased limit.
func (s *TaskBudgetStore) Update(taskID string, newMaxCostUSD float64) {
	s.Register(taskID, newMaxCostUSD)
}

// Lookup returns the max budget for a task. Returns (0, false) if the task
// has no registered budget.
func (s *TaskBudgetStore) Lookup(taskID string) (float64, bool) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	budget, ok := s.budgets[taskID]
	return budget, ok
}

// Remove deletes the budget registration for a task (e.g., after task completion).
func (s *TaskBudgetStore) Remove(taskID string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	delete(s.budgets, taskID)
}

// CircuitBreakerConfig holds the configuration for circuit breaker behavior.
type CircuitBreakerConfig struct {
	// WarningThreshold is the percentage (0.0–1.0) of budget at which a warning
	// is emitted. Default: 0.8 (80%).
	WarningThreshold float64

	// Notifier receives budget warning notifications. If nil, a no-op notifier is used.
	Notifier Notifier

	// TaskBudgets stores per-task budget caps.
	TaskBudgets *TaskBudgetStore
}

// warningsSent tracks which tasks have already had their 80% warning sent,
// to avoid repeated notifications.
type warningTracker struct {
	mu   sync.RWMutex
	sent map[string]bool
}

func newWarningTracker() *warningTracker {
	return &warningTracker{sent: make(map[string]bool)}
}

func (w *warningTracker) hasSent(taskID string) bool {
	w.mu.RLock()
	defer w.mu.RUnlock()
	return w.sent[taskID]
}

func (w *warningTracker) markSent(taskID string) {
	w.mu.Lock()
	defer w.mu.Unlock()
	w.sent[taskID] = true
}

func (w *warningTracker) reset(taskID string) {
	w.mu.Lock()
	defer w.mu.Unlock()
	delete(w.sent, taskID)
}

// circuitBreakerState holds the runtime state for the circuit breaker.
type circuitBreakerState struct {
	notifier         Notifier
	taskBudgets      *TaskBudgetStore
	warningThreshold float64
	warnings         *warningTracker
}

// SetCircuitBreakerConfig configures the circuit breaker on the SQLiteLedger.
// This must be called after NewSQLiteLedger to enable budget enforcement.
func (l *SQLiteLedger) SetCircuitBreakerConfig(cfg CircuitBreakerConfig) {
	notifier := cfg.Notifier
	if notifier == nil {
		notifier = &noopNotifier{}
	}

	threshold := cfg.WarningThreshold
	if threshold <= 0 || threshold >= 1.0 {
		threshold = 0.8
	}

	taskBudgets := cfg.TaskBudgets
	if taskBudgets == nil {
		taskBudgets = NewTaskBudgetStore()
	}

	l.cbState = &circuitBreakerState{
		notifier:         notifier,
		taskBudgets:      taskBudgets,
		warningThreshold: threshold,
		warnings:         newWarningTracker(),
	}
}

// CheckCircuitBreaker evaluates whether a task should be halted due to:
//  1. Per-task budget exhaustion (100% of max_cost_usd reached) → "budget_exceeded"
//  2. Global daily ceiling exceeded → "daily_ceiling_exceeded"
//
// It also triggers a warning notification when the task reaches 80% of its budget.
// Returns (shouldBreak, reason, error).
func (l *SQLiteLedger) CheckCircuitBreaker(ctx context.Context, taskID string) (bool, string, error) {
	// Ensure daily window is current.
	l.rotateDailyIfNeeded()

	// If circuit breaker is not configured, always allow (no break).
	if l.cbState == nil {
		return false, "", nil
	}

	// --- Check per-task budget ---
	if maxBudget, ok := l.cbState.taskBudgets.Lookup(taskID); ok && maxBudget > 0 {
		cumulative, err := l.TaskCumulative(ctx, taskID)
		if err != nil {
			return false, "", fmt.Errorf("circuit breaker: get task cumulative: %w", err)
		}

		// Check 100% threshold first (hard stop).
		if cumulative >= maxBudget {
			return true, "budget_exceeded", nil
		}

		// Check 80% warning threshold (soft notification).
		warningLevel := maxBudget * l.cbState.warningThreshold
		if cumulative >= warningLevel && !l.cbState.warnings.hasSent(taskID) {
			msg := fmt.Sprintf(
				"Task %s has reached %.0f%% of its budget (%.4f / %.4f USD)",
				taskID, l.cbState.warningThreshold*100, cumulative, maxBudget,
			)
			// Best-effort notification — don't fail the circuit breaker check on notify error.
			_ = l.cbState.notifier.Notify(ctx, taskID, msg)
			l.cbState.warnings.markSent(taskID)
		}
	}

	// --- Check global daily ceiling ---
	dailyCeiling, err := l.getDailyCeiling(ctx)
	if err != nil {
		return false, "", fmt.Errorf("circuit breaker: get daily ceiling: %w", err)
	}

	if dailyCeiling > 0 {
		dailyTotal := l.loadDailyTotal()
		if dailyTotal >= dailyCeiling {
			return true, "daily_ceiling_exceeded", nil
		}
	}

	return false, "", nil
}

// getDailyCeiling retrieves the configured daily ceiling from the database.
// Returns 0 if no ceiling is configured (unlimited).
func (l *SQLiteLedger) getDailyCeiling(ctx context.Context) (float64, error) {
	var maxDailyUSD float64
	err := l.db.QueryRowContext(ctx,
		`SELECT max_daily_usd FROM daily_ceilings WHERE id = 1`,
	).Scan(&maxDailyUSD)
	if err == sql.ErrNoRows {
		return 0, nil
	}
	if err != nil {
		return 0, fmt.Errorf("query daily ceiling: %w", err)
	}
	return maxDailyUSD, nil
}

// ResetTaskWarning clears the warning-sent state for a task. Call this when
// a founder increases the task budget so the warning can re-fire at the new 80% mark.
func (l *SQLiteLedger) ResetTaskWarning(taskID string) {
	if l.cbState != nil {
		l.cbState.warnings.reset(taskID)
	}
}
