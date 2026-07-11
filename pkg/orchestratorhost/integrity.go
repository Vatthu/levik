// integrity.go implements periodic integrity checksum verification on the
// Execution_Trace. The verifier runs as a background goroutine and validates
// the hash chain every N records (default: 100), alerting the founder if
// trace integrity is violated.
//
// This wraps the telemetry.TraceVerifier with a higher-level interface that
// integrates with the orchestratorhost lifecycle (start/stop) and provides
// the configurable-interval checksum verification required by Requirement 55.4.
//
// Requirements: 55.4
package orchestratorhost

import (
	"context"
	"fmt"
	"sync"
	"time"

	"github.com/Vatthu/vikram/pkg/logger"
	"github.com/Vatthu/vikram/pkg/telemetry"
)

// IntegrityVerifierConfig holds configuration for the periodic integrity verifier.
type IntegrityVerifierConfig struct {
	// CheckInterval is how often the verifier polls for new records. Default: 30s.
	CheckInterval time.Duration
	// BatchSize is the number of records that triggers a verification pass. Default: 100.
	BatchSize int
	// Notifier receives alerts when integrity violations are detected.
	Notifier IntegrityNotifier
	// Fetcher provides access to trace records for verification.
	Fetcher telemetry.TraceRecordFetcher
}

// IntegrityNotifier delivers integrity violation alerts to the founder.
type IntegrityNotifier interface {
	Notify(ctx context.Context, alertType, message string) error
}

// IntegrityVerifier periodically verifies the Execution_Trace hash chain
// integrity. It checks every BatchSize records and alerts if violations
// are detected.
type IntegrityVerifier struct {
	config  IntegrityVerifierConfig
	inner   *telemetry.TraceVerifier
	fetcher telemetry.TraceRecordFetcher

	mu               sync.Mutex
	lastCheckedCount int
	violationCount   int
	lastCheckTime    time.Time
	running          bool

	stopCh chan struct{}
	doneCh chan struct{}
}

// DefaultIntegrityVerifierConfig returns the default configuration.
func DefaultIntegrityVerifierConfig() IntegrityVerifierConfig {
	return IntegrityVerifierConfig{
		CheckInterval: 30 * time.Second,
		BatchSize:     100,
	}
}

// NewIntegrityVerifier creates a new IntegrityVerifier. It wraps the existing
// telemetry.TraceVerifier with additional lifecycle management and record-count
// based triggering.
func NewIntegrityVerifier(cfg IntegrityVerifierConfig) (*IntegrityVerifier, error) {
	if cfg.Fetcher == nil {
		return nil, ErrIntegrityFetcherRequired
	}
	if cfg.CheckInterval <= 0 {
		cfg.CheckInterval = 30 * time.Second
	}
	if cfg.BatchSize <= 0 {
		cfg.BatchSize = 100
	}

	// Create a no-op notifier if none provided.
	notifier := cfg.Notifier
	if notifier == nil {
		notifier = &noopIntegrityNotifier{}
	}

	// Build the underlying telemetry.TraceVerifier with matching config.
	tvConfig := telemetry.TraceVerifierConfig{
		CheckInterval: cfg.CheckInterval,
		BatchSize:     cfg.BatchSize,
		GenesisHash:   telemetry.DefaultTraceVerifierConfig().GenesisHash,
	}

	// Create a wrapper notifier that bridges the telemetry.Notifier interface.
	bridgeNotifier := &integrityNotifierBridge{inner: notifier}
	inner := telemetry.NewTraceVerifier(cfg.Fetcher, bridgeNotifier, tvConfig)

	return &IntegrityVerifier{
		config:  cfg,
		inner:   inner,
		fetcher: cfg.Fetcher,
		stopCh:  make(chan struct{}),
		doneCh:  make(chan struct{}),
	}, nil
}

// Start begins periodic integrity verification in a background goroutine.
func (iv *IntegrityVerifier) Start(ctx context.Context) {
	iv.mu.Lock()
	if iv.running {
		iv.mu.Unlock()
		return
	}
	iv.running = true
	iv.mu.Unlock()

	go iv.run(ctx)
}

