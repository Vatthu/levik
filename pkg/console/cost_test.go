package console

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/Vatthu/vikram/pkg/config"
)

// testCfg provides a minimal config for console tests.
var testCfg = config.Config{
	Agents: config.AgentsConfig{
		List: []config.AgentConfig{
			{ID: "agent-1", Role: "implementer", Provider: "openai", Model: "gpt-4"},
			{ID: "agent-2", Role: "reviewer", Provider: "anthropic", Model: "claude-3"},
		},
	},
}

func newTestServerWithOrchestrator(handler http.Handler) (*Server, *httptest.Server) {
	orchServer := httptest.NewServer(handler)
	server := &Server{
		hub:            newWSHub(),
		progressHub:    NewProgressHub(),
		cfg:            &testCfg,
		orchBaseURL:    orchServer.URL,
		orchHTTPClient: orchServer.Client(),
	}
	return server, orchServer
}

func TestCostOverviewReturnsData(t *testing.T) {
	orchHandler := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/telemetry/cost" {
			http.Error(w, "not found", http.StatusNotFound)
			return
		}
		resp := CostDashboardResponse{
			Overview: CostOverview{
				TodayUSD:          12.50,
				WeekUSD:           67.30,
				MonthUSD:          245.00,
				ProjectedMonthUSD: 310.00,
				DailyCeilingUSD:   50.00,
				DailyUsedUSD:      12.50,
			},
			Utilizations: []BudgetUtilization{
				{
					Scope:    "system",
					Label:    "Daily ceiling",
					UsedUSD:  12.50,
					LimitUSD: 50.00,
				},
				{
					Scope:    "role",
					Label:    "implementer",
					UsedUSD:  42.00,
					LimitUSD: 50.00,
				},
			},
		}
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(resp)
	})

	server, orchTS := newTestServerWithOrchestrator(orchHandler)
	defer orchTS.Close()
	defer server.progressHub.Stop()

	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodGet, "/api/cost/overview", nil)
	server.handleAPICostOverview(recorder, request)

	if recorder.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d: %s", recorder.Code, recorder.Body.String())
	}

	var result CostDashboardResponse
	if err := json.Unmarshal(recorder.Body.Bytes(), &result); err != nil {
		t.Fatalf("failed to decode response: %v", err)
	}

	if result.Overview.TodayUSD != 12.50 {
		t.Fatalf("expected today 12.50, got %f", result.Overview.TodayUSD)
	}
	if result.Overview.ProjectedMonthUSD != 310.00 {
		t.Fatalf("expected projected 310, got %f", result.Overview.ProjectedMonthUSD)
	}
}

func TestCostOverview80PercentWarning(t *testing.T) {
	orchHandler := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		resp := CostDashboardResponse{
			Overview: CostOverview{TodayUSD: 45.00, DailyCeilingUSD: 50.00},
			Utilizations: []BudgetUtilization{
				{Scope: "system", Label: "Daily ceiling", UsedUSD: 45.00, LimitUSD: 50.00},
				{Scope: "task", Label: "task-001", UsedUSD: 5.00, LimitUSD: 10.00},
			},
		}
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(resp)
	})

	server, orchTS := newTestServerWithOrchestrator(orchHandler)
	defer orchTS.Close()
	defer server.progressHub.Stop()

	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodGet, "/api/cost/overview", nil)
	server.handleAPICostOverview(recorder, request)

	if recorder.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", recorder.Code)
	}

	var result CostDashboardResponse
	json.Unmarshal(recorder.Body.Bytes(), &result)

	// 45/50 = 90% → should have warning
	if !result.Utilizations[0].Warning {
		t.Fatal("expected warning for 90% utilization")
	}
	if result.Utilizations[0].Percentage != 90 {
		t.Fatalf("expected 90%%, got %f%%", result.Utilizations[0].Percentage)
	}

	// 5/10 = 50% → no warning
	if result.Utilizations[1].Warning {
		t.Fatal("expected no warning for 50% utilization")
	}
}

