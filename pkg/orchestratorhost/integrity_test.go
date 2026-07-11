package orchestratorhost

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"sync"
	"testing"
	"time"

	"github.com/Vatthu/vikram/pkg/telemetry"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// mockTraceRecordFetcher simulates trace record storage for tests.
type mockTraceRecordFetcher struct {
	mu      sync.Mutex
	records []telemetry.TraceRecord
}

func newMockFetcher() *mockTraceRecordFetcher {
	return &mockTraceRecordFetcher{}
}

func (f *mockTraceRecordFetcher) FetchRecords(_ context.Context, startSeq, endSeq int) ([]telemetry.TraceRecord, error) {
	f.mu.Lock()
	defer f.mu.Unlock()

	var result []telemetry.TraceRecord
	for _, r := range f.records {
		if r.SequenceNumber >= startSeq && r.SequenceNumber <= endSeq {
			result = append(result, r)
		}
	}
	return result, nil
}

func (f *mockTraceRecordFetcher) TotalRecords(_ context.Context) (int, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	return len(f.records), nil
}

func (f *mockTraceRecordFetcher) addValidChain(count int) {
	f.mu.Lock()
	defer f.mu.Unlock()

	genesisHash := sha256.Sum256([]byte("vikram-0.1.0"))
	prevHash := hex.EncodeToString(genesisHash[:])

	for i := 0; i < count; i++ {
		state := map[string]interface{}{"phase": "planning"}
		ndInputs := map[string]interface{}{}

		hash := telemetry.ComputeRecordHash(
			i,
			fmt.Sprintf("task-%d", i),
			"phase_transition",
			float64(1700000000+i*10),
			state,
			"default_policy",
			"proceed",
			ndInputs,
			prevHash,
		)

		f.records = append(f.records, telemetry.TraceRecord{
			SequenceNumber:         i,
			TaskID:                 fmt.Sprintf("task-%d", i),
			DecisionType:           "phase_transition",
			Timestamp:              float64(1700000000 + i*10),
			StateSnapshot:          state,
			PolicyEvaluated:        "default_policy",
			Outcome:                "proceed",
			NonDeterministicInputs: ndInputs,
			PreviousHash:           prevHash,
			RecordHash:             hash,
		})

		prevHash = hash
	}
}

// mockIntegrityNotifier captures notifications for test assertions.
type mockIntegrityNotifier struct {
	mu     sync.Mutex
	alerts []string
}

func (n *mockIntegrityNotifier) Notify(_ context.Context, alertType, message string) error {
	n.mu.Lock()
	defer n.mu.Unlock()
	n.alerts = append(n.alerts, alertType+": "+message)
	return nil
}

func (n *mockIntegrityNotifier) getAlerts() []string {
	n.mu.Lock()
	defer n.mu.Unlock()
	copied := make([]string, len(n.alerts))
	copy(copied, n.alerts)
	return copied
}

func TestIntegrityVerifier_RequiresFetcher(t *testing.T) {
	_, err := NewIntegrityVerifier(IntegrityVerifierConfig{})
	assert.Error(t, err)
	assert.Equal(t, ErrIntegrityFetcherRequired, err)
}

func TestIntegrityVerifier_DefaultConfig(t *testing.T) {
	cfg := DefaultIntegrityVerifierConfig()
	assert.Equal(t, 30*time.Second, cfg.CheckInterval)
	assert.Equal(t, 100, cfg.BatchSize)
}

func TestIntegrityVerifier_ValidChain(t *testing.T) {
	fetcher := newMockFetcher()
	fetcher.addValidChain(100)

	notifier := &mockIntegrityNotifier{}

	iv, err := NewIntegrityVerifier(IntegrityVerifierConfig{
		CheckInterval: 50 * time.Millisecond,
		BatchSize:     100,
		Fetcher:       fetcher,
		Notifier:      notifier,
	})
	require.NoError(t, err)

	ctx, cancel := context.WithTimeout(context.Background(), 500*time.Millisecond)
	defer cancel()

	iv.Start(ctx)
	defer iv.Stop()

	// Wait for the verifier to run at least once.
	time.Sleep(200 * time.Millisecond)

	// No violations should be detected.
	assert.Equal(t, 0, iv.ViolationCount())
	assert.Empty(t, notifier.getAlerts())
}

func TestIntegrityVerifier_TamperedRecord(t *testing.T) {
	fetcher := newMockFetcher()
	fetcher.addValidChain(100)

	// Tamper with a record in the middle.
	fetcher.mu.Lock()
	fetcher.records[50].Outcome = "tampered_outcome"
	fetcher.mu.Unlock()

	notifier := &mockIntegrityNotifier{}

	iv, err := NewIntegrityVerifier(IntegrityVerifierConfig{
		CheckInterval: 50 * time.Millisecond,
		BatchSize:     100,
		Fetcher:       fetcher,
		Notifier:      notifier,
	})
	require.NoError(t, err)

	ctx, cancel := context.WithTimeout(context.Background(), 500*time.Millisecond)
	defer cancel()

	iv.Start(ctx)
	defer iv.Stop()

	// Wait for the verifier to detect the tampering.
	time.Sleep(200 * time.Millisecond)

	// Violation should be detected.
	assert.Greater(t, iv.ViolationCount(), 0)
}

func TestIntegrityVerifier_InsufficientRecords(t *testing.T) {
	fetcher := newMockFetcher()
	fetcher.addValidChain(50) // Only 50 records, batch size is 100.

	notifier := &mockIntegrityNotifier{}

	iv, err := NewIntegrityVerifier(IntegrityVerifierConfig{
		CheckInterval: 50 * time.Millisecond,
		BatchSize:     100,
		Fetcher:       fetcher,
		Notifier:      notifier,
	})
	require.NoError(t, err)

	ctx, cancel := context.WithTimeout(context.Background(), 300*time.Millisecond)
	defer cancel()

	iv.Start(ctx)
	defer iv.Stop()

	// Wait for a couple of ticks.
	time.Sleep(150 * time.Millisecond)

	// Should not verify (not enough records).
	assert.Equal(t, 0, iv.ViolationCount())
	assert.True(t, iv.LastCheckTime().IsZero())
}

func TestIntegrityVerifier_StartStop(t *testing.T) {
	fetcher := newMockFetcher()

	iv, err := NewIntegrityVerifier(IntegrityVerifierConfig{
		CheckInterval: 100 * time.Millisecond,
		BatchSize:     10,
		Fetcher:       fetcher,
	})
	require.NoError(t, err)

	assert.False(t, iv.IsRunning())

	ctx := context.Background()
	iv.Start(ctx)
	assert.True(t, iv.IsRunning())

	// Double start should be a no-op.
	iv.Start(ctx)
	assert.True(t, iv.IsRunning())

	iv.Stop()
	// Give the goroutine time to exit.
	time.Sleep(50 * time.Millisecond)
	assert.False(t, iv.IsRunning())
}