// Stop halts the periodic integrity verification.
func (iv *IntegrityVerifier) Stop() {
	iv.mu.Lock()
	if !iv.running {
		iv.mu.Unlock()
		return
	}
	iv.mu.Unlock()

	close(iv.stopCh)
	<-iv.doneCh
}

// LastCheckTime returns the timestamp of the last integrity check.
func (iv *IntegrityVerifier) LastCheckTime() time.Time {
	iv.mu.Lock()
	defer iv.mu.Unlock()
	return iv.lastCheckTime
}

// ViolationCount returns the number of integrity violations detected.
func (iv *IntegrityVerifier) ViolationCount() int {
	iv.mu.Lock()
	defer iv.mu.Unlock()
	return iv.violationCount
}

// IsRunning returns whether the verifier is actively running.
func (iv *IntegrityVerifier) IsRunning() bool {
	iv.mu.Lock()
	defer iv.mu.Unlock()
	return iv.running
}

// run is the main loop that periodically checks for new records and triggers
// verification every BatchSize records.
func (iv *IntegrityVerifier) run(ctx context.Context) {
	defer func() {
		iv.mu.Lock()
		iv.running = false
		iv.mu.Unlock()
		close(iv.doneCh)
	}()

	ticker := time.NewTicker(iv.config.CheckInterval)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			return
		case <-iv.stopCh:
			return
		case <-ticker.C:
			iv.checkAndVerify(ctx)
		}
	}
}

// checkAndVerify queries the total record count and triggers verification
// when enough new records have accumulated since the last check.
func (iv *IntegrityVerifier) checkAndVerify(ctx context.Context) {
	total, err := iv.fetcher.TotalRecords(ctx)
	if err != nil {
		logger.Warn(fmt.Sprintf("Integrity verifier: failed to get total records: %v", err))
		return
	}

	iv.mu.Lock()
	lastChecked := iv.lastCheckedCount
	iv.mu.Unlock()

	// Only verify when enough new records have accumulated.
	newRecords := total - lastChecked
	if newRecords < iv.config.BatchSize {
		return
	}

	// Verify in batches of BatchSize.
	startSeq := lastChecked
	endSeq := startSeq + iv.config.BatchSize

	err = iv.inner.VerifyRange(ctx, startSeq, endSeq)

	iv.mu.Lock()
	iv.lastCheckTime = time.Now()
	if err != nil {
		iv.violationCount++
		iv.mu.Unlock()

		logger.Error(fmt.Sprintf("Integrity violation detected in trace records %d-%d: %v", startSeq, endSeq, err))

		// Alert the founder.
		if iv.config.Notifier != nil {
			alertMsg := fmt.Sprintf("Execution trace integrity violation detected: records %d through %d have inconsistent hashes. The trace may have been tampered with.", startSeq, endSeq)
			_ = iv.config.Notifier.Notify(ctx, "integrity_violation", alertMsg)
		}
	} else {
		iv.lastCheckedCount = endSeq
		iv.mu.Unlock()
		logger.Info(fmt.Sprintf("Integrity check passed for trace records %d-%d", startSeq, endSeq))
	}
}

// integrityNotifierBridge adapts IntegrityNotifier to the telemetry.Notifier interface.
type integrityNotifierBridge struct {
	inner IntegrityNotifier
}

func (b *integrityNotifierBridge) Notify(ctx context.Context, alertType, message string) error {
	return b.inner.Notify(ctx, alertType, message)
}

// noopIntegrityNotifier is a no-op implementation for when no notifier is configured.
type noopIntegrityNotifier struct{}

func (n *noopIntegrityNotifier) Notify(_ context.Context, _, _ string) error { return nil }

// ErrIntegrityFetcherRequired is returned when no fetcher is provided.
var ErrIntegrityFetcherRequired = errIntegrityFetcherRequired("integrity: trace record fetcher is required")

type errIntegrityFetcherRequired string

func (e errIntegrityFetcherRequired) Error() string { return string(e) }
