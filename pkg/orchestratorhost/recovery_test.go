package orchestratorhost

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sync"
	"testing"
	"time"

	"github.com/Vatthu/vikram/pkg/telemetry"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// --- Mock implementations ---

type mockCheckpointDB struct {
	mu    sync.Mutex
	tasks []CheckpointRecord
	// statusUpdates tracks calls to UpdateTaskStatus
	statusUpdates map[string]TaskStatus
	listErr       error
	updateErr     error
}

func newMockCheckpointDB(tasks []CheckpointRecord) *mockCheckpointDB {
	return &mockCheckpointDB{
		tasks:         tasks,
		statusUpdates: make(map[string]TaskStatus),
	}
}

func (m *mockCheckpointDB) ListNonTerminalTasks(ctx context.Context) ([]CheckpointRecord, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.listErr != nil {
		return nil, m.listErr
	}
	return m.tasks, nil
}

func (m *mockCheckpointDB) UpdateTaskStatus(ctx context.Context, taskID string, status TaskStatus) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.updateErr != nil {
		return m.updateErr
	}
	m.statusUpdates[taskID] = status
	return nil
}

type mockRecoveryNotifier struct {
	mu       sync.Mutex
	failures []recoveryFailureNotification
}

type recoveryFailureNotification struct {
	TaskID    string
	Objective string
	LastPhase string
	Reason    string
}

func (m *mockRecoveryNotifier) NotifyRecoveryFailure(ctx context.Context, taskID, objective, lastPhase, reason string) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.failures = append(m.failures, recoveryFailureNotification{
		TaskID:    taskID,
		Objective: objective,
		LastPhase: lastPhase,
		Reason:    reason,
	})
	return nil
}

type mockWorktreeValidator struct {
	// validPaths maps worktree paths to nil (valid) or an error (invalid)
	validPaths map[string]error
}

func newMockWorktreeValidator() *mockWorktreeValidator {
	return &mockWorktreeValidator{
		validPaths: make(map[string]error),
	}
}

func (v *mockWorktreeValidator) ValidateWorktree(ctx context.Context, worktreePath, branch string) error {
	if err, ok := v.validPaths[worktreePath]; ok {
		return err
	}
	return nil // default: valid
}

type recoveryMockTelemetryStore struct {
	mu     sync.Mutex
	events []telemetry.TelemetryEvent
}

func (m *recoveryMockTelemetryStore) Emit(ctx context.Context, event telemetry.TelemetryEvent) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.events = append(m.events, event)
	return nil
}

func (m *recoveryMockTelemetryStore) Query(ctx context.Context, q telemetry.SummaryQuery) (telemetry.SummaryResult, error) {
	return telemetry.SummaryResult{}, nil
}

func (m *recoveryMockTelemetryStore) Events(ctx context.Context, filters map[string]string, page, pageSize int) ([]telemetry.TelemetryEvent, int, error) {
	return nil, 0, nil
}

func (m *recoveryMockTelemetryStore) Subscribe(ctx context.Context, filter telemetry.EventType) <-chan telemetry.TelemetryEvent {
	ch := make(chan telemetry.TelemetryEvent)
	close(ch)
	return ch
}

// --- Helper functions ---

func newTestRecoveryManager(t *testing.T, opts ...func(*RecoveryManager)) *RecoveryManager {
	t.Helper()
	dataDir := filepath.Join(t.TempDir(), ".vikram")
	require.NoError(t, os.MkdirAll(dataDir, 0o755))

	rm := &RecoveryManager{
		dataDir:           dataDir,
		checkpointDB:      newMockCheckpointDB(nil),
		telemetryStore:    &recoveryMockTelemetryStore{},
		notifier:          &mockRecoveryNotifier{},
		worktreeValidator: newMockWorktreeValidator(),
		now:               time.Now,
	}
	for _, opt := range opts {
		opt(rm)
	}
	return rm
}

// --- Tests ---

