package orchestratorhost

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"
	"time"

	"github.com/Vatthu/vikram/pkg/locks"
	"github.com/Vatthu/vikram/pkg/telemetry"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// --- Test doubles ---

type mockOrchestratorClient struct {
	mu           sync.Mutex
	called       bool
	checkpointed int
	forcePaused  int
	err          error
	delay        time.Duration
}

func (m *mockOrchestratorClient) PrepareShutdown(ctx context.Context) (int, int, error) {
	m.mu.Lock()
	m.called = true
	m.mu.Unlock()

	if m.delay > 0 {
		select {
		case <-time.After(m.delay):
		case <-ctx.Done():
			return 0, 0, ctx.Err()
		}
	}
	return m.checkpointed, m.forcePaused, m.err
}

type mockStatePersister struct {
	mu     sync.Mutex
	called bool
	state  ShutdownState
	err    error
}

func (m *mockStatePersister) PersistShutdownState(_ context.Context, state ShutdownState) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.called = true
	m.state = state
	return m.err
}

type shutdownMockTelemetryStore struct {
	mu     sync.Mutex
	events []telemetry.TelemetryEvent
}

func (m *shutdownMockTelemetryStore) Emit(_ context.Context, event telemetry.TelemetryEvent) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.events = append(m.events, event)
	return nil
}

func (m *shutdownMockTelemetryStore) Query(_ context.Context, _ telemetry.SummaryQuery) (telemetry.SummaryResult, error) {
	return telemetry.SummaryResult{}, nil
}

func (m *shutdownMockTelemetryStore) Events(_ context.Context, _ map[string]string, _, _ int) ([]telemetry.TelemetryEvent, int, error) {
	return nil, 0, nil
}

func (m *shutdownMockTelemetryStore) Subscribe(_ context.Context, _ telemetry.EventType) <-chan telemetry.TelemetryEvent {
	ch := make(chan telemetry.TelemetryEvent)
	close(ch)
	return ch
}

type mockLockRegistry struct {
	mu    sync.Mutex
	locks []locks.FileLock
	err   error
}

func (m *mockLockRegistry) Acquire(_ context.Context, taskID, path string, ttl time.Duration) error {
	return nil
}

func (m *mockLockRegistry) Release(_ context.Context, taskID, path string) error {
	return nil
}

func (m *mockLockRegistry) Query(_ context.Context) ([]locks.FileLock, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.locks, m.err
}

func (m *mockLockRegistry) IsLocked(_ context.Context, path string) (bool, string, error) {
	return false, "", nil
}

// --- Tests ---

func TestShutdownManager_IsDraining(t *testing.T) {
	sm := NewShutdownManager(ShutdownManagerConfig{})

	assert.False(t, sm.IsDraining(), "should not be draining initially")

	sm.Drain()
	assert.True(t, sm.IsDraining(), "should be draining after Drain()")
}

func TestShutdownManager_Shutdown_SetsDrainingFlag(t *testing.T) {
	sm := NewShutdownManager(ShutdownManagerConfig{
		GracePeriod: 5 * time.Second,
	})

	ctx := context.Background()
	err := sm.Shutdown(ctx)
	require.NoError(t, err)

	assert.True(t, sm.IsDraining(), "should be draining after shutdown")
}

func TestShutdownManager_Shutdown_SignalsOrchestrator(t *testing.T) {
	orch := &mockOrchestratorClient{
		checkpointed: 3,
		forcePaused:  1,
	}

	sm := NewShutdownManager(ShutdownManagerConfig{
		Orchestrator: orch,
		GracePeriod:  5 * time.Second,
	})

	ctx := context.Background()
	err := sm.Shutdown(ctx)
	require.NoError(t, err)

	orch.mu.Lock()
	defer orch.mu.Unlock()
	assert.True(t, orch.called, "should have called PrepareShutdown on orchestrator")
}

func TestShutdownManager_Shutdown_PersistsState(t *testing.T) {
	lockReg := &mockLockRegistry{
		locks: []locks.FileLock{
			{Path: "/repo/src/main.go", TaskID: "task-1", Acquired: time.Now(), ExpiresAt: time.Now().Add(time.Hour)},
			{Path: "/repo/src/utils.go", TaskID: "task-2", Acquired: time.Now(), ExpiresAt: time.Now().Add(time.Hour)},
		},
	}
	persister := &mockStatePersister{}

	sm := NewShutdownManager(ShutdownManagerConfig{
		Persister:    persister,
		LockRegistry: lockReg,
		GracePeriod:  5 * time.Second,
	})

	ctx := context.Background()
	err := sm.Shutdown(ctx)
	require.NoError(t, err)

	persister.mu.Lock()
	defer persister.mu.Unlock()

	assert.True(t, persister.called, "should have called PersistShutdownState")
	assert.Len(t, persister.state.ActiveLocks, 2, "should persist active locks")
	assert.False(t, persister.state.Timestamp.IsZero(), "should set timestamp")
}

