package telemetry

import (
	"context"
	"sync"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// mockHealthNotifier records all notifications for test verification.
type mockHealthNotifier struct {
	mu            sync.Mutex
	notifications []healthNotification
}

type healthNotification struct {
	alertType string
	message   string
}

func (m *mockHealthNotifier) Notify(_ context.Context, alertType, message string) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.notifications = append(m.notifications, healthNotification{alertType: alertType, message: message})
	return nil
}

func (m *mockHealthNotifier) getNotifications() []healthNotification {
	m.mu.Lock()
	defer m.mu.Unlock()
	result := make([]healthNotification, len(m.notifications))
	copy(result, m.notifications)
	return result
}

// mockStore implements Store for testing the HealthMonitor without SQLite.
type mockStore struct {
	mu          sync.Mutex
	subscribers []*mockSubscription
}

type mockSubscription struct {
	filter EventType
	ch     chan TelemetryEvent
}

func newMockStore() *mockStore {
	return &mockStore{}
}

func (s *mockStore) Emit(_ context.Context, event TelemetryEvent) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	for _, sub := range s.subscribers {
		if sub.filter == "" || sub.filter == event.EventType {
			select {
			case sub.ch <- event:
			default:
			}
		}
	}
	return nil
}

func (s *mockStore) Query(_ context.Context, _ SummaryQuery) (SummaryResult, error) {
	return SummaryResult{}, nil
}

func (s *mockStore) Events(_ context.Context, _ map[string]string, _, _ int) ([]TelemetryEvent, int, error) {
	return nil, 0, nil
}

func (s *mockStore) Subscribe(_ context.Context, filter EventType) <-chan TelemetryEvent {
	s.mu.Lock()
	defer s.mu.Unlock()
	ch := make(chan TelemetryEvent, 100)
	s.subscribers = append(s.subscribers, &mockSubscription{filter: filter, ch: ch})
	return ch
}

// --- Tests ---

func TestHealthMonitor_ErrorRateThresholdDetection(t *testing.T) {
	notifier := &mockHealthNotifier{}
	store := newMockStore()
	now := time.Date(2024, 6, 15, 14, 0, 0, 0, time.UTC)

	hm := NewHealthMonitor(store,
		WithAlertConfig(AlertConfig{
			ErrorRateThreshold:   0.30,
			ErrorRateWindow:      10 * time.Minute,
			LatencyThreshold:     60 * time.Second,
			LatencyWindow:        5 * time.Minute,
			ProviderDownAttempts: 3,
		}),
		WithHealthNotifier(notifier),
		WithClock(func() time.Time { return now }),
	)

	// Feed 10 events: 4 failures, 6 successes = 40% error rate > 30% threshold.
	events := make([]TelemetryEvent, 10)
	for i := 0; i < 10; i++ {
		events[i] = TelemetryEvent{
			EventType: EventAgentCallEnd,
			Timestamp: now.Add(-time.Duration(10-i) * time.Minute / 2),
			Attributes: map[string]interface{}{
				"success":    i >= 4, // first 4 are failures
				"latency_ms": float64(1000),
				"provider":   "openai",
			},
		}
	}

	alerts := hm.Evaluate(hm.config, events)

	// Should have an error rate alert.
	var foundErrorAlert bool
	for _, a := range alerts {
		if a.Type == AlertErrorRate {
			foundErrorAlert = true
			assert.Equal(t, "critical", a.Severity)
			assert.Contains(t, a.Message, "40.0%")
			assert.Contains(t, a.Message, "30.0%")
		}
	}
	require.True(t, foundErrorAlert, "expected error rate alert to be triggered")
}