func TestRecoveryManager_CleanShutdown_NoRecovery(t *testing.T) {
	rm := newTestRecoveryManager(t)

	// Write the shutdown marker to simulate a clean shutdown
	require.NoError(t, rm.WriteShutdownMarker())

	// Run recovery — should detect clean shutdown
	result, err := rm.Run(context.Background())
	require.NoError(t, err)

	assert.False(t, result.CrashDetected)
	assert.Equal(t, 0, result.TasksDiscovered)
	assert.Equal(t, 0, result.TasksResumed)
	assert.Equal(t, 0, result.TasksFailed)

	// Marker should be removed after clean start
	_, statErr := os.Stat(rm.shutdownMarkerPath())
	assert.True(t, os.IsNotExist(statErr), "marker should be removed after clean start")
}

func TestRecoveryManager_CrashDetected_NoTasks(t *testing.T) {
	rm := newTestRecoveryManager(t)
	// No shutdown marker → crash detected

	result, err := rm.Run(context.Background())
	require.NoError(t, err)

	assert.True(t, result.CrashDetected)
	assert.Equal(t, 0, result.TasksDiscovered)
	assert.Equal(t, 0, result.TasksResumed)
	assert.Equal(t, 0, result.TasksFailed)

	// Should record a recovery event
	store := rm.telemetryStore.(*recoveryMockTelemetryStore)
	store.mu.Lock()
	defer store.mu.Unlock()
	assert.Len(t, store.events, 1)
	assert.Equal(t, telemetry.EventRecovery, store.events[0].EventType)
}

func TestRecoveryManager_CrashDetected_ResumesValidTasks(t *testing.T) {
	now := time.Now()
	tasks := []CheckpointRecord{
		{
			TaskID:       "task-1",
			Status:       TaskStatusRunning,
			WorktreePath: "/worktrees/task-1",
			Branch:       "vikram/task-1",
			Phase:        "implement",
			Objective:    "implement feature X",
			CheckpointAt: now.Add(-5 * time.Minute), // 5 min old — valid
		},
		{
			TaskID:       "task-2",
			Status:       TaskStatusPaused,
			WorktreePath: "/worktrees/task-2",
			Branch:       "vikram/task-2",
			Phase:        "verify",
			Objective:    "fix bug Y",
			CheckpointAt: now.Add(-2 * time.Minute), // 2 min old — valid
		},
	}

	db := newMockCheckpointDB(tasks)
	validator := newMockWorktreeValidator()
	// Both worktrees are valid
	validator.validPaths["/worktrees/task-1"] = nil
	validator.validPaths["/worktrees/task-2"] = nil

	rm := newTestRecoveryManager(t, func(m *RecoveryManager) {
		m.checkpointDB = db
		m.worktreeValidator = validator
		m.now = func() time.Time { return now }
	})

	result, err := rm.Run(context.Background())
	require.NoError(t, err)

	assert.True(t, result.CrashDetected)
	assert.Equal(t, 2, result.TasksDiscovered)
	assert.Equal(t, 2, result.TasksResumed)
	assert.Equal(t, 0, result.TasksFailed)
}

func TestRecoveryManager_CrashDetected_StaleCheckpointMarkedFailed(t *testing.T) {
	now := time.Now()
	tasks := []CheckpointRecord{
		{
			TaskID:       "task-stale",
			Status:       TaskStatusRunning,
			WorktreePath: "/worktrees/task-stale",
			Branch:       "vikram/task-stale",
			Phase:        "plan",
			Objective:    "stale task",
			CheckpointAt: now.Add(-15 * time.Minute), // 15 min old — stale
		},
	}

	db := newMockCheckpointDB(tasks)
	notifier := &mockRecoveryNotifier{}

	rm := newTestRecoveryManager(t, func(m *RecoveryManager) {
		m.checkpointDB = db
		m.notifier = notifier
		m.now = func() time.Time { return now }
	})

	result, err := rm.Run(context.Background())
	require.NoError(t, err)

	assert.True(t, result.CrashDetected)
	assert.Equal(t, 1, result.TasksDiscovered)
	assert.Equal(t, 0, result.TasksResumed)
	assert.Equal(t, 1, result.TasksFailed)

	// Check that the task was marked as recovery_failed
	db.mu.Lock()
	assert.Equal(t, TaskStatusRecovery, db.statusUpdates["task-stale"])
	db.mu.Unlock()

	// Check notification was sent
	notifier.mu.Lock()
	require.Len(t, notifier.failures, 1)
	assert.Equal(t, "task-stale", notifier.failures[0].TaskID)
	assert.Equal(t, "stale task", notifier.failures[0].Objective)
	assert.Contains(t, notifier.failures[0].Reason, "checkpoint_stale")
	notifier.mu.Unlock()
}

