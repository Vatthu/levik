package orchestratorhost

import (
	"bytes"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"testing"

	"github.com/Vatthu/vikram/pkg/costledger"
	"github.com/stretchr/testify/require"
)

// setupIntegrationServer creates a Server backed by a real SQLiteLedger using an
// in-memory-equivalent temp SQLite database. This tests the full end-to-end flow
// from HTTP handler → cost ledger → SQLite and back.
func setupIntegrationServer(t *testing.T) (*Server, *costledger.SQLiteLedger) {
	t.Helper()

	root := t.TempDir()
	dbPath := filepath.Join(root, "cost_test.db")

	db, err := costledger.OpenDB(dbPath)
	require.NoError(t, err)
	t.Cleanup(func() { db.Close() })

	ledger, err := costledger.NewSQLiteLedger(db, costledger.LedgerConfig{ResetHour: 0})
	require.NoError(t, err)

	server := NewServer(Config{
		SocketPath:    filepath.Join(root, "run", "vikramd.sock"),
		WorkspaceRoot: root,
	}, nil)
	server.SetLedger(ledger)

	return server, ledger
}

// TestIntegration_RecordAndQueryCumulative verifies that recording a cost event
// via POST /v1/cost/record stores the record and that querying GET /v1/cost/task/{id}
// returns the correct cumulative cost.
// Validates: Requirements 1.1, 2.3
func TestIntegration_RecordAndQueryCumulative(t *testing.T) {
	server, _ := setupIntegrationServer(t)
	handler := server.handler()

	// Record a single cost event.
	body := costRecordRequest{
		RecordID:     "integ-rec-001",
		TaskID:       "task_integ_1",
		Role:         "engineer",
		Model:        "gpt-4",
		Provider:     "openai",
		WorkPhase:    "implementation",
		InputTokens:  2000,
		OutputTokens: 1000,
		CostUSD:      0.10,
		Estimated:    false,
		DurationMS:   1500,
		InvocationID: "inv-001",
	}
	reqBody, err := json.Marshal(body)
	require.NoError(t, err)

	req := httptest.NewRequest(http.MethodPost, "/v1/cost/record", bytes.NewReader(reqBody))
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)
	require.Equal(t, http.StatusOK, rec.Code)

	var recordResp map[string]interface{}
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &recordResp))
	require.Equal(t, true, recordResp["recorded"])
	require.Equal(t, "integ-rec-001", recordResp["record_id"])

	// Query cumulative for that task.
	req = httptest.NewRequest(http.MethodGet, "/v1/cost/task/task_integ_1", nil)
	req.SetPathValue("task_id", "task_integ_1")
	rec = httptest.NewRecorder()
	handler.ServeHTTP(rec, req)
	require.Equal(t, http.StatusOK, rec.Code)

	var cumulResp map[string]interface{}
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &cumulResp))
	require.Equal(t, "task_integ_1", cumulResp["task_id"])
	require.InDelta(t, 0.10, cumulResp["cumulative_usd"], 0.001)
}

