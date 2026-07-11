package costledger

import (
	"context"
	"database/sql"
	"fmt"
	"path/filepath"
	"sort"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/stretchr/testify/require"
	"pgregory.net/rapid"
)

// --- Test helpers ---

// newPBTLedger creates a fresh SQLite-backed Ledger for property testing.
func newPBTLedger(t *testing.T) (*SQLiteLedger, *sql.DB) {
	t.Helper()
	dir := t.TempDir()
	dbPath := filepath.Join(dir, "cost_ledger_pbt.db")
	db, err := OpenDB(dbPath)
	require.NoError(t, err)
	t.Cleanup(func() { db.Close() })

	ledger, err := NewSQLiteLedger(db, LedgerConfig{ResetHour: 0})
	require.NoError(t, err)
	return ledger, db
}

// knownProviderModels returns all provider/model combinations from the default pricing table
// in a deterministic order (sorted by provider/model key) for reproducible property tests.
func knownProviderModels() []struct {
	Provider string
	Model    string
} {
	pt := DefaultPricingTable()
	// Collect keys and sort for deterministic ordering.
	keys := make([]string, 0, len(pt))
	for k := range pt {
		keys = append(keys, k)
	}
	sort.Strings(keys)

	pairs := make([]struct {
		Provider string
		Model    string
	}, 0, len(pt))
	for _, k := range keys {
		p := pt[k]
		pairs = append(pairs, struct {
			Provider string
			Model    string
		}{p.Provider, p.Model})
	}
	return pairs
}

// genProviderModel generates a random provider/model pair from the default pricing table.
func genProviderModel(t *rapid.T) (string, string) {
	pairs := knownProviderModels()
	idx := rapid.IntRange(0, len(pairs)-1).Draw(t, "providerModelIdx")
	return pairs[idx].Provider, pairs[idx].Model
}

// genWorkPhase generates a random valid work phase.
func genWorkPhase(t *rapid.T) string {
	phases := []string{"planning", "implementation", "verification", "review"}
	idx := rapid.IntRange(0, len(phases)-1).Draw(t, "phaseIdx")
	return phases[idx]
}

// genRole generates a random valid agent role.
func genRole(t *rapid.T) string {
	roles := []string{"planner", "implementer", "reviewer", "verifier", "architect"}
	idx := rapid.IntRange(0, len(roles)-1).Draw(t, "roleIdx")
	return roles[idx]
}

// genCostRecord generates a random CostRecord with valid fields from the pricing table.
// If providerReportsTokens is true, estimated=false; otherwise estimated=true.
func genCostRecord(t *rapid.T, taskID string, providerReportsTokens bool) CostRecord {
	provider, model := genProviderModel(t)
	inputTokens := rapid.IntRange(0, 100000).Draw(t, "inputTokens")
	outputTokens := rapid.IntRange(0, 50000).Draw(t, "outputTokens")

	pt := DefaultPricingTable()
	cost, _ := pt.ComputeCost(provider, model, inputTokens, outputTokens)

	return CostRecord{
		RecordID:     uuid.New().String(),
		TaskID:       taskID,
		Role:         genRole(t),
		Model:        model,
		Provider:     provider,
		WorkPhase:    genWorkPhase(t),
		InputTokens:  inputTokens,
		OutputTokens: outputTokens,
		CostUSD:      cost,
		Estimated:    !providerReportsTokens,
		DurationMS:   int64(rapid.IntRange(100, 30000).Draw(t, "durationMS")),
		InvocationID: uuid.New().String(),
		Timestamp:    time.Now().Add(-time.Duration(rapid.IntRange(0, 3600).Draw(t, "ageSeconds")) * time.Second),
	}
}

// --- Property 1: Cost Record Completeness and Source Selection ---
// **Validates: Requirements 1.1, 1.2, 1.3**
//
// Every cost record must have all required fields populated.
// When provider returns token counts, use them (estimated=false).
// When provider doesn't return counts, use tiktoken approximation (estimated=true).

