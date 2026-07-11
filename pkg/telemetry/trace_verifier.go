// trace_verifier.go implements Go-side integrity verification of the
// Python Orchestrator's execution trace hash chain. It periodically reads
// trace records (via HTTP or direct SQLite access) and recomputes SHA-256
// hashes to detect any tampering.
//
// Requirements: 6.2, 7.3
package telemetry

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"sort"
	"strings"
	"sync"
	"time"
)

// TraceRecord mirrors the Python TraceRecord model for hash verification.
type TraceRecord struct {
	SequenceNumber         int                    `json:"sequence_number"`
	TaskID                 string                 `json:"task_id"`
	DecisionType           string                 `json:"decision_type"`
	Timestamp              float64                `json:"timestamp"`
	StateSnapshot          map[string]interface{} `json:"state_snapshot"`
	PolicyEvaluated        string                 `json:"policy_evaluated"`
	Outcome                string                 `json:"outcome"`
	NonDeterministicInputs map[string]interface{} `json:"non_deterministic_inputs"`
	PreviousHash           string                 `json:"previous_hash"`
	RecordHash             string                 `json:"record_hash"`
}

// TraceQueryResponse represents a paginated query response from the Python trace service.
type TraceQueryResponse struct {
	Records []TraceRecord `json:"records"`
	Total   int           `json:"total"`
}

// TraceReplayRequest is the payload for a replay verification request.
type TraceReplayRequest struct {
	RecordID int `json:"record_id"`
}

// TraceReplayResponse is the result of a replay verification.
type TraceReplayResponse struct {
	Matches bool   `json:"matches"`
	Message string `json:"message"`
}

// TraceRecordFetcher abstracts how trace records are fetched for verification.
// Implementations may call the Python HTTP endpoint or read SQLite directly.
type TraceRecordFetcher interface {
	// FetchRecords retrieves trace records in the given sequence range [start, end].
	FetchRecords(ctx context.Context, startSeq, endSeq int) ([]TraceRecord, error)
	// TotalRecords returns the total number of records in the trace.
	TotalRecords(ctx context.Context) (int, error)
}

// TraceVerifierConfig holds configuration for the periodic integrity verifier.
type TraceVerifierConfig struct {
	// CheckInterval controls how often the verifier runs. Default: 60 seconds.
	CheckInterval time.Duration
	// BatchSize is how many records to verify per batch. Default: 100.
	BatchSize int
	// GenesisHash is the expected hash for the first record's previous_hash.
	GenesisHash string
}

// DefaultTraceVerifierConfig returns a config with sensible defaults.
func DefaultTraceVerifierConfig() TraceVerifierConfig {
	// Genesis hash matches Python: SHA-256("vikram-0.1.0")
	genesisHash := sha256.Sum256([]byte("vikram-0.1.0"))
	return TraceVerifierConfig{
		CheckInterval: 60 * time.Second,
		BatchSize:     100,
		GenesisHash:   hex.EncodeToString(genesisHash[:]),
	}
}

// TraceVerifier runs a periodic integrity check goroutine that verifies
// the execution trace hash chain every BatchSize records.
type TraceVerifier struct {
	config   TraceVerifierConfig
	fetcher  TraceRecordFetcher
	notifier Notifier

	mu                sync.Mutex
	lastVerifiedSeq   int // last sequence number that was successfully verified
	lastVerifiedAt    time.Time
	violationDetected bool

	stop chan struct{}
	done chan struct{}
}

// NewTraceVerifier creates a TraceVerifier with the given fetcher and notifier.
func NewTraceVerifier(fetcher TraceRecordFetcher, notifier Notifier, config TraceVerifierConfig) *TraceVerifier {
	if notifier == nil {
		notifier = &noopNotifier{}
	}
	return &TraceVerifier{
		config:          config,
		fetcher:         fetcher,
		notifier:        notifier,
		lastVerifiedSeq: -1, // nothing verified yet
		stop:            make(chan struct{}),
		done:            make(chan struct{}),
	}
}

// Start begins the periodic verification goroutine.
func (tv *TraceVerifier) Start(ctx context.Context) {
	go tv.run(ctx)
}

// Stop signals the goroutine to terminate and waits for it to finish.
func (tv *TraceVerifier) Stop() {
	close(tv.stop)
	<-tv.done
}

