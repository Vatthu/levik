package telemetry

import (
	"context"
	"fmt"
	"sync"
	"time"

	"github.com/google/uuid"
)

// Notifier defines the interface for delivering health alerts to the founder.
// This mirrors the same pattern used in pkg/costledger.
type Notifier interface {
	// Notify sends an alert message. The alertType identifies the category
	// (e.g., "error_rate", "latency", "provider_down").
	Notify(ctx context.Context, alertType, message string) error
}

// noopNotifier is a default no-op implementation used when no notifier is configured.
type noopNotifier struct{}

func (n *noopNotifier) Notify(_ context.Context, _, _ string) error { return nil }

// HealthMonitor runs as a background goroutine, subscribing to agent_call_end
// events and computing rolling window metrics to detect health degradation.
type HealthMonitor struct {
	config   AlertConfig
	notifier Notifier
	store    Store

	mu sync.Mutex
	// Rolling window of call outcomes for error rate computation.
	errorWindow []callOutcome
	// Rolling window of latency observations.
	latencyWindow []latencyObservation
	// Per-provider consecutive failure counters.
	providerFailures map[string]int

	// Clock function for testing (defaults to time.Now).
	nowFunc func() time.Time

	stop chan struct{}
	done chan struct{}
}

// callOutcome records whether a single agent call succeeded or failed.
type callOutcome struct {
	timestamp time.Time
	success   bool
}

// latencyObservation records the latency of a single agent call.
type latencyObservation struct {
	timestamp time.Time
	latencyMS float64
}

// HealthMonitorOption configures the HealthMonitor.
type HealthMonitorOption func(*HealthMonitor)

// WithAlertConfig sets the alert configuration.
func WithAlertConfig(cfg AlertConfig) HealthMonitorOption {
	return func(hm *HealthMonitor) {
		hm.config = cfg
	}
}

// WithNotifier sets the notifier for alert delivery.
func WithHealthNotifier(n Notifier) HealthMonitorOption {
	return func(hm *HealthMonitor) {
		if n != nil {
			hm.notifier = n
		}
	}
}

// WithClock sets a custom clock function for testing.
func WithClock(fn func() time.Time) HealthMonitorOption {
	return func(hm *HealthMonitor) {
		hm.nowFunc = fn
	}
}

// NewHealthMonitor creates a HealthMonitor that subscribes to the given Store's
// event stream and evaluates health thresholds periodically.
func NewHealthMonitor(store Store, opts ...HealthMonitorOption) *HealthMonitor {
	hm := &HealthMonitor{
		config:           DefaultAlertConfig(),
		notifier:         &noopNotifier{},
		store:            store,
		errorWindow:      make([]callOutcome, 0, 256),
		latencyWindow:    make([]latencyObservation, 0, 256),
		providerFailures: make(map[string]int),
		nowFunc:          time.Now,
		stop:             make(chan struct{}),
		done:             make(chan struct{}),
	}
	for _, opt := range opts {
		opt(hm)
	}
	return hm
}

// Start begins the background goroutine that subscribes to events and
// periodically evaluates health thresholds.
func (hm *HealthMonitor) Start(ctx context.Context) {
	go hm.run(ctx)
}

// Stop signals the background goroutine to terminate and waits for it to finish.
func (hm *HealthMonitor) Stop() {
	close(hm.stop)
	<-hm.done
}

// run is the main background loop.
func (hm *HealthMonitor) run(ctx context.Context) {
	defer close(hm.done)

	subCtx, cancel := context.WithCancel(ctx)
	defer cancel()

	// Subscribe to agent_call_end events.
	events := hm.store.Subscribe(subCtx, EventAgentCallEnd)

	// Periodic threshold evaluation every 30 seconds.
	ticker := time.NewTicker(30 * time.Second)
	defer ticker.Stop()

	for {
		select {
		case <-hm.stop:
			return
		case ev, ok := <-events:
			if !ok {
				return
			}
			hm.ingestEvent(ev)
		case <-ticker.C:
			hm.evaluate()
		}
	}
}

