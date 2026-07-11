// Package costledger provides per-call cost attribution, per-task budget enforcement,
// budget strategy allocation, cost forecasting, and system-wide daily circuit breaker
// functionality for the Vikram autonomous engineering platform.
package costledger

import (
	"context"
	"time"
)

// CostRecord represents a single LLM provider call with full cost attribution.
type CostRecord struct {
	RecordID     string    `json:"record_id"`
	TaskID       string    `json:"task_id"`
	Role         string    `json:"role"`
	Model        string    `json:"model"`
	Provider     string    `json:"provider"`
	WorkPhase    string    `json:"work_phase"`
	InputTokens  int       `json:"input_tokens"`
	OutputTokens int       `json:"output_tokens"`
	CostUSD      float64   `json:"cost_usd"`
	Estimated    bool      `json:"estimated"`
	DurationMS   int64     `json:"duration_ms"`
	InvocationID string    `json:"invocation_id"`
	Timestamp    time.Time `json:"timestamp"`
}

// BudgetStrategy defines percentage-based allocation of budget across work phases.
// All fields are percentages (0–100) that should sum to 100.
type BudgetStrategy struct {
	Planning       float64 `json:"planning"`       // percentage allocated to planning phase
	Implementation float64 `json:"implementation"` // percentage allocated to implementation phase
	Verification   float64 `json:"verification"`   // percentage allocated to verification phase
	Review         float64 `json:"review"`         // percentage allocated to review phase
}

// CostForecast represents a cost estimate for a task based on historical data.
type CostForecast struct {
	MinCostUSD      float64 `json:"min_cost_usd"`
	ExpectedCostUSD float64 `json:"expected_cost_usd"`
	MaxCostUSD      float64 `json:"max_cost_usd"`
	ConfidenceLevel float64 `json:"confidence_level"` // 0.0–1.0
	BasisTaskCount  int     `json:"basis_task_count"` // number of historical tasks used
}

// DailyCeiling represents the system-wide daily spending limit configuration.
type DailyCeiling struct {
	MaxDailyUSD float64 `json:"max_daily_usd"`
	ResetHour   int     `json:"reset_hour"` // UTC hour (0–23) at which the daily accumulator resets
}

// Ledger defines the interface for cost tracking, budget enforcement, and forecasting.
type Ledger interface {
	// Record persists a cost record and updates in-memory accumulators.
	Record(ctx context.Context, rec CostRecord) error

	// TaskCumulative returns the total cost incurred by a task across all calls.
	TaskCumulative(ctx context.Context, taskID string) (float64, error)

	// DailyTotal returns the system-wide daily spend since the last reset.
	DailyTotal(ctx context.Context) (float64, error)

	// PhaseBudgetRemaining returns the remaining budget for a specific work phase
	// within a task, based on the task's BudgetStrategy and total budget.
	PhaseBudgetRemaining(ctx context.Context, taskID, phase string) (float64, error)

	// Forecast produces a cost estimate for a new task based on complexity tier
	// and target file count, using historical data.
	Forecast(ctx context.Context, complexity string, targetFiles int) (CostForecast, error)

	// CheckCircuitBreaker evaluates whether a task should be halted due to budget
	// exhaustion. Returns (shouldBreak, reason, error).
	CheckCircuitBreaker(ctx context.Context, taskID string) (bool, string, error)
}
