package console

import (
	"context"
	"net/http"
	"time"
)

// --- Team Health Models (Requirement 47) ---

// AgentHealthMetrics holds rolling metrics for a single agent.
// Requirement 47.2: calls, tokens, cost, error rate, p95 latency in last 24h.
type AgentHealthMetrics struct {
	Calls24h     int     `json:"calls_24h"`
	Tokens24h    int     `json:"tokens_24h"`
	Cost24hUSD   float64 `json:"cost_24h_usd"`
	ErrorRate    float64 `json:"error_rate"`
	P95LatencyMS int64   `json:"p95_latency_ms"`
}

// AgentHealthStatus represents the health state of a single agent.
// Requirement 47.1: role, provider, model, status, formation membership.
type AgentHealthStatus struct {
	AgentID       string             `json:"agent_id"`
	Role          string             `json:"role"`
	Provider      string             `json:"provider"`
	Model         string             `json:"model"`
	Status        string             `json:"status"` // idle, active, errored, budget-exhausted
	Formation     string             `json:"formation,omitempty"`
	Metrics       AgentHealthMetrics `json:"metrics"`
	HealthWarning bool               `json:"health_warning"`
	WarningReason string             `json:"warning_reason,omitempty"`
	FallbackChain []string           `json:"fallback_chain,omitempty"`
	IsPrimary     bool               `json:"is_primary"`
}

// TeamHealthResponse is the full response for the team health endpoint.
type TeamHealthResponse struct {
	Agents      []AgentHealthStatus `json:"agents"`
	TotalAgents int                 `json:"total_agents"`
	Healthy     int                 `json:"healthy"`
	Warning     int                 `json:"warning"`
	Errored     int                 `json:"errored"`
}

// --- Team Health API Handlers ---

// handleAPITeamHealth serves GET /api/team/health.
// Returns per-agent health metrics with warning flags.
// Requirement 47.3: warning when error rate > 20% over last 20 calls.
func (s *Server) handleAPITeamHealth(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		s.writeError(w, http.StatusMethodNotAllowed, "GET only")
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), 10*time.Second)
	defer cancel()

	var result TeamHealthResponse
	if err := s.orchestratorJSON(ctx, http.MethodGet, "/v1/telemetry/summary?view=team_health", nil, &result); err != nil {
		if !isOrchestratorHTTPError(err) {
			// Orchestrator unreachable: build health from local config
			result = s.buildLocalTeamHealth()
			s.writeOK(w, result)
			return
		}
		s.writeError(w, http.StatusBadGateway, err.Error())
		return
	}

	// Apply health warning logic: error rate > 20% triggers warning
	healthy, warning, errored := 0, 0, 0
	for i := range result.Agents {
		agent := &result.Agents[i]
		if agent.Metrics.ErrorRate > 20 {
			agent.HealthWarning = true
			if agent.WarningReason == "" {
				agent.WarningReason = "error rate exceeds 20%"
			}
		}
		switch {
		case agent.Status == "errored" || agent.HealthWarning:
			if agent.Status == "errored" {
				errored++
			} else {
				warning++
			}
		default:
			healthy++
		}
	}
	result.TotalAgents = len(result.Agents)
	result.Healthy = healthy
	result.Warning = warning
	result.Errored = errored

	s.writeOK(w, result)
}

// buildLocalTeamHealth constructs a basic team health response from local
// agent config when the orchestrator is not reachable.
func (s *Server) buildLocalTeamHealth() TeamHealthResponse {
	agents := make([]AgentHealthStatus, 0, len(s.cfg.Agents.List))
	for _, a := range s.cfg.Agents.List {
		agents = append(agents, AgentHealthStatus{
			AgentID:   a.ID,
			Role:      a.Role,
			Provider:  a.Provider,
			Model:     a.Model,
			Status:    "idle",
			IsPrimary: true,
			Metrics:   AgentHealthMetrics{},
		})
	}
	return TeamHealthResponse{
		Agents:      agents,
		TotalAgents: len(agents),
		Healthy:     len(agents),
	}
}