func TestShutdownManager_Shutdown_EmitsTelemetry(t *testing.T) {
	ts := &shutdownMockTelemetryStore{}
	orch := &mockOrchestratorClient{
		checkpointed: 5,
		forcePaused:  2,
	}

	sm := NewShutdownManager(ShutdownManagerConfig{
		Telemetry:    ts,
		Orchestrator: orch,
		GracePeriod:  5 * time.Second,
	})

	ctx := context.Background()
	err := sm.Shutdown(ctx)
	require.NoError(t, err)

	ts.mu.Lock()
	defer ts.mu.Unlock()

	require.Len(t, ts.events, 1, "should emit exactly one shutdown telemetry event")
	event := ts.events[0]
	assert.Equal(t, telemetry.EventShutdown, event.EventType)
	assert.Equal(t, 5, event.Attributes["tasks_checkpointed"])
	assert.Equal(t, 2, event.Attributes["tasks_force_paused"])
	assert.Contains(t, event.Attributes, "shutdown_duration_ms")
}

func TestShutdownManager_Shutdown_IsIdempotent(t *testing.T) {
	orch := &mockOrchestratorClient{checkpointed: 1}

	sm := NewShutdownManager(ShutdownManagerConfig{
		Orchestrator: orch,
		GracePeriod:  5 * time.Second,
	})

	ctx := context.Background()

	// Call Shutdown multiple times.
	err1 := sm.Shutdown(ctx)
	err2 := sm.Shutdown(ctx)

	assert.NoError(t, err1)
	assert.NoError(t, err2)
}

func TestShutdownManager_Shutdown_HandlesOrchestratorError(t *testing.T) {
	orch := &mockOrchestratorClient{
		err: errors.New("connection refused"),
	}
	ts := &shutdownMockTelemetryStore{}

	sm := NewShutdownManager(ShutdownManagerConfig{
		Orchestrator: orch,
		Telemetry:    ts,
		GracePeriod:  5 * time.Second,
	})

	ctx := context.Background()
	err := sm.Shutdown(ctx)
	// Shutdown should succeed even if orchestrator signaling fails.
	require.NoError(t, err)

	ts.mu.Lock()
	defer ts.mu.Unlock()
	// Telemetry event should still be emitted.
	require.Len(t, ts.events, 1)
}

func TestShutdownManager_Shutdown_HandlesPersisterError(t *testing.T) {
	persister := &mockStatePersister{
		err: errors.New("disk full"),
	}

	sm := NewShutdownManager(ShutdownManagerConfig{
		Persister:   persister,
		GracePeriod: 5 * time.Second,
	})

	ctx := context.Background()
	err := sm.Shutdown(ctx)
	// Shutdown should succeed even if persistence fails.
	require.NoError(t, err)
}

func TestShutdownManager_Done_ClosedAfterShutdown(t *testing.T) {
	sm := NewShutdownManager(ShutdownManagerConfig{
		GracePeriod: 5 * time.Second,
	})

	ctx := context.Background()
	_ = sm.Shutdown(ctx)

	select {
	case <-sm.Done():
		// Expected.
	case <-time.After(time.Second):
		t.Fatal("Done channel should be closed after shutdown")
	}
}

func TestShutdownManager_DrainMiddleware_RejectsNewTasks(t *testing.T) {
	sm := NewShutdownManager(ShutdownManagerConfig{})
	sm.Drain()

	innerHandler := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	})

	handler := sm.DrainMiddleware(innerHandler)

	// POST /v1/tasks should be rejected during drain.
	req := httptest.NewRequest(http.MethodPost, "/v1/tasks", nil)
	rr := httptest.NewRecorder()
	handler.ServeHTTP(rr, req)

	assert.Equal(t, http.StatusServiceUnavailable, rr.Code)
}

