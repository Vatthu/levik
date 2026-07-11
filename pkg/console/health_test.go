package console

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestTeamHealthReturnsAgentMetrics(t *testing.T) {
	orchHandler := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/telemetry/summary" {
			http.Error(w, "not found", http.StatusNotFound)
			return
		}
		resp := TeamHealthResponse{
			Agents: []AgentHealthStatus{
				{
					AgentID:  "agent-1",
					Role:     "implementer",
					Provider: "openai",
					Model:    "gpt-4",
					Status:   "active",
					Metrics: AgentHealthMetrics{
						Calls24h:     45,
						Tokens24h:    120000,
						Cost24hUSD:   8.50,
						ErrorRate:    5.0,
						P95LatencyMS: 3200,
					},
					IsPrimary: true,
				},
				{
					AgentID:  "agent-2",
					Role:     "reviewer",
					Provider: "anthropic",
					Model:    "claude-3",
					Status:   "idle",
					Metrics: AgentHealthMetrics{
						Calls24h:     20,
						Tokens24h:    60000,
						Cost24hUSD:   4.20,
						ErrorRate:    25.0,
						P95LatencyMS: 5000,
					},
					IsPrimary: true,
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
	request := httptest.NewRequest(http.MethodGet, "/api/team/health", nil)
	server.handleAPITeamHealth(recorder, request)

	if recorder.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d: %s", recorder.Code, recorder.Body.String())
	}

	var result TeamHealthResponse
	if err := json.Unmarshal(recorder.Body.Bytes(), &result); err != nil {
		t.Fatalf("failed to decode response: %v", err)
	}

	if len(result.Agents) != 2 {
		t.Fatalf("expected 2 agents, got %d", len(result.Agents))
	}
	if result.TotalAgents != 2 {
		t.Fatalf("expected total_agents=2, got %d", result.TotalAgents)
	}
}

func TestTeamHealthWarningAbove20Percent(t *testing.T) {
	orchHandler := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		resp := TeamHealthResponse{
			Agents: []AgentHealthStatus{
				{
					AgentID: "agent-ok",
					Role:    "implementer",
					Status:  "active",
					Metrics: AgentHealthMetrics{ErrorRate: 10.0},
				},
				{
					AgentID: "agent-warn",
					Role:    "reviewer",
					Status:  "active",
					Metrics: AgentHealthMetrics{ErrorRate: 25.0},
				},
				{
					AgentID: "agent-errored",
					Role:    "planner",
					Status:  "errored",
					Metrics: AgentHealthMetrics{ErrorRate: 50.0},
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
	request := httptest.NewRequest(http.MethodGet, "/api/team/health", nil)
	server.handleAPITeamHealth(recorder, request)

	if recorder.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", recorder.Code)
	}

	var result TeamHealthResponse
	json.Unmarshal(recorder.Body.Bytes(), &result)

	// Agent with 10% error rate should NOT have warning
	if result.Agents[0].HealthWarning {
		t.Fatal("agent-ok: should NOT have health warning at 10%")
	}

	// Agent with 25% error rate should have warning
	if !result.Agents[1].HealthWarning {
		t.Fatal("agent-warn: should have health warning at 25%")
	}
	if result.Agents[1].WarningReason == "" {
		t.Fatal("agent-warn: should have warning reason")
	}

	// Agent with errored status AND high error rate
	if !result.Agents[2].HealthWarning {
		t.Fatal("agent-errored: should have health warning at 50%")
	}

	// Summary counts
	if result.Healthy != 1 {
		t.Fatalf("expected 1 healthy, got %d", result.Healthy)
	}
	if result.Warning != 1 {
		t.Fatalf("expected 1 warning, got %d", result.Warning)
	}
	if result.Errored != 1 {
		t.Fatalf("expected 1 errored, got %d", result.Errored)
	}
}

func TestTeamHealthOrchestratorUnreachableFallsBackToLocalConfig(t *testing.T) {
	// Use closed server to simulate unreachable orchestrator
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
	request := httptest.NewRequest(http.MethodGet, "/api/team/health", nil)
	server.handleAPITeamHealth(recorder, request)

	if recorder.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", recorder.Code)
	}

	var result TeamHealthResponse
	json.Unmarshal(recorder.Body.Bytes(), &result)

	// Should fall back to local config agents
	if result.TotalAgents != 2 {
		t.Fatalf("expected 2 agents from local config, got %d", result.TotalAgents)
	}
	if result.Agents[0].AgentID != "agent-1" {
		t.Fatalf("expected agent-1, got %s", result.Agents[0].AgentID)
	}
	if result.Agents[0].Status != "idle" {
		t.Fatalf("expected idle status for local fallback, got %s", result.Agents[0].Status)
	}
}

func TestTeamHealthRejectsNonGET(t *testing.T) {
	server := &Server{hub: newWSHub(), progressHub: NewProgressHub(), cfg: &testCfg}
	defer server.progressHub.Stop()

	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodPost, "/api/team/health", nil)
	server.handleAPITeamHealth(recorder, request)

	if recorder.Code != http.StatusMethodNotAllowed {
		t.Fatalf("expected 405, got %d", recorder.Code)
	}
}

func TestTeamHealthFallbackChainDisplay(t *testing.T) {
	orchHandler := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		resp := TeamHealthResponse{
			Agents: []AgentHealthStatus{
				{
					AgentID:       "agent-primary",
					Role:          "implementer",
					Status:        "active",
					Metrics:       AgentHealthMetrics{ErrorRate: 5.0},
					FallbackChain: []string{"agent-fallback-1", "agent-fallback-2"},
					IsPrimary:     true,
				},
				{
					AgentID:   "agent-fallback-1",
					Role:      "implementer",
					Status:    "idle",
					Metrics:   AgentHealthMetrics{ErrorRate: 2.0},
					IsPrimary: false,
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
	request := httptest.NewRequest(http.MethodGet, "/api/team/health", nil)
	server.handleAPITeamHealth(recorder, request)

	if recorder.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", recorder.Code)
	}

	var result TeamHealthResponse
	json.Unmarshal(recorder.Body.Bytes(), &result)

	// Check primary agent has fallback chain
	primary := result.Agents[0]
	if !primary.IsPrimary {
		t.Fatal("expected agent-primary to be primary")
	}
	if len(primary.FallbackChain) != 2 {
		t.Fatalf("expected 2 fallbacks, got %d", len(primary.FallbackChain))
	}
	if primary.FallbackChain[0] != "agent-fallback-1" {
		t.Fatalf("expected fallback-1, got %s", primary.FallbackChain[0])
	}

	// Check secondary is not primary
	if result.Agents[1].IsPrimary {
		t.Fatal("expected agent-fallback-1 to NOT be primary")
	}
}