func TestHealthMonitor_ErrorRateBelowThreshold(t *testing.T) {
	store := newMockStore()
	now := time.Date(2024, 6, 15, 14, 0, 0, 0, time.UTC)

	hm := NewHealthMonitor(store,
		WithAlertConfig(AlertConfig{
			ErrorRateThreshold:   0.30,
			ErrorRateWindow:      10 * time.Minute,
			LatencyThreshold:     60 * time.Second,
			LatencyWindow:        5 * time.Minute,
			ProviderDownAttempts: 3,
		}),
		WithClock(func() time.Time { return now }),
	)

	// Feed 10 events: 2 failures, 8 successes = 20% error rate < 30% threshold.
	events := make([]TelemetryEvent, 10)
	for i := 0; i < 10; i++ {
		events[i] = TelemetryEvent{
			EventType: EventAgentCallEnd,
			Timestamp: now.Add(-time.Duration(10-i) * time.Minute / 2),
			Attributes: map[string]interface{}{
				"success":    i >= 2, // first 2 are failures
				"latency_ms": float64(1000),
				"provider":   "openai",
			},
		}
	}

	alerts := hm.Evaluate(hm.config, events)

	// Should NOT have an error rate alert.
	for _, a := range alerts {
		assert.NotEqual(t, AlertErrorRate, a.Type, "should not trigger error rate alert at 20%%")
	}
}

func TestHealthMonitor_LatencyThresholdDetection(t *testing.T) {
	notifier := &mockHealthNotifier{}
	store := newMockStore()
	now := time.Date(2024, 6, 15, 14, 0, 0, 0, time.UTC)

	hm := NewHealthMonitor(store,
		WithAlertConfig(AlertConfig{
			ErrorRateThreshold:   0.30,
			ErrorRateWindow:      10 * time.Minute,
			LatencyThreshold:     60 * time.Second, // 60000ms
			LatencyWindow:        5 * time.Minute,
			ProviderDownAttempts: 3,
		}),
		WithHealthNotifier(notifier),
		WithClock(func() time.Time { return now }),
	)

	// Feed events with latency > 60s (70000ms average).
	events := make([]TelemetryEvent, 5)
	for i := 0; i < 5; i++ {
		events[i] = TelemetryEvent{
			EventType: EventAgentCallEnd,
			Timestamp: now.Add(-time.Duration(5-i) * time.Minute / 2),
			Attributes: map[string]interface{}{
				"success":    true,
				"latency_ms": float64(70000), // 70 seconds
				"provider":   "anthropic",
			},
		}
	}

	alerts := hm.Evaluate(hm.config, events)

	var foundLatencyAlert bool
	for _, a := range alerts {
		if a.Type == AlertLatency {
			foundLatencyAlert = true
			assert.Equal(t, "warning", a.Severity)
			assert.Contains(t, a.Message, "1m10s")
			assert.Contains(t, a.Message, "exceeds threshold")
		}
	}
	require.True(t, foundLatencyAlert, "expected latency alert to be triggered")
}

func TestHealthMonitor_LatencyBelowThreshold(t *testing.T) {
	store := newMockStore()
	now := time.Date(2024, 6, 15, 14, 0, 0, 0, time.UTC)

	hm := NewHealthMonitor(store,
		WithAlertConfig(AlertConfig{
			ErrorRateThreshold:   0.30,
			ErrorRateWindow:      10 * time.Minute,
			LatencyThreshold:     60 * time.Second,
			LatencyWindow:        5 * time.Minute,
			ProviderDownAttempts: 3,
		}),
		WithClock(func() time.Time { return now }),
	)

	// Feed events with latency < 60s.
	events := make([]TelemetryEvent, 5)
	for i := 0; i < 5; i++ {
		events[i] = TelemetryEvent{
			EventType: EventAgentCallEnd,
			Timestamp: now.Add(-time.Duration(5-i) * time.Minute / 2),
			Attributes: map[string]interface{}{
				"success":    true,
				"latency_ms": float64(30000), // 30 seconds
				"provider":   "anthropic",
			},
		}
	}

	alerts := hm.Evaluate(hm.config, events)

	for _, a := range alerts {
		assert.NotEqual(t, AlertLatency, a.Type, "should not trigger latency alert at 30s")
	}
}