// ingestEvent processes a single agent_call_end event, updating rolling windows
// and per-provider failure tracking.
func (hm *HealthMonitor) ingestEvent(ev TelemetryEvent) {
	hm.mu.Lock()
	defer hm.mu.Unlock()

	now := ev.Timestamp
	if now.IsZero() {
		now = hm.nowFunc()
	}

	// Determine success.
	success := true
	if s, ok := ev.Attributes["success"]; ok {
		switch v := s.(type) {
		case bool:
			success = v
		case float64:
			success = v != 0
		case int:
			success = v != 0
		}
	}

	// Record outcome for error rate window.
	hm.errorWindow = append(hm.errorWindow, callOutcome{
		timestamp: now,
		success:   success,
	})

	// Record latency if present.
	if latency, ok := ev.Attributes["latency_ms"]; ok {
		var latencyMS float64
		switch v := latency.(type) {
		case float64:
			latencyMS = v
		case int:
			latencyMS = float64(v)
		case int64:
			latencyMS = float64(v)
		}
		if latencyMS > 0 {
			hm.latencyWindow = append(hm.latencyWindow, latencyObservation{
				timestamp: now,
				latencyMS: latencyMS,
			})
		}
	}

	// Track per-provider consecutive failures.
	provider := ""
	if p, ok := ev.Attributes["provider"]; ok {
		if ps, ok := p.(string); ok {
			provider = ps
		}
	}
	if provider != "" {
		if success {
			hm.providerFailures[provider] = 0
		} else {
			hm.providerFailures[provider]++
			// Check for provider-down immediately on reaching threshold.
			if hm.providerFailures[provider] >= hm.config.ProviderDownAttempts {
				hm.fireProviderDownAlert(provider)
			}
		}
	}
}

// evaluate checks rolling window metrics against configured thresholds
// and fires alerts as appropriate.
func (hm *HealthMonitor) evaluate() {
	hm.mu.Lock()
	defer hm.mu.Unlock()

	now := hm.nowFunc()

	// Prune and compute error rate.
	hm.pruneErrorWindow(now)
	if errorRate := hm.computeErrorRate(); errorRate > hm.config.ErrorRateThreshold {
		hm.fireAlert(Alert{
			ID:        uuid.New().String(),
			Type:      AlertErrorRate,
			Message:   fmt.Sprintf("Platform error rate %.1f%% exceeds threshold %.1f%% over %v window", errorRate*100, hm.config.ErrorRateThreshold*100, hm.config.ErrorRateWindow),
			Severity:  "critical",
			Timestamp: now,
			Details: map[string]interface{}{
				"error_rate":  errorRate,
				"threshold":   hm.config.ErrorRateThreshold,
				"window":      hm.config.ErrorRateWindow.String(),
				"sample_size": len(hm.errorWindow),
			},
		})
	}

	// Prune and compute average latency.
	hm.pruneLatencyWindow(now)
	if avgLatency := hm.computeAvgLatency(); avgLatency > hm.config.LatencyThreshold {
		hm.fireAlert(Alert{
			ID:        uuid.New().String(),
			Type:      AlertLatency,
			Message:   fmt.Sprintf("Average latency %v exceeds threshold %v over %v window", avgLatency.Round(time.Millisecond), hm.config.LatencyThreshold, hm.config.LatencyWindow),
			Severity:  "warning",
			Timestamp: now,
			Details: map[string]interface{}{
				"avg_latency_ms": avgLatency.Milliseconds(),
				"threshold_ms":   hm.config.LatencyThreshold.Milliseconds(),
				"window":         hm.config.LatencyWindow.String(),
				"sample_size":    len(hm.latencyWindow),
			},
		})
	}
}

