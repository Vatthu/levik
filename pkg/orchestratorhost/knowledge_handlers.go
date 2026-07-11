package orchestratorhost

import (
	"net/http"
	"strconv"
)

// --- Types for Knowledge Store API ---

// ApproachEffectivenessRecord represents effectiveness scoring for an approach group.
type ApproachEffectivenessRecord struct {
	RepoPath       string  `json:"repo_path"`
	TaskType       string  `json:"task_type"`
	ComplexityTier string  `json:"complexity_tier"`
	TotalRecords   int     `json:"total_records"`
	SuccessRate    float64 `json:"success_rate"`
	CostEfficiency float64 `json:"cost_efficiency"`
	TimeEfficiency float64 `json:"time_efficiency"`
	FirstPassRate  float64 `json:"first_pass_rate"`
	CompositeScore float64 `json:"composite_score"`
}

// FailurePatternRecord represents a recognized recurring failure pattern.
type FailurePatternRecord struct {
	PatternID              string   `json:"pattern_id"`
	RepoPath               string   `json:"repo_path"`
	FailureClass           string   `json:"failure_class"`
	ErrorSignature         string   `json:"error_signature"`
	Frequency              int      `json:"frequency"`
	LastSeen               float64  `json:"last_seen"`
	SuccessfulAlternatives []string `json:"successful_alternatives"`
}

// --- Interface for Knowledge Store data access ---

// knowledgeStore is the interface Go uses to interact with knowledge store data.
// Implementations proxy to the Python orchestrator's KnowledgeStore.
type knowledgeStore interface {
	GetApproachEffectiveness(repoPath, taskType, tier string) ([]ApproachEffectivenessRecord, error)
	GetFailurePatterns(repoPath, failureClass, model string, minFrequency int) ([]FailurePatternRecord, error)
}

// SetKnowledgeStore configures the knowledge store used by the host server.
// Must be called before Start.
func (s *Server) SetKnowledgeStore(ks knowledgeStore) {
	s.knowledgeStore = ks
}

// --- Handlers ---

// handleKnowledgeApproaches handles GET /v1/knowledge/approaches — returns approach effectiveness data.
// Supports query parameters: repo_path, task_type, complexity_tier.
// Validates: Requirements 39.4, 40.1, 40.2
func (s *Server) handleKnowledgeApproaches(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "method not allowed"})
		return
	}

	if s.knowledgeStore == nil {
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{"error": "knowledge store not configured"})
		return
	}

	// Parse query parameters for filtering
	repoPath := r.URL.Query().Get("repo_path")
	taskType := r.URL.Query().Get("task_type")
	complexityTier := r.URL.Query().Get("complexity_tier")

	records, err := s.knowledgeStore.GetApproachEffectiveness(repoPath, taskType, complexityTier)
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": err.Error()})
		return
	}

	writeJSON(w, http.StatusOK, map[string]interface{}{
		"approaches": records,
	})
}

// handleKnowledgeFailures handles GET /v1/knowledge/failures — returns failure pattern statistics.
// Supports query parameters: repo_path, failure_class, model, min_frequency.
// Validates: Requirements 41.4, 40.3
func (s *Server) handleKnowledgeFailures(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "method not allowed"})
		return
	}

	if s.knowledgeStore == nil {
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{"error": "knowledge store not configured"})
		return
	}

	// Parse query parameters for filtering
	repoPath := r.URL.Query().Get("repo_path")
	failureClass := r.URL.Query().Get("failure_class")
	model := r.URL.Query().Get("model")
	minFrequencyStr := r.URL.Query().Get("min_frequency")

	minFrequency := 0
	if minFrequencyStr != "" {
		parsed, err := strconv.Atoi(minFrequencyStr)
		if err != nil {
			writeJSON(w, http.StatusBadRequest, map[string]string{"error": "min_frequency must be an integer"})
			return
		}
		minFrequency = parsed
	}

	patterns, err := s.knowledgeStore.GetFailurePatterns(repoPath, failureClass, model, minFrequency)
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": err.Error()})
		return
	}

	writeJSON(w, http.StatusOK, map[string]interface{}{
		"patterns": patterns,
	})
}
