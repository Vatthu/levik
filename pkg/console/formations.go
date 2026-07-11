package console

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"strings"
	"time"
)

// --- Formation Editor Models (Requirement 50) ---

// FormationBudgetStrategy represents budget allocation across work phases.
// Requirement 50.1: budget strategy configuration in Formation editor.
type FormationBudgetStrategy struct {
	Planning       float64 `json:"planning"`
	Implementation float64 `json:"implementation"`
	Verification   float64 `json:"verification"`
	Review         float64 `json:"review"`
}

// FormationRoleSlot represents a role-to-model mapping within a Formation.
// Requirement 50.1: role slots and assigned models.
type FormationRoleSlot struct {
	Role     string `json:"role"`
	Provider string `json:"provider"`
	Model    string `json:"model"`
}

// FormationEffectiveness contains metrics for a Formation.
// Requirement 50.2: success rate, cost-per-task, average duration, comparison.
type FormationEffectiveness struct {
	SuccessRate     float64 `json:"success_rate"`
	CostPerTaskUSD  float64 `json:"cost_per_task_usd"`
	AvgDurationSecs float64 `json:"avg_duration_secs"`
	TaskCount       int     `json:"task_count"`
}

// Formation represents a team topology configuration.
// Requirement 50.1: Formation editor with role slots, models, budget, verification.
type Formation struct {
	Name                 string                  `json:"name"`
	TaskType             string                  `json:"task_type"`
	RoleSlots            []FormationRoleSlot     `json:"role_slots"`
	BudgetStrategy       FormationBudgetStrategy `json:"budget_strategy"`
	VerificationProtocol string                  `json:"verification_protocol"`
	Effectiveness        *FormationEffectiveness `json:"effectiveness,omitempty"`
}

// FormationCloneRequest is the payload for cloning a Formation.
// Requirement 50.3: clone-and-modify for new Formations.
type FormationCloneRequest struct {
	NewName   string           `json:"new_name"`
	Overrides *FormationUpdate `json:"overrides,omitempty"`
}

// FormationUpdate holds fields that can be updated on a Formation.
type FormationUpdate struct {
	RoleSlots            []FormationRoleSlot      `json:"role_slots,omitempty"`
	BudgetStrategy       *FormationBudgetStrategy `json:"budget_strategy,omitempty"`
	VerificationProtocol string                   `json:"verification_protocol,omitempty"`
}

// ABTestConfig configures an A/B test between two Formations.
// Requirement 50.4: percentage traffic splits and automatic winner promotion.
type ABTestConfig struct {
	TaskType     string  `json:"task_type"`
	FormationA   string  `json:"formation_a"`
	FormationB   string  `json:"formation_b"`
	SplitPercent ABSplit `json:"split_percent"`
	TrialTasks   int     `json:"trial_tasks"`
	AutoPromote  bool    `json:"auto_promote"`
}

// ABSplit defines the traffic percentage split for an A/B test.
type ABSplit struct {
	A int `json:"a"`
	B int `json:"b"`
}

// ABTestStatus represents the current status of an A/B test.
type ABTestStatus struct {
	TaskType       string                  `json:"task_type"`
	FormationA     string                  `json:"formation_a"`
	FormationB     string                  `json:"formation_b"`
	SplitPercent   ABSplit                 `json:"split_percent"`
	TrialTasks     int                     `json:"trial_tasks"`
	CompletedTasks int                     `json:"completed_tasks"`
	AutoPromote    bool                    `json:"auto_promote"`
	Winner         string                  `json:"winner,omitempty"`
	MetricsA       *FormationEffectiveness `json:"metrics_a,omitempty"`
	MetricsB       *FormationEffectiveness `json:"metrics_b,omitempty"`
}

// ABTestPromoteRequest identifies which A/B test winner to promote.
type ABTestPromoteRequest struct {
	TaskType string `json:"task_type"`
	Winner   string `json:"winner"`
}

// FormationListResponse wraps a list of formations with effectiveness.
type FormationListResponse struct {
	Formations []Formation `json:"formations"`
}

// --- Formation API Handlers ---