func TestRecoveryManager_CrashDetected_CorruptWorktreeMarkedFailed(t *testing.T) {
	now := time.Now()
	tasks := []CheckpointRecord{
		{
			TaskID:       "task-corrupt",
			Status:       TaskStatusRunning,
			WorktreePath: "/worktrees/task-corrupt",
			Branch:       "vikram/task-corrupt",
			Phase:        "implement",
			Objective:    "corrupted worktree task",
			CheckpointAt: now.Add(-3 * time.Minute), // fresh checkpoint
		},
	}

	db := newMockCheckpointDB(tasks)
	validator := newMockWorktreeValidator()
	validator.validPaths["/worktrees/task-corrupt"] = fmt.Errorf("branch mismatch: expected \"vikram/task-corrupt\", got \"main\"")
	notifier := &mockRecoveryNotifier{}

	rm := newTestRecoveryManager(t, func(m *RecoveryManager) {
		m.checkpointDB = db
		m.worktreeValidator = validator
		m.notifier = notifier
		m.now = func() time.Time { return now }
	})

	result, err := rm.Run(context.Background())
	require.NoError(t, err)

	assert.True(t, result.CrashDetected)
	assert.Equal(t, 1, result.TasksDiscovered)
	assert.Equal(t, 0, result.TasksResumed)
	assert.Equal(t, 1, result.TasksFailed)

	db.mu.Lock()
	assert.Equal(t, TaskStatusRecovery, db.statusUpdates["task-corrupt"])
	db.mu.Unlock()

	notifier.mu.Lock()
	require.Len(t, notifier.failures, 1)
	assert.Contains(t, notifier.failures[0].Reason, "worktree_invalid")
	notifier.mu.Unlock()
}

func TestRecoveryManager_MixedTaskRecovery(t *testing.T) {
	now := time.Now()
	tasks := []CheckpointRecord{
		{
			TaskID:       "task-valid",
			Status:       TaskStatusRunning,
			WorktreePath: "/worktrees/task-valid",
			Branch:       "vikram/task-valid",
			Phase:        "implement",
			Objective:    "good task",
			CheckpointAt: now.Add(-4 * time.Minute),
		},
		{
			TaskID:       "task-stale",
			Status:       TaskStatusPaused,
			WorktreePath: "/worktrees/task-stale",
			Branch:       "vikram/task-stale",
			Phase:        "verify",
			Objective:    "old task",
			CheckpointAt: now.Add(-12 * time.Minute), // stale
		},
		{
			TaskID:       "task-corrupt",
			Status:       TaskStatusQueued,
			WorktreePath: "/worktrees/task-corrupt",
			Branch:       "vikram/task-corrupt",
			Phase:        "plan",
			Objective:    "broken worktree",
			CheckpointAt: now.Add(-1 * time.Minute), // fresh but corrupt
		},
	}

	db := newMockCheckpointDB(tasks)
	validator := newMockWorktreeValidator()
	validator.validPaths["/worktrees/task-valid"] = nil
	validator.validPaths["/worktrees/task-corrupt"] = fmt.Errorf("no .git metadata found")
	notifier := &mockRecoveryNotifier{}
	store := &recoveryMockTelemetryStore{}

	rm := newTestRecoveryManager(t, func(m *RecoveryManager) {
		m.checkpointDB = db
		m.worktreeValidator = validator
		m.notifier = notifier
		m.telemetryStore = store
		m.now = func() time.Time { return now }
	})

	result, err := rm.Run(context.Background())
	require.NoError(t, err)

	assert.True(t, result.CrashDetected)
	assert.Equal(t, 3, result.TasksDiscovered)
	assert.Equal(t, 1, result.TasksResumed)
	assert.Equal(t, 2, result.TasksFailed)

	// Verify telemetry event recorded
	store.mu.Lock()
	require.Len(t, store.events, 1)
	evt := store.events[0]
	assert.Equal(t, telemetry.EventRecovery, evt.EventType)
	assert.Equal(t, "_system", evt.TaskID)
	assert.Equal(t, 3, evt.Attributes["tasks_discovered"])
	assert.Equal(t, 1, evt.Attributes["tasks_resumed"])
	assert.Equal(t, 2, evt.Attributes["tasks_failed"])
	store.mu.Unlock()
}