// TestIntegration_MultipleCostsAggregation verifies that recording multiple costs
// for the same task produces correct cumulative aggregation, and that the daily
// total reflects the sum across all tasks.
// Validates: Requirements 1.1, 5.2
func TestIntegration_MultipleCostsAggregation(t *testing.T) {
	server, _ := setupIntegrationServer(t)
	handler := server.handler()

	// Record 5 cost events for the same task.
	costs := []float64{0.05, 0.10, 0.15, 0.08, 0.12}
	expectedTotal := 0.0
	for i, cost := range costs {
		expectedTotal += cost
		body := costRecordRequest{
			RecordID:     fmt.Sprintf("multi-rec-%03d", i+1),
			TaskID:       "task_multi",
			Role:         "engineer",
			Model:        "gpt-4",
			Provider:     "openai",
			WorkPhase:    "implementation",
			InputTokens:  1000 + i*200,
			OutputTokens: 500 + i*100,
			CostUSD:      cost,
			Estimated:    false,
			DurationMS:   int64(1000 + i*500),
			InvocationID: fmt.Sprintf("inv-%03d", i+1),
		}
		reqBody, err := json.Marshal(body)
		require.NoError(t, err)

		req := httptest.NewRequest(http.MethodPost, "/v1/cost/record", bytes.NewReader(reqBody))
		rec := httptest.NewRecorder()
		handler.ServeHTTP(rec, req)
		require.Equal(t, http.StatusOK, rec.Code, "record %d failed", i)
	}

	// Query cumulative — should be the sum of all costs.
	req := httptest.NewRequest(http.MethodGet, "/v1/cost/task/task_multi", nil)
	req.SetPathValue("task_id", "task_multi")
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)
	require.Equal(t, http.StatusOK, rec.Code)

	var cumulResp map[string]interface{}
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &cumulResp))
	require.InDelta(t, expectedTotal, cumulResp["cumulative_usd"], 0.001)

	// Also record costs for a different task and verify daily total includes both.
	body := costRecordRequest{
		RecordID:     "other-rec-001",
		TaskID:       "task_other",
		Role:         "reviewer",
		Model:        "claude-3",
		Provider:     "anthropic",
		WorkPhase:    "review",
		InputTokens:  3000,
		OutputTokens: 500,
		CostUSD:      0.20,
		Estimated:    false,
		DurationMS:   2000,
		InvocationID: "inv-other-001",
	}
	reqBody, err := json.Marshal(body)
	require.NoError(t, err)
	req = httptest.NewRequest(http.MethodPost, "/v1/cost/record", bytes.NewReader(reqBody))
	rec = httptest.NewRecorder()
	handler.ServeHTTP(rec, req)
	require.Equal(t, http.StatusOK, rec.Code)

	// Check daily total reflects all tasks.
	req = httptest.NewRequest(http.MethodGet, "/v1/cost/daily", nil)
	rec = httptest.NewRecorder()
	handler.ServeHTTP(rec, req)
	require.Equal(t, http.StatusOK, rec.Code)

	var dailyResp map[string]interface{}
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &dailyResp))
	require.InDelta(t, expectedTotal+0.20, dailyResp["daily_total_usd"], 0.001)

	// Verify the other task has its own independent cumulative.
	req = httptest.NewRequest(http.MethodGet, "/v1/cost/task/task_other", nil)
	req.SetPathValue("task_id", "task_other")
	rec = httptest.NewRecorder()
	handler.ServeHTTP(rec, req)
	require.Equal(t, http.StatusOK, rec.Code)

	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &cumulResp))
	require.InDelta(t, 0.20, cumulResp["cumulative_usd"], 0.001)
}

// TestIntegration_CircuitBreakerDailyCeiling verifies that when a daily cost ceiling
// is configured and costs exceed it, the circuit breaker activates for subsequent
// task checks.
// Validates: Requirements 2.3, 5.2
func TestIntegration_CircuitBreakerDailyCeiling(t *testing.T) {
	root := t.TempDir()
	dbPath := filepath.Join(root, "cost_cb_test.db")

	db, err := costledger.OpenDB(dbPath)
	require.NoError(t, err)
	t.Cleanup(func() { db.Close() })

	// Configure a low daily ceiling.
	_, err = db.Exec(`INSERT INTO daily_ceilings (id, max_daily_usd, reset_hour, created_at, updated_at)
		VALUES (1, 0.25, 0, datetime('now'), datetime('now'))`)
	require.NoError(t, err)

	ledger, err := costledger.NewSQLiteLedger(db, costledger.LedgerConfig{ResetHour: 0})
	require.NoError(t, err)

	// Configure the circuit breaker with a task budget store.
	budgets := costledger.NewTaskBudgetStore()
	budgets.Register("task_cb", 1.00) // generous per-task budget

	ledger.SetCircuitBreakerConfig(costledger.CircuitBreakerConfig{
		WarningThreshold: 0.8,
		TaskBudgets:      budgets,
	})

	server := NewServer(Config{
		SocketPath:    filepath.Join(root, "run", "vikramd.sock"),
		WorkspaceRoot: root,
	}, nil)
	server.SetLedger(ledger)
	handler := server.handler()

	// Record costs that stay below the ceiling.
	body := costRecordRequest{
		RecordID:     "cb-rec-001",
		TaskID:       "task_cb",
		Role:         "engineer",
		Model:        "gpt-4",
		Provider:     "openai",
		WorkPhase:    "implementation",
		InputTokens:  1500,
		OutputTokens: 800,
		CostUSD:      0.10,
		Estimated:    false,
		DurationMS:   1200,
		InvocationID: "inv-cb-001",
	}
	reqBody, _ := json.Marshal(body)
	req := httptest.NewRequest(http.MethodPost, "/v1/cost/record", bytes.NewReader(reqBody))
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)
	require.Equal(t, http.StatusOK, rec.Code)

	// Circuit breaker should NOT trip yet (0.10 < 0.25 ceiling).
	shouldBreak, reason, err := ledger.CheckCircuitBreaker(t.Context(), "task_cb")
	require.NoError(t, err)
	require.False(t, shouldBreak, "should not break when under ceiling")
	require.Empty(t, reason)

	// Record another cost that pushes total above the daily ceiling.
	body.RecordID = "cb-rec-002"
	body.InvocationID = "inv-cb-002"
	body.CostUSD = 0.20
	reqBody, _ = json.Marshal(body)
	req = httptest.NewRequest(http.MethodPost, "/v1/cost/record", bytes.NewReader(reqBody))
	rec = httptest.NewRecorder()
	handler.ServeHTTP(rec, req)
	require.Equal(t, http.StatusOK, rec.Code)

	// Now the daily total is 0.30, which exceeds the 0.25 ceiling.
	// Circuit breaker should trip with "daily_ceiling_exceeded".
	shouldBreak, reason, err = ledger.CheckCircuitBreaker(t.Context(), "task_cb")
	require.NoError(t, err)
	require.True(t, shouldBreak, "should break when daily ceiling exceeded")
	require.Equal(t, "daily_ceiling_exceeded", reason)
}