// pruneErrorWindow removes entries older than the error rate window.
func (hm *HealthMonitor) pruneErrorWindow(now time.Time) {
	cutoff := now.Add(-hm.config.ErrorRateWindow)
	start := 0
	for start < len(hm.errorWindow) && hm.errorWindow[start].timestamp.Before(cutoff) {
		start++
	}
	if start > 0 {
		hm.errorWindow = hm.errorWindow[start:]
	}
}

// pruneLatencyWindow removes entries older than the latency window.
func (hm *HealthMonitor) pruneLatencyWindow(now time.Time) {
	cutoff := now.Add(-hm.config.LatencyWindow)
	start := 0
	for start < len(hm.latencyWindow) && hm.latencyWindow[start].timestamp.Before(cutoff) {
		start++
	}
	if start > 0 {
		hm.latencyWindow = hm.latencyWindow[start:]
	}
}

// computeErrorRate returns the error rate (0.0–1.0) from the current error window.
// Returns 0 if there are no observations.
func (hm *HealthMonitor) computeErrorRate() float64 {
	if len(hm.errorWindow) == 0 {
		return 0
	}
	failures := 0
	for _, o := range hm.errorWindow {
		if !o.success {
			failures++
		}
	}
	return float64(failures) / float64(len(hm.errorWindow))
}

// computeAvgLatency returns the average latency from the current latency window.
// Returns 0 if there are no observations.
func (hm *HealthMonitor) computeAvgLatency() time.Duration {
	if len(hm.latencyWindow) == 0 {
		return 0
	}
	var total float64
	for _, o := range hm.latencyWindow {
		total += o.latencyMS
	}
	avgMS := total / float64(len(hm.latencyWindow))
	return time.Duration(avgMS) * time.Millisecond
}

// fireProviderDownAlert sends a provider-down alert if not muted/quiet.
func (hm *HealthMonitor) fireProviderDownAlert(provider string) {
	now := hm.nowFunc()
	hm.fireAlert(Alert{
		ID:        uuid.New().String(),
		Type:      AlertProviderDown,
		Message:   fmt.Sprintf("Provider %q unreachable after %d consecutive failures", provider, hm.config.ProviderDownAttempts),
		Severity:  "critical",
		Timestamp: now,
		Details: map[string]interface{}{
			"provider":             provider,
			"consecutive_failures": hm.providerFailures[provider],
		},
	})
}

// fireAlert delivers an alert via the notifier, respecting quiet hours and muted types.
func (hm *HealthMonitor) fireAlert(alert Alert) {
	// Check if this alert type is muted.
	if hm.isMuted(alert.Type) {
		return
	}

	// Check quiet hours for non-critical alerts.
	if alert.Severity != "critical" && hm.IsQuietHours(hm.config, alert.Timestamp) {
		return
	}

	// Deliver via notifier (best-effort).
	_ = hm.notifier.Notify(context.Background(), string(alert.Type), alert.Message)
}

// isMuted returns true if the given alert type is in the muted list.
func (hm *HealthMonitor) isMuted(alertType AlertType) bool {
	for _, muted := range hm.config.MutedAlertTypes {
		if muted == string(alertType) {
			return true
		}
	}
	return false
}

// IsQuietHours returns true if the given time falls within the configured quiet hours window.
func (hm *HealthMonitor) IsQuietHours(config AlertConfig, now time.Time) bool {
	if config.QuietHoursStart == config.QuietHoursEnd {
		return false // No quiet hours configured.
	}

	hour := now.UTC().Hour()

	if config.QuietHoursStart < config.QuietHoursEnd {
		// Simple range: e.g., 22–06 means start=22, end=6 wraps around midnight.
		// But this branch handles non-wrapping: e.g., start=9, end=17.
		return hour >= config.QuietHoursStart && hour < config.QuietHoursEnd
	}

	// Wrapping range: e.g., start=22, end=6 means quiet from 22:00 to 06:00.
	return hour >= config.QuietHoursStart || hour < config.QuietHoursEnd
}

