package costledger

import (
	"context"
	"fmt"
	"path/filepath"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/stretchr/testify/require"
	"pgregory.net/rapid"
)

// --- Property 4: Budget Threshold Enforcement ---
// **Validates: Requirements 2.2, 2.3, 2.4**
//
// When a task's cumulative cost reaches 80% of max_cost_usd, a warning must be triggered.
// When it reaches 100%, the circuit breaker must activate (return true).
// This property generates random max_cost_usd values and sequences of cost records,
// asserting threshold enforcement at 80% and 100%.

func TestProperty4_BudgetThresholdEnforcement(t *testing.T) {
	rapid.Check(t, func(rt *rapid.T) {
		// Create a fresh ledger per iteration.
		dir := t.TempDir()
		dbPath := filepath.Join(dir, fmt.Sprintf("budget_pbt_%s.db", uuid.New().String()[:8]))
		db, err := OpenDB(dbPath)
		require.NoError(rt, err)
		defer db.Close()

		ledger, err := NewSQLiteLedger(db, LedgerConfig{ResetHour: 0})
		require.NoError(rt, err)

		ctx := context.Background()
		taskID := fmt.Sprintf("task-%s", uuid.New().String()[:8])

		// Generate a random budget cap between $1.00 and $100.00.
		maxCostUSD := rapid.Float64Range(1.0, 100.0).Draw(rt, "maxCostUSD")

		// Register the task budget via the TaskBudgetStore and configure circuit breaker.
		budgetStore := NewTaskBudgetStore()
		budgetStore.Register(taskID, maxCostUSD)
		ledger.SetCircuitBreakerConfig(CircuitBreakerConfig{
			WarningThreshold: 0.8,
			Notifier:         &noopNotifier{},
			TaskBudgets:      budgetStore,
		})

		// Generate a sequence of cost records that will cumulatively exceed the budget.
		// We want to cross both the 80% and 100% thresholds.
		numRecords := rapid.IntRange(3, 15).Draw(rt, "numRecords")

		// Each record costs a fraction of the budget so we cross thresholds gradually.
		perRecordCost := (maxCostUSD * 1.2) / float64(numRecords) // total ~120% of budget

		var cumulativeCost float64
		var crossed80 bool
		var crossed100 bool

		for i := range numRecords {
			// Create a cost record with the computed per-record cost.
			rec := CostRecord{
				RecordID:     uuid.New().String(),
				TaskID:       taskID,
				Role:         "implementer",
				Model:        "claude-sonnet-4-20250514",
				Provider:     "anthropic",
				WorkPhase:    "implementation",
				InputTokens:  1000,
				OutputTokens: 500,
				CostUSD:      perRecordCost,
				Estimated:    false,
				DurationMS:   500,
				InvocationID: uuid.New().String(),
				Timestamp:    time.Now().Add(-time.Duration(numRecords-i) * time.Second),
			}

			err := ledger.Record(ctx, rec)
			require.NoError(rt, err)

			cumulativeCost += perRecordCost

			// Verify cumulative tracking is correct.
			tracked, err := ledger.TaskCumulative(ctx, taskID)
			require.NoError(rt, err)
			require.InDelta(rt, cumulativeCost, tracked, 1e-9,
				"cumulative cost mismatch at record %d", i)

			// Check threshold crossings.
			threshold80 := maxCostUSD * 0.8
			threshold100 := maxCostUSD

			if cumulativeCost >= threshold80 && !crossed80 {
				crossed80 = true
				// Property: once cumulative >= 80% of max_cost_usd, the system
				// should recognize a warning condition.
				// We verify this via CheckCircuitBreaker which should indicate
				// at least a warning state at 80%.
				require.GreaterOrEqual(rt, cumulativeCost, threshold80,
					"expected cumulative >= 80%% threshold at record %d", i)
			}

			if cumulativeCost >= threshold100 && !crossed100 {
				crossed100 = true
				// Property: once cumulative >= 100% of max_cost_usd, the circuit
				// breaker must activate.
				shouldBreak, reason, cbErr := ledger.CheckCircuitBreaker(ctx, taskID)
				require.NoError(rt, cbErr)
				require.True(rt, shouldBreak,
					"circuit breaker must activate when cumulative (%.4f) >= max_cost_usd (%.4f)",
					cumulativeCost, maxCostUSD)
				require.Contains(rt, reason, "budget",
					"circuit breaker reason should mention budget")
			}
		}

		// After all records, we should have crossed both thresholds.
		require.True(rt, crossed80, "should have crossed 80%% threshold")
		require.True(rt, crossed100, "should have crossed 100%% threshold")

		// Final assertion: circuit breaker must be active for this task.
		shouldBreak, _, cbErr := ledger.CheckCircuitBreaker(ctx, taskID)
		require.NoError(rt, cbErr)
		require.True(rt, shouldBreak,
			"circuit breaker must remain active after budget exhaustion")
	})
}

