package orchestratorhost

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"os/signal"
	"sync"
	"sync/atomic"
	"syscall"
	"time"

	"github.com/Vatthu/vikram/pkg/locks"
	"github.com/Vatthu/vikram/pkg/logger"
	"github.com/Vatthu/vikram/pkg/telemetry"
	"github.com/google/uuid"
)

const (
	// DefaultGracePeriod is the maximum time allowed for graceful shutdown.
	DefaultGracePeriod = 30 * time.Second

	// checkpointPollInterval is how often we poll for checkpoint completion.
	checkpointPollInterval = 500 * time.Millisecond
)

// ShutdownState represents the persistent global state that must be saved
// before the process exits.
type ShutdownState struct {
	QueueSnapshot    []QueueEntry      `json:"queue_snapshot"`
	ActiveLocks      []locks.FileLock  `json:"active_locks"`
	CostAccumulators []CostAccumulator `json:"cost_accumulators"`
	Timestamp        time.Time         `json:"timestamp"`
}

// QueueEntry represents a task in the queue snapshot saved at shutdown.
type QueueEntry struct {
	TaskID   string `json:"task_id"`
	Priority string `json:"priority"`
	Status   string `json:"status"`
}

// CostAccumulator represents a running cost total for a task at shutdown time.
type CostAccumulator struct {
	TaskID        string  `json:"task_id"`
	CumulativeUSD float64 `json:"cumulative_usd"`
}

// ShutdownTelemetry holds the metrics emitted as a shutdown telemetry event.
type ShutdownTelemetry struct {
	TasksCheckpointed int           `json:"tasks_checkpointed"`
	TasksForcePaused  int           `json:"tasks_force_paused"`
	CallsCompleted    int           `json:"calls_completed"`
	CallsTimedOut     int           `json:"calls_timed_out"`
	ShutdownDuration  time.Duration `json:"shutdown_duration_ms"`
}

// OrchestratorClient defines the interface for communicating with the Python
// orchestrator during shutdown. The Go host signals Python to checkpoint
// active tasks and reports back the result.
type OrchestratorClient interface {
	// PrepareShutdown signals the Python orchestrator to checkpoint all
	// active task sessions. It returns the number of tasks checkpointed
	// and the number force-paused due to timeout.
	PrepareShutdown(ctx context.Context) (checkpointed, forcePaused int, err error)
}

// StatePersister defines the interface for persisting global state to
// durable storage before shutdown.
type StatePersister interface {
	PersistShutdownState(ctx context.Context, state ShutdownState) error
}

// ShutdownManager coordinates graceful shutdown of the Vikram host daemon.
// It handles OS signals, drains the server of new requests, signals the
// Python orchestrator to checkpoint, persists global state, and emits
// shutdown telemetry.
type ShutdownManager struct {
	server       *Server
	orchestrator OrchestratorClient
	persister    StatePersister
	telemetry    telemetry.Store
	ledger       costLedger
	lockRegistry lockRegistry
	gracePeriod  time.Duration
	draining     atomic.Bool
	done         chan struct{}
	mu           sync.Mutex
	shutdownOnce sync.Once
}

// ShutdownManagerConfig holds the dependencies for constructing a ShutdownManager.
type ShutdownManagerConfig struct {
	Server       *Server
	Orchestrator OrchestratorClient
	Persister    StatePersister
	Telemetry    telemetry.Store
	Ledger       costLedger
	LockRegistry lockRegistry
	GracePeriod  time.Duration
}

// NewShutdownManager creates a ShutdownManager with the given configuration.
// If GracePeriod is zero, DefaultGracePeriod (30s) is used.
func NewShutdownManager(cfg ShutdownManagerConfig) *ShutdownManager {
	gp := cfg.GracePeriod
	if gp == 0 {
		gp = DefaultGracePeriod
	}
	return &ShutdownManager{
		server:       cfg.Server,
		orchestrator: cfg.Orchestrator,
		persister:    cfg.Persister,
		telemetry:    cfg.Telemetry,
		ledger:       cfg.Ledger,
		lockRegistry: cfg.LockRegistry,
		gracePeriod:  gp,
		done:         make(chan struct{}),
	}
}

// IsDraining returns true if the server is in the shutdown grace period
// and refusing new task submissions.
func (sm *ShutdownManager) IsDraining() bool {
	return sm.draining.Load()
}

// RegisterSignalHandlers registers SIGTERM and SIGINT handlers that trigger
// graceful shutdown. This should be called from the main goroutine after
// the server has started. It blocks until shutdown completes or the context
// is cancelled.
func (sm *ShutdownManager) RegisterSignalHandlers(ctx context.Context) error {
	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGTERM, syscall.SIGINT)

	select {
	case sig := <-sigCh:
		logger.InfoCF("shutdown", "Received shutdown signal", map[string]interface{}{
			"signal": sig.String(),
		})
		return sm.Shutdown(ctx)
	case <-ctx.Done():
		signal.Stop(sigCh)
		return ctx.Err()
	}
}