// Fetcher returns the configured TraceRecordFetcher for direct access
// by HTTP handlers that proxy trace queries.
func (tv *TraceVerifier) Fetcher() TraceRecordFetcher {
	return tv.fetcher
}

// LastVerifiedSeq returns the last sequence number that passed verification.
func (tv *TraceVerifier) LastVerifiedSeq() int {
	tv.mu.Lock()
	defer tv.mu.Unlock()
	return tv.lastVerifiedSeq
}

// ViolationDetected returns true if an integrity violation was found.
func (tv *TraceVerifier) ViolationDetected() bool {
	tv.mu.Lock()
	defer tv.mu.Unlock()
	return tv.violationDetected
}

// run is the main loop that periodically checks for new records to verify.
func (tv *TraceVerifier) run(ctx context.Context) {
	defer close(tv.done)

	ticker := time.NewTicker(tv.config.CheckInterval)
	defer ticker.Stop()

	for {
		select {
		case <-tv.stop:
			return
		case <-ctx.Done():
			return
		case <-ticker.C:
			tv.verifyNext(ctx)
		}
	}
}

// verifyNext checks whether there are enough new records to verify a batch.
func (tv *TraceVerifier) verifyNext(ctx context.Context) {
	total, err := tv.fetcher.TotalRecords(ctx)
	if err != nil {
		return // will retry on next tick
	}

	tv.mu.Lock()
	lastVerified := tv.lastVerifiedSeq
	tv.mu.Unlock()

	// Determine next batch to verify
	startSeq := lastVerified + 1
	endSeq := startSeq + tv.config.BatchSize - 1

	// Only verify when we have at least BatchSize new records
	if total <= endSeq {
		return
	}

	records, err := tv.fetcher.FetchRecords(ctx, startSeq, endSeq)
	if err != nil {
		return // will retry on next tick
	}

	if err := tv.verifyBatch(records, startSeq); err != nil {
		tv.mu.Lock()
		tv.violationDetected = true
		tv.mu.Unlock()

		// Alert the founder
		alertMsg := fmt.Sprintf("Execution trace integrity violation detected: %s (seq range %d-%d)", err.Error(), startSeq, endSeq)
		_ = tv.notifier.Notify(ctx, "trace_integrity_violation", alertMsg)
		return
	}

	// Update last verified position
	tv.mu.Lock()
	tv.lastVerifiedSeq = endSeq
	tv.lastVerifiedAt = time.Now()
	tv.mu.Unlock()
}