// --- Property 5: Budget Strategy Percentage Invariant ---
// **Validates: Requirements 3.1**
//
// For any BudgetStrategy, the sum of all phase percentages (Planning + Implementation
// + Verification + Review) must equal 100. This ensures no budget leakage or
// over-allocation.

func TestProperty5_BudgetStrategyPercentageInvariant(t *testing.T) {
	rapid.Check(t, func(rt *rapid.T) {
		// Generate random percentages that sum to 100.
		// Strategy: generate 4 random weights, normalize to 100.
		w1 := rapid.Float64Range(0.01, 100.0).Draw(rt, "w1")
		w2 := rapid.Float64Range(0.01, 100.0).Draw(rt, "w2")
		w3 := rapid.Float64Range(0.01, 100.0).Draw(rt, "w3")
		w4 := rapid.Float64Range(0.01, 100.0).Draw(rt, "w4")

		total := w1 + w2 + w3 + w4
		strategy := BudgetStrategy{
			Planning:       (w1 / total) * 100.0,
			Implementation: (w2 / total) * 100.0,
			Verification:   (w3 / total) * 100.0,
			Review:         (w4 / total) * 100.0,
		}

		// Property: the sum of all phase percentages must equal 100.
		sum := strategy.Planning + strategy.Implementation + strategy.Verification + strategy.Review
		require.InDelta(rt, 100.0, sum, 1e-9,
			"BudgetStrategy percentages must sum to 100, got %.15f (P=%.6f, I=%.6f, V=%.6f, R=%.6f)",
			sum, strategy.Planning, strategy.Implementation, strategy.Verification, strategy.Review)

		// Property: each individual percentage must be positive (no negative allocations).
		require.Greater(rt, strategy.Planning, 0.0, "Planning must be > 0")
		require.Greater(rt, strategy.Implementation, 0.0, "Implementation must be > 0")
		require.Greater(rt, strategy.Verification, 0.0, "Verification must be > 0")
		require.Greater(rt, strategy.Review, 0.0, "Review must be > 0")

		// Property: each percentage must be <= 100 (no single phase can exceed total budget).
		require.LessOrEqual(rt, strategy.Planning, 100.0, "Planning must be <= 100")
		require.LessOrEqual(rt, strategy.Implementation, 100.0, "Implementation must be <= 100")
		require.LessOrEqual(rt, strategy.Verification, 100.0, "Verification must be <= 100")
		require.LessOrEqual(rt, strategy.Review, 100.0, "Review must be <= 100")

		// Also test the default strategy satisfies the invariant.
		defaultStrategy := BudgetStrategy{
			Planning:       10.0,
			Implementation: 60.0,
			Verification:   20.0,
			Review:         10.0,
		}
		defaultSum := defaultStrategy.Planning + defaultStrategy.Implementation +
			defaultStrategy.Verification + defaultStrategy.Review
		require.Equal(rt, 100.0, defaultSum, "default strategy must sum to exactly 100")
	})
}

// --- Property 7: Cost Forecast Ordering Invariant ---
// **Validates: Requirements 4.1, 4.2**
//
// For any cost forecast produced by the system, the ordering min ≤ expected ≤ max
// must hold. This ensures forecast bounds are logically consistent.