func TestHealthMonitor_ProviderDownDetection(t *testing.T) {
	notifier := &mockHealthNotifier{}
	store := newMockStore()
	now := time.Date(2024, 6, 15, 14, 0, 0, 0, time.UTC)

	hm := NewHealthMonitor(store,
		WithAlertConfig(AlertConfig{
			ErrorRateThreshold:   0.90, // High threshold so error rate alert doesn't fire
			ErrorRateWindow:      10 * time.Minute,
			LatencyThreshold:     120 * time.Second,
			LatencyWindow:        5 * time.Minute,
			ProviderDownAttempts: 3,
		}),
		WithHealthNotifier(notifier),
		WithClock(func() time.Time { return now }),
	)

	// Feed 3 consecutive failures from the same provider.
	events := []TelemetryEvent{
		{
			EventType: EventAgentCallEnd,
			Timestamp: now.Add(-3 * time.Minute),
			Attributes: map[string]interface{}{
				"success":    false,
				"latency_ms": float64(5000),
				"provider":   "openai",
			},
		},
		{
			EventType: EventAgentCallEnd,
			Timestamp: now.Add(-2 * time.Minute),
			Attributes: map[string]interface{}{
				"success":    false,
				"latency_ms": float64(5000),
				"provider":   "openai",
			},
		},
		{
			EventType: EventAgentCallEnd,
			Timestamp: now.Add(-1 * time.Minute),
			Attributes: map[string]interface{}{
				"success":    false,
				"latency_ms": float64(5000),
				"provider":   "openai",
			},
		},
	}

	alerts := hm.Evaluate(hm.config, events)

	var foundProviderDown bool
	for _, a := range alerts {
		if a.Type == AlertProviderDown {
			foundProviderDown = true
			assert.Equal(t, "critical", a.Severity)
			assert.Contains(t, a.Message, "openai")
			assert.Contains(t, a.Message, "3 consecutive failures")
		}
	}
	require.True(t, foundProviderDown, "expected provider-down alert")
}

func TestHealthMonitor_ProviderDownResetOnSuccess(t *testing.T) {
	store := newMockStore()
	now := time.Date(2024, 6, 15, 14, 0, 0, 0, time.UTC)

	hm := NewHealthMonitor(store,
		WithAlertConfig(AlertConfig{
			ErrorRateThreshold:   0.90,
			ErrorRateWindow:      10 * time.Minute,
			LatencyThreshold:     120 * time.Second,
			LatencyWindow:        5 * time.Minute,
			ProviderDownAttempts: 3,
		}),
		WithClock(func() time.Time { return now }),
	)

	// 2 failures, then a success, then 2 more failures — should NOT trigger provider-down.
	events := []TelemetryEvent{
		{EventType: EventAgentCallEnd, Timestamp: now.Add(-5 * time.Minute), Attributes: map[string]interface{}{"success": false, "latency_ms": float64(5000), "provider": "openai"}},
		{EventType: EventAgentCallEnd, Timestamp: now.Add(-4 * time.Minute), Attributes: map[string]interface{}{"success": false, "latency_ms": float64(5000), "provider": "openai"}},
		{EventType: EventAgentCallEnd, Timestamp: now.Add(-3 * time.Minute), Attributes: map[string]interface{}{"success": true, "latency_ms": float64(5000), "provider": "openai"}},
		{EventType: EventAgentCallEnd, Timestamp: now.Add(-2 * time.Minute), Attributes: map[string]interface{}{"success": false, "latency_ms": float64(5000), "provider": "openai"}},
		{EventType: EventAgentCallEnd, Timestamp: now.Add(-1 * time.Minute), Attributes: map[string]interface{}{"success": false, "latency_ms": float64(5000), "provider": "openai"}},
	}

	alerts := hm.Evaluate(hm.config, events)

	for _, a := range alerts {
		assert.NotEqual(t, AlertProviderDown, a.Type, "provider-down should not fire — success resets counter")
	}
}

func TestHealthMonitor_QuietHoursSuppression(t *testing.T) {
	notifier := &mockHealthNotifier{}
	store := newMockStore()
	// 23:30 UTC — within quiet hours 22–06.
	now := time.Date(2024, 6, 15, 23, 30, 0, 0, time.UTC)

	hm := NewHealthMonitor(store,
		WithAlertConfig(AlertConfig{
			ErrorRateThreshold:   0.30,
			ErrorRateWindow:      10 * time.Minute,
			LatencyThreshold:     60 * time.Second,
			LatencyWindow:        5 * time.Minute,
			ProviderDownAttempts: 3,
			QuietHoursStart:      22,
			QuietHoursEnd:        6,
		}),
		WithHealthNotifier(notifier),
		WithClock(func() time.Time { return now }),
	)

	// Feed high-latency events (warning severity — should be suppressed during quiet hours).
	for i := 0; i < 5; i++ {
		ev := TelemetryEvent{
			EventType: EventAgentCallEnd,
			Timestamp: now.Add(-time.Duration(5-i) * 30 * time.Second),
			Attributes: map[string]interface{}{
				"success":    true,
				"latency_ms": float64(70000),
				"provider":   "anthropic",
			},
		}
		hm.ingestEvent(ev)
	}

	// Evaluate — latency alert (severity=warning) should be suppressed.
	hm.evaluate()

	notifications := notifier.getNotifications()
	for _, n := range notifications {
		assert.NotEqual(t, string(AlertLatency), n.alertType,
			"latency warning should be suppressed during quiet hours")
	}
}

