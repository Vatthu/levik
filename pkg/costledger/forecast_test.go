package costledger

import (
	"context"
	"fmt"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

func TestForecast_NoHistoricalData_UsesHeuristic(t *testing.T) {
	ledger := setupTestLedger(t)
	ctx := context.Background()

	forecast, err := ledger.Forecast(ctx, "moderate", 5)
	require.NoError(t, err)

	// With no historical data, should use heuristic:
	// moderate: baseCostPerFile=0.05, files=5 → expected=0.25
	assert.Equal(t, 0, forecast.BasisTaskCount)
	assert.Equal(t, 0.0, forecast.ConfidenceLevel)
	assert.InDelta(t, 0.25*0.4, forecast.MinCostUSD, 1e-10) // 0.25 * 0.4 = 0.10
	assert.InDelta(t, 0.25, forecast.ExpectedCostUSD, 1e-10)
	assert.InDelta(t, 0.25*2.5, forecast.MaxCostUSD, 1e-10) // 0.25 * 2.5 = 0.625
}

func TestForecast_HeuristicByTier(t *testing.T) {
	ledger := setupTestLedger(t)
	ctx := context.Background()

	tests := []struct {
		tier        string
		files       int
		expectedMin float64
		expectedExp float64
		expectedMax float64
	}{
		{"routine", 3, 0.03 * 0.5, 0.03, 0.03 * 2.0},
		{"moderate", 1, 0.05 * 0.4, 0.05, 0.05 * 2.5},
		{"complex", 4, 0.60 * 0.3, 0.60, 0.60 * 3.0},
		{"critical", 2, 0.80 * 0.3, 0.80, 0.80 * 4.0},
	}

	for _, tc := range tests {
		t.Run(tc.tier, func(t *testing.T) {
			forecast, err := ledger.Forecast(ctx, tc.tier, tc.files)
			require.NoError(t, err)
			assert.Equal(t, 0, forecast.BasisTaskCount)
			assert.Equal(t, 0.0, forecast.ConfidenceLevel)
			assert.InDelta(t, tc.expectedMin, forecast.MinCostUSD, 1e-10)
			assert.InDelta(t, tc.expectedExp, forecast.ExpectedCostUSD, 1e-10)
			assert.InDelta(t, tc.expectedMax, forecast.MaxCostUSD, 1e-10)
		})
	}
}

func TestForecast_UnknownTierFallsBackToModerate(t *testing.T) {
	ledger := setupTestLedger(t)
	ctx := context.Background()

	forecast, err := ledger.Forecast(ctx, "unknown_tier", 2)
	require.NoError(t, err)

	// Should use "moderate" heuristic: baseCostPerFile=0.05, files=2 → expected=0.10
	assert.InDelta(t, 0.10, forecast.ExpectedCostUSD, 1e-10)
	assert.Equal(t, 0, forecast.BasisTaskCount)
}

func TestForecast_ZeroFilesFallsBackToOne(t *testing.T) {
	ledger := setupTestLedger(t)
	ctx := context.Background()

	forecast, err := ledger.Forecast(ctx, "routine", 0)
	require.NoError(t, err)

	// Should treat 0 files as 1: baseCostPerFile=0.01, files=1 → expected=0.01
	assert.InDelta(t, 0.01, forecast.ExpectedCostUSD, 1e-10)
}

func TestForecast_WithHistoricalData(t *testing.T) {
	ledger := setupTestLedger(t)
	ctx := context.Background()
	now := time.Now().UTC()

	// Seed historical data: 10 tasks with "moderate" complexity tier.
	// Costs: 0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.45, 0.50, 0.55
	for i := range 10 {
		taskID := fmt.Sprintf("hist-task-%d", i)
		cost := 0.10 + float64(i)*0.05

		// Record task metadata.
		err := ledger.RecordTaskMetadata(ctx, taskID, "moderate", 3)
		require.NoError(t, err)

		// Record a cost record for this task.
		rec := CostRecord{
			RecordID:     fmt.Sprintf("hist-rec-%d", i),
			TaskID:       taskID,
			Role:         "implementer",
			Model:        "gpt-4o",
			Provider:     "openai",
			WorkPhase:    "implementation",
			InputTokens:  1000,
			OutputTokens: 500,
			CostUSD:      cost,
			Estimated:    false,
			DurationMS:   2000,
			InvocationID: fmt.Sprintf("hist-inv-%d", i),
			Timestamp:    now.Add(-time.Duration(i) * time.Hour),
		}
		err = ledger.Record(ctx, rec)
		require.NoError(t, err)
	}

	forecast, err := ledger.Forecast(ctx, "moderate", 3)
	require.NoError(t, err)

	assert.Equal(t, 10, forecast.BasisTaskCount)
	// Confidence = min(10/20, 1.0) = 0.5
	assert.InDelta(t, 0.5, forecast.ConfidenceLevel, 1e-10)
	// P10 of [0.10, 0.15, 0.20, ..., 0.55]
	assert.Greater(t, forecast.MinCostUSD, 0.0)
	// P50 (median) should be between 0.30 and 0.35
	assert.Greater(t, forecast.ExpectedCostUSD, 0.25)
	assert.Less(t, forecast.ExpectedCostUSD, 0.40)
	// P90 should be near the upper end
	assert.Greater(t, forecast.MaxCostUSD, 0.40)
	// Invariant: min <= expected <= max
	assert.LessOrEqual(t, forecast.MinCostUSD, forecast.ExpectedCostUSD)
	assert.LessOrEqual(t, forecast.ExpectedCostUSD, forecast.MaxCostUSD)
}

func TestForecast_ConfidenceSaturatesAtOne(t *testing.T) {
	ledger := setupTestLedger(t)
	ctx := context.Background()
	now := time.Now().UTC()

	// Seed 25 tasks (above 20 threshold for max confidence).
	for i := range 25 {
		taskID := fmt.Sprintf("conf-task-%d", i)
		err := ledger.RecordTaskMetadata(ctx, taskID, "routine", 1)
		require.NoError(t, err)

		rec := CostRecord{
			RecordID:     fmt.Sprintf("conf-rec-%d", i),
			TaskID:       taskID,
			Role:         "planner",
			Model:        "gpt-4o-mini",
			Provider:     "openai",
			WorkPhase:    "planning",
			InputTokens:  500,
			OutputTokens: 200,
			CostUSD:      0.005 + float64(i)*0.001,
			Estimated:    false,
			DurationMS:   800,
			InvocationID: fmt.Sprintf("conf-inv-%d", i),
			Timestamp:    now.Add(-time.Duration(i) * time.Hour),
		}
		err = ledger.Record(ctx, rec)
		require.NoError(t, err)
	}

	forecast, err := ledger.Forecast(ctx, "routine", 1)
	require.NoError(t, err)

	assert.Equal(t, 25, forecast.BasisTaskCount)
	// Confidence = min(25/20, 1.0) = 1.0
	assert.InDelta(t, 1.0, forecast.ConfidenceLevel, 1e-10)
}

func TestForecast_OrderingInvariant(t *testing.T) {
	ledger := setupTestLedger(t)
	ctx := context.Background()
	now := time.Now().UTC()

	// Seed varied costs for "complex" tier.
	costs := []float64{0.50, 0.80, 1.20, 0.30, 2.00, 0.70, 1.50, 0.90}
	for i, cost := range costs {
		taskID := fmt.Sprintf("order-task-%d", i)
		err := ledger.RecordTaskMetadata(ctx, taskID, "complex", 6)
		require.NoError(t, err)

		rec := CostRecord{
			RecordID:     fmt.Sprintf("order-rec-%d", i),
			TaskID:       taskID,
			Role:         "implementer",
			Model:        "claude-sonnet-4-20250514",
			Provider:     "anthropic",
			WorkPhase:    "implementation",
			InputTokens:  5000,
			OutputTokens: 2000,
			CostUSD:      cost,
			Estimated:    false,
			DurationMS:   3000,
			InvocationID: fmt.Sprintf("order-inv-%d", i),
			Timestamp:    now.Add(-time.Duration(i) * time.Hour),
		}
		err = ledger.Record(ctx, rec)
		require.NoError(t, err)
	}

	forecast, err := ledger.Forecast(ctx, "complex", 6)
	require.NoError(t, err)

	// Invariant: min <= expected <= max
	assert.LessOrEqual(t, forecast.MinCostUSD, forecast.ExpectedCostUSD)
	assert.LessOrEqual(t, forecast.ExpectedCostUSD, forecast.MaxCostUSD)
	assert.Greater(t, forecast.MinCostUSD, 0.0)
}

func TestForecast_MultipleRecordsPerTask(t *testing.T) {
	ledger := setupTestLedger(t)
	ctx := context.Background()
	now := time.Now().UTC()

	// A task with multiple cost records should sum to get total task cost.
	err := ledger.RecordTaskMetadata(ctx, "multi-rec-task", "moderate", 3)
	require.NoError(t, err)

	for i := range 4 {
		rec := CostRecord{
			RecordID:     fmt.Sprintf("multi-rec-%d", i),
			TaskID:       "multi-rec-task",
			Role:         "implementer",
			Model:        "gpt-4o",
			Provider:     "openai",
			WorkPhase:    "implementation",
			InputTokens:  1000,
			OutputTokens: 500,
			CostUSD:      0.10, // 4 records * 0.10 = 0.40 total
			Estimated:    false,
			DurationMS:   1000,
			InvocationID: fmt.Sprintf("multi-inv-%d", i),
			Timestamp:    now.Add(-time.Duration(i) * time.Minute),
		}
		err = ledger.Record(ctx, rec)
		require.NoError(t, err)
	}

	forecast, err := ledger.Forecast(ctx, "moderate", 3)
	require.NoError(t, err)

	// Single task, so min=expected=max=0.40
	assert.Equal(t, 1, forecast.BasisTaskCount)
	assert.InDelta(t, 0.40, forecast.MinCostUSD, 1e-10)
	assert.InDelta(t, 0.40, forecast.ExpectedCostUSD, 1e-10)
	assert.InDelta(t, 0.40, forecast.MaxCostUSD, 1e-10)
}

func TestRecordTaskMetadata(t *testing.T) {
	ledger := setupTestLedger(t)
	ctx := context.Background()

	err := ledger.RecordTaskMetadata(ctx, "task-meta-1", "complex", 8)
	require.NoError(t, err)

	tier, files, err := ledger.GetTaskMetadata(ctx, "task-meta-1")
	require.NoError(t, err)
	assert.Equal(t, "complex", tier)
	assert.Equal(t, 8, files)
}

func TestRecordTaskMetadata_Upsert(t *testing.T) {
	ledger := setupTestLedger(t)
	ctx := context.Background()

	err := ledger.RecordTaskMetadata(ctx, "task-upsert", "routine", 1)
	require.NoError(t, err)

	// Update the same task.
	err = ledger.RecordTaskMetadata(ctx, "task-upsert", "complex", 10)
	require.NoError(t, err)

	tier, files, err := ledger.GetTaskMetadata(ctx, "task-upsert")
	require.NoError(t, err)
	assert.Equal(t, "complex", tier)
	assert.Equal(t, 10, files)
}

func TestGetTaskMetadata_NotFound(t *testing.T) {
	ledger := setupTestLedger(t)
	ctx := context.Background()

	tier, files, err := ledger.GetTaskMetadata(ctx, "nonexistent")
	require.NoError(t, err)
	assert.Equal(t, "", tier)
	assert.Equal(t, 0, files)
}

func TestUpdateForecast_OnTrack(t *testing.T) {
	ledger := setupTestLedger(t)
	ctx := context.Background()
	now := time.Now().UTC()

	// Seed some historical data.
	for i := range 5 {
		taskID := fmt.Sprintf("update-hist-%d", i)
		err := ledger.RecordTaskMetadata(ctx, taskID, "moderate", 3)
		require.NoError(t, err)

		rec := CostRecord{
			RecordID:     fmt.Sprintf("update-hist-rec-%d", i),
			TaskID:       taskID,
			Role:         "implementer",
			Model:        "gpt-4o",
			Provider:     "openai",
			WorkPhase:    "implementation",
			InputTokens:  2000,
			OutputTokens: 1000,
			CostUSD:      0.20,
			Estimated:    false,
			DurationMS:   2000,
			InvocationID: fmt.Sprintf("update-hist-inv-%d", i),
			Timestamp:    now.Add(-time.Duration(i+1) * 24 * time.Hour),
		}
		err = ledger.Record(ctx, rec)
		require.NoError(t, err)
	}

	// Create a new task that has spent some cost.
	err := ledger.RecordTaskMetadata(ctx, "current-task", "moderate", 3)
	require.NoError(t, err)

	rec := CostRecord{
		RecordID:     "current-rec-1",
		TaskID:       "current-task",
		Role:         "planner",
		Model:        "gpt-4o",
		Provider:     "openai",
		WorkPhase:    "planning",
		InputTokens:  500,
		OutputTokens: 200,
		CostUSD:      0.05, // Well below expected (0.20)
		Estimated:    false,
		DurationMS:   1000,
		InvocationID: "current-inv-1",
		Timestamp:    now,
	}
	err = ledger.Record(ctx, rec)
	require.NoError(t, err)

	// UpdateForecast should return the base forecast since we're on track.
	updated, err := ledger.UpdateForecast(ctx, "current-task", "moderate", 3)
	require.NoError(t, err)

	// Should match base forecast from historical data.
	baseForecast, err := ledger.Forecast(ctx, "moderate", 3)
	require.NoError(t, err)

	// Note: current-task is now also in the historical data, but that's fine.
	assert.InDelta(t, baseForecast.ExpectedCostUSD, updated.ExpectedCostUSD, 1e-10)
}

func TestPercentile(t *testing.T) {
	tests := []struct {
		name   string
		data   []float64
		p      float64
		expect float64
	}{
		{"empty", nil, 0.5, 0},
		{"single", []float64{42.0}, 0.5, 42.0},
		{"two_median", []float64{10.0, 20.0}, 0.5, 15.0},
		{"two_p10", []float64{10.0, 20.0}, 0.1, 11.0},
		{"even_median", []float64{1, 2, 3, 4, 5, 6, 7, 8, 9, 10}, 0.5, 5.5},
		{"p10_of_10", []float64{1, 2, 3, 4, 5, 6, 7, 8, 9, 10}, 0.1, 1.9},
		{"p90_of_10", []float64{1, 2, 3, 4, 5, 6, 7, 8, 9, 10}, 0.9, 9.1},
		{"p0", []float64{1, 2, 3}, 0.0, 1.0},
		{"p100", []float64{1, 2, 3}, 1.0, 3.0},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			result := percentile(tc.data, tc.p)
			assert.InDelta(t, tc.expect, result, 1e-10)
		})
	}
}