// TestIntegration_CircuitBreakerPerTaskBudget verifies that when a task's
// cumulative cost reaches its per-task budget, the circuit breaker activates.
// Validates: Requirements 2.3
func TestIntegration_CircuitBreakerPerTaskBudget(t *testing.T) {
	root := t.TempDir()
	dbPath := filepath.Join(root, "cost_budget_test.db")

	db, err := costledger.OpenDB(dbPath)
	require.NoError(t, err)
	t.Cleanup(func() { db.Close() })

	ledger, err := costledger.NewSQLiteLedger(db, costledger.LedgerConfig{ResetHour: 0})
	require.NoError(t, err)

	// Set a low per-task budget.
	budgets := costledger.NewTaskBudgetStore()
	budgets.Register("task_budget", 0.15) // $0.15 cap

	ledger.SetCircuitBreakerConfig(costledger.CircuitBreakerConfig{
		WarningThreshold: 0.8,
		TaskBudgets:      budgets,
	})

	server := NewServer(Config{
		SocketPath:    filepath.Join(root, "run", "vikramd.sock"),
		WorkspaceRoot: root,
	}, nil)
	server.SetLedger(ledger)
	handler := server.handler()

	// Record first cost (below budget).
	body := costRecordRequest{
		RecordID:     "budget-rec-001",
		TaskID:       "task_budget",
		Role:         "engineer",
		Model:        "gpt-4",
		Provider:     "openai",
		WorkPhase:    "planning",
		InputTokens:  1000,
		OutputTokens: 500,
		CostUSD:      0.08,
		Estimated:    false,
		DurationMS:   900,
		InvocationID: "inv-budget-001",
	}
	reqBody, _ := json.Marshal(body)
	req := httptest.NewRequest(http.MethodPost, "/v1/cost/record", bytes.NewReader(reqBody))
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)
	require.Equal(t, http.StatusOK, rec.Code)

	// Not tripped yet (0.08 < 0.15).
	shouldBreak, _, err := ledger.CheckCircuitBreaker(t.Context(), "task_budget")
	require.NoError(t, err)
	require.False(t, shouldBreak)

	// Record second cost pushing past the budget.
	body.RecordID = "budget-rec-002"
	body.InvocationID = "inv-budget-002"
	body.CostUSD = 0.10
	reqBody, _ = json.Marshal(body)
	req = httptest.NewRequest(http.MethodPost, "/v1/cost/record", bytes.NewReader(reqBody))
	rec = httptest.NewRecorder()
	handler.ServeHTTP(rec, req)
	require.Equal(t, http.StatusOK, rec.Code)

	// Now cumulative is 0.18, which exceeds 0.15 cap.
	shouldBreak, reason, err := ledger.CheckCircuitBreaker(t.Context(), "task_budget")
	require.NoError(t, err)
	require.True(t, shouldBreak, "should break when per-task budget exceeded")
	require.Equal(t, "budget_exceeded", reason)
}

