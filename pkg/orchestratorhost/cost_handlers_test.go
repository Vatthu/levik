package orchestratorhost

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"testing"

	"github.com/Vatthu/vikram/pkg/costledger"
	"github.com/Vatthu/vikram/pkg/orchestrator"
	"github.com/stretchr/testify/require"
)

// stubLedger is a test double implementing the costLedger interface.
type stubLedger struct {
	records    []costledger.CostRecord
	taskTotals map[string]float64
	dailyTotal float64
	forecast   costledger.CostForecast
	recordErr  error
}

func newStubLedger() *stubLedger {
	return &stubLedger{
		taskTotals: make(map[string]float64),
	}
}

func (l *stubLedger) Record(_ context.Context, rec costledger.CostRecord) error {
	if l.recordErr != nil {
		return l.recordErr
	}
	l.records = append(l.records, rec)
	l.taskTotals[rec.TaskID] += rec.CostUSD
	l.dailyTotal += rec.CostUSD
	return nil
}

func (l *stubLedger) TaskCumulative(_ context.Context, taskID string) (float64, error) {
	return l.taskTotals[taskID], nil
}

func (l *stubLedger) DailyTotal(_ context.Context) (float64, error) {
	return l.dailyTotal, nil
}

func (l *stubLedger) Forecast(_ context.Context, complexity string, targetFiles int) (costledger.CostForecast, error) {
	return l.forecast, nil
}

func TestCostRecordEndpointRecordsEvent(t *testing.T) {
	root := t.TempDir()
	ledger := newStubLedger()
	server := NewServer(Config{
		SocketPath:    filepath.Join(root, "run", "vikramd.sock"),
		WorkspaceRoot: root,
	}, nil)
	server.SetLedger(ledger)

	body := costRecordRequest{
		RecordID:     "rec-001",
		TaskID:       "task_123",
		Role:         "engineer",
		Model:        "gpt-4",
		Provider:     "openai",
		WorkPhase:    "implementation",
		InputTokens:  1500,
		OutputTokens: 800,
		CostUSD:      0.045,
		Estimated:    false,
		DurationMS:   2300,
		InvocationID: "inv-001",
	}
	reqBody, err := json.Marshal(body)
	require.NoError(t, err)

	req := httptest.NewRequest(http.MethodPost, "/v1/cost/record", bytes.NewReader(reqBody))
	rec := httptest.NewRecorder()
	server.handler().ServeHTTP(rec, req)

	require.Equal(t, http.StatusOK, rec.Code)

	var resp map[string]interface{}
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &resp))
	require.Equal(t, true, resp["recorded"])
	require.Equal(t, "rec-001", resp["record_id"])

	require.Len(t, ledger.records, 1)
	require.Equal(t, "task_123", ledger.records[0].TaskID)
	require.Equal(t, "engineer", ledger.records[0].Role)
	require.Equal(t, 1500, ledger.records[0].InputTokens)
	require.Equal(t, 0.045, ledger.records[0].CostUSD)
}

func TestCostRecordEndpointRejectsMissingFields(t *testing.T) {
	root := t.TempDir()
	ledger := newStubLedger()
	server := NewServer(Config{
		SocketPath:    filepath.Join(root, "run", "vikramd.sock"),
		WorkspaceRoot: root,
	}, nil)
	server.SetLedger(ledger)

	// Missing record_id and task_id
	body := costRecordRequest{
		Role: "engineer",
	}
	reqBody, err := json.Marshal(body)
	require.NoError(t, err)

	req := httptest.NewRequest(http.MethodPost, "/v1/cost/record", bytes.NewReader(reqBody))
	rec := httptest.NewRecorder()
	server.handler().ServeHTTP(rec, req)

	require.Equal(t, http.StatusBadRequest, rec.Code)
	require.Contains(t, rec.Body.String(), "required")
}

func TestCostRecordEndpointReturns503WithoutLedger(t *testing.T) {
	root := t.TempDir()
	server := NewServer(Config{
		SocketPath:    filepath.Join(root, "run", "vikramd.sock"),
		WorkspaceRoot: root,
	}, nil)
	// No ledger set

	body := costRecordRequest{RecordID: "rec-001", TaskID: "task_123"}
	reqBody, err := json.Marshal(body)
	require.NoError(t, err)

	req := httptest.NewRequest(http.MethodPost, "/v1/cost/record", bytes.NewReader(reqBody))
	rec := httptest.NewRecorder()
	server.handler().ServeHTTP(rec, req)

	require.Equal(t, http.StatusServiceUnavailable, rec.Code)
}

func TestCostTaskCumulativeEndpointReturnsTotalCost(t *testing.T) {
	root := t.TempDir()
	ledger := newStubLedger()
	ledger.taskTotals["task_456"] = 1.25
	server := NewServer(Config{
		SocketPath:    filepath.Join(root, "run", "vikramd.sock"),
		WorkspaceRoot: root,
	}, nil)
	server.SetLedger(ledger)

	req := httptest.NewRequest(http.MethodGet, "/v1/cost/task/task_456", nil)
	req.SetPathValue("task_id", "task_456")
	rec := httptest.NewRecorder()
	server.handler().ServeHTTP(rec, req)

	require.Equal(t, http.StatusOK, rec.Code)

	var resp map[string]interface{}
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &resp))
	require.Equal(t, "task_456", resp["task_id"])
	require.InDelta(t, 1.25, resp["cumulative_usd"], 0.001)
}

