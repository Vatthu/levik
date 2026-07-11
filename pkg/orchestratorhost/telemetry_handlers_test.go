package orchestratorhost

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"testing"
	"time"

	"github.com/Vatthu/vikram/pkg/orchestrator"
	"github.com/Vatthu/vikram/pkg/telemetry"
	"github.com/Vatthu/vikram/pkg/tools"
	"github.com/stretchr/testify/require"
)

// stubTelemetryStore implements telemetry.Store for testing.
type stubTelemetryStore struct {
	events []telemetry.TelemetryEvent
}

func (s *stubTelemetryStore) Emit(_ context.Context, event telemetry.TelemetryEvent) error {
	s.events = append(s.events, event)
	return nil
}

func (s *stubTelemetryStore) Query(_ context.Context, q telemetry.SummaryQuery) (telemetry.SummaryResult, error) {
	return telemetry.SummaryResult{
		TotalCost:    1.25,
		TotalTokens:  5000,
		CallCount:    10,
		AvgLatencyMS: 450.5,
		ErrorRate:    0.1,
	}, nil
}

func (s *stubTelemetryStore) Events(_ context.Context, filters map[string]string, page, pageSize int) ([]telemetry.TelemetryEvent, int, error) {
	var filtered []telemetry.TelemetryEvent
	for _, e := range s.events {
		match := true
		if taskID, ok := filters["task_id"]; ok && e.TaskID != taskID {
			match = false
		}
		if eventType, ok := filters["event_type"]; ok && string(e.EventType) != eventType {
			match = false
		}
		if match {
			filtered = append(filtered, e)
		}
	}
	total := len(filtered)
	start := (page - 1) * pageSize
	if start >= total {
		return nil, total, nil
	}
	end := start + pageSize
	if end > total {
		end = total
	}
	return filtered[start:end], total, nil
}

func (s *stubTelemetryStore) Subscribe(_ context.Context, filter telemetry.EventType) <-chan telemetry.TelemetryEvent {
	ch := make(chan telemetry.TelemetryEvent, 10)
	return ch
}

func newTestServerWithTelemetry(t *testing.T) (*Server, *stubTelemetryStore) {
	t.Helper()
	root := t.TempDir()
	store := &stubTelemetryStore{}
	server := NewServer(Config{
		SocketPath:     filepath.Join(root, "run", "vikramd.sock"),
		WorkspaceRoot:  root,
		TelemetryStore: store,
	}, nil)
	return server, store
}

func TestTelemetryEmitAcceptsEvent(t *testing.T) {
	server, store := newTestServerWithTelemetry(t)

	event := telemetry.TelemetryEvent{
		EventID:   "evt-001",
		EventType: telemetry.EventAgentCallStart,
		TaskID:    "task_abc",
		Timestamp: time.Now().UTC(),
		Attributes: map[string]interface{}{
			"role":  "planner",
			"model": "claude-4",
		},
	}
	body, err := json.Marshal(event)
	require.NoError(t, err)

	req := httptest.NewRequest(http.MethodPost, "/v1/telemetry/emit", bytes.NewReader(body))
	rec := httptest.NewRecorder()
	server.handler().ServeHTTP(rec, req)

	require.Equal(t, http.StatusOK, rec.Code)
	require.Len(t, store.events, 1)
	require.Equal(t, "evt-001", store.events[0].EventID)
	require.Equal(t, telemetry.EventAgentCallStart, store.events[0].EventType)
	require.Equal(t, "task_abc", store.events[0].TaskID)
}

func TestTelemetryEmitRejectsGetMethod(t *testing.T) {
	server, _ := newTestServerWithTelemetry(t)

	req := httptest.NewRequest(http.MethodGet, "/v1/telemetry/emit", nil)
	rec := httptest.NewRecorder()
	server.handler().ServeHTTP(rec, req)

	require.Equal(t, http.StatusMethodNotAllowed, rec.Code)
}

