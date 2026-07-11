package costledger

import (
	"context"
	"fmt"
	"math"
	"path/filepath"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/stretchr/testify/require"
	"pgregory.net/rapid"
)

// --- Property 6: Phase Budget Allocation Correctness ---
// **Validates: Requirements 3.2, 3.3**
//
// For any task with a BudgetStrategy, the sum of all phase allocations
// (planning + implementation + verification + review) as dollar amounts must
// equal the total task budget. PhaseBudgetRemaining for each phase must be
// non-negative and ≤ that phase's allocation. After recording cost for a phase,
// PhaseBudgetRemaining must decrease correctly.

func TestProperty6_PhaseBudgetAllocationCorrectness(t *testing.T) {
	rapid.Check(t, func(rt *rapid.T) {
		// Create a fresh ledger per iteration.
		dir := t.TempDir()
		dbPath := filepath.Join(dir, fmt.Sprintf("phase_budget_pbt_%s.db", uuid.New().String()[:8]))
		db, err := OpenDB(dbPath)
		require.NoError(rt, err)
		defer db.Close()

		ledger, err := NewSQLiteLedger(db, LedgerConfig{ResetHour: 0})
		require.NoError(rt, err)

		ctx := context.Background()
		taskID := fmt.Sprintf("task-%s", uuid.New().String()[:8])

		// Generate a random total budget between $1.00 and $100.00.
		totalBudget := rapid.Float64Range(1.0, 100.0).Draw(rt, "totalBudget")

		// Generate random BudgetStrategy percentages that sum to 100.
		// Strategy: generate 4 random weights, then normalize to 100.
		w1 := rapid.Float64Range(0.01, 100.0).Draw(rt, "w1")
		w2 := rapid.Float64Range(0.01, 100.0).Draw(rt, "w2")
		w3 := rapid.Float64Range(0.01, 100.0).Draw(rt, "w3")
		w4 := rapid.Float64Range(0.01, 100.0).Draw(rt, "w4")

		total := w1 + w2 + w3 + w4
		planning := (w1 / total) * 100.0
		implementation := (w2 / total) * 100.0
		verification := (w3 / total) * 100.0
		review := (w4 / total) * 100.0

		// Insert the budget strategy into the DB as "default" task type.
		_, err = db.ExecContext(ctx, `
			INSERT OR REPLACE INTO budget_strategies (task_type, planning, implementation, verification, review, created_at, updated_at)
			VALUES ('default', ?, ?, ?, ?, datetime('now'), datetime('now'))`,
			planning, implementation, verification, review,
		)
		require.NoError(rt, err)

		// Register the task budget via the TaskBudgetStore.
		budgetStore := NewTaskBudgetStore()
		budgetStore.Register(taskID, totalBudget)
		ledger.SetCircuitBreakerConfig(CircuitBreakerConfig{
			WarningThreshold: 0.8,
			Notifier:         &noopNotifier{},
			TaskBudgets:      budgetStore,
		})

		// Compute expected dollar allocations per phase.
		planningAlloc := totalBudget * (planning / 100.0)
		implementationAlloc := totalBudget * (implementation / 100.0)
		verificationAlloc := totalBudget * (verification / 100.0)
		reviewAlloc := totalBudget * (review / 100.0)

		// Property 1: Sum of all phase allocations must equal total budget.
		sumAllocations := planningAlloc + implementationAlloc + verificationAlloc + reviewAlloc
		require.InDelta(rt, totalBudget, sumAllocations, 1e-9,
			"sum of phase allocations (%.10f) must equal total budget (%.10f)",
			sumAllocations, totalBudget)

		// Property 2: Before any spending, PhaseBudgetRemaining for each phase
		// must equal its allocation and be non-negative.
		phases := []struct {
			name  string
			alloc float64
		}{
			{"planning", planningAlloc},
			{"implementation", implementationAlloc},
			{"verification", verificationAlloc},
			{"review", reviewAlloc},
		}

		for _, phase := range phases {
			remaining, err := ledger.PhaseBudgetRemaining(ctx, taskID, phase.name)
			require.NoError(rt, err)

			// PhaseBudgetRemaining must be non-negative.
			require.GreaterOrEqual(rt, remaining, 0.0,
				"PhaseBudgetRemaining for %s must be >= 0", phase.name)

			// PhaseBudgetRemaining must equal the phase allocation (no spend yet).
			require.InDelta(rt, phase.alloc, remaining, 1e-9,
				"initial PhaseBudgetRemaining for %s (%.10f) must equal allocation (%.10f)",
				phase.name, remaining, phase.alloc)

			// PhaseBudgetRemaining must be ≤ phase allocation.
			require.LessOrEqual(rt, remaining, phase.alloc+1e-9,
				"PhaseBudgetRemaining for %s must be <= allocation", phase.name)
		}

		// Property 3: After recording cost for a phase, PhaseBudgetRemaining decreases correctly.
		// Pick a random phase to spend into.
		phaseIdx := rapid.IntRange(0, 3).Draw(rt, "spendPhaseIdx")
		spendPhase := phases[phaseIdx]

		// Generate a random spend amount that's a fraction of the phase allocation.
		// Use between 10% and 90% of the allocation to avoid floating point edge cases.
		spendFraction := rapid.Float64Range(0.1, 0.9).Draw(rt, "spendFraction")
		spendAmount := spendPhase.alloc * spendFraction

		// Record a cost record for the chosen phase.
		rec := CostRecord{
			RecordID:     uuid.New().String(),
			TaskID:       taskID,
			Role:         "implementer",
			Model:        "claude-sonnet-4-20250514",
			Provider:     "anthropic",
			WorkPhase:    spendPhase.name,
			InputTokens:  1000,
			OutputTokens: 500,
			CostUSD:      spendAmount,
			Estimated:    false,
			DurationMS:   500,
			InvocationID: uuid.New().String(),
			Timestamp:    time.Now(),
		}
		err = ledger.Record(ctx, rec)
		require.NoError(rt, err)

		// After spending, PhaseBudgetRemaining should decrease by the spend amount.
		remainingAfter, err := ledger.PhaseBudgetRemaining(ctx, taskID, spendPhase.name)
		require.NoError(rt, err)

		expectedRemaining := spendPhase.alloc - spendAmount
		require.InDelta(rt, expectedRemaining, remainingAfter, 1e-9,
			"after spending %.10f in %s, remaining (%.10f) should equal allocation (%.10f) - spend (%.10f)",
			spendAmount, spendPhase.name, remainingAfter, spendPhase.alloc, spendAmount)

		// Property: remaining must still be non-negative.
		require.GreaterOrEqual(rt, remainingAfter, 0.0,
			"PhaseBudgetRemaining for %s must remain >= 0 after spending", spendPhase.name)

		// Property: remaining must be ≤ allocation.
		require.LessOrEqual(rt, remainingAfter, spendPhase.alloc+1e-9,
			"PhaseBudgetRemaining for %s must remain <= allocation after spending", spendPhase.name)

		// Property 4: Other phases should be unaffected.
		for i, phase := range phases {
			if i == phaseIdx {
				continue
			}
			otherRemaining, err := ledger.PhaseBudgetRemaining(ctx, taskID, phase.name)
			require.NoError(rt, err)
			require.InDelta(rt, phase.alloc, otherRemaining, 1e-9,
				"PhaseBudgetRemaining for %s should be unaffected by spending in %s",
				phase.name, spendPhase.name)
		}

		// Property 5: After spending the full phase allocation, remaining should be 0.
		// Record additional cost to exhaust the phase budget.
		remainingToExhaust := spendPhase.alloc - spendAmount
		if remainingToExhaust > 1e-12 {
			rec2 := CostRecord{
				RecordID:     uuid.New().String(),
				TaskID:       taskID,
				Role:         "implementer",
				Model:        "claude-sonnet-4-20250514",
				Provider:     "anthropic",
				WorkPhase:    spendPhase.name,
				InputTokens:  500,
				OutputTokens: 250,
				CostUSD:      remainingToExhaust,
				Estimated:    false,
				DurationMS:   300,
				InvocationID: uuid.New().String(),
				Timestamp:    time.Now(),
			}
			err = ledger.Record(ctx, rec2)
			require.NoError(rt, err)

			exhaustedRemaining, err := ledger.PhaseBudgetRemaining(ctx, taskID, spendPhase.name)
			require.NoError(rt, err)
			require.InDelta(rt, 0.0, exhaustedRemaining, 1e-9,
				"PhaseBudgetRemaining for %s must be 0 after full exhaustion", spendPhase.name)
		}

		// Property 6: Overspend should clamp remaining to 0 (not go negative).
		overSpendAmount := rapid.Float64Range(0.01, 10.0).Draw(rt, "overSpendAmount")
		rec3 := CostRecord{
			RecordID:     uuid.New().String(),
			TaskID:       taskID,
			Role:         "implementer",
			Model:        "claude-sonnet-4-20250514",
			Provider:     "anthropic",
			WorkPhase:    spendPhase.name,
			InputTokens:  200,
			OutputTokens: 100,
			CostUSD:      overSpendAmount,
			Estimated:    false,
			DurationMS:   200,
			InvocationID: uuid.New().String(),
			Timestamp:    time.Now(),
		}
		err = ledger.Record(ctx, rec3)
		require.NoError(rt, err)

		overSpentRemaining, err := ledger.PhaseBudgetRemaining(ctx, taskID, spendPhase.name)
		require.NoError(rt, err)
		require.GreaterOrEqual(rt, overSpentRemaining, 0.0,
			"PhaseBudgetRemaining must never go negative, even after overspend")
		require.True(rt, math.Abs(overSpentRemaining) < 1e-9,
			"PhaseBudgetRemaining should be 0 (clamped) after overspend, got %.10f", overSpentRemaining)
	})
}