func TestCostTaskCumulativeEndpointRejectsInvalidTaskID(t *testing.T) {
	root := t.TempDir()
	ledger := newStubLedger()
	server := NewServer(Config{
		SocketPath:    filepath.Join(root, "run", "vikramd.sock"),
		WorkspaceRoot: root,
	}, nil)
	server.SetLedger(ledger)

	req := httptest.NewRequest(http.MethodGet, "/v1/cost/task/bad!id@here", nil)
	req.SetPathValue("task_id", "bad!id@here")
	rec := httptest.NewRecorder()
	server.handler().ServeHTTP(rec, req)

	require.Equal(t, http.StatusBadRequest, rec.Code)
	require.Contains(t, rec.Body.String(), "unsupported characters")
}

func TestCostForecastEndpointReturnsForecast(t *testing.T) {
	root := t.TempDir()
	ledger := newStubLedger()
	ledger.forecast = costledger.CostForecast{
		MinCostUSD:      0.05,
		ExpectedCostUSD: 0.15,
		MaxCostUSD:      0.45,
		ConfidenceLevel: 0.8,
		BasisTaskCount:  16,
	}
	server := NewServer(Config{
		SocketPath:    filepath.Join(root, "run", "vikramd.sock"),
		WorkspaceRoot: root,
	}, nil)
	server.SetLedger(ledger)

	body := costForecastRequest{
		Complexity:  "moderate",
		TargetFiles: 3,
	}
	reqBody, err := json.Marshal(body)
	require.NoError(t, err)

	req := httptest.NewRequest(http.MethodPost, "/v1/cost/forecast", bytes.NewReader(reqBody))
	rec := httptest.NewRecorder()
	server.handler().ServeHTTP(rec, req)

	require.Equal(t, http.StatusOK, rec.Code)

	var resp costledger.CostForecast
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &resp))
	require.InDelta(t, 0.05, resp.MinCostUSD, 0.001)
	require.InDelta(t, 0.15, resp.ExpectedCostUSD, 0.001)
	require.InDelta(t, 0.45, resp.MaxCostUSD, 0.001)
	require.InDelta(t, 0.8, resp.ConfidenceLevel, 0.001)
	require.Equal(t, 16, resp.BasisTaskCount)
}

func TestCostForecastEndpointRejectsMissingComplexity(t *testing.T) {
	root := t.TempDir()
	ledger := newStubLedger()
	server := NewServer(Config{
		SocketPath:    filepath.Join(root, "run", "vikramd.sock"),
		WorkspaceRoot: root,
	}, nil)
	server.SetLedger(ledger)

	body := costForecastRequest{TargetFiles: 3}
	reqBody, err := json.Marshal(body)
	require.NoError(t, err)

	req := httptest.NewRequest(http.MethodPost, "/v1/cost/forecast", bytes.NewReader(reqBody))
	rec := httptest.NewRecorder()
	server.handler().ServeHTTP(rec, req)

	require.Equal(t, http.StatusBadRequest, rec.Code)
	require.Contains(t, rec.Body.String(), "complexity is required")
}

func TestCostDailyEndpointReturnsDailyTotal(t *testing.T) {
	root := t.TempDir()
	ledger := newStubLedger()
	ledger.dailyTotal = 3.75
	server := NewServer(Config{
		SocketPath:    filepath.Join(root, "run", "vikramd.sock"),
		WorkspaceRoot: root,
	}, nil)
	server.SetLedger(ledger)

	req := httptest.NewRequest(http.MethodGet, "/v1/cost/daily", nil)
	rec := httptest.NewRecorder()
	server.handler().ServeHTTP(rec, req)

	require.Equal(t, http.StatusOK, rec.Code)

	var resp map[string]interface{}
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &resp))
	require.InDelta(t, 3.75, resp["daily_total_usd"], 0.001)
}

func TestCostDailyEndpointReturns503WithoutLedger(t *testing.T) {
	root := t.TempDir()
	server := NewServer(Config{
		SocketPath:    filepath.Join(root, "run", "vikramd.sock"),
		WorkspaceRoot: root,
	}, nil)

	req := httptest.NewRequest(http.MethodGet, "/v1/cost/daily", nil)
	rec := httptest.NewRecorder()
	server.handler().ServeHTTP(rec, req)

	require.Equal(t, http.StatusServiceUnavailable, rec.Code)
}

func TestAgentThinkRecordsCostWhenLedgerConfigured(t *testing.T) {
	root := t.TempDir()
	ledger := newStubLedger()
	server := NewServer(Config{
		SocketPath:    filepath.Join(root, "run", "vikramd.sock"),
		WorkspaceRoot: root,
		AgentThink: func(_ context.Context, req orchestrator.AgentThinkRequest) (orchestrator.AgentThinkResponse, error) {
			return orchestrator.AgentThinkResponse{
				TaskID:  req.TaskID,
				Role:    req.Role,
				Content: "Here is my response to your question.",
			}, nil
		},
	}, nil)
	server.SetLedger(ledger)

	body := map[string]string{
		"task_id": "task_789",
		"role":    "engineer",
		"prompt":  "Write tests for the module.",
	}
	reqBody, err := json.Marshal(body)
	require.NoError(t, err)

	req := httptest.NewRequest(http.MethodPost, "/v1/agent/think", bytes.NewReader(reqBody))
	rec := httptest.NewRecorder()
	server.handler().ServeHTTP(rec, req)

	require.Equal(t, http.StatusOK, rec.Code)
	require.Len(t, ledger.records, 1)
	require.Equal(t, "task_789", ledger.records[0].TaskID)
	require.Equal(t, "engineer", ledger.records[0].Role)
	require.True(t, ledger.records[0].Estimated)
	require.Greater(t, ledger.records[0].DurationMS, int64(-1))
}