// Evaluate checks current event state against alert thresholds and returns
// any triggered alerts. This implements the Alerter interface's Evaluate method
// for batch evaluation scenarios.
func (hm *HealthMonitor) Evaluate(config AlertConfig, events []TelemetryEvent) []Alert {
	hm.mu.Lock()
	defer hm.mu.Unlock()

	// Temporarily override config for this evaluation.
	origConfig := hm.config
	hm.config = config

	// Reset state for fresh evaluation.
	hm.errorWindow = hm.errorWindow[:0]
	hm.latencyWindow = hm.latencyWindow[:0]
	hm.providerFailures = make(map[string]int)

	// Ingest all provided events (without lock since we already hold it).
	for _, ev := range events {
		now := ev.Timestamp
		if now.IsZero() {
			now = hm.nowFunc()
		}

		success := true
		if s, ok := ev.Attributes["success"]; ok {
			switch v := s.(type) {
			case bool:
				success = v
			case float64:
				success = v != 0
			case int:
				success = v != 0
			}
		}

		hm.errorWindow = append(hm.errorWindow, callOutcome{
			timestamp: now,
			success:   success,
		})

		if latency, ok := ev.Attributes["latency_ms"]; ok {
			var latencyMS float64
			switch v := latency.(type) {
			case float64:
				latencyMS = v
			case int:
				latencyMS = float64(v)
			case int64:
				latencyMS = float64(v)
			}
			if latencyMS > 0 {
				hm.latencyWindow = append(hm.latencyWindow, latencyObservation{
					timestamp: now,
					latencyMS: latencyMS,
				})
			}
		}

		provider := ""
		if p, ok := ev.Attributes["provider"]; ok {
			if ps, ok := p.(string); ok {
				provider = ps
			}
		}
		if provider != "" {
			if success {
				hm.providerFailures[provider] = 0
			} else {
				hm.providerFailures[provider]++
			}
		}
	}

	// Collect alerts.
	var alerts []Alert
	now := hm.nowFunc()

	// Error rate check.
	hm.pruneErrorWindow(now)
	if errorRate := hm.computeErrorRate(); errorRate > config.ErrorRateThreshold {
		alerts = append(alerts, Alert{
			ID:        uuid.New().String(),
			Type:      AlertErrorRate,
			Message:   fmt.Sprintf("Platform error rate %.1f%% exceeds threshold %.1f%%", errorRate*100, config.ErrorRateThreshold*100),
			Severity:  "critical",
			Timestamp: now,
			Details: map[string]interface{}{
				"error_rate": errorRate,
				"threshold":  config.ErrorRateThreshold,
			},
		})
	}

	// Latency check.
	hm.pruneLatencyWindow(now)
	if avgLatency := hm.computeAvgLatency(); avgLatency > config.LatencyThreshold {
		alerts = append(alerts, Alert{
			ID:        uuid.New().String(),
			Type:      AlertLatency,
			Message:   fmt.Sprintf("Average latency %v exceeds threshold %v", avgLatency.Round(time.Millisecond), config.LatencyThreshold),
			Severity:  "warning",
			Timestamp: now,
			Details: map[string]interface{}{
				"avg_latency_ms": avgLatency.Milliseconds(),
				"threshold_ms":   config.LatencyThreshold.Milliseconds(),
			},
		})
	}

	// Provider-down checks.
	for provider, failures := range hm.providerFailures {
		if failures >= config.ProviderDownAttempts {
			alerts = append(alerts, Alert{
				ID:        uuid.New().String(),
				Type:      AlertProviderDown,
				Message:   fmt.Sprintf("Provider %q unreachable after %d consecutive failures", provider, failures),
				Severity:  "critical",
				Timestamp: now,
				Details: map[string]interface{}{
					"provider":             provider,
					"consecutive_failures": failures,
				},
			})
		}
	}

	// Restore original config.
	hm.config = origConfig

	return alerts
}