func TestTelemetryEmitReturnsServiceUnavailableWhenNoStore(t *testing.T) {
	root := t.TempDir()
	server := NewServer(Config{
		SocketPath:    filepath.Join(root, "run", "vikramd.sock"),
		WorkspaceRoot: root,
	}, nil)

	event := telemetry.TelemetryEvent{
		EventType: telemetry.EventHostAction,
		TaskID:    "task_x",
	}
	body, _ := json.Marshal(event)

	req := httptest.NewRequest(http.MethodPost, "/v1/telemetry/emit", bytes.NewReader(body))
	rec := httptest.NewRecorder()
	server.handler().ServeHTTP(rec, req)

	require.Equal(t, http.StatusServiceUnavailable, rec.Code)
}

func TestTelemetrySummaryReturnsAggregatedMetrics(t *testing.T) {
	server, _ := newTestServerWithTelemetry(t)

	req := httptest.NewRequest(http.MethodGet, "/v1/telemetry/summary?start_time=2024-01-01T00:00:00Z&end_time=2024-12-31T23:59:59Z", nil)
	rec := httptest.NewRecorder()
	server.handler().ServeHTTP(rec, req)

	require.Equal(t, http.StatusOK, rec.Code)

	var result telemetry.SummaryResult
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &result))
	require.Equal(t, 1.25, result.TotalCost)
	require.Equal(t, int64(5000), result.TotalTokens)
	require.Equal(t, int64(10), result.CallCount)
	require.InDelta(t, 450.5, result.AvgLatencyMS, 0.01)
	require.InDelta(t, 0.1, result.ErrorRate, 0.001)
}

func TestTelemetrySummaryRejectsPostMethod(t *testing.T) {
	server, _ := newTestServerWithTelemetry(t)

	req := httptest.NewRequest(http.MethodPost, "/v1/telemetry/summary", nil)
	rec := httptest.NewRecorder()
	server.handler().ServeHTTP(rec, req)

	require.Equal(t, http.StatusMethodNotAllowed, rec.Code)
}

func TestTelemetryEventsReturnsPaginatedEvents(t *testing.T) {
	server, store := newTestServerWithTelemetry(t)

	// Pre-populate some events.
	for i := 0; i < 5; i++ {
		store.events = append(store.events, telemetry.TelemetryEvent{
			EventID:   "evt-" + string(rune('A'+i)),
			EventType: telemetry.EventAgentCallEnd,
			TaskID:    "task_main",
			Timestamp: time.Now().UTC(),
		})
	}

	req := httptest.NewRequest(http.MethodGet, "/v1/telemetry/events?task_id=task_main&page=1&page_size=3", nil)
	rec := httptest.NewRecorder()
	server.handler().ServeHTTP(rec, req)

	require.Equal(t, http.StatusOK, rec.Code)

	var resp telemetryEventsResponse
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &resp))
	require.Equal(t, 5, resp.Total)
	require.Equal(t, 1, resp.Page)
	require.Equal(t, 3, resp.PageSize)
	require.Len(t, resp.Events, 3)
}

func TestTelemetryEventsFiltersCorrectly(t *testing.T) {
	server, store := newTestServerWithTelemetry(t)

	store.events = append(store.events,
		telemetry.TelemetryEvent{EventID: "e1", EventType: telemetry.EventAgentCallStart, TaskID: "task_a"},
		telemetry.TelemetryEvent{EventID: "e2", EventType: telemetry.EventHostAction, TaskID: "task_a"},
		telemetry.TelemetryEvent{EventID: "e3", EventType: telemetry.EventAgentCallStart, TaskID: "task_b"},
	)

	req := httptest.NewRequest(http.MethodGet, "/v1/telemetry/events?event_type=agent_call_start&task_id=task_a", nil)
	rec := httptest.NewRecorder()
	server.handler().ServeHTTP(rec, req)

	require.Equal(t, http.StatusOK, rec.Code)

	var resp telemetryEventsResponse
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &resp))
	require.Equal(t, 1, resp.Total)
	require.Len(t, resp.Events, 1)
	require.Equal(t, "e1", resp.Events[0].EventID)
}