func TestCostOverviewOrchestratorUnreachable(t *testing.T) {
	// Use a closed server to simulate unreachable orchestrator
	orchServer := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {}))
	orchServer.Close()

	server := &Server{
		hub:            newWSHub(),
		progressHub:    NewProgressHub(),
		cfg:            &testCfg,
		orchBaseURL:    orchServer.URL,
		orchHTTPClient: orchServer.Client(),
	}
	defer server.progressHub.Stop()

	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodGet, "/api/cost/overview", nil)
	server.handleAPICostOverview(recorder, request)

	// Should return OK with empty data
	if recorder.Code != http.StatusOK {
		t.Fatalf("expected 200 with empty data on unreachable, got %d", recorder.Code)
	}

	var result CostDashboardResponse
	json.Unmarshal(recorder.Body.Bytes(), &result)
	if result.Overview.TodayUSD != 0 {
		t.Fatal("expected zeroed overview when orchestrator unreachable")
	}
}

func TestCostOverviewRejectsNonGET(t *testing.T) {
	server := &Server{hub: newWSHub(), progressHub: NewProgressHub(), cfg: &testCfg}
	defer server.progressHub.Stop()

	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodPost, "/api/cost/overview", nil)
	server.handleAPICostOverview(recorder, request)

	if recorder.Code != http.StatusMethodNotAllowed {
		t.Fatalf("expected 405, got %d", recorder.Code)
	}
}

func TestCostBreakdownValidDimensions(t *testing.T) {
	orchHandler := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		dim := r.URL.Query().Get("dimension")
		resp := CostBreakdownResponse{
			Dimension: dim,
			TimeRange: r.URL.Query().Get("range"),
			Entries: []CostBreakdownEntry{
				{Label: "implementer", CostUSD: 5.00, Calls: 20, Tokens: 50000},
				{Label: "reviewer", CostUSD: 2.50, Calls: 10, Tokens: 25000},
			},
		}
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(resp)
	})

	server, orchTS := newTestServerWithOrchestrator(orchHandler)
	defer orchTS.Close()
	defer server.progressHub.Stop()

	dimensions := []string{"role", "model", "task", "phase", "provider"}
	for _, dim := range dimensions {
		recorder := httptest.NewRecorder()
		request := httptest.NewRequest(http.MethodGet, "/api/cost/breakdown?dimension="+dim+"&range=today", nil)
		server.handleAPICostBreakdown(recorder, request)

		if recorder.Code != http.StatusOK {
			t.Fatalf("dimension %s: expected 200, got %d", dim, recorder.Code)
		}

		var result CostBreakdownResponse
		json.Unmarshal(recorder.Body.Bytes(), &result)
		if result.Dimension != dim {
			t.Fatalf("expected dimension %s, got %s", dim, result.Dimension)
		}
		if len(result.Entries) != 2 {
			t.Fatalf("expected 2 entries, got %d", len(result.Entries))
		}
	}
}

func TestCostBreakdownInvalidDimension(t *testing.T) {
	server := &Server{hub: newWSHub(), progressHub: NewProgressHub(), cfg: &testCfg}
	defer server.progressHub.Stop()

	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodGet, "/api/cost/breakdown?dimension=invalid", nil)
	server.handleAPICostBreakdown(recorder, request)

	if recorder.Code != http.StatusBadRequest {
		t.Fatalf("expected 400, got %d", recorder.Code)
	}
}

func TestCostBreakdownInvalidRange(t *testing.T) {
	server := &Server{hub: newWSHub(), progressHub: NewProgressHub(), cfg: &testCfg}
	defer server.progressHub.Stop()

	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodGet, "/api/cost/breakdown?dimension=role&range=invalid", nil)
	server.handleAPICostBreakdown(recorder, request)

	if recorder.Code != http.StatusBadRequest {
		t.Fatalf("expected 400, got %d", recorder.Code)
	}
}

func TestCostBreakdownDefaultsToRoleAndToday(t *testing.T) {
	orchHandler := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		dim := r.URL.Query().Get("dimension")
		rng := r.URL.Query().Get("range")
		resp := CostBreakdownResponse{Dimension: dim, TimeRange: rng, Entries: []CostBreakdownEntry{}}
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(resp)
	})

	server, orchTS := newTestServerWithOrchestrator(orchHandler)
	defer orchTS.Close()
	defer server.progressHub.Stop()

	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodGet, "/api/cost/breakdown", nil)
	server.handleAPICostBreakdown(recorder, request)

	if recorder.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", recorder.Code)
	}

	var result CostBreakdownResponse
	json.Unmarshal(recorder.Body.Bytes(), &result)
	if result.Dimension != "role" {
		t.Fatalf("expected default dimension 'role', got %s", result.Dimension)
	}
	if result.TimeRange != "today" {
		t.Fatalf("expected default range 'today', got %s", result.TimeRange)
	}
}