func TestHealthMonitor_QuietHoursDoesNotSuppressCritical(t *testing.T) {
	notifier := &mockHealthNotifier{}
	store := newMockStore()
	// 23:30 UTC — within quiet hours 22–06.
	now := time.Date(2024, 6, 15, 23, 30, 0, 0, time.UTC)

	hm := NewHealthMonitor(store,
		WithAlertConfig(AlertConfig{
			ErrorRateThreshold:   0.30,
			ErrorRateWindow:      10 * time.Minute,
			LatencyThreshold:     60 * time.Second,
			LatencyWindow:        5 * time.Minute,
			ProviderDownAttempts: 3,
			QuietHoursStart:      22,
			QuietHoursEnd:        6,
		}),
		WithHealthNotifier(notifier),
		WithClock(func() time.Time { return now }),
	)

	// Feed high error rate events (critical severity — should NOT be suppressed).
	for i := 0; i < 10; i++ {
		ev := TelemetryEvent{
			EventType: EventAgentCallEnd,
			Timestamp: now.Add(-time.Duration(10-i) * 30 * time.Second),
			Attributes: map[string]interface{}{
				"success":    i >= 4, // 40% error rate
				"latency_ms": float64(1000),
				"provider":   "openai",
			},
		}
		hm.ingestEvent(ev)
	}

	hm.evaluate()

	notifications := notifier.getNotifications()
	var foundErrorAlert bool
	for _, n := range notifications {
		if n.alertType == string(AlertErrorRate) {
			foundErrorAlert = true
		}
	}
	assert.True(t, foundErrorAlert, "critical error rate alert should fire even during quiet hours")
}

func TestHealthMonitor_MutedAlertTypesFiltering(t *testing.T) {
	notifier := &mockHealthNotifier{}
	store := newMockStore()
	now := time.Date(2024, 6, 15, 14, 0, 0, 0, time.UTC)

	hm := NewHealthMonitor(store,
		WithAlertConfig(AlertConfig{
			ErrorRateThreshold:   0.30,
			ErrorRateWindow:      10 * time.Minute,
			LatencyThreshold:     60 * time.Second,
			LatencyWindow:        5 * time.Minute,
			ProviderDownAttempts: 3,
			MutedAlertTypes:      []string{string(AlertErrorRate)},
		}),
		WithHealthNotifier(notifier),
		WithClock(func() time.Time { return now }),
	)

	// Feed events that would trigger error rate alert.
	for i := 0; i < 10; i++ {
		ev := TelemetryEvent{
			EventType: EventAgentCallEnd,
			Timestamp: now.Add(-time.Duration(10-i) * 30 * time.Second),
			Attributes: map[string]interface{}{
				"success":    i >= 4, // 40% error rate
				"latency_ms": float64(1000),
				"provider":   "openai",
			},
		}
		hm.ingestEvent(ev)
	}

	hm.evaluate()

	notifications := notifier.getNotifications()
	for _, n := range notifications {
		assert.NotEqual(t, string(AlertErrorRate), n.alertType,
			"error_rate alerts should be muted")
	}
}

