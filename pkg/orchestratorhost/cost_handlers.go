package orchestratorhost

import (
	"net/http"
	"time"

	"github.com/Vatthu/vikram/pkg/costledger"
)

// costRecordRequest is the JSON body for POST /v1/cost/record.
type costRecordRequest struct {
	RecordID     string  `json:"record_id"`
	TaskID       string  `json:"task_id"`
	Role         string  `json:"role"`
	Model        string  `json:"model"`
	Provider     string  `json:"provider"`
	WorkPhase    string  `json:"work_phase"`
	InputTokens  int     `json:"input_tokens"`
	OutputTokens int     `json:"output_tokens"`
	CostUSD      float64 `json:"cost_usd"`
	Estimated    bool    `json:"estimated"`
	DurationMS   int64   `json:"duration_ms"`
	InvocationID string  `json:"invocation_id"`
	Timestamp    string  `json:"timestamp,omitempty"`
}

// costForecastRequest is the JSON body for POST /v1/cost/forecast.
type costForecastRequest struct {
	Complexity  string `json:"complexity"`
	TargetFiles int    `json:"target_files"`
}

// handleCostRecord handles POST /v1/cost/record — records a cost event from an agent call.
func (s *Server) handleCostRecord(w http.ResponseWriter, r *http.Request) {
	if s.ledger == nil {
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{"error": "cost ledger not configured"})
		return
	}

	var req costRecordRequest
	if err := decodeJSON(w, r, &req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	if req.TaskID == "" || req.RecordID == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "record_id and task_id are required"})
		return
	}
	if !taskIDPattern.MatchString(req.TaskID) {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "task_id contains unsupported characters"})
		return
	}

	ts := time.Now().UTC()
	if req.Timestamp != "" {
		parsed, err := time.Parse(time.RFC3339, req.Timestamp)
		if err == nil {
			ts = parsed.UTC()
		}
	}

	rec := costledger.CostRecord{
		RecordID:     req.RecordID,
		TaskID:       req.TaskID,
		Role:         req.Role,
		Model:        req.Model,
		Provider:     req.Provider,
		WorkPhase:    req.WorkPhase,
		InputTokens:  req.InputTokens,
		OutputTokens: req.OutputTokens,
		CostUSD:      req.CostUSD,
		Estimated:    req.Estimated,
		DurationMS:   req.DurationMS,
		InvocationID: req.InvocationID,
		Timestamp:    ts,
	}

	if err := s.ledger.Record(r.Context(), rec); err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": err.Error()})
		return
	}

	writeJSON(w, http.StatusOK, map[string]interface{}{
		"recorded":  true,
		"record_id": req.RecordID,
		"task_id":   req.TaskID,
	})
}

// handleCostTaskCumulative handles GET /v1/cost/task/{task_id} — returns cumulative cost for a task.
func (s *Server) handleCostTaskCumulative(w http.ResponseWriter, r *http.Request) {
	if s.ledger == nil {
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{"error": "cost ledger not configured"})
		return
	}

	taskID := r.PathValue("task_id")
	if taskID == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "task_id is required"})
		return
	}
	if !taskIDPattern.MatchString(taskID) {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "task_id contains unsupported characters"})
		return
	}

	total, err := s.ledger.TaskCumulative(r.Context(), taskID)
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": err.Error()})
		return
	}

	writeJSON(w, http.StatusOK, map[string]interface{}{
		"task_id":        taskID,
		"cumulative_usd": total,
	})
}

// handleCostForecast handles POST /v1/cost/forecast — produces a cost forecast for a new task.
func (s *Server) handleCostForecast(w http.ResponseWriter, r *http.Request) {
	if s.ledger == nil {
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{"error": "cost ledger not configured"})
		return
	}

	var req costForecastRequest
	if err := decodeJSON(w, r, &req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	if req.Complexity == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "complexity is required"})
		return
	}
	if req.TargetFiles < 1 {
		req.TargetFiles = 1
	}

	forecast, err := s.ledger.Forecast(r.Context(), req.Complexity, req.TargetFiles)
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": err.Error()})
		return
	}

	writeJSON(w, http.StatusOK, forecast)
}

// handleCostDaily handles GET /v1/cost/daily — returns system-wide daily spend.
func (s *Server) handleCostDaily(w http.ResponseWriter, r *http.Request) {
	if s.ledger == nil {
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{"error": "cost ledger not configured"})
		return
	}

	total, err := s.ledger.DailyTotal(r.Context())
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": err.Error()})
		return
	}

	writeJSON(w, http.StatusOK, map[string]interface{}{
		"daily_total_usd": total,
	})
}