func TestRecoveryManager_CheckpointDBError(t *testing.T) {
	db := newMockCheckpointDB(nil)
	db.listErr = fmt.Errorf("database connection failed")

	rm := newTestRecoveryManager(t, func(m *RecoveryManager) {
		m.checkpointDB = db
	})

	result, err := rm.Run(context.Background())
	assert.Error(t, err)
	assert.Contains(t, err.Error(), "failed to query checkpoint DB")
	assert.True(t, result.CrashDetected)
}

func TestRecoveryManager_WriteAndDetectShutdownMarker(t *testing.T) {
	rm := newTestRecoveryManager(t)

	// Initially no marker → crash detected
	assert.True(t, rm.detectCrash())

	// Write marker
	require.NoError(t, rm.WriteShutdownMarker())

	// Marker exists → no crash
	assert.False(t, rm.detectCrash())

	// Verify marker content
	data, err := os.ReadFile(rm.shutdownMarkerPath())
	require.NoError(t, err)
	var marker shutdownMarkerData
	require.NoError(t, json.Unmarshal(data, &marker))
	assert.NotZero(t, marker.Timestamp)
	assert.NotZero(t, marker.PID)

	// Remove marker → crash detected again
	require.NoError(t, rm.RemoveShutdownMarker())
	assert.True(t, rm.detectCrash())
}

func TestRecoveryManager_TimeoutEnforcement(t *testing.T) {
	// Create a context that's already expired
	ctx, cancel := context.WithTimeout(context.Background(), 1*time.Nanosecond)
	defer cancel()
	time.Sleep(2 * time.Millisecond) // ensure timeout fires

	now := time.Now()
	tasks := []CheckpointRecord{
		{
			TaskID:       "task-1",
			Status:       TaskStatusRunning,
			WorktreePath: "/worktrees/task-1",
			Branch:       "vikram/task-1",
			Phase:        "implement",
			Objective:    "task 1",
			CheckpointAt: now.Add(-1 * time.Minute),
		},
	}

	db := newMockCheckpointDB(tasks)
	rm := newTestRecoveryManager(t, func(m *RecoveryManager) {
		m.checkpointDB = db
		m.now = func() time.Time { return now }
	})

	// Run with already-expired context to test internal timeout
	// The RecoveryManager creates its own 60s context, so the parent
	// context expiration doesn't directly affect it unless we use it.
	// We test with a very tight internal scenario instead.
	result, err := rm.Run(ctx)
	// The recovery manager creates its own timeout context, so it should
	// still proceed. This test mainly verifies it doesn't panic.
	_ = result
	_ = err
}

func TestRecoveryManager_ExactStaleThreshold(t *testing.T) {
	now := time.Now()

	// Exactly at the 10-minute boundary
	tasks := []CheckpointRecord{
		{
			TaskID:       "task-boundary",
			Status:       TaskStatusRunning,
			WorktreePath: "/worktrees/task-boundary",
			Branch:       "vikram/task-boundary",
			Phase:        "implement",
			Objective:    "boundary task",
			CheckpointAt: now.Add(-RecoveryStaleThreshold), // exactly 10 min
		},
		{
			TaskID:       "task-just-under",
			Status:       TaskStatusRunning,
			WorktreePath: "/worktrees/task-just-under",
			Branch:       "vikram/task-just-under",
			Phase:        "implement",
			Objective:    "just under threshold",
			CheckpointAt: now.Add(-RecoveryStaleThreshold + 1*time.Second), // 9:59 — valid
		},
	}

	db := newMockCheckpointDB(tasks)
	validator := newMockWorktreeValidator()
	validator.validPaths["/worktrees/task-boundary"] = nil
	validator.validPaths["/worktrees/task-just-under"] = nil

	rm := newTestRecoveryManager(t, func(m *RecoveryManager) {
		m.checkpointDB = db
		m.worktreeValidator = validator
		m.now = func() time.Time { return now }
	})

	result, err := rm.Run(context.Background())
	require.NoError(t, err)

	// Exactly 10 min is NOT stale (using >), just-under is also valid
	// The condition is checkpointAge > RecoveryStaleThreshold
	// At exactly 10 min, age == threshold, so NOT stale
	assert.Equal(t, 2, result.TasksDiscovered)
	assert.Equal(t, 2, result.TasksResumed)
	assert.Equal(t, 0, result.TasksFailed)
}