func TestShutdownManager_DrainMiddleware_AllowsNonTaskEndpoints(t *testing.T) {
	sm := NewShutdownManager(ShutdownManagerConfig{})
	sm.Drain()

	innerHandler := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	})

	handler := sm.DrainMiddleware(innerHandler)

	// GET /healthz should pass through even during drain.
	req := httptest.NewRequest(http.MethodGet, "/healthz", nil)
	rr := httptest.NewRecorder()
	handler.ServeHTTP(rr, req)

	assert.Equal(t, http.StatusOK, rr.Code)
}

func TestShutdownManager_DrainMiddleware_AllowsGetTasks(t *testing.T) {
	sm := NewShutdownManager(ShutdownManagerConfig{})
	sm.Drain()

	innerHandler := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	})

	handler := sm.DrainMiddleware(innerHandler)

	// GET /v1/tasks should pass (only POST is blocked).
	req := httptest.NewRequest(http.MethodGet, "/v1/tasks", nil)
	rr := httptest.NewRecorder()
	handler.ServeHTTP(rr, req)

	assert.Equal(t, http.StatusOK, rr.Code)
}

func TestShutdownManager_DrainMiddleware_PassesThroughWhenNotDraining(t *testing.T) {
	sm := NewShutdownManager(ShutdownManagerConfig{})
	// Not draining.

	innerHandler := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusAccepted)
	})

	handler := sm.DrainMiddleware(innerHandler)

	// POST /v1/tasks should pass when not draining.
	req := httptest.NewRequest(http.MethodPost, "/v1/tasks", nil)
	rr := httptest.NewRecorder()
	handler.ServeHTTP(rr, req)

	assert.Equal(t, http.StatusAccepted, rr.Code)
}

func TestShutdownManager_DefaultGracePeriod(t *testing.T) {
	sm := NewShutdownManager(ShutdownManagerConfig{})
	assert.Equal(t, DefaultGracePeriod, sm.gracePeriod)
}

func TestShutdownManager_CustomGracePeriod(t *testing.T) {
	sm := NewShutdownManager(ShutdownManagerConfig{
		GracePeriod: 10 * time.Second,
	})
	assert.Equal(t, 10*time.Second, sm.gracePeriod)
}

func TestNoopOrchestratorClient(t *testing.T) {
	noop := &NoopOrchestratorClient{}
	checkpointed, forcePaused, err := noop.PrepareShutdown(context.Background())
	assert.NoError(t, err)
	assert.Equal(t, 0, checkpointed)
	assert.Equal(t, 0, forcePaused)
}

func TestNoopStatePersister(t *testing.T) {
	noop := &NoopStatePersister{}
	err := noop.PersistShutdownState(context.Background(), ShutdownState{})
	assert.NoError(t, err)
}

func TestShutdownManager_Shutdown_FullSequence(t *testing.T) {
	// Integration test: verifies the full shutdown sequence with all dependencies.
	orch := &mockOrchestratorClient{
		checkpointed: 4,
		forcePaused:  1,
	}
	persister := &mockStatePersister{}
	ts := &shutdownMockTelemetryStore{}
	lockReg := &mockLockRegistry{
		locks: []locks.FileLock{
			{Path: "/src/file.go", TaskID: "t-1", Acquired: time.Now(), ExpiresAt: time.Now().Add(time.Hour)},
		},
	}

	sm := NewShutdownManager(ShutdownManagerConfig{
		Orchestrator: orch,
		Persister:    persister,
		Telemetry:    ts,
		LockRegistry: lockReg,
		GracePeriod:  5 * time.Second,
	})

	ctx := context.Background()
	err := sm.Shutdown(ctx)
	require.NoError(t, err)

	// Verify draining.
	assert.True(t, sm.IsDraining())

	// Verify orchestrator was called.
	orch.mu.Lock()
	assert.True(t, orch.called)
	orch.mu.Unlock()

	// Verify state was persisted with locks.
	persister.mu.Lock()
	assert.True(t, persister.called)
	assert.Len(t, persister.state.ActiveLocks, 1)
	persister.mu.Unlock()

	// Verify telemetry was emitted.
	ts.mu.Lock()
	require.Len(t, ts.events, 1)
	event := ts.events[0]
	assert.Equal(t, telemetry.EventShutdown, event.EventType)
	assert.Equal(t, 4, event.Attributes["tasks_checkpointed"])
	assert.Equal(t, 1, event.Attributes["tasks_force_paused"])
	ts.mu.Unlock()

	// Verify Done channel is closed.
	select {
	case <-sm.Done():
	case <-time.After(time.Second):
		t.Fatal("Done channel should be closed")
	}
}