// handleAPIFormations serves GET /api/formations.
// Returns all formations with effectiveness metrics.
// Requirement 50.1, 50.2
func (s *Server) handleAPIFormations(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		s.writeError(w, http.StatusMethodNotAllowed, "GET only")
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), 10*time.Second)
	defer cancel()

	var result FormationListResponse
	if err := s.orchestratorJSON(ctx, http.MethodGet, "/v1/formations", nil, &result); err != nil {
		if !isOrchestratorHTTPError(err) {
			s.writeOK(w, FormationListResponse{Formations: []Formation{}})
			return
		}
		s.writeError(w, http.StatusBadGateway, err.Error())
		return
	}

	s.writeOK(w, result)
}

// handleAPIFormationByID serves GET/PUT /api/formations/{id}.
// GET: returns formation details with effectiveness.
// PUT: updates formation configuration (role slots, models, budget, verification).
// Requirements 50.1, 50.2
func (s *Server) handleAPIFormationByID(w http.ResponseWriter, r *http.Request) {
	id := extractPathParam(r, "/api/formations/")
	if id == "" || id == "ab-test" {
		// Avoid collision with /api/formations/ab-test routes
		http.NotFound(w, r)
		return
	}

	switch r.Method {
	case http.MethodGet:
		s.handleGetFormation(w, r, id)
	case http.MethodPut:
		s.handleUpdateFormation(w, r, id)
	default:
		s.writeError(w, http.StatusMethodNotAllowed, "GET or PUT only")
	}
}

func (s *Server) handleGetFormation(w http.ResponseWriter, r *http.Request, id string) {
	ctx, cancel := context.WithTimeout(r.Context(), 10*time.Second)
	defer cancel()

	var result Formation
	if err := s.orchestratorJSON(ctx, http.MethodGet, "/v1/formations/"+id, nil, &result); err != nil {
		if isOrchestratorHTTPError(err) {
			var orchErr orchestratorHTTPError
			if isAs(err, &orchErr) && orchErr.statusCode == http.StatusNotFound {
				s.writeError(w, http.StatusNotFound, "formation not found")
				return
			}
			s.writeError(w, http.StatusBadGateway, err.Error())
			return
		}
		s.writeError(w, http.StatusBadGateway, "orchestrator unreachable")
		return
	}

	s.writeOK(w, result)
}

func (s *Server) handleUpdateFormation(w http.ResponseWriter, r *http.Request, id string) {
	var update FormationUpdate
	if err := json.NewDecoder(r.Body).Decode(&update); err != nil {
		s.writeError(w, http.StatusBadRequest, "invalid JSON body")
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), 10*time.Second)
	defer cancel()

	var result Formation
	if err := s.orchestratorJSON(ctx, http.MethodPut, "/v1/formations/"+id, update, &result); err != nil {
		if isOrchestratorHTTPError(err) {
			var orchErr orchestratorHTTPError
			if isAs(err, &orchErr) && orchErr.statusCode == http.StatusNotFound {
				s.writeError(w, http.StatusNotFound, "formation not found")
				return
			}
			s.writeError(w, http.StatusBadGateway, err.Error())
			return
		}
		s.writeError(w, http.StatusBadGateway, "orchestrator unreachable")
		return
	}

	s.writeOK(w, result)
}

// handleAPIFormationClone serves POST /api/formations/{id}/clone.
// Clones an existing formation with optional overrides.
// Requirement 50.3
func (s *Server) handleAPIFormationClone(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		s.writeError(w, http.StatusMethodNotAllowed, "POST only")
		return
	}

	// Extract formation ID from path: /api/formations/{id}/clone
	path := r.URL.Path
	trimmed := strings.TrimPrefix(path, "/api/formations/")
	parts := strings.SplitN(trimmed, "/", 2)
	if len(parts) < 2 || parts[0] == "" {
		s.writeError(w, http.StatusBadRequest, "missing formation ID")
		return
	}
	id := parts[0]

	var cloneReq FormationCloneRequest
	if err := json.NewDecoder(r.Body).Decode(&cloneReq); err != nil {
		s.writeError(w, http.StatusBadRequest, "invalid JSON body")
		return
	}
	if cloneReq.NewName == "" {
		s.writeError(w, http.StatusBadRequest, "new_name is required")
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), 10*time.Second)
	defer cancel()

	var result Formation
	if err := s.orchestratorJSON(ctx, http.MethodPost, "/v1/formations/"+id+"/clone", cloneReq, &result); err != nil {
		if isOrchestratorHTTPError(err) {
			var orchErr orchestratorHTTPError
			if isAs(err, &orchErr) && orchErr.statusCode == http.StatusNotFound {
				s.writeError(w, http.StatusNotFound, "source formation not found")
				return
			}
			if isAs(err, &orchErr) && orchErr.statusCode == http.StatusConflict {
				s.writeError(w, http.StatusConflict, "formation with that name already exists")
				return
			}
			s.writeError(w, http.StatusBadGateway, err.Error())
			return
		}
		s.writeError(w, http.StatusBadGateway, "orchestrator unreachable")
		return
	}

	s.writeJSON(w, http.StatusCreated, result)
}