func TestTelemetryCostReturnsGroupedBreakdown(t *testing.T) {
	server, _ := newTestServerWithTelemetry(t)

	req := httptest.NewRequest(http.MethodGet, "/v1/telemetry/cost?start_time=2024-01-01T00:00:00Z&end_time=2024-12-31T23:59:59Z&group_by=task_id,role", nil)
	rec := httptest.NewRecorder()
	server.handler().ServeHTTP(rec, req)

	require.Equal(t, http.StatusOK, rec.Code)

	var result telemetry.SummaryResult
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &result))
	require.Equal(t, 1.25, result.TotalCost)
}

func TestTelemetryCostDefaultsGroupByTaskID(t *testing.T) {
	server, _ := newTestServerWithTelemetry(t)

	// No group_by param — should default to task_id grouping.
	req := httptest.NewRequest(http.MethodGet, "/v1/telemetry/cost", nil)
	rec := httptest.NewRecorder()
	server.handler().ServeHTTP(rec, req)

	require.Equal(t, http.StatusOK, rec.Code)
}

func TestAgentThinkEmitsTelemetryStartEnd(t *testing.T) {
	server, store := newTestServerWithTelemetry(t)

	// Configure a mock AgentThink function.
	server.cfg.AgentThink = func(_ context.Context, req orchestrator.AgentThinkRequest) (orchestrator.AgentThinkResponse, error) {
		return orchestrator.AgentThinkResponse{
			TaskID:  req.TaskID,
			Role:    req.Role,
			Content: "mock response",
		}, nil
	}

	body, _ := json.Marshal(orchestrator.AgentThinkRequest{
		TaskID:       "task_tel",
		Role:         "implementer",
		Prompt:       "write code",
		Model:        "gpt-4o",
		ProviderName: "openai",
	})

	req := httptest.NewRequest(http.MethodPost, "/v1/agent/think", bytes.NewReader(body))
	rec := httptest.NewRecorder()
	server.handler().ServeHTTP(rec, req)

	require.Equal(t, http.StatusOK, rec.Code)

	// Should have 2 telemetry events: agent_call_start and agent_call_end.
	require.Len(t, store.events, 2)
	require.Equal(t, telemetry.EventAgentCallStart, store.events[0].EventType)
	require.Equal(t, "task_tel", store.events[0].TaskID)
	require.Equal(t, "implementer", store.events[0].Attributes["role"])
	require.Equal(t, "gpt-4o", store.events[0].Attributes["model"])
	require.Equal(t, "openai", store.events[0].Attributes["provider"])

	require.Equal(t, telemetry.EventAgentCallEnd, store.events[1].EventType)
	require.Equal(t, "task_tel", store.events[1].TaskID)
	require.Equal(t, true, store.events[1].Attributes["success"])
}

func TestExecEmitsHostActionTelemetry(t *testing.T) {
	server, store := newTestServerWithTelemetry(t)

	// Use a stub exec tool.
	exitCode := 0
	server.execTool = &stubExecTool{
		result: &tools.ToolResult{
			ForLLM:   "hello output",
			IsError:  false,
			ExitCode: &exitCode,
		},
	}

	body, _ := json.Marshal(orchestrator.HostActionRequest{
		TaskID:     "task_exec",
		ActionName: "exec",
		WorkingDir: "/tmp",
		Arguments:  map[string]interface{}{"command": "echo hello"},
	})

	req := httptest.NewRequest(http.MethodPost, "/v1/exec", bytes.NewReader(body))
	rec := httptest.NewRecorder()
	server.handler().ServeHTTP(rec, req)

	require.Equal(t, http.StatusOK, rec.Code)

	// Should have 1 host_action telemetry event.
	require.Len(t, store.events, 1)
	require.Equal(t, telemetry.EventHostAction, store.events[0].EventType)
	require.Equal(t, "task_exec", store.events[0].TaskID)
	require.Equal(t, "exec", store.events[0].Attributes["action_name"])
	require.Equal(t, true, store.events[0].Attributes["success"])
	require.Equal(t, 0, store.events[0].Attributes["exit_code"])
}