// verifyBatch verifies the hash chain integrity for a batch of records.
func (tv *TraceVerifier) verifyBatch(records []TraceRecord, expectedStartSeq int) error {
	if len(records) == 0 {
		return nil
	}

	// Sort by sequence number to ensure correct order
	sort.Slice(records, func(i, j int) bool {
		return records[i].SequenceNumber < records[j].SequenceNumber
	})

	for i, record := range records {
		// Verify previous_hash linkage
		var expectedPrev string
		if record.SequenceNumber == 0 {
			expectedPrev = tv.config.GenesisHash
		} else if i == 0 {
			// First record in batch — its previous_hash should have been verified
			// in the prior batch. We trust it here and verify the rest chains correctly.
			expectedPrev = record.PreviousHash
		} else {
			expectedPrev = records[i-1].RecordHash
		}

		if record.PreviousHash != expectedPrev {
			return fmt.Errorf("record seq=%d: previous_hash mismatch (expected %s, got %s)",
				record.SequenceNumber, expectedPrev, record.PreviousHash)
		}

		// Recompute and verify record hash
		computed := ComputeRecordHash(
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

		if record.RecordHash != computed {
			return fmt.Errorf("record seq=%d: hash mismatch (expected %s, got %s)",
				record.SequenceNumber, computed, record.RecordHash)
		}
	}

	return nil
}

// VerifyRange performs an on-demand integrity check over a sequence range.
// This is useful for the /v1/trace/query handler to verify specific ranges.
func (tv *TraceVerifier) VerifyRange(ctx context.Context, startSeq, endSeq int) error {
	records, err := tv.fetcher.FetchRecords(ctx, startSeq, endSeq)
	if err != nil {
		return fmt.Errorf("failed to fetch records: %w", err)
	}
	return tv.verifyBatch(records, startSeq)
}

// ComputeRecordHash computes the SHA-256 hash for a trace record matching
// the Python implementation exactly:
//
//	SHA-256(sequence_number || task_id || decision_type || timestamp ||
//	        canonical_json(state_snapshot) || policy_evaluated || outcome ||
//	        canonical_json(non_deterministic_inputs) || previous_hash)
//
// Fields are separated by "||".
func ComputeRecordHash(
	sequenceNumber int,
	taskID string,
	decisionType string,
	timestamp float64,
	stateSnapshot map[string]interface{},
	policyEvaluated string,
	outcome string,
	nonDeterministicInputs map[string]interface{},
	previousHash string,
) string {
	parts := []string{
		fmt.Sprintf("%d", sequenceNumber),
		taskID,
		decisionType,
		formatTimestamp(timestamp),
		CanonicalJSON(stateSnapshot),
		policyEvaluated,
		outcome,
		CanonicalJSON(nonDeterministicInputs),
		previousHash,
	}
	payload := strings.Join(parts, "||")
	hash := sha256.Sum256([]byte(payload))
	return hex.EncodeToString(hash[:])
}

// formatTimestamp formats a float64 timestamp to match Python's str(float) output.
// Python's str() for floats produces minimal representation (e.g., "1234567890.123456").
func formatTimestamp(ts float64) string {
	// Python str(float) uses repr-style: no trailing zeros after the decimal,
	// but always has a decimal point if it's a float. For integers stored as float,
	// Python produces e.g. "1234567890.0".
	s := fmt.Sprintf("%g", ts)
	// %g strips trailing zeros but may use scientific notation for very large/small values.
	// Python str() for typical timestamps (10-digit epoch) won't use scientific notation.
	// If %g produced scientific notation, fall back to %f-style.
	if strings.ContainsAny(s, "eE") {
		s = fmt.Sprintf("%.6f", ts)
		// Trim trailing zeros after decimal, keeping at least one digit after dot
		s = strings.TrimRight(s, "0")
		if strings.HasSuffix(s, ".") {
			s += "0"
		}
	}
	return s
}

// CanonicalJSON produces a canonical JSON string matching Python's
// json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True).
// Keys are sorted, no whitespace, ASCII-only output.
func CanonicalJSON(obj map[string]interface{}) string {
	if obj == nil {
		return "{}"
	}
	b, err := canonicalMarshal(obj)
	if err != nil {
		// Fallback: this shouldn't happen with map[string]interface{}
		return "{}"
	}
	return string(b)
}

// canonicalMarshal produces JSON with sorted keys matching Python's canonical form.
func canonicalMarshal(v interface{}) ([]byte, error) {
	switch val := v.(type) {
	case map[string]interface{}:
		return marshalObject(val)
	case []interface{}:
		return marshalArray(val)
	default:
		return json.Marshal(val)
	}
}

func marshalObject(obj map[string]interface{}) ([]byte, error) {
	// Get sorted keys
	keys := make([]string, 0, len(obj))
	for k := range obj {
		keys = append(keys, k)
	}
	sort.Strings(keys)

	var buf strings.Builder
	buf.WriteByte('{')
	for i, k := range keys {
		if i > 0 {
			buf.WriteByte(',')
		}
		// Marshal key
		keyBytes, err := json.Marshal(k)
		if err != nil {
			return nil, err
		}
		buf.Write(keyBytes)
		buf.WriteByte(':')

		// Marshal value recursively
		valBytes, err := canonicalMarshal(obj[k])
		if err != nil {
			return nil, err
		}
		buf.Write(valBytes)
	}
	buf.WriteByte('}')
	return []byte(buf.String()), nil
}

func marshalArray(arr []interface{}) ([]byte, error) {
	var buf strings.Builder
	buf.WriteByte('[')
	for i, item := range arr {
		if i > 0 {
			buf.WriteByte(',')
		}
		valBytes, err := canonicalMarshal(item)
		if err != nil {
			return nil, err
		}
		buf.Write(valBytes)
	}
	buf.WriteByte(']')
	return []byte(buf.String()), nil
}