// handleAPIFormationABTest serves POST /api/formations/ab-test.
// Configures an A/B test with percentage traffic splits.
// Requirement 50.4
func (s *Server) handleAPIFormationABTest(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		s.writeError(w, http.StatusMethodNotAllowed, "POST only")
		return
	}

	var abConfig ABTestConfig
	if err := json.NewDecoder(r.Body).Decode(&abConfig); err != nil {
		s.writeError(w, http.StatusBadRequest, "invalid JSON body")
		return
	}

	// Validate split percentages sum to 100
	if abConfig.SplitPercent.A+abConfig.SplitPercent.B != 100 {
		s.writeError(w, http.StatusBadRequest, "split percentages must sum to 100")
		return
	}
	if abConfig.FormationA == "" || abConfig.FormationB == "" {
		s.writeError(w, http.StatusBadRequest, "formation_a and formation_b are required")
		return
	}
	if abConfig.TaskType == "" {
		s.writeError(w, http.StatusBadRequest, "task_type is required")
		return
	}
	if abConfig.TrialTasks <= 0 {
		s.writeError(w, http.StatusBadRequest, "trial_tasks must be positive")
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), 10*time.Second)
	defer cancel()

	var result ABTestStatus
	if err := s.orchestratorJSON(ctx, http.MethodPost, "/v1/formations/ab-test", abConfig, &result); err != nil {
		if !isOrchestratorHTTPError(err) {
			s.writeError(w, http.StatusBadGateway, "orchestrator unreachable")
			return
		}
		s.writeError(w, http.StatusBadGateway, err.Error())
		return
	}

	s.writeJSON(w, http.StatusCreated, result)
}

// handleAPIFormationABPromote serves POST /api/formations/ab-test/promote.
// Promotes the winning formation from an A/B test.
// Requirement 50.4
func (s *Server) handleAPIFormationABPromote(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		s.writeError(w, http.StatusMethodNotAllowed, "POST only")
		return
	}

	var promoteReq ABTestPromoteRequest
	if err := json.NewDecoder(r.Body).Decode(&promoteReq); err != nil {
		s.writeError(w, http.StatusBadRequest, "invalid JSON body")
		return
	}
	if promoteReq.TaskType == "" {
		s.writeError(w, http.StatusBadRequest, "task_type is required")
		return
	}
	if promoteReq.Winner == "" {
		s.writeError(w, http.StatusBadRequest, "winner is required")
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), 10*time.Second)
	defer cancel()

	var result Formation
	if err := s.orchestratorJSON(ctx, http.MethodPost, "/v1/formations/ab-test/promote", promoteReq, &result); err != nil {
		if !isOrchestratorHTTPError(err) {
			s.writeError(w, http.StatusBadGateway, "orchestrator unreachable")
			return
		}
		s.writeError(w, http.StatusBadGateway, err.Error())
		return
	}

	s.writeOK(w, result)
}

// --- Helpers ---

// extractPathParam extracts the remaining path after a prefix.
// For example, extractPathParam(r, "/api/formations/") on "/api/formations/my-formation"
// returns "my-formation".
func extractPathParam(r *http.Request, prefix string) string {
	path := r.URL.Path
	if !strings.HasPrefix(path, prefix) {
		return ""
	}
	remainder := strings.TrimPrefix(path, prefix)
	// Remove any trailing path segments (e.g., /clone)
	if idx := strings.Index(remainder, "/"); idx != -1 {
		remainder = remainder[:idx]
	}
	return remainder
}

// isAs is a helper that wraps errors.As for orchestratorHTTPError.
func isAs(err error, target *orchestratorHTTPError) bool {
	return errors.As(err, target)
}