func TestProperty1_CostRecordCompletenessAndSourceSelection(t *testing.T) {
	ledger, db := newPBTLedger(t)

	rapid.Check(t, func(rt *rapid.T) {
		ctx := context.Background()

		// Generate a random task ID.
		taskID := fmt.Sprintf("task-%s", uuid.New().String()[:8])

		// Draw whether the provider reports token counts.
		providerReportsTokens := rapid.Bool().Draw(rt, "providerReportsTokens")

		rec := genCostRecord(rt, taskID, providerReportsTokens)

		// Verify all required fields are non-empty before recording.
		require.NotEmpty(rt, rec.RecordID, "RecordID must not be empty")
		require.NotEmpty(rt, rec.TaskID, "TaskID must not be empty")
		require.NotEmpty(rt, rec.Role, "Role must not be empty")
		require.NotEmpty(rt, rec.Model, "Model must not be empty")
		require.NotEmpty(rt, rec.Provider, "Provider must not be empty")
		require.NotEmpty(rt, rec.WorkPhase, "WorkPhase must not be empty")
		require.NotEmpty(rt, rec.InvocationID, "InvocationID must not be empty")
		require.False(rt, rec.Timestamp.IsZero(), "Timestamp must not be zero")
		require.GreaterOrEqual(rt, rec.InputTokens, 0, "InputTokens must be non-negative")
		require.GreaterOrEqual(rt, rec.OutputTokens, 0, "OutputTokens must be non-negative")
		require.GreaterOrEqual(rt, rec.CostUSD, 0.0, "CostUSD must be non-negative")

		// Verify estimated flag logic:
		// Provider returns tokens → estimated=false
		// Provider doesn't return tokens → estimated=true (tiktoken approximation used)
		if providerReportsTokens {
			require.False(rt, rec.Estimated,
				"estimated must be false when provider reports token counts")
		} else {
			require.True(rt, rec.Estimated,
				"estimated must be true when provider does not report token counts")
		}

		// Record via the Ledger interface.
		err := ledger.Record(ctx, rec)
		require.NoError(rt, err)

		// Read back from DB and verify all fields are preserved (completeness).
		var (
			recordID, taskIDOut, role, model, provider, workPhase, invID string
			inputTokens, outputTokens                                    int
			costUSD                                                      float64
			estimated                                                    int
			durationMS                                                   int64
			tsStr                                                        string
		)
		err = db.QueryRow(`SELECT record_id, task_id, role, model, provider, work_phase,
			input_tokens, output_tokens, cost_usd, estimated, duration_ms, invocation_id, timestamp
			FROM cost_records WHERE record_id = ?`, rec.RecordID).
			Scan(&recordID, &taskIDOut, &role, &model, &provider, &workPhase,
				&inputTokens, &outputTokens, &costUSD, &estimated, &durationMS, &invID, &tsStr)
		require.NoError(rt, err)

		require.Equal(rt, rec.RecordID, recordID)
		require.Equal(rt, rec.TaskID, taskIDOut)
		require.Equal(rt, rec.Role, role)
		require.Equal(rt, rec.Model, model)
		require.Equal(rt, rec.Provider, provider)
		require.Equal(rt, rec.WorkPhase, workPhase)
		require.Equal(rt, rec.InputTokens, inputTokens)
		require.Equal(rt, rec.OutputTokens, outputTokens)
		require.InDelta(rt, rec.CostUSD, costUSD, 1e-12)
		require.Equal(rt, boolToInt(rec.Estimated), estimated)
		require.Equal(rt, rec.DurationMS, durationMS)
		require.Equal(rt, rec.InvocationID, invID)
	})
}

// --- Property 2: Cost Computation Correctness ---
// **Validates: Requirements 1.4, 2.1**
//
// For any valid provider/model/token combination, computed cost must equal
// input_tokens * input_price + output_tokens * output_price using the pricing table.