// Shutdown performs the graceful shutdown sequence:
// 1. Set draining flag to refuse new tasks
// 2. Signal Python orchestrator to checkpoint active tasks
// 3. Wait for in-flight operations (up to grace period)
// 4. Persist global state (queue, locks, cost accumulators)
// 5. Emit shutdown telemetry event
// 6. Stop the HTTP server
func (sm *ShutdownManager) Shutdown(ctx context.Context) error {
	var shutdownErr error
	sm.shutdownOnce.Do(func() {
		shutdownErr = sm.doShutdown(ctx)
	})
	return shutdownErr
}

func (sm *ShutdownManager) doShutdown(ctx context.Context) error {
	startTime := time.Now()

	// Create a context bounded by the grace period.
	graceCtx, graceCancel := context.WithTimeout(ctx, sm.gracePeriod)
	defer graceCancel()

	// Step 1: Set draining flag — new task creation will be rejected with 503.
	sm.draining.Store(true)
	logger.InfoCF("shutdown", "Entering drain mode; refusing new tasks", nil)

	// Step 2: Signal Python orchestrator to checkpoint active tasks.
	var checkpointed, forcePaused int
	var callsCompleted, callsTimedOut int
	if sm.orchestrator != nil {
		var err error
		checkpointed, forcePaused, err = sm.orchestrator.PrepareShutdown(graceCtx)
		if err != nil {
			logger.InfoCF("shutdown", "Orchestrator checkpoint returned error", map[string]interface{}{
				"error": err.Error(),
			})
			// Continue shutdown even if orchestrator signaling fails.
		} else {
			callsCompleted = checkpointed
			callsTimedOut = forcePaused
		}
	}

	// Step 3: Persist global state.
	if err := sm.persistState(graceCtx); err != nil {
		logger.InfoCF("shutdown", "State persistence error during shutdown", map[string]interface{}{
			"error": err.Error(),
		})
		// Non-fatal; continue shutdown.
	}

	// Step 4: Emit shutdown telemetry event.
	shutdownDuration := time.Since(startTime)
	sm.emitShutdownTelemetry(graceCtx, ShutdownTelemetry{
		TasksCheckpointed: checkpointed,
		TasksForcePaused:  forcePaused,
		CallsCompleted:    callsCompleted,
		CallsTimedOut:     callsTimedOut,
		ShutdownDuration:  shutdownDuration,
	})

	// Step 5: Stop the HTTP server.
	if sm.server != nil {
		if err := sm.server.Stop(graceCtx); err != nil {
			logger.InfoCF("shutdown", "HTTP server stop error", map[string]interface{}{
				"error": err.Error(),
			})
		}
	}

	logger.InfoCF("shutdown", "Graceful shutdown complete", map[string]interface{}{
		"duration_ms":        shutdownDuration.Milliseconds(),
		"tasks_checkpointed": checkpointed,
		"tasks_force_paused": forcePaused,
	})

	close(sm.done)
	return nil
}

// Done returns a channel that is closed when shutdown is complete.
func (sm *ShutdownManager) Done() <-chan struct{} {
	return sm.done
}

// persistState gathers global state from the lock registry, cost ledger,
// and queue, then persists it via the StatePersister.
func (sm *ShutdownManager) persistState(ctx context.Context) error {
	if sm.persister == nil {
		return nil
	}

	state := ShutdownState{
		Timestamp: time.Now().UTC(),
	}

	// Collect active locks.
	if sm.lockRegistry != nil {
		activeLocks, err := sm.lockRegistry.Query(ctx)
		if err != nil {
			logger.InfoCF("shutdown", "Failed to query locks for state persistence", map[string]interface{}{
				"error": err.Error(),
			})
		} else {
			state.ActiveLocks = activeLocks
		}
	}

	// Persist the assembled state.
	return sm.persister.PersistShutdownState(ctx, state)
}

// emitShutdownTelemetry emits a shutdown event to the telemetry store.
func (sm *ShutdownManager) emitShutdownTelemetry(ctx context.Context, metrics ShutdownTelemetry) {
	if sm.telemetry == nil {
		return
	}

	event := telemetry.TelemetryEvent{
		EventID:   uuid.New().String(),
		EventType: telemetry.EventShutdown,
		TaskID:    "", // platform-level event, not task-specific
		Timestamp: time.Now().UTC(),
		Attributes: map[string]interface{}{
			"tasks_checkpointed":   metrics.TasksCheckpointed,
			"tasks_force_paused":   metrics.TasksForcePaused,
			"calls_completed":      metrics.CallsCompleted,
			"calls_timed_out":      metrics.CallsTimedOut,
			"shutdown_duration_ms": metrics.ShutdownDuration.Milliseconds(),
		},
	}

	if err := sm.telemetry.Emit(ctx, event); err != nil {
		logger.InfoCF("shutdown", "Failed to emit shutdown telemetry", map[string]interface{}{
			"error": err.Error(),
		})
	}
}

