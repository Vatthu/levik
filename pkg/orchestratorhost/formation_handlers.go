package orchestratorhost

import (
	"encoding/json"
	"io"
	"net/http"
	"strings"
)

// --- Types for Model Performance and Formation API ---

// ModelPerformanceRecord represents performance stats for a model+tier combination.
type ModelPerformanceRecord struct {
	Model          string  `json:"model"`
	Provider       string  `json:"provider"`
	Role           string  `json:"role"`
	ComplexityTier string  `json:"complexity_tier"`
	SuccessRate    float64 `json:"success_rate"`
	AvgLatencyMS   float64 `json:"avg_latency_ms"`
	CostPerSuccess float64 `json:"cost_per_success"`
	TotalCalls     int     `json:"total_calls"`
}

// BudgetStrategy represents percentage allocation of budget across work phases.
type BudgetStrategy struct {
	Planning       float64 `json:"planning"`
	Implementation float64 `json:"implementation"`
	Verification   float64 `json:"verification"`
	Review         float64 `json:"review"`
}

// FormationModelCapability defines a model's capabilities within a formation.
type FormationModelCapability struct {
	Model                    string `json:"model"`
	Provider                 string `json:"provider"`
	CostTier                 int    `json:"cost_tier"`
	CapabilityScore          int    `json:"capability_score"`
	SupportsStructuredOutput bool   `json:"supports_structured_output"`
}

// Formation represents a team topology configuration for a task type.
type Formation struct {
	Name                 string                              `json:"name"`
	TaskType             string                              `json:"task_type"`
	RoleModelMappings    map[string]FormationModelCapability `json:"role_model_mappings"`
	BudgetStrategy       BudgetStrategy                      `json:"budget_strategy"`
	VerificationProtocol string                              `json:"verification_protocol"`
}

// FormationEffectivenessEntry represents effectiveness for one formation/task_type pair.
type FormationEffectivenessEntry struct {
	FormationName string             `json:"formation_name"`
	Scores        map[string]float64 `json:"scores"` // task_type -> effectiveness score
}

// --- Interface for Formation data access ---

// formationStore is the interface Go uses to interact with formation data.
// Implementations may proxy to the Python orchestrator or serve from a local cache.
type formationStore interface {
	GetModelPerformance() ([]ModelPerformanceRecord, error)
	ListFormations() ([]Formation, error)
	CreateFormation(f Formation) (Formation, error)
	UpdateFormation(name string, f Formation) (Formation, error)
	DeleteFormation(name string) (bool, error)
	GetEffectiveness() ([]FormationEffectivenessEntry, error)
}

// SetFormationStore configures the formation store used by the host server.
// Must be called before Start.
func (s *Server) SetFormationStore(fs formationStore) {
	s.formationStore = fs
}

// --- Handlers ---

// handleModelPerformance handles GET /v1/models/performance — returns model performance stats.
// Validates: Requirements 13.4
func (s *Server) handleModelPerformance(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "method not allowed"})
		return
	}

	if s.formationStore == nil {
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{"error": "formation store not configured"})
		return
	}

	records, err := s.formationStore.GetModelPerformance()
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": err.Error()})
		return
	}

	writeJSON(w, http.StatusOK, map[string]interface{}{
		"records": records,
	})
}

// handleFormations handles GET/POST /v1/formations — list or create formations.
// Validates: Requirements 14.4
func (s *Server) handleFormations(w http.ResponseWriter, r *http.Request) {
	if s.formationStore == nil {
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{"error": "formation store not configured"})
		return
	}

	switch r.Method {
	case http.MethodGet:
		s.handleListFormations(w, r)
	case http.MethodPost:
		s.handleCreateFormation(w, r)
	default:
		writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "method not allowed"})
	}
}

// handleFormationByName handles PUT/DELETE /v1/formations/{name} — update or delete a formation.
// Validates: Requirements 14.4
func (s *Server) handleFormationByName(w http.ResponseWriter, r *http.Request) {
	if s.formationStore == nil {
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{"error": "formation store not configured"})
		return
	}

	switch r.Method {
	case http.MethodPut:
		s.handleUpdateFormation(w, r)
	case http.MethodDelete:
		s.handleDeleteFormation(w, r)
	default:
		writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "method not allowed"})
	}
}

// handleFormationEffectiveness handles GET /v1/formations/effectiveness — returns effectiveness comparison.
// Validates: Requirements 15.4
func (s *Server) handleFormationEffectiveness(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "method not allowed"})
		return
	}

	if s.formationStore == nil {
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{"error": "formation store not configured"})
		return
	}

	entries, err := s.formationStore.GetEffectiveness()
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": err.Error()})
		return
	}

	writeJSON(w, http.StatusOK, map[string]interface{}{
		"formations": entries,
	})
}

// --- Internal handler helpers ---

func (s *Server) handleListFormations(w http.ResponseWriter, r *http.Request) {
	formations, err := s.formationStore.ListFormations()
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": err.Error()})
		return
	}

	writeJSON(w, http.StatusOK, map[string]interface{}{
		"formations": formations,
	})
}

func (s *Server) handleCreateFormation(w http.ResponseWriter, r *http.Request) {
	body, err := io.ReadAll(io.LimitReader(r.Body, maxInboundBodyBytes))
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "failed to read request body"})
		return
	}
	defer r.Body.Close()

	var f Formation
	if err := json.Unmarshal(body, &f); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid JSON: " + err.Error()})
		return
	}

	if strings.TrimSpace(f.Name) == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "name is required"})
		return
	}
	if strings.TrimSpace(f.TaskType) == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "task_type is required"})
		return
	}

	created, err := s.formationStore.CreateFormation(f)
	if err != nil {
		// If error indicates already exists, return 409
		if strings.Contains(err.Error(), "already exists") {
			writeJSON(w, http.StatusConflict, map[string]string{"error": err.Error()})
			return
		}
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": err.Error()})
		return
	}

	writeJSON(w, http.StatusCreated, created)
}

func (s *Server) handleUpdateFormation(w http.ResponseWriter, r *http.Request) {
	name := r.PathValue("name")
	if name == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "formation name is required"})
		return
	}

	body, err := io.ReadAll(io.LimitReader(r.Body, maxInboundBodyBytes))
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "failed to read request body"})
		return
	}
	defer r.Body.Close()

	var f Formation
	if err := json.Unmarshal(body, &f); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid JSON: " + err.Error()})
		return
	}

	if strings.TrimSpace(f.Name) == "" {
		f.Name = name
	}

	updated, err := s.formationStore.UpdateFormation(name, f)
	if err != nil {
		if strings.Contains(err.Error(), "not found") {
			writeJSON(w, http.StatusNotFound, map[string]string{"error": err.Error()})
			return
		}
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": err.Error()})
		return
	}

	writeJSON(w, http.StatusOK, updated)
}

func (s *Server) handleDeleteFormation(w http.ResponseWriter, r *http.Request) {
	name := r.PathValue("name")
	if name == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "formation name is required"})
		return
	}

	deleted, err := s.formationStore.DeleteFormation(name)
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": err.Error()})
		return
	}

	if !deleted {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "formation '" + name + "' not found"})
		return
	}

	writeJSON(w, http.StatusOK, map[string]interface{}{
		"deleted": true,
		"name":    name,
	})
}