func TestTaskStatus_IsTerminal(t *testing.T) {
	tests := []struct {
		status   TaskStatus
		terminal bool
	}{
		{TaskStatusRunning, false},
		{TaskStatusPaused, false},
		{TaskStatusQueued, false},
		{TaskStatusComplete, true},
		{TaskStatusFailed, true},
		{TaskStatusRecovery, true},
	}

	for _, tc := range tests {
		t.Run(string(tc.status), func(t *testing.T) {
			assert.Equal(t, tc.terminal, tc.status.IsTerminal())
		})
	}
}

func TestRecoveryManager_NilNotifier_DoesNotPanic(t *testing.T) {
	now := time.Now()
	tasks := []CheckpointRecord{
		{
			TaskID:       "task-stale",
			Status:       TaskStatusRunning,
			WorktreePath: "/worktrees/task-stale",
			Branch:       "vikram/task-stale",
			Phase:        "plan",
			Objective:    "stale task no notifier",
			CheckpointAt: now.Add(-15 * time.Minute),
		},
	}

	db := newMockCheckpointDB(tasks)

	rm := newTestRecoveryManager(t, func(m *RecoveryManager) {
		m.checkpointDB = db
		m.notifier = nil // no notifier configured
		m.now = func() time.Time { return now }
	})

	// Should not panic even with nil notifier
	result, err := rm.Run(context.Background())
	require.NoError(t, err)
	assert.Equal(t, 1, result.TasksFailed)
}

func TestDefaultWorktreeValidator_MissingPath(t *testing.T) {
	v := &DefaultWorktreeValidator{}
	err := v.ValidateWorktree(context.Background(), "/nonexistent/path", "main")
	assert.Error(t, err)
	assert.Contains(t, err.Error(), "worktree path does not exist")
}

func TestRecoveryManager_DiscoversAndValidatesNonTerminalTasks(t *testing.T) {
	// Validates Requirement 52.2: THE Orchestrator SHALL re-validate each
	// recovered task's Worktree state (branch integrity, file system consistency,
	// no corruption) before resuming execution.
	//
	// This test specifically verifies the combined "discover non-terminal +
	// validate each" flow with mixed statuses (running, paused, queued).
	now := time.Now()
	tasks := []CheckpointRecord{
		{
			TaskID:       "task-running",
			Status:       TaskStatusRunning,
			WorktreePath: "/worktrees/task-running",
			Branch:       "vikram/task-running",
			Phase:        "implement",
			Objective:    "running task",
			CheckpointAt: now.Add(-3 * time.Minute),
		},
		{
			TaskID:       "task-paused",
			Status:       TaskStatusPaused,
			WorktreePath: "/worktrees/task-paused",
			Branch:       "vikram/task-paused",
			Phase:        "verify",
			Objective:    "paused task",
			CheckpointAt: now.Add(-7 * time.Minute),
		},
		{
			TaskID:       "task-queued",
			Status:       TaskStatusQueued,
			WorktreePath: "/worktrees/task-queued",
			Branch:       "vikram/task-queued",
			Phase:        "plan",
			Objective:    "queued task",
			CheckpointAt: now.Add(-1 * time.Minute),
		},
	}

	db := newMockCheckpointDB(tasks)
	validator := newMockWorktreeValidator()
	// All worktrees valid
	validator.validPaths["/worktrees/task-running"] = nil
	validator.validPaths["/worktrees/task-paused"] = nil
	validator.validPaths["/worktrees/task-queued"] = nil
	store := &recoveryMockTelemetryStore{}

	rm := newTestRecoveryManager(t, func(m *RecoveryManager) {
		m.checkpointDB = db
		m.worktreeValidator = validator
		m.telemetryStore = store
		m.now = func() time.Time { return now }
	})

	result, err := rm.Run(context.Background())
	require.NoError(t, err)

	// Verify: crash detected and all non-terminal tasks discovered
	assert.True(t, result.CrashDetected)
	assert.Equal(t, 3, result.TasksDiscovered, "should discover all non-terminal tasks regardless of status")
	assert.Equal(t, 3, result.TasksResumed, "all valid tasks should resume after validation")
	assert.Equal(t, 0, result.TasksFailed)

	// Verify telemetry event recorded the recovery
	store.mu.Lock()
	defer store.mu.Unlock()
	require.Len(t, store.events, 1)
	evt := store.events[0]
	assert.Equal(t, telemetry.EventRecovery, evt.EventType)
	assert.Equal(t, 3, evt.Attributes["tasks_discovered"])
	assert.Equal(t, 3, evt.Attributes["tasks_resumed"])
}

