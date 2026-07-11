package orchestratorhost

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/Vatthu/vikram/pkg/telemetry"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// testTraceFetcher implements telemetry.TraceRecordFetcher for handler tests.
type testTraceFetcher struct {
	records []telemetry.TraceRecord
}

func (f *testTraceFetcher) FetchRecords(_ context.Context, startSeq, endSeq int) ([]telemetry.TraceRecord, error) {
	var result []telemetry.TraceRecord
	for _, r := range f.records {
		if r.SequenceNumber >= startSeq && r.SequenceNumber <= endSeq {
			result = append(result, r)
		}
	}
	return result, nil
}

func (f *testTraceFetcher) TotalRecords(_ context.Context) (int, error) {
	return len(f.records), nil
}

// testNotifier is a no-op notifier for handler tests.
type testNotifier struct{}

func (n *testNotifier) SendToChannel(_ context.Context, _, _, _ string) error { return nil }

func buildTestTraceRecords(count int) []telemetry.TraceRecord {
	cfg := telemetry.DefaultTraceVerifierConfig()
	records := make([]telemetry.TraceRecord, count)
	prevHash := cfg.GenesisHash

	for i := 0; i < count; i++ {
		state := map[string]interface{}{"step": float64(i)}
		ndInputs := map[string]interface{}{}
		timestamp := 1700000000.0 + float64(i)*100.0

		hash := telemetry.ComputeRecordHash(
			i,
			fmt.Sprintf("task-%03d", i%3),
			"phase_transition",
			timestamp,
			state,
			"test_policy",
			"proceed",
			ndInputs,
			prevHash,
		)

		records[i] = telemetry.TraceRecord{
			SequenceNumber:         i,
			TaskID:                 fmt.Sprintf("task-%03d", i%3),
			DecisionType:           "phase_transition",
			Timestamp:              timestamp,
			StateSnapshot:          state,
			PolicyEvaluated:        "test_policy",
			Outcome:                "proceed",
			NonDeterministicInputs: ndInputs,
			PreviousHash:           prevHash,
			RecordHash:             hash,
		}

		prevHash = hash
	}
	return records
}

func setupTraceTestServer(records []telemetry.TraceRecord) *Server {
	cfg := telemetry.DefaultTraceVerifierConfig()
	fetcher := &testTraceFetcher{records: records}
	tv := telemetry.NewTraceVerifier(fetcher, nil, cfg)

	s := NewServer(Config{
		SocketPath:    "/tmp/test-trace.sock",
		WorkspaceRoot: "/tmp",
	}, &testNotifier{})
	s.SetTraceVerifier(tv)
	return s
}

func TestHandleTraceQuery_Success(t *testing.T) {
	records := buildTestTraceRecords(10)
	s := setupTraceTestServer(records)

	req := httptest.NewRequest(http.MethodGet, "/v1/trace/query?start_seq=0&end_seq=4", nil)
	w := httptest.NewRecorder()

	s.handleTraceQuery(w, req)

	assert.Equal(t, http.StatusOK, w.Code)

	var resp telemetry.TraceQueryResponse
	err := json.NewDecoder(w.Body).Decode(&resp)
	require.NoError(t, err)
	assert.Equal(t, 5, resp.Total)
	assert.Len(t, resp.Records, 5)
}

func TestHandleTraceQuery_FilterByTaskID(t *testing.T) {
	records := buildTestTraceRecords(9) // task-000, task-001, task-002 rotating
	s := setupTraceTestServer(records)

	req := httptest.NewRequest(http.MethodGet, "/v1/trace/query?start_seq=0&end_seq=8&task_id=task-000", nil)
	w := httptest.NewRecorder()

	s.handleTraceQuery(w, req)

	assert.Equal(t, http.StatusOK, w.Code)

	var resp telemetry.TraceQueryResponse
	err := json.NewDecoder(w.Body).Decode(&resp)
	require.NoError(t, err)
	// Records 0, 3, 6 have task-000
	assert.Equal(t, 3, resp.Total)
}

func TestHandleTraceQuery_MethodNotAllowed(t *testing.T) {
	s := setupTraceTestServer(nil)

	req := httptest.NewRequest(http.MethodPost, "/v1/trace/query", nil)
	w := httptest.NewRecorder()

	s.handleTraceQuery(w, req)

	assert.Equal(t, http.StatusMethodNotAllowed, w.Code)
}

func TestHandleTraceQuery_NotConfigured(t *testing.T) {
	s := NewServer(Config{
		SocketPath:    "/tmp/test.sock",
		WorkspaceRoot: "/tmp",
	}, &testNotifier{})
	// Do NOT set trace verifier

	req := httptest.NewRequest(http.MethodGet, "/v1/trace/query", nil)
	w := httptest.NewRecorder()

	s.handleTraceQuery(w, req)

	assert.Equal(t, http.StatusServiceUnavailable, w.Code)
}

func TestHandleTraceReplay_ValidRecord(t *testing.T) {
	records := buildTestTraceRecords(5)
	s := setupTraceTestServer(records)

	body, _ := json.Marshal(telemetry.TraceReplayRequest{RecordID: 2})
	req := httptest.NewRequest(http.MethodPost, "/v1/trace/replay", bytes.NewReader(body))
	w := httptest.NewRecorder()

	s.handleTraceReplay(w, req)

	assert.Equal(t, http.StatusOK, w.Code)

	var resp telemetry.TraceReplayResponse
	err := json.NewDecoder(w.Body).Decode(&resp)
	require.NoError(t, err)
	assert.True(t, resp.Matches)
	assert.Contains(t, resp.Message, "integrity verified")
}

func TestHandleTraceReplay_TamperedRecord(t *testing.T) {
	records := buildTestTraceRecords(5)
	// Tamper with record 2's outcome so hash won't match
	records[2].Outcome = "tampered_outcome"

	s := setupTraceTestServer(records)

	body, _ := json.Marshal(telemetry.TraceReplayRequest{RecordID: 2})
	req := httptest.NewRequest(http.MethodPost, "/v1/trace/replay", bytes.NewReader(body))
	w := httptest.NewRecorder()

	s.handleTraceReplay(w, req)

	assert.Equal(t, http.StatusOK, w.Code)

	var resp telemetry.TraceReplayResponse
	err := json.NewDecoder(w.Body).Decode(&resp)
	require.NoError(t, err)
	assert.False(t, resp.Matches)
	assert.Contains(t, resp.Message, "hash mismatch")
}

func TestHandleTraceReplay_NotFound(t *testing.T) {
	records := buildTestTraceRecords(5)
	s := setupTraceTestServer(records)

	body, _ := json.Marshal(telemetry.TraceReplayRequest{RecordID: 999})
	req := httptest.NewRequest(http.MethodPost, "/v1/trace/replay", bytes.NewReader(body))
	w := httptest.NewRecorder()

	s.handleTraceReplay(w, req)

	assert.Equal(t, http.StatusNotFound, w.Code)
}

func TestHandleTraceReplay_MethodNotAllowed(t *testing.T) {
	s := setupTraceTestServer(nil)

	req := httptest.NewRequest(http.MethodGet, "/v1/trace/replay", nil)
	w := httptest.NewRecorder()

	s.handleTraceReplay(w, req)

	assert.Equal(t, http.StatusMethodNotAllowed, w.Code)
}