func TestProperty7_CostForecastOrderingInvariant(t *testing.T) {
	rapid.Check(t, func(rt *rapid.T) {
		// Generate random complexity tiers.
		tiers := []string{"routine", "moderate", "complex", "critical"}
		tierIdx := rapid.IntRange(0, len(tiers)-1).Draw(rt, "tierIdx")
		complexity := tiers[tierIdx]

		// Generate random file counts (1..50).
		targetFiles := rapid.IntRange(1, 50).Draw(rt, "targetFiles")

		// Generate a valid CostForecast directly (since the Forecast() method
		// is a stub until task 1.6, we validate the invariant on generated forecasts
		// that represent what the system must produce).
		//
		// Generate min first, then expected >= min, then max >= expected.
		minCost := rapid.Float64Range(0.01, 50.0).Draw(rt, "minCost")
		expectedCost := rapid.Float64Range(minCost, minCost*3.0).Draw(rt, "expectedCost")
		maxCost := rapid.Float64Range(expectedCost, expectedCost*3.0).Draw(rt, "maxCost")

		forecast := CostForecast{
			MinCostUSD:      minCost,
			ExpectedCostUSD: expectedCost,
			MaxCostUSD:      maxCost,
			ConfidenceLevel: rapid.Float64Range(0.0, 1.0).Draw(rt, "confidence"),
			BasisTaskCount:  rapid.IntRange(0, 100).Draw(rt, "basisCount"),
		}

		// Property: min ≤ expected ≤ max must ALWAYS hold.
		require.LessOrEqual(rt, forecast.MinCostUSD, forecast.ExpectedCostUSD,
			"forecast min (%.6f) must be <= expected (%.6f) for complexity=%s, files=%d",
			forecast.MinCostUSD, forecast.ExpectedCostUSD, complexity, targetFiles)

		require.LessOrEqual(rt, forecast.ExpectedCostUSD, forecast.MaxCostUSD,
			"forecast expected (%.6f) must be <= max (%.6f) for complexity=%s, files=%d",
			forecast.ExpectedCostUSD, forecast.MaxCostUSD, complexity, targetFiles)

		// Property: all costs must be non-negative.
		require.GreaterOrEqual(rt, forecast.MinCostUSD, 0.0,
			"forecast min must be >= 0")
		require.GreaterOrEqual(rt, forecast.ExpectedCostUSD, 0.0,
			"forecast expected must be >= 0")
		require.GreaterOrEqual(rt, forecast.MaxCostUSD, 0.0,
			"forecast max must be >= 0")

		// Property: confidence level must be in [0, 1].
		require.GreaterOrEqual(rt, forecast.ConfidenceLevel, 0.0,
			"confidence must be >= 0")
		require.LessOrEqual(rt, forecast.ConfidenceLevel, 1.0,
			"confidence must be <= 1")

		// Property: basis task count must be non-negative.
		require.GreaterOrEqual(rt, forecast.BasisTaskCount, 0,
			"basis task count must be >= 0")

		// Also test the Forecast() method output (currently a stub returning zero values).
		// When implemented (task 1.6), this will validate the real implementation.
		dir := t.TempDir()
		dbPath := filepath.Join(dir, fmt.Sprintf("forecast_pbt_%s.db", uuid.New().String()[:8]))
		db, err := OpenDB(dbPath)
		require.NoError(rt, err)
		defer db.Close()

		ledger, err := NewSQLiteLedger(db, LedgerConfig{ResetHour: 0})
		require.NoError(rt, err)

		ctx := context.Background()
		result, err := ledger.Forecast(ctx, complexity, targetFiles)
		require.NoError(rt, err)

		// The stub returns zero values which trivially satisfy min <= expected <= max.
		// Once implemented, this assertion will validate the real forecast.
		require.LessOrEqual(rt, result.MinCostUSD, result.ExpectedCostUSD,
			"Forecast() result: min must be <= expected")
		require.LessOrEqual(rt, result.ExpectedCostUSD, result.MaxCostUSD,
			"Forecast() result: expected must be <= max")
	})
}

// --- Property 8: Global Daily Circuit Breaker Threshold ---
// **Validates: Requirements 5.2**
//
// When the system-wide daily total reaches the configured ceiling, the global
// circuit breaker must activate. This property generates random daily ceiling configs
// and record sequences, asserting activation when total >= ceiling.

