package orchestratorhost

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strconv"
	"strings"

	"github.com/Vatthu/vikram/pkg/telemetry"
)

// TraceQueryParams defines the query parameters accepted by GET /v1/trace/query.
type TraceQueryParams struct {
	TaskID       string `json:"task_id,omitempty"`
	DecisionType string `json:"decision_type,omitempty"`
	StartTime    string `json:"start_time,omitempty"`
	EndTime      string `json:"end_time,omitempty"`
	Outcome      string `json:"outcome,omitempty"`
	StartSeq     int    `json:"start_seq,omitempty"`
	EndSeq       int    `json:"end_seq,omitempty"`
}

// handleTraceQuery proxies execution trace queries. It either uses the
// configured TraceVerifier's fetcher to read records directly, or returns
// an error if no trace verifier is configured.
//
// GET /v1/trace/query?task_id=...&decision_type=...&start_seq=0&end_seq=99
func (s *Server) handleTraceQuery(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "method not allowed"})
		return
	}

	if s.traceVerifier == nil {
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{"error": "trace verifier not configured"})
		return
	}

	q := r.URL.Query()

	startSeq := 0
	endSeq := -1 // -1 means "all available"

	if v := q.Get("start_seq"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n >= 0 {
			startSeq = n
		}
	}
	if v := q.Get("end_seq"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n >= 0 {
			endSeq = n
		}
	}

	// If no explicit end_seq, fetch up to start + 1000
	if endSeq < 0 {
		endSeq = startSeq + 999
	}

	// Cap batch size at 1000
	if endSeq-startSeq > 999 {
		endSeq = startSeq + 999
	}

	records, err := s.traceVerifier.Fetcher().FetchRecords(r.Context(), startSeq, endSeq)
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{
			"error": fmt.Sprintf("failed to fetch trace records: %v", err),
		})
		return
	}

	// Apply additional filters
	filtered := filterTraceRecords(records, q)

	writeJSON(w, http.StatusOK, telemetry.TraceQueryResponse{
		Records: filtered,
		Total:   len(filtered),
	})
}

// handleTraceReplay verifies replay of a specific trace record.
//
// POST /v1/trace/replay  { "record_id": 42 }
func (s *Server) handleTraceReplay(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "method not allowed"})
		return
	}

	if s.traceVerifier == nil {
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{"error": "trace verifier not configured"})
		return
	}

	body, err := io.ReadAll(io.LimitReader(r.Body, maxInboundBodyBytes))
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "failed to read request body"})
		return
	}

	var req telemetry.TraceReplayRequest
	if err := json.Unmarshal(body, &req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid JSON: " + err.Error()})
		return
	}

	// Fetch the specific record and verify its hash integrity
	records, err := s.traceVerifier.Fetcher().FetchRecords(r.Context(), req.RecordID, req.RecordID)
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{
			"error": fmt.Sprintf("failed to fetch record %d: %v", req.RecordID, err),
		})
		return
	}

	if len(records) == 0 {
		writeJSON(w, http.StatusNotFound, telemetry.TraceReplayResponse{
			Matches: false,
			Message: fmt.Sprintf("record with sequence_number %d not found", req.RecordID),
		})
		return
	}

	record := records[0]

	// Verify the record's hash is correctly computed
	computed := telemetry.ComputeRecordHash(
		record.SequenceNumber,
		record.TaskID,
		record.DecisionType,
		record.Timestamp,
		record.StateSnapshot,
		record.PolicyEvaluated,
		record.Outcome,
		record.NonDeterministicInputs,
		record.PreviousHash,
	)

	if computed != record.RecordHash {
		writeJSON(w, http.StatusOK, telemetry.TraceReplayResponse{
			Matches: false,
			Message: fmt.Sprintf("hash mismatch for record %d: stored=%s computed=%s", req.RecordID, record.RecordHash, computed),
		})
		return
	}

	writeJSON(w, http.StatusOK, telemetry.TraceReplayResponse{
		Matches: true,
		Message: fmt.Sprintf("record %d integrity verified: hash matches", req.RecordID),
	})
}

// filterTraceRecords applies optional query parameters to filter records.
func filterTraceRecords(records []telemetry.TraceRecord, q interface{ Get(string) string }) []telemetry.TraceRecord {
	taskID := q.Get("task_id")
	decisionType := q.Get("decision_type")
	outcome := q.Get("outcome")

	if taskID == "" && decisionType == "" && outcome == "" {
		return records
	}

	filtered := make([]telemetry.TraceRecord, 0, len(records))
	for _, r := range records {
		if taskID != "" && r.TaskID != taskID {
			continue
		}
		if decisionType != "" && r.DecisionType != decisionType {
			continue
		}
		if outcome != "" && !strings.EqualFold(r.Outcome, outcome) {
			continue
		}
		filtered = append(filtered, r)
	}
	return filtered
}
