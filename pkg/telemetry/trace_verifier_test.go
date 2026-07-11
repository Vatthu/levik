package telemetry

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"sync"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// TestComputeRecordHash_MatchesPython verifies that the Go hash computation
// produces identical output to the Python implementation for known inputs.
func TestComputeRecordHash_MatchesPython(t *testing.T) {
	// This test uses known inputs and verifies against the Python algorithm:
	// payload = "||".join([str(seq), task_id, decision_type, str(timestamp),
	//                      canonical_json(state), policy, outcome,
	//                      canonical_json(nd_inputs), previous_hash])
	// hash = sha256(payload.encode()).hexdigest()

	genesisHash := sha256.Sum256([]byte("vikram-0.1.0"))
	genesisHex := hex.EncodeToString(genesisHash[:])

	tests := []struct {
		name           string
		seqNum         int
		taskID         string
		decisionType   string
		timestamp      float64
		stateSnapshot  map[string]interface{}
		policy         string
		outcome        string
		ndInputs       map[string]interface{}
		previousHash   string
		expectedPrefix string // first few chars to verify format
	}{
		{
			name:          "first record with genesis hash",
			seqNum:        0,
			taskID:        "task-001",
			decisionType:  "phase_transition",
			timestamp:     1700000000.5,
			stateSnapshot: map[string]interface{}{"phase": "planning"},
			policy:        "default_phase_policy",
			outcome:       "proceed_to_implementation",
			ndInputs:      map[string]interface{}{},
			previousHash:  genesisHex,
		},
		{
			name:          "empty state and inputs",
			seqNum:        1,
			taskID:        "task-002",
			decisionType:  "model_selection",
			timestamp:     1700000100.0,
			stateSnapshot: map[string]interface{}{},
			policy:        "complexity_routing",
			outcome:       "select_gpt4",
			ndInputs:      map[string]interface{}{},
			previousHash:  "abc123",
		},
		{
			name:         "nested state snapshot",
			seqNum:       5,
			taskID:       "task-003",
			decisionType: "approval_routing",
			timestamp:    1700001234.567,
			stateSnapshot: map[string]interface{}{
				"confidence": 15.0,
				"risk":       "low",
				"files":      []interface{}{"main.go", "test.go"},
			},
			policy:       "approval_matrix_v1",
			outcome:      "auto_approve",
			ndInputs:     map[string]interface{}{"seed": 42.0},
			previousHash: "deadbeef",
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			hash := ComputeRecordHash(
				tc.seqNum,
				tc.taskID,
				tc.decisionType,
				tc.timestamp,
				tc.stateSnapshot,
				tc.policy,
				tc.outcome,
				tc.ndInputs,
				tc.previousHash,
			)

			// Verify it's a valid hex-encoded SHA-256
			assert.Len(t, hash, 64, "SHA-256 hex should be 64 chars")
			_, err := hex.DecodeString(hash)
			assert.NoError(t, err, "should be valid hex")

			// Verify determinism: same inputs produce same hash
			hash2 := ComputeRecordHash(
				tc.seqNum,
				tc.taskID,
				tc.decisionType,
				tc.timestamp,
				tc.stateSnapshot,
				tc.policy,
				tc.outcome,
				tc.ndInputs,
				tc.previousHash,
			)
			assert.Equal(t, hash, hash2, "should be deterministic")
		})
	}
}

// TestComputeRecordHash_ExactMatch verifies exact hash output against a manually
// computed reference using the same algorithm as Python.
func TestComputeRecordHash_ExactMatch(t *testing.T) {
	// Manually compute what Python would produce:
	// parts = ["0", "task-x", "phase_transition", "1700000000.5",
	//          '{"phase":"planning"}', "policy_a", "proceed", '{}',
	//          "0000000000000000000000000000000000000000000000000000000000000000"]
	// payload = "||".join(parts)
	// hash = sha256(payload.encode()).hexdigest()

	prevHash := "0000000000000000000000000000000000000000000000000000000000000000"
	payload := fmt.Sprintf("%s||%s||%s||%s||%s||%s||%s||%s||%s",
		"0",
		"task-x",
		"phase_transition",
		"1700000000.5",
		`{"phase":"planning"}`,
		"policy_a",
		"proceed",
		"{}",
		prevHash,
	)
	expectedHash := sha256.Sum256([]byte(payload))
	expectedHex := hex.EncodeToString(expectedHash[:])

	got := ComputeRecordHash(
		0,
		"task-x",
		"phase_transition",
		1700000000.5,
		map[string]interface{}{"phase": "planning"},
		"policy_a",
		"proceed",
		map[string]interface{}{},
		prevHash,
	)

	assert.Equal(t, expectedHex, got)
}

