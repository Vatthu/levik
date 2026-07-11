package orchestratorhost

import (
	"net/http"
	"strconv"
)

// AuditRecord represents a single approval audit entry returned by the
// GET /v1/approvals/audit endpoint.
type AuditRecord struct {
	AuditID              string   `json:"audit_id"`
	TaskID               string   `json:"task_id"`
	Timestamp            float64  `json:"timestamp"`
	ChangeContextJSON    string   `json:"change_context_json"`
	RuleMatched          *string  `json:"rule_matched"`
	RoutingOutcome       string   `json:"routing_outcome"`
	ConfidenceAtDecision *float64 `json:"confidence_at_decision"`
}

// AuditQueryResponse is the response payload for GET /v1/approvals/audit.
type AuditQueryResponse struct {
	Records []AuditRecord `json:"records"`
	Total   int           `json:"total"`
}

// auditStore is the interface for querying approval audit records.
// This is implemented by a backing store (SQLite or proxy to Python).
type auditStore interface {
	QueryAuditRecords(
		taskID, routingOutcome, rule string,
		timeRangeStart, timeRangeEnd float64,
		limit int,
	) ([]AuditRecord, error)
}

// handleApprovalsAudit handles GET /v1/approvals/audit.
// It supports filtering by task_id, routing_outcome, time_range (start/end), and rule.
//
// Query parameters:
//   - task_id: filter by task ID
//   - routing_outcome: filter by routing outcome (auto_approve, founder_review, escalate_and_halt)
//   - time_start: filter records with timestamp >= this Unix timestamp (float)
//   - time_end: filter records with timestamp <= this Unix timestamp (float)
//   - rule: filter by matched rule name
//   - limit: maximum records to return (default 100, max 1000)
//
// Validates: Requirements 33.2
func (s *Server) handleApprovalsAudit(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "method not allowed"})
		return
	}

	if s.auditStore == nil {
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{"error": "audit store not configured"})
		return
	}

	q := r.URL.Query()

	taskID := q.Get("task_id")
	routingOutcome := q.Get("routing_outcome")
	rule := q.Get("rule")

	var timeStart, timeEnd float64
	if v := q.Get("time_start"); v != "" {
		if f, err := strconv.ParseFloat(v, 64); err == nil {
			timeStart = f
		}
	}
	if v := q.Get("time_end"); v != "" {
		if f, err := strconv.ParseFloat(v, 64); err == nil {
			timeEnd = f
		}
	}

	limit := 100
	if v := q.Get("limit"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 {
			limit = n
		}
	}
	if limit > 1000 {
		limit = 1000
	}

	records, err := s.auditStore.QueryAuditRecords(taskID, routingOutcome, rule, timeStart, timeEnd, limit)
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": err.Error()})
		return
	}

	writeJSON(w, http.StatusOK, AuditQueryResponse{
		Records: records,
		Total:   len(records),
	})
}