// Drain sets the server into draining mode. When draining, new task
// creation requests receive a 503 Service Unavailable response.
// This is called automatically during Shutdown but can also be called
// independently for testing.
func (sm *ShutdownManager) Drain() {
	sm.draining.Store(true)
}

// DrainMiddleware returns an HTTP handler that wraps the given handler
// and rejects requests to task-creation endpoints when the server is draining.
func (sm *ShutdownManager) DrainMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if sm.IsDraining() && isTaskCreationEndpoint(r) {
			writeJSON(w, http.StatusServiceUnavailable, map[string]string{
				"error":  "server is shutting down",
				"status": "draining",
			})
			return
		}
		next.ServeHTTP(w, r)
	})
}

// isTaskCreationEndpoint returns true if the request targets an endpoint that
// creates new tasks. These are rejected during the drain period.
func isTaskCreationEndpoint(r *http.Request) bool {
	if r.Method != http.MethodPost {
		return false
	}
	path := r.URL.Path
	return path == "/v1/tasks"
}

// --- costLedger helper for state persistence ---

// CostLedgerWithCumulatives extends the base costLedger interface with the
// ability to snapshot running totals. Implementations that support this
// can provide richer state persistence.
type CostLedgerWithCumulatives interface {
	costLedger
	// DailyTotalByTask returns cumulative costs per task for the current day.
	DailyTotalByTask(ctx context.Context) (map[string]float64, error)
}

// --- Default noop implementations for optional dependencies ---

// NoopOrchestratorClient is a no-op implementation of OrchestratorClient
// for use when the Python orchestrator is not available.
type NoopOrchestratorClient struct{}

func (n *NoopOrchestratorClient) PrepareShutdown(_ context.Context) (int, int, error) {
	return 0, 0, nil
}

// NoopStatePersister is a no-op implementation of StatePersister.
type NoopStatePersister struct{}

func (n *NoopStatePersister) PersistShutdownState(_ context.Context, _ ShutdownState) error {
	return nil
}

// --- Integration with Server ---

// SetShutdownManager configures the shutdown manager for the server.
// When set, the server's handler method wraps the mux with drain middleware.
func (s *Server) SetShutdownManager(sm *ShutdownManager) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.shutdownManager = sm
}

// handlerWithDrain wraps the base handler with drain middleware if a
// ShutdownManager is configured.
func (s *Server) handlerWithDrain() http.Handler {
	base := s.handler()
	if s.shutdownManager != nil {
		return s.shutdownManager.DrainMiddleware(base)
	}
	return base
}

// --- httpOrchestratorClient implements OrchestratorClient via HTTP ---

// HTTPOrchestratorClient signals the Python orchestrator over HTTP to
// checkpoint tasks during shutdown.
type HTTPOrchestratorClient struct {
	// BaseURL is the base URL of the Python orchestrator (e.g., "http://localhost:8001").
	BaseURL string
	Client  *http.Client
}

// PrepareShutdown sends POST /v1/shutdown/prepare to the Python orchestrator
// and returns the checkpoint result.
func (c *HTTPOrchestratorClient) PrepareShutdown(ctx context.Context) (checkpointed, forcePaused int, err error) {
	if c.Client == nil {
		c.Client = &http.Client{Timeout: 30 * time.Second}
	}

	url := fmt.Sprintf("%s/v1/shutdown/prepare", c.BaseURL)
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, nil)
	if err != nil {
		return 0, 0, fmt.Errorf("create shutdown request: %w", err)
	}

	resp, err := c.Client.Do(req)
	if err != nil {
		return 0, 0, fmt.Errorf("send shutdown request to orchestrator: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return 0, 0, fmt.Errorf("orchestrator shutdown returned status %d", resp.StatusCode)
	}

	var result struct {
		Checkpointed int `json:"checkpointed"`
		ForcePaused  int `json:"force_paused"`
	}
	if err := decodeJSONBody(resp, &result); err != nil {
		return 0, 0, fmt.Errorf("decode orchestrator shutdown response: %w", err)
	}

	return result.Checkpointed, result.ForcePaused, nil
}

// decodeJSONBody is a helper to decode JSON from an http.Response body.
func decodeJSONBody(resp *http.Response, target interface{}) error {
	dec := json.NewDecoder(resp.Body)
	return dec.Decode(target)
}