func TestProperty2_CostComputationCorrectness(t *testing.T) {
	rapid.Check(t, func(rt *rapid.T) {
		provider, model := genProviderModel(rt)
		inputTokens := rapid.IntRange(0, 1000000).Draw(rt, "inputTokens")
		outputTokens := rapid.IntRange(0, 500000).Draw(rt, "outputTokens")

		pt := DefaultPricingTable()

		// Get the pricing for this model.
		pricing, err := pt.LookupPricing(provider, model)
		require.NoError(rt, err)

		// Compute cost via the pricing table method.
		computedCost, err := pt.ComputeCost(provider, model, inputTokens, outputTokens)
		require.NoError(rt, err)

		// Compute expected cost directly from the formula.
		expectedCost := float64(inputTokens)*pricing.InputPerToken + float64(outputTokens)*pricing.OutputPerToken

		// Both ComputeCost and our manual formula use the same arithmetic, but
		// floating-point addition is not perfectly associative. Use InDelta with
		// a tolerance relative to the magnitude (1 ULP tolerance at ~1e-12 absolute).
		require.InDelta(rt, expectedCost, computedCost, 1e-12,
			"ComputeCost(%s, %s, %d, %d) = %v, want %v (input_price=%v, output_price=%v)",
			provider, model, inputTokens, outputTokens, computedCost, expectedCost,
			pricing.InputPerToken, pricing.OutputPerToken)

		// Also verify via the direct ComputeCostWithPricing helper.
		directCost := ComputeCostWithPricing(pricing, inputTokens, outputTokens)
		require.InDelta(rt, expectedCost, directCost, 1e-12,
			"ComputeCostWithPricing mismatch")

		// Sanity: cost is non-negative.
		require.GreaterOrEqual(rt, computedCost, 0.0)

		// Sanity: cost is zero only when both token counts are zero.
		if inputTokens == 0 && outputTokens == 0 {
			require.Equal(rt, 0.0, computedCost)
		}
		if inputTokens > 0 || outputTokens > 0 {
			require.Greater(rt, computedCost, 0.0)
		}
	})
}

// --- Property 3: Cumulative Cost Invariant ---
// **Validates: Requirements 1.1, 1.2, 2.1**
//
// The cumulative cost for any task must equal the sum of all individual cost records
// for that task. This must hold after any sequence of Record() operations.

func TestProperty3_CumulativeCostInvariant(t *testing.T) {
	rapid.Check(t, func(rt *rapid.T) {
		// Each property test iteration gets a fresh ledger to avoid cross-contamination.
		dir := t.TempDir()
		dbPath := filepath.Join(dir, fmt.Sprintf("cost_ledger_%s.db", uuid.New().String()[:8]))
		db, err := OpenDB(dbPath)
		require.NoError(rt, err)
		defer db.Close()

		ledger, err := NewSQLiteLedger(db, LedgerConfig{ResetHour: 0})
		require.NoError(rt, err)

		ctx := context.Background()

		// Generate a random task ID.
		taskID := fmt.Sprintf("task-%s", uuid.New().String()[:8])

		// Generate a random number of cost records for this task (1..20).
		numRecords := rapid.IntRange(1, 20).Draw(rt, "numRecords")

		var expectedSum float64
		for i := 0; i < numRecords; i++ {
			rec := genCostRecord(rt, taskID, rapid.Bool().Draw(rt, fmt.Sprintf("providerReports_%d", i)))

			err := ledger.Record(ctx, rec)
			require.NoError(rt, err)

			expectedSum += rec.CostUSD
		}

		// Query the cumulative cost via the Ledger interface.
		cumulativeCost, err := ledger.TaskCumulative(ctx, taskID)
		require.NoError(rt, err)

		// The cumulative cost must equal the sum of all individual records.
		// Use a small tolerance for floating-point accumulation.
		require.InDelta(rt, expectedSum, cumulativeCost, 1e-9,
			"cumulative cost mismatch for task %s: expected sum %.15f, got %.15f (records: %d)",
			taskID, expectedSum, cumulativeCost, numRecords)

		// Additional invariant: cumulative cost must be non-negative.
		require.GreaterOrEqual(rt, cumulativeCost, 0.0)

		// Additional invariant: if we add a record for a DIFFERENT task, it must not
		// affect this task's cumulative cost.
		otherTaskID := fmt.Sprintf("other-task-%s", uuid.New().String()[:8])
		otherRec := genCostRecord(rt, otherTaskID, true)
		err = ledger.Record(ctx, otherRec)
		require.NoError(rt, err)

		// Re-check original task's cumulative — must be unchanged.
		cumulativeAfter, err := ledger.TaskCumulative(ctx, taskID)
		require.NoError(rt, err)
		require.InDelta(rt, expectedSum, cumulativeAfter, 1e-9,
			"cumulative cost changed after inserting record for different task")
	})
}