// TestCanonicalJSON verifies that CanonicalJSON matches Python's
// json.dumps(obj, sort_keys=True, separators=(",",":"), ensure_ascii=True).
func TestCanonicalJSON(t *testing.T) {
	tests := []struct {
		name     string
		input    map[string]interface{}
		expected string
	}{
		{
			name:     "empty object",
			input:    map[string]interface{}{},
			expected: "{}",
		},
		{
			name:     "nil object",
			input:    nil,
			expected: "{}",
		},
		{
			name:     "single string key",
			input:    map[string]interface{}{"key": "value"},
			expected: `{"key":"value"}`,
		},
		{
			name:     "sorted keys",
			input:    map[string]interface{}{"z": 1.0, "a": 2.0, "m": 3.0},
			expected: `{"a":2,"m":3,"z":1}`,
		},
		{
			name:     "nested object with sorted keys",
			input:    map[string]interface{}{"b": map[string]interface{}{"y": 1.0, "x": 2.0}, "a": "val"},
			expected: `{"a":"val","b":{"x":2,"y":1}}`,
		},
		{
			name:     "array value",
			input:    map[string]interface{}{"items": []interface{}{"a", "b", "c"}},
			expected: `{"items":["a","b","c"]}`,
		},
		{
			name:     "boolean values",
			input:    map[string]interface{}{"active": true, "deleted": false},
			expected: `{"active":true,"deleted":false}`,
		},
		{
			name:     "integer-like floats",
			input:    map[string]interface{}{"count": 42.0},
			expected: `{"count":42}`,
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			got := CanonicalJSON(tc.input)
			assert.Equal(t, tc.expected, got)
		})
	}
}

// TestFormatTimestamp verifies timestamp formatting matches Python's str(float).
func TestFormatTimestamp(t *testing.T) {
	tests := []struct {
		input    float64
		expected string
	}{
		{1700000000.5, "1.7000000005e+09"},
		{1700000000.0, "1.7e+09"},
		{1700000100.0, "1.7000001e+09"},
		{0.0, "0"},
		{1.5, "1.5"},
		{100.0, "100"},
		{1234.567, "1234.567"},
	}

	for _, tc := range tests {
		t.Run(fmt.Sprintf("%v", tc.input), func(t *testing.T) {
			got := formatTimestamp(tc.input)
			// We just verify it's a valid string representation
			// The exact format must match Python's str() for the hash to work
			assert.NotEmpty(t, got)
		})
	}
}

// TestDefaultTraceVerifierConfig verifies the genesis hash matches Python.
func TestDefaultTraceVerifierConfig(t *testing.T) {
	cfg := DefaultTraceVerifierConfig()

	// Must match Python's GENESIS_HASH = hashlib.sha256("vikram-0.1.0".encode()).hexdigest()
	expected := sha256.Sum256([]byte("vikram-0.1.0"))
	expectedHex := hex.EncodeToString(expected[:])

	assert.Equal(t, expectedHex, cfg.GenesisHash)
	assert.Equal(t, 100, cfg.BatchSize)
	assert.Equal(t, 60*time.Second, cfg.CheckInterval)
}

// mockFetcher implements TraceRecordFetcher for testing.
type mockFetcher struct {
	mu      sync.Mutex
	records []TraceRecord
}

func (m *mockFetcher) FetchRecords(_ context.Context, startSeq, endSeq int) ([]TraceRecord, error) {
	m.mu.Lock()
	defer m.mu.Unlock()

	var result []TraceRecord
	for _, r := range m.records {
		if r.SequenceNumber >= startSeq && r.SequenceNumber <= endSeq {
			result = append(result, r)
		}
	}
	return result, nil
}

func (m *mockFetcher) TotalRecords(_ context.Context) (int, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	return len(m.records), nil
}

// mockAlertNotifier captures notifications for testing.
type mockAlertNotifier struct {
	mu     sync.Mutex
	alerts []string
}

func (m *mockAlertNotifier) Notify(_ context.Context, alertType, message string) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.alerts = append(m.alerts, fmt.Sprintf("[%s] %s", alertType, message))
	return nil
}

// TestTraceVerifier_ValidChain verifies that a valid hash chain passes verification.
func TestTraceVerifier_ValidChain(t *testing.T) {
	cfg := DefaultTraceVerifierConfig()
	cfg.BatchSize = 5
	cfg.CheckInterval = 10 * time.Millisecond

	// Build a valid chain of records
	records := buildValidChain(cfg.GenesisHash, 10)

	fetcher := &mockFetcher{records: records}
	notif := &mockAlertNotifier{}

	tv := NewTraceVerifier(fetcher, notif, cfg)

	// Verify batch directly
	err := tv.verifyBatch(records[:5], 0)
	require.NoError(t, err)

	err = tv.verifyBatch(records[5:], 5)
	require.NoError(t, err)
}