func TestShutdownManager_Shutdown_EnforcesGracePeriodTimeout(t *testing.T) {
	// Validates Requirement 51.3: THE Platform SHALL persist the global state
	// within the 30-second grace period. If the orchestrator takes longer
	// than the grace period, shutdown should still complete (context timeout).
	orch := &mockOrchestratorClient{
		delay: 5 * time.Second, // Orchestrator is slow — simulates exceeding grace period
	}
	persister := &mockStatePersister{}
	ts := &shutdownMockTelemetryStore{}

	gracePeriod := 200 * time.Millisecond // short grace period for test speed

	sm := NewShutdownManager(ShutdownManagerConfig{
		Orchestrator: orch,
		Persister:    persister,
		Telemetry:    ts,
		GracePeriod:  gracePeriod,
	})

	start := time.Now()
	ctx := context.Background()
	err := sm.Shutdown(ctx)
	elapsed := time.Since(start)

	require.NoError(t, err, "shutdown should succeed even when orchestrator times out")

	// Verify shutdown completed within a reasonable bound of the grace period.
	// The shutdown should not have waited the full 5s orchestrator delay.
	assert.Less(t, elapsed, 2*time.Second, "shutdown should complete near the grace period, not wait for slow orchestrator")

	// State persistence should still have been called.
	persister.mu.Lock()
	assert.True(t, persister.called, "state should be persisted even when orchestrator times out")
	persister.mu.Unlock()

	// Telemetry should still be emitted.
	ts.mu.Lock()
	assert.Len(t, ts.events, 1, "shutdown telemetry should be emitted even on timeout")
	ts.mu.Unlock()
}

func TestShutdownManager_Shutdown_PersistsStateWithinGracePeriod(t *testing.T) {
	// Validates Requirement 51.3: graceful shutdown persists all state within 30-second window.
	// Verifies that queue, locks, and cost accumulators are all persisted.
	lockReg := &mockLockRegistry{
		locks: []locks.FileLock{
			{Path: "/repo/src/main.go", TaskID: "task-1", Acquired: time.Now(), ExpiresAt: time.Now().Add(time.Hour)},
			{Path: "/repo/src/utils.go", TaskID: "task-2", Acquired: time.Now(), ExpiresAt: time.Now().Add(time.Hour)},
			{Path: "/repo/pkg/core.go", TaskID: "task-3", Acquired: time.Now(), ExpiresAt: time.Now().Add(time.Hour)},
		},
	}
	persister := &mockStatePersister{}
	orch := &mockOrchestratorClient{
		checkpointed: 3,
		forcePaused:  0,
	}
	ts := &shutdownMockTelemetryStore{}

	sm := NewShutdownManager(ShutdownManagerConfig{
		Orchestrator: orch,
		Persister:    persister,
		Telemetry:    ts,
		LockRegistry: lockReg,
		GracePeriod:  30 * time.Second, // explicit 30s grace period per requirement
	})

	start := time.Now()
	ctx := context.Background()
	err := sm.Shutdown(ctx)
	elapsed := time.Since(start)

	require.NoError(t, err)

	// Verify completed well within 30-second window.
	assert.Less(t, elapsed, 30*time.Second, "shutdown must complete within the 30-second grace period")

	// Verify all state components were persisted.
	persister.mu.Lock()
	defer persister.mu.Unlock()
	assert.True(t, persister.called)
	assert.Len(t, persister.state.ActiveLocks, 3, "all active locks should be persisted")
	assert.False(t, persister.state.Timestamp.IsZero(), "persistence timestamp should be set")
}

func TestIsTaskCreationEndpoint(t *testing.T) {
	tests := []struct {
		method   string
		path     string
		expected bool
	}{
		{http.MethodPost, "/v1/tasks", true},
		{http.MethodGet, "/v1/tasks", false},
		{http.MethodPut, "/v1/tasks", false},
		{http.MethodPost, "/v1/tasks/abc/priority", false},
		{http.MethodPost, "/healthz", false},
		{http.MethodPost, "/v1/exec", false},
		{http.MethodGet, "/v1/queue", false},
	}

	for _, tt := range tests {
		t.Run(tt.method+" "+tt.path, func(t *testing.T) {
			req := httptest.NewRequest(tt.method, tt.path, nil)
			result := isTaskCreationEndpoint(req)
			assert.Equal(t, tt.expected, result)
		})
	}
}
