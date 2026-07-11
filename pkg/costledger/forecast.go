package costledger

import (
	"context"
	"database/sql"
	"fmt"
	"math"
	"sort"
	"time"
)

// Default heuristic estimates (USD) per complexity tier when no historical data exists.
// These are base costs per target file, derived from typical model usage patterns.
var defaultHeuristics = map[string]struct {
	baseCostPerFile float64
	minMultiplier   float64
	maxMultiplier   float64
}{
	"routine":  {baseCostPerFile: 0.01, minMultiplier: 0.5, maxMultiplier: 2.0},
	"moderate": {baseCostPerFile: 0.05, minMultiplier: 0.4, maxMultiplier: 2.5},
	"complex":  {baseCostPerFile: 0.15, minMultiplier: 0.3, maxMultiplier: 3.0},
	"critical": {baseCostPerFile: 0.40, minMultiplier: 0.3, maxMultiplier: 4.0},
}

// Forecast produces a cost estimate for a new task based on historical cost data
// for tasks of similar complexity. If no historical data exists, falls back to
// heuristic estimates based on complexity tier and target file count.
//
// Statistical approach:
//   - min = P10 (10th percentile) of historical task costs for the tier
//   - expected = median (P50) of historical task costs for the tier
//   - max = P90 (90th percentile) of historical task costs for the tier
//   - confidence = min(basisTaskCount / 20, 1.0)
func (l *SQLiteLedger) Forecast(ctx context.Context, complexity string, targetFiles int) (CostForecast, error) {
	// Query historical costs for tasks with matching complexity tier.
	taskCosts, err := l.getHistoricalTaskCosts(ctx, complexity)
	if err != nil {
		return CostForecast{}, fmt.Errorf("costledger: forecast query failed: %w", err)
	}

	if len(taskCosts) == 0 {
		// No historical data — use heuristic estimate.
		return l.heuristicForecast(complexity, targetFiles), nil
	}

	// Sort costs for percentile computation.
	sort.Float64s(taskCosts)

	basisCount := len(taskCosts)
	minCost := percentile(taskCosts, 0.10)
	expectedCost := percentile(taskCosts, 0.50)
	maxCost := percentile(taskCosts, 0.90)
	confidence := math.Min(float64(basisCount)/20.0, 1.0)

	return CostForecast{
		MinCostUSD:      minCost,
		ExpectedCostUSD: expectedCost,
		MaxCostUSD:      maxCost,
		ConfidenceLevel: confidence,
		BasisTaskCount:  basisCount,
	}, nil
}

// UpdateForecast recalculates the forecast at a phase transition, incorporating
// actual spend so far to refine the remaining cost estimate.
// It returns an updated forecast reflecting actual consumption vs predicted.
func (l *SQLiteLedger) UpdateForecast(ctx context.Context, taskID, complexity string, targetFiles int) (CostForecast, error) {
	// Get the base forecast from historical data.
	baseForecast, err := l.Forecast(ctx, complexity, targetFiles)
	if err != nil {
		return CostForecast{}, err
	}

	// Get actual spend so far for this task.
	actualSpend, err := l.TaskCumulative(ctx, taskID)
	if err != nil {
		return CostForecast{}, fmt.Errorf("costledger: update forecast cumulative query: %w", err)
	}

	// If the task has already spent more than expected, adjust the forecast upward.
	if actualSpend > baseForecast.ExpectedCostUSD {
		// Scale factor based on how much we've exceeded expected.
		scaleFactor := actualSpend / baseForecast.ExpectedCostUSD
		return CostForecast{
			MinCostUSD:      actualSpend,
			ExpectedCostUSD: actualSpend * (1.0 + 0.2*scaleFactor),
			MaxCostUSD:      baseForecast.MaxCostUSD * scaleFactor,
			ConfidenceLevel: baseForecast.ConfidenceLevel * 0.8, // Lower confidence when exceeding
			BasisTaskCount:  baseForecast.BasisTaskCount,
		}, nil
	}

	// Otherwise, the forecast remains as-is but we note we're on track.
	return baseForecast, nil
}