// TestIntegration_ForecastWithHistoricalData verifies that the forecast endpoint
// returns reasonable results when historical cost data exists in the database.
// Validates: Requirements 1.1
func TestIntegration_ForecastWithHistoricalData(t *testing.T) {
	root := t.TempDir()
	dbPath := filepath.Join(root, "cost_forecast_test.db")

	db, err := costledger.OpenDB(dbPath)
	require.NoError(t, err)
	t.Cleanup(func() { db.Close() })

	ledger, err := costledger.NewSQLiteLedger(db, costledger.LedgerConfig{ResetHour: 0})
	require.NoError(t, err)

	server := NewServer(Config{
		SocketPath:    filepath.Join(root, "run", "vikramd.sock"),
		WorkspaceRoot: root,
	}, nil)
	server.SetLedger(ledger)
	handler := server.handler()

	ctx := t.Context()

	// Seed historical data: multiple tasks of "moderate" complexity with varying costs.
	historicalCosts := []float64{0.08, 0.12, 0.10, 0.15, 0.09, 0.11, 0.14, 0.07, 0.13, 0.10}
	for i, cost := range historicalCosts {
		taskID := fmt.Sprintf("hist_task_%03d", i+1)

		// Record task metadata (complexity tier).
		err := ledger.RecordTaskMetadata(ctx, taskID, "moderate", 3)
		require.NoError(t, err)

		// Record cost records for each historical task.
		err = ledger.Record(ctx, costledger.CostRecord{
			RecordID:     fmt.Sprintf("hist-rec-%03d", i+1),
			TaskID:       taskID,
			Role:         "engineer",
			Model:        "gpt-4",
			Provider:     "openai",
			WorkPhase:    "implementation",
			InputTokens:  2000,
			OutputTokens: 1000,
			CostUSD:      cost,
			Estimated:    false,
			DurationMS:   1500,
			InvocationID: fmt.Sprintf("hist-inv-%03d", i+1),
		})
		require.NoError(t, err)
	}

	// Request a forecast via the HTTP endpoint.
	body := costForecastRequest{
		Complexity:  "moderate",
		TargetFiles: 3,
	}
	reqBody, err := json.Marshal(body)
	require.NoError(t, err)

	req := httptest.NewRequest(http.MethodPost, "/v1/cost/forecast", bytes.NewReader(reqBody))
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)
	require.Equal(t, http.StatusOK, rec.Code)

	var forecast costledger.CostForecast
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &forecast))

	// With 10 historical tasks, confidence should be 10/20 = 0.5.
	require.InDelta(t, 0.5, forecast.ConfidenceLevel, 0.01)
	require.Equal(t, 10, forecast.BasisTaskCount)

	// The forecast values should be within the range of historical data.
	require.Greater(t, forecast.MinCostUSD, 0.0)
	require.Greater(t, forecast.ExpectedCostUSD, forecast.MinCostUSD)
	require.Greater(t, forecast.MaxCostUSD, forecast.ExpectedCostUSD)
	require.LessOrEqual(t, forecast.MaxCostUSD, 0.20) // should be bounded by data

	// Verify heuristic-based forecast for a tier with no data.
	body.Complexity = "critical"
	reqBody, _ = json.Marshal(body)
	req = httptest.NewRequest(http.MethodPost, "/v1/cost/forecast", bytes.NewReader(reqBody))
	rec = httptest.NewRecorder()
	handler.ServeHTTP(rec, req)
	require.Equal(t, http.StatusOK, rec.Code)

	var heuristicForecast costledger.CostForecast
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &heuristicForecast))

	// Heuristic forecast has zero confidence and zero basis tasks.
	require.Equal(t, 0.0, heuristicForecast.ConfidenceLevel)
	require.Equal(t, 0, heuristicForecast.BasisTaskCount)
	require.Greater(t, heuristicForecast.ExpectedCostUSD, 0.0)
}