func TestHealthMonitor_MutedDoesNotAffectOtherTypes(t *testing.T) {
	notifier := &mockHealthNotifier{}
	store := newMockStore()
	now := time.Date(2024, 6, 15, 14, 0, 0, 0, time.UTC)

	hm := NewHealthMonitor(store,
		WithAlertConfig(AlertConfig{
			ErrorRateThreshold:   0.30,
			ErrorRateWindow:      10 * time.Minute,
			LatencyThreshold:     60 * time.Second,
			LatencyWindow:        5 * time.Minute,
			ProviderDownAttempts: 3,
			MutedAlertTypes:      []string{string(AlertErrorRate)}, // Only error_rate is muted
		}),
		WithHealthNotifier(notifier),
		WithClock(func() time.Time { return now }),
	)

	// Feed events that would trigger latency alert (not muted).
	for i := 0; i < 5; i++ {
		ev := TelemetryEvent{
			EventType: EventAgentCallEnd,
			Timestamp: now.Add(-time.Duration(5-i) * 30 * time.Second),
			Attributes: map[string]interface{}{
				"success":    true,
				"latency_ms": float64(70000), // 70s > 60s threshold
				"provider":   "anthropic",
			},
		}
		hm.ingestEvent(ev)
	}

	hm.evaluate()

	notifications := notifier.getNotifications()
	var foundLatency bool
	for _, n := range notifications {
		if n.alertType == string(AlertLatency) {
			foundLatency = true
		}
	}
	assert.True(t, foundLatency, "latency alert should still fire — only error_rate is muted")
}

func TestHealthMonitor_IsQuietHours(t *testing.T) {
	store := newMockStore()
	hm := NewHealthMonitor(store)

	tests := []struct {
		name  string
		start int
		end   int
		hour  int
		want  bool
	}{
		{"no quiet hours (same start/end)", 0, 0, 14, false},
		{"non-wrapping in range", 9, 17, 12, true},
		{"non-wrapping before range", 9, 17, 8, false},
		{"non-wrapping after range", 9, 17, 18, false},
		{"wrapping in range (late night)", 22, 6, 23, true},
		{"wrapping in range (early morning)", 22, 6, 3, true},
		{"wrapping out of range", 22, 6, 14, false},
		{"wrapping at boundary start", 22, 6, 22, true},
		{"wrapping at boundary end", 22, 6, 6, false},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			cfg := AlertConfig{QuietHoursStart: tt.start, QuietHoursEnd: tt.end}
			now := time.Date(2024, 1, 1, tt.hour, 0, 0, 0, time.UTC)
			assert.Equal(t, tt.want, hm.IsQuietHours(cfg, now))
		})
	}
}

func TestHealthMonitor_WindowPruning(t *testing.T) {
	store := newMockStore()
	now := time.Date(2024, 6, 15, 14, 0, 0, 0, time.UTC)

	hm := NewHealthMonitor(store,
		WithAlertConfig(AlertConfig{
			ErrorRateThreshold:   0.30,
			ErrorRateWindow:      10 * time.Minute,
			LatencyThreshold:     60 * time.Second,
			LatencyWindow:        5 * time.Minute,
			ProviderDownAttempts: 3,
		}),
		WithClock(func() time.Time { return now }),
	)

	// Events far outside the window should be pruned.
	events := []TelemetryEvent{
		{
			EventType: EventAgentCallEnd,
			Timestamp: now.Add(-30 * time.Minute), // 30 min ago — outside 10 min window
			Attributes: map[string]interface{}{
				"success":    false,
				"latency_ms": float64(90000), // high latency but outside window
				"provider":   "openai",
			},
		},
		{
			EventType: EventAgentCallEnd,
			Timestamp: now.Add(-1 * time.Minute), // 1 min ago — inside window
			Attributes: map[string]interface{}{
				"success":    true,
				"latency_ms": float64(1000),
				"provider":   "openai",
			},
		},
	}

	alerts := hm.Evaluate(hm.config, events)

	// Only 1 event in window (the recent success) — error rate is 0%, no alerts.
	for _, a := range alerts {
		assert.NotEqual(t, AlertErrorRate, a.Type, "old event should be pruned")
		assert.NotEqual(t, AlertLatency, a.Type, "old latency event should be pruned")
	}
}

func TestHealthMonitor_BackgroundStartStop(t *testing.T) {
	store := newMockStore()
	hm := NewHealthMonitor(store)

	ctx := context.Background()
	hm.Start(ctx)

	// Give it a moment to start.
	time.Sleep(10 * time.Millisecond)

	// Should stop cleanly.
	hm.Stop()

	// Verify done channel is closed.
	select {
	case <-hm.done:
		// Success.
	case <-time.After(time.Second):
		t.Fatal("HealthMonitor did not stop within timeout")
	}
}