// RecordTaskMetadata stores the complexity tier and target file count for a task,
// enabling future forecast queries to group costs by similar tasks.
func (l *SQLiteLedger) RecordTaskMetadata(ctx context.Context, taskID, complexityTier string, targetFiles int) error {
	l.dbMu.Lock()
	defer l.dbMu.Unlock()

	_, err := l.db.ExecContext(ctx, `
		INSERT OR REPLACE INTO task_metadata (task_id, complexity_tier, target_files, created_at)
		VALUES (?, ?, ?, ?)`,
		taskID, complexityTier, targetFiles, time.Now().UTC().Format(time.RFC3339),
	)
	if err != nil {
		return fmt.Errorf("costledger: record task metadata: %w", err)
	}
	return nil
}

// getHistoricalTaskCosts queries the total cost per task for all tasks
// matching the given complexity tier. Returns a slice of per-task total costs.
func (l *SQLiteLedger) getHistoricalTaskCosts(ctx context.Context, complexity string) ([]float64, error) {
	rows, err := l.db.QueryContext(ctx, `
		SELECT SUM(cr.cost_usd) as total_cost
		FROM cost_records cr
		INNER JOIN task_metadata tm ON cr.task_id = tm.task_id
		WHERE tm.complexity_tier = ?
		GROUP BY cr.task_id
		ORDER BY total_cost`,
		complexity,
	)
	if err != nil {
		return nil, fmt.Errorf("query historical costs: %w", err)
	}
	defer rows.Close()

	var costs []float64
	for rows.Next() {
		var cost float64
		if err := rows.Scan(&cost); err != nil {
			return nil, fmt.Errorf("scan historical cost: %w", err)
		}
		costs = append(costs, cost)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("iterate historical costs: %w", err)
	}
	return costs, nil
}

// heuristicForecast returns a forecast based on predefined cost heuristics
// when no historical data is available for the complexity tier.
func (l *SQLiteLedger) heuristicForecast(complexity string, targetFiles int) CostForecast {
	h, ok := defaultHeuristics[complexity]
	if !ok {
		// Default to "moderate" heuristic for unknown tiers.
		h = defaultHeuristics["moderate"]
	}

	files := targetFiles
	if files < 1 {
		files = 1
	}

	expectedCost := h.baseCostPerFile * float64(files)
	minCost := expectedCost * h.minMultiplier
	maxCost := expectedCost * h.maxMultiplier

	return CostForecast{
		MinCostUSD:      minCost,
		ExpectedCostUSD: expectedCost,
		MaxCostUSD:      maxCost,
		ConfidenceLevel: 0.0, // Zero confidence since no historical data
		BasisTaskCount:  0,
	}
}

// percentile returns the value at the given percentile (0.0–1.0) from a sorted slice.
// Uses linear interpolation between adjacent values.
func percentile(sorted []float64, p float64) float64 {
	if len(sorted) == 0 {
		return 0
	}
	if len(sorted) == 1 {
		return sorted[0]
	}

	// Clamp percentile to valid range.
	if p <= 0 {
		return sorted[0]
	}
	if p >= 1 {
		return sorted[len(sorted)-1]
	}

	// Use the "nearest rank" method with interpolation.
	rank := p * float64(len(sorted)-1)
	lower := int(math.Floor(rank))
	upper := int(math.Ceil(rank))

	if lower == upper {
		return sorted[lower]
	}

	// Linear interpolation.
	fraction := rank - float64(lower)
	return sorted[lower]*(1-fraction) + sorted[upper]*fraction
}

// GetTaskMetadata retrieves the metadata for a task if it exists.
func (l *SQLiteLedger) GetTaskMetadata(ctx context.Context, taskID string) (complexityTier string, targetFiles int, err error) {
	err = l.db.QueryRowContext(ctx,
		`SELECT complexity_tier, target_files FROM task_metadata WHERE task_id = ?`,
		taskID,
	).Scan(&complexityTier, &targetFiles)
	if err == sql.ErrNoRows {
		return "", 0, nil
	}
	if err != nil {
		return "", 0, fmt.Errorf("costledger: get task metadata: %w", err)
	}
	return complexityTier, targetFiles, nil
}
