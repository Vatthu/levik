package orchestratorhost

import (
	"encoding/json"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/Vatthu/vikram/pkg/telemetry"
)

// handleTelemetryEmit accepts a TelemetryEvent JSON body and persists it
// via the configured telemetry Store.
func (s *Server) handleTelemetryEmit(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "method not allowed"})
		return
	}
	if s.cfg.TelemetryStore == nil {
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{"error": "telemetry store not configured"})
		return
	}

	var event telemetry.TelemetryEvent
	if err := decodeJSON(w, r, &event); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}

	if err := s.cfg.TelemetryStore.Emit(r.Context(), event); err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "failed to emit telemetry event"})
		return
	}

	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

// handleTelemetrySummary returns aggregated metrics for a specified time window,
// optionally grouped by role, model, or task_id.
func (s *Server) handleTelemetrySummary(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "method not allowed"})
		return
	}
	if s.cfg.TelemetryStore == nil {
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{"error": "telemetry store not configured"})
		return
	}

	q, err := parseSummaryQuery(r)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}

	result, err := s.cfg.TelemetryStore.Query(r.Context(), q)
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "failed to query telemetry summary"})
		return
	}

	writeJSON(w, http.StatusOK, result)
}

// handleTelemetryEvents returns paginated raw telemetry events filtered by
// task_id, event_type, role, or model.
func (s *Server) handleTelemetryEvents(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "method not allowed"})
		return
	}
	if s.cfg.TelemetryStore == nil {
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{"error": "telemetry store not configured"})
		return
	}

	filters := map[string]string{}
	for _, key := range []string{"task_id", "event_type", "role", "model"} {
		if val := r.URL.Query().Get(key); val != "" {
			filters[key] = val
		}
	}

	page := 1
	pageSize := 50
	if p := r.URL.Query().Get("page"); p != "" {
		if v, err := strconv.Atoi(p); err == nil && v > 0 {
			page = v
		}
	}
	if ps := r.URL.Query().Get("page_size"); ps != "" {
		if v, err := strconv.Atoi(ps); err == nil && v > 0 && v <= 200 {
			pageSize = v
		}
	}

	events, total, err := s.cfg.TelemetryStore.Events(r.Context(), filters, page, pageSize)
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "failed to query telemetry events"})
		return
	}

	writeJSON(w, http.StatusOK, telemetryEventsResponse{
		Events:   events,
		Total:    total,
		Page:     page,
		PageSize: pageSize,
	})
}

// handleTelemetryCost returns cost breakdown for a specified time window,
// grouped by task, role, model, or phase.
func (s *Server) handleTelemetryCost(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "method not allowed"})
		return
	}
	if s.cfg.TelemetryStore == nil {
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{"error": "telemetry store not configured"})
		return
	}

	q, err := parseSummaryQuery(r)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}

	// For cost endpoint, default group_by to task_id if not specified.
	if len(q.GroupBy) == 0 {
		q.GroupBy = []string{"task_id"}
	}

	result, err := s.cfg.TelemetryStore.Query(r.Context(), q)
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "failed to query telemetry cost"})
		return
	}

	writeJSON(w, http.StatusOK, result)
}

// telemetryEventsResponse wraps paginated event results.
type telemetryEventsResponse struct {
	Events   []telemetry.TelemetryEvent `json:"events"`
	Total    int                        `json:"total"`
	Page     int                        `json:"page"`
	PageSize int                        `json:"page_size"`
}

// parseSummaryQuery extracts SummaryQuery from HTTP request query params.
func parseSummaryQuery(r *http.Request) (telemetry.SummaryQuery, error) {
	var q telemetry.SummaryQuery

	startStr := r.URL.Query().Get("start_time")
	endStr := r.URL.Query().Get("end_time")

	if startStr != "" {
		t, err := parseTimeParam(startStr)
		if err != nil {
			return q, err
		}
		q.StartTime = t
	} else {
		// Default to last 24 hours.
		q.StartTime = time.Now().UTC().Add(-24 * time.Hour)
	}

	if endStr != "" {
		t, err := parseTimeParam(endStr)
		if err != nil {
			return q, err
		}
		q.EndTime = t
	} else {
		q.EndTime = time.Now().UTC()
	}

	if groupBy := r.URL.Query().Get("group_by"); groupBy != "" {
		q.GroupBy = strings.Split(groupBy, ",")
	}

	return q, nil
}

// parseTimeParam parses a time string in RFC3339 or Unix timestamp format.
func parseTimeParam(s string) (time.Time, error) {
	// Try RFC3339 first.
	if t, err := time.Parse(time.RFC3339, s); err == nil {
		return t, nil
	}
	// Try RFC3339Nano.
	if t, err := time.Parse(time.RFC3339Nano, s); err == nil {
		return t, nil
	}
	// Try Unix timestamp (seconds).
	if ts, err := strconv.ParseInt(s, 10, 64); err == nil {
		return time.Unix(ts, 0).UTC(), nil
	}
	return time.Time{}, &json.UnmarshalTypeError{Value: "invalid time format: " + s}
}