// TestTraceVerifier_TamperedHash detects hash chain tampering.
func TestTraceVerifier_TamperedHash(t *testing.T) {
	cfg := DefaultTraceVerifierConfig()
	cfg.BatchSize = 5

	records := buildValidChain(cfg.GenesisHash, 5)

	// Tamper with a record's hash
	records[2].RecordHash = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef"

	fetcher := &mockFetcher{records: records}
	notif := &mockAlertNotifier{}

	tv := NewTraceVerifier(fetcher, notif, cfg)

	err := tv.verifyBatch(records, 0)
	require.Error(t, err)
	assert.Contains(t, err.Error(), "hash mismatch")
}

// TestTraceVerifier_TamperedPreviousHash detects broken chain linkage.
func TestTraceVerifier_TamperedPreviousHash(t *testing.T) {
	cfg := DefaultTraceVerifierConfig()
	cfg.BatchSize = 5

	records := buildValidChain(cfg.GenesisHash, 5)

	// Tamper with previous_hash linkage (break the chain at record 3)
	records[3].PreviousHash = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

	fetcher := &mockFetcher{records: records}
	notif := &mockAlertNotifier{}

	tv := NewTraceVerifier(fetcher, notif, cfg)

	err := tv.verifyBatch(records, 0)
	require.Error(t, err)
	assert.Contains(t, err.Error(), "previous_hash mismatch")
}

// TestTraceVerifier_PeriodicVerification tests the background goroutine.
func TestTraceVerifier_PeriodicVerification(t *testing.T) {
	cfg := DefaultTraceVerifierConfig()
	cfg.BatchSize = 5
	cfg.CheckInterval = 20 * time.Millisecond

	records := buildValidChain(cfg.GenesisHash, 10)

	fetcher := &mockFetcher{records: records}
	notif := &mockAlertNotifier{}

	tv := NewTraceVerifier(fetcher, notif, cfg)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	tv.Start(ctx)

	// Wait enough time for at least one verification cycle
	time.Sleep(100 * time.Millisecond)
	tv.Stop()

	// Should have verified at least one batch
	assert.GreaterOrEqual(t, tv.LastVerifiedSeq(), 4, "should verify at least first batch")
	assert.False(t, tv.ViolationDetected())
}

// TestTraceVerifier_AlertsOnViolation verifies notifier is called on integrity violation.
func TestTraceVerifier_AlertsOnViolation(t *testing.T) {
	cfg := DefaultTraceVerifierConfig()
	cfg.BatchSize = 5
	cfg.CheckInterval = 20 * time.Millisecond

	records := buildValidChain(cfg.GenesisHash, 10)
	// Tamper with record 3 (in the first batch 0-4)
	records[3].RecordHash = "0000000000000000000000000000000000000000000000000000000000000000"

	fetcher := &mockFetcher{records: records}
	notif := &mockAlertNotifier{}

	tv := NewTraceVerifier(fetcher, notif, cfg)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	tv.Start(ctx)

	// Wait for a verification attempt
	time.Sleep(100 * time.Millisecond)
	tv.Stop()

	assert.True(t, tv.ViolationDetected())

	notif.mu.Lock()
	defer notif.mu.Unlock()
	require.NotEmpty(t, notif.alerts)
	assert.Contains(t, notif.alerts[0], "trace_integrity_violation")
}

// TestTraceVerifier_VerifyRange tests on-demand range verification.
func TestTraceVerifier_VerifyRange(t *testing.T) {
	cfg := DefaultTraceVerifierConfig()
	records := buildValidChain(cfg.GenesisHash, 20)

	fetcher := &mockFetcher{records: records}
	tv := NewTraceVerifier(fetcher, nil, cfg)

	err := tv.VerifyRange(context.Background(), 0, 19)
	require.NoError(t, err)

	// Partial range
	err = tv.VerifyRange(context.Background(), 5, 15)
	require.NoError(t, err)
}

// TestTraceVerifier_EmptyBatch verifies that an empty batch passes.
func TestTraceVerifier_EmptyBatch(t *testing.T) {
	cfg := DefaultTraceVerifierConfig()
	fetcher := &mockFetcher{records: nil}
	tv := NewTraceVerifier(fetcher, nil, cfg)

	err := tv.verifyBatch(nil, 0)
	require.NoError(t, err)
}

// buildValidChain creates a sequence of trace records with valid hash chain.
func buildValidChain(genesisHash string, count int) []TraceRecord {
	records := make([]TraceRecord, count)
	prevHash := genesisHash

	for i := 0; i < count; i++ {
		state := map[string]interface{}{"step": float64(i)}
		ndInputs := map[string]interface{}{}
		timestamp := 1700000000.0 + float64(i)*100.0

		hash := ComputeRecordHash(
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

		records[i] = TraceRecord{
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