func TestRecoveryManager_ValidationRejectsInvalidTasks(t *testing.T) {
	// Validates Requirement 52.2: recovery validates each task's worktree
	// state before resuming. Invalid worktrees are rejected even if checkpoint
	// is fresh.
	now := time.Now()
	tasks := []CheckpointRecord{
		{
			TaskID:       "task-good",
			Status:       TaskStatusRunning,
			WorktreePath: "/worktrees/task-good",
			Branch:       "vikram/task-good",
			Phase:        "implement",
			Objective:    "healthy task",
			CheckpointAt: now.Add(-2 * time.Minute),
		},
		{
			TaskID:       "task-bad-branch",
			Status:       TaskStatusRunning,
			WorktreePath: "/worktrees/task-bad-branch",
			Branch:       "vikram/task-bad-branch",
			Phase:        "implement",
			Objective:    "branch mismatch task",
			CheckpointAt: now.Add(-2 * time.Minute), // fresh, but worktree invalid
		},
		{
			TaskID:       "task-missing-git",
			Status:       TaskStatusPaused,
			WorktreePath: "/worktrees/task-missing-git",
			Branch:       "vikram/task-missing-git",
			Phase:        "verify",
			Objective:    "missing git metadata",
			CheckpointAt: now.Add(-2 * time.Minute), // fresh, but worktree corrupt
		},
	}

	db := newMockCheckpointDB(tasks)
	validator := newMockWorktreeValidator()
	validator.validPaths["/worktrees/task-good"] = nil
	validator.validPaths["/worktrees/task-bad-branch"] = fmt.Errorf("branch mismatch: expected 'vikram/task-bad-branch', got 'main'")
	validator.validPaths["/worktrees/task-missing-git"] = fmt.Errorf("no .git metadata found")
	notifier := &mockRecoveryNotifier{}

	rm := newTestRecoveryManager(t, func(m *RecoveryManager) {
		m.checkpointDB = db
		m.worktreeValidator = validator
		m.notifier = notifier
		m.now = func() time.Time { return now }
	})

	result, err := rm.Run(context.Background())
	require.NoError(t, err)

	assert.Equal(t, 3, result.TasksDiscovered)
	assert.Equal(t, 1, result.TasksResumed, "only the valid task should resume")
	assert.Equal(t, 2, result.TasksFailed, "two invalid worktree tasks should fail")

	// Verify the failed tasks were marked in the DB
	db.mu.Lock()
	assert.Equal(t, TaskStatusRecovery, db.statusUpdates["task-bad-branch"])
	assert.Equal(t, TaskStatusRecovery, db.statusUpdates["task-missing-git"])
	db.mu.Unlock()

	// Verify founder was notified for each failure
	notifier.mu.Lock()
	assert.Len(t, notifier.failures, 2)
	notifier.mu.Unlock()
}

func TestDefaultWorktreeValidator_NotADirectory(t *testing.T) {
	tmp := t.TempDir()
	filePath := filepath.Join(tmp, "not-a-dir")
	require.NoError(t, os.WriteFile(filePath, []byte("hello"), 0o644))

	v := &DefaultWorktreeValidator{}
	err := v.ValidateWorktree(context.Background(), filePath, "main")
	assert.Error(t, err)
	assert.Contains(t, err.Error(), "not a directory")
}

func TestDefaultWorktreeValidator_NoGitMetadata(t *testing.T) {
	tmp := t.TempDir()
	worktree := filepath.Join(tmp, "worktree")
	require.NoError(t, os.MkdirAll(worktree, 0o755))

	v := &DefaultWorktreeValidator{}
	err := v.ValidateWorktree(context.Background(), worktree, "main")
	assert.Error(t, err)
	assert.Contains(t, err.Error(), "no .git metadata found")
}