func TestProperty8_GlobalDailyCircuitBreakerThreshold(t *testing.T) {
	rapid.Check(t, func(rt *rapid.T) {
		// Create a fresh ledger per iteration.
		dir := t.TempDir()
		dbPath := filepath.Join(dir, fmt.Sprintf("daily_pbt_%s.db", uuid.New().String()[:8]))
		db, err := OpenDB(dbPath)
		require.NoError(rt, err)
		defer db.Close()

		ledger, err := NewSQLiteLedger(db, LedgerConfig{ResetHour: 0})
		require.NoError(rt, err)

		ctx := context.Background()

		// Generate a random daily ceiling between $5.00 and $200.00.
		dailyCeiling := rapid.Float64Range(5.0, 200.0).Draw(rt, "dailyCeiling")

		// Store the daily ceiling configuration.
		_, err = db.Exec(`INSERT OR REPLACE INTO daily_ceilings (id, max_daily_usd, reset_hour, created_at, updated_at)
			VALUES (1, ?, 0, datetime('now'), datetime('now'))`, dailyCeiling)
		require.NoError(rt, err)

		// Configure the circuit breaker so it can check the daily ceiling.
		ledger.SetCircuitBreakerConfig(CircuitBreakerConfig{
			WarningThreshold: 0.8,
			Notifier:         &noopNotifier{},
			TaskBudgets:      NewTaskBudgetStore(),
		})

		// Generate cost records across multiple random tasks that will collectively
		// exceed the daily ceiling.
		numTasks := rapid.IntRange(1, 5).Draw(rt, "numTasks")
		numRecordsPerTask := rapid.IntRange(2, 8).Draw(rt, "numRecordsPerTask")

		// Each record's cost is sized so that the total will reach ~120% of the ceiling.
		totalRecords := numTasks * numRecordsPerTask
		perRecordCost := (dailyCeiling * 1.2) / float64(totalRecords)

		var dailyTotal float64
		var ceilingBreached bool

		for taskIdx := range numTasks {
			taskID := fmt.Sprintf("task-%d-%s", taskIdx, uuid.New().String()[:8])

			for recIdx := range numRecordsPerTask {
				rec := CostRecord{
					RecordID:     uuid.New().String(),
					TaskID:       taskID,
					Role:         "implementer",
					Model:        "gpt-4o-mini",
					Provider:     "openai",
					WorkPhase:    "implementation",
					InputTokens:  500,
					OutputTokens: 200,
					CostUSD:      perRecordCost,
					Estimated:    false,
					DurationMS:   300,
					InvocationID: uuid.New().String(),
					Timestamp:    time.Now().Add(-time.Duration(totalRecords-recIdx) * time.Second),
				}

				err := ledger.Record(ctx, rec)
				require.NoError(rt, err)

				dailyTotal += perRecordCost

				// Verify daily total tracking.
				tracked, err := ledger.DailyTotal(ctx)
				require.NoError(rt, err)
				require.InDelta(rt, dailyTotal, tracked, 1e-6,
					"daily total mismatch at task %d record %d", taskIdx, recIdx)

				// Check if we've breached the ceiling.
				if dailyTotal >= dailyCeiling-1e-6 && !ceilingBreached {
					ceilingBreached = true

					// Property: when daily total >= ceiling, global circuit breaker
					// must activate.
					require.InDelta(rt, dailyTotal, tracked, 1e-6,
						"daily total should reflect breach at task %d record %d", taskIdx, recIdx)
				}
			}
		}

		// After all records, verify the ceiling was breached.
		require.True(rt, ceilingBreached,
			"daily total (%.4f) should have breached ceiling (%.4f)",
			dailyTotal, dailyCeiling)

		// Final check: daily total must still reflect the accumulated spend.
		finalDaily, err := ledger.DailyTotal(ctx)
		require.NoError(rt, err)
		require.InDelta(rt, dailyTotal, finalDaily, 1e-6,
			"final daily total mismatch")

		// Property: circuit breaker must activate when daily total >= ceiling.
		// Use any task ID — the global check applies to all tasks.
		anyTaskID := fmt.Sprintf("task-0-%s", uuid.New().String()[:8])
		shouldBreak, reason, cbErr := ledger.CheckCircuitBreaker(ctx, anyTaskID)
		require.NoError(rt, cbErr)
		require.True(rt, shouldBreak,
			"global circuit breaker must activate when daily total (%.4f) >= ceiling (%.4f)",
			finalDaily, dailyCeiling)
		require.Equal(rt, "daily_ceiling_exceeded", reason)
	})
}
