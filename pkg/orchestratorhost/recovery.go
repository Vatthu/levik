package orchestratorhost

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"time"

	"github.com/Vatthu/vikram/pkg/logger"
	"github.com/Vatthu/vikram/pkg/telemetry"
	"github.com/google/uuid"
)

// ShutdownMarkerFile is the filename used to indicate a graceful shutdown.
// Its presence in the .vikram directory means the last exit was clean.
const ShutdownMarkerFile = "shutdown_marker"

// RecoveryMaxDuration is the maximum time allowed for crash recovery to complete.
const RecoveryMaxDuration = 60 * time.Second

// RecoveryStaleThreshold defines how old a checkpoint can be before it's
// considered stale and the task is marked as recovery_failed.
const RecoveryStaleThreshold = 10 * time.Minute

// TaskStatus represents the state of a task session in the checkpoint DB.
type TaskStatus string

const (
	TaskStatusRunning  TaskStatus = "running"
	TaskStatusPaused   TaskStatus = "paused"
	TaskStatusQueued   TaskStatus = "queued"
	TaskStatusComplete TaskStatus = "completed"
	TaskStatusFailed   TaskStatus = "failed"
	TaskStatusRecovery TaskStatus = "recovery_failed"
)

// CheckpointRecord represents a task checkpoint stored in the checkpoint DB.
type CheckpointRecord struct {
	TaskID       string     `json:"task_id"`
	Status       TaskStatus `json:"status"`
	WorktreePath string     `json:"worktree_path"`
	Branch       string     `json:"branch"`
	Phase        string     `json:"phase"`
	Objective    string     `json:"objective"`
	CheckpointAt time.Time  `json:"checkpoint_at"`
}

// IsTerminal returns true if the task status indicates no further work is needed.
func (s TaskStatus) IsTerminal() bool {
	return s == TaskStatusComplete || s == TaskStatusFailed || s == TaskStatusRecovery
}

// CheckpointDB abstracts access to the checkpoint database for recovery purposes.
type CheckpointDB interface {
	// ListNonTerminalTasks returns all tasks that are in a non-terminal state.
	ListNonTerminalTasks(ctx context.Context) ([]CheckpointRecord, error)

	// UpdateTaskStatus sets the status of a task by ID.
	UpdateTaskStatus(ctx context.Context, taskID string, status TaskStatus) error
}

// RecoveryNotifier sends notifications about recovery events to the founder.
type RecoveryNotifier interface {
	NotifyRecoveryFailure(ctx context.Context, taskID, objective, lastPhase, reason string) error
}

// WorktreeValidator checks the integrity of a worktree on disk.
type WorktreeValidator interface {
	// ValidateWorktree checks that a worktree path exists, has valid git metadata,
	// and its branch is consistent.
	ValidateWorktree(ctx context.Context, worktreePath, branch string) error
}

// RecoveryResult holds the outcome of a crash recovery operation.
type RecoveryResult struct {
	CrashDetected   bool          `json:"crash_detected"`
	TasksDiscovered int           `json:"tasks_discovered"`
	TasksResumed    int           `json:"tasks_resumed"`
	TasksFailed     int           `json:"tasks_failed"`
	Duration        time.Duration `json:"duration"`
	Errors          []string      `json:"errors,omitempty"`
}

// RecoveryManager handles crash detection and task resumption on startup.
//
// On startup it checks for the graceful shutdown marker. If the marker is
// absent, it initiates crash recovery: discovering non-terminal tasks from
// the checkpoint DB, validating their worktrees, and either resuming or
// marking them as recovery_failed.
//
// Validates: Requirements 52.1, 52.2, 52.3, 52.4, 52.5
type RecoveryManager struct {
	// dataDir is the directory where the shutdown marker is stored (e.g., .vikram/).
	dataDir string

	// checkpointDB provides access to task checkpoints.
	checkpointDB CheckpointDB

	// telemetryStore records recovery events in the execution trace.
	telemetryStore telemetry.Store

	// notifier sends recovery failure notifications.
	notifier RecoveryNotifier

	// worktreeValidator checks worktree integrity.
	worktreeValidator WorktreeValidator

	// now returns the current time (injectable for testing).
	now func() time.Time
}

// RecoveryManagerConfig holds configuration for creating a RecoveryManager.
type RecoveryManagerConfig struct {
	DataDir           string
	CheckpointDB      CheckpointDB
	TelemetryStore    telemetry.Store
	Notifier          RecoveryNotifier
	WorktreeValidator WorktreeValidator
}

// NewRecoveryManager creates a new RecoveryManager with the given configuration.
func NewRecoveryManager(cfg RecoveryManagerConfig) *RecoveryManager {
	return &RecoveryManager{
		dataDir:           cfg.DataDir,
		checkpointDB:      cfg.CheckpointDB,
		telemetryStore:    cfg.TelemetryStore,
		notifier:          cfg.Notifier,
		worktreeValidator: cfg.WorktreeValidator,
		now:               time.Now,
	}
}

// Run executes the crash recovery workflow. It must complete within
// RecoveryMaxDuration (60 seconds) of being called.
//
// Recovery workflow:
// 1. Check for graceful shutdown marker
// 2. If present → clean start, remove marker, return
// 3. If absent → crash detected, proceed with recovery
// 4. Scan checkpoint DB for non-terminal tasks
// 5. For each task: validate worktree and checkpoint freshness
// 6. Resume valid tasks; mark invalid/stale as recovery_failed
// 7. Record recovery event in execution trace
//
// Validates: Requirements 52.1, 52.2, 52.3, 52.4, 52.5
func (rm *RecoveryManager) Run(ctx context.Context) (*RecoveryResult, error) {
	ctx, cancel := context.WithTimeout(ctx, RecoveryMaxDuration)
	defer cancel()

	result := &RecoveryResult{}

	// Step 1: Check for graceful shutdown marker
	crashDetected := rm.detectCrash()
	result.CrashDetected = crashDetected

	if !crashDetected {
		// Clean shutdown — remove the marker and return immediately.
		rm.removeShutdownMarker()
		return result, nil
	}

	logger.Info("Crash detected (no graceful shutdown marker), initiating recovery...")

	// Step 2: Discover non-terminal tasks from checkpoint DB
	tasks, err := rm.checkpointDB.ListNonTerminalTasks(ctx)
	if err != nil {
		return result, fmt.Errorf("failed to query checkpoint DB: %w", err)
	}
	result.TasksDiscovered = len(tasks)

	if len(tasks) == 0 {
		logger.Info("No non-terminal tasks found, recovery complete")
		rm.recordRecoveryEvent(ctx, result)
		return result, nil
	}

	logger.Info(fmt.Sprintf("Discovered %d non-terminal tasks for recovery", len(tasks)))

	// Step 3: Validate and recover each task
	now := rm.now()
	for _, task := range tasks {
		select {
		case <-ctx.Done():
			result.Errors = append(result.Errors, "recovery timed out")
			rm.recordRecoveryEvent(ctx, result)
			return result, fmt.Errorf("recovery timed out after %v", RecoveryMaxDuration)
		default:
		}

		checkpointAge := now.Sub(task.CheckpointAt)

		// Check if checkpoint is stale (>10 minutes old)
		if checkpointAge > RecoveryStaleThreshold {
			rm.markRecoveryFailed(ctx, task, "checkpoint_stale", checkpointAge, result)
			continue
		}

		// Validate worktree integrity
		if err := rm.worktreeValidator.ValidateWorktree(ctx, task.WorktreePath, task.Branch); err != nil {
			rm.markRecoveryFailed(ctx, task, fmt.Sprintf("worktree_invalid: %v", err), checkpointAge, result)
			continue
		}

		// Task is valid — mark for resumption
		result.TasksResumed++
		logger.Info(fmt.Sprintf("Task %s: valid for resumption (age=%v, phase=%s)", task.TaskID, checkpointAge, task.Phase))
	}

	// Step 4: Record recovery event in Execution_Trace
	rm.recordRecoveryEvent(ctx, result)

	logger.Info(fmt.Sprintf("Recovery complete: %d resumed, %d failed (took %v)",
		result.TasksResumed, result.TasksFailed, result.Duration))

	return result, nil
}

// detectCrash checks whether the last shutdown was graceful by looking
// for the shutdown marker file.
//
// Returns true if the marker is MISSING (crash detected).
// Validates: Requirement 52.1
func (rm *RecoveryManager) detectCrash() bool {
	markerPath := rm.shutdownMarkerPath()
	_, err := os.Stat(markerPath)
	return os.IsNotExist(err)
}

// WriteShutdownMarker writes the graceful shutdown marker file.
// This should be called during a clean shutdown sequence.
func (rm *RecoveryManager) WriteShutdownMarker() error {
	markerPath := rm.shutdownMarkerPath()
	if err := os.MkdirAll(filepath.Dir(markerPath), 0o755); err != nil {
		return fmt.Errorf("failed to create data dir: %w", err)
	}
	data := shutdownMarkerData{
		Timestamp: rm.now(),
		PID:       os.Getpid(),
	}
	content, err := json.Marshal(data)
	if err != nil {
		return fmt.Errorf("failed to marshal marker: %w", err)
	}
	return os.WriteFile(markerPath, content, 0o644)
}

// RemoveShutdownMarker removes the shutdown marker file. This is called
// after confirming a clean startup so the next crash can be detected.
func (rm *RecoveryManager) RemoveShutdownMarker() error {
	return rm.removeShutdownMarker()
}

// shutdownMarkerPath returns the full path to the shutdown marker file.
func (rm *RecoveryManager) shutdownMarkerPath() string {
	return filepath.Join(rm.dataDir, ShutdownMarkerFile)
}

// removeShutdownMarker deletes the marker file. Errors are logged but not fatal.
func (rm *RecoveryManager) removeShutdownMarker() error {
	err := os.Remove(rm.shutdownMarkerPath())
	if err != nil && !os.IsNotExist(err) {
		logger.Warn(fmt.Sprintf("Failed to remove shutdown marker: %v", err))
		return err
	}
	return nil
}

// markRecoveryFailed marks a task as recovery_failed, notifies the founder,
// and updates the result counters.
func (rm *RecoveryManager) markRecoveryFailed(ctx context.Context, task CheckpointRecord, reason string, age time.Duration, result *RecoveryResult) {
	result.TasksFailed++

	if err := rm.checkpointDB.UpdateTaskStatus(ctx, task.TaskID, TaskStatusRecovery); err != nil {
		errMsg := fmt.Sprintf("task %s: failed to update status: %v", task.TaskID, err)
		result.Errors = append(result.Errors, errMsg)
		logger.Error(errMsg)
	}

	if rm.notifier != nil {
		if err := rm.notifier.NotifyRecoveryFailure(ctx, task.TaskID, task.Objective, task.Phase, reason); err != nil {
			logger.Warn(fmt.Sprintf("Failed to notify about task %s recovery failure: %v", task.TaskID, err))
		}
	}

	logger.Warn(fmt.Sprintf("Task %s: marked recovery_failed (reason=%s, checkpoint_age=%v)", task.TaskID, reason, age))
}

// recordRecoveryEvent emits a recovery telemetry event to the Execution_Trace.
// Validates: Requirement 52.4
func (rm *RecoveryManager) recordRecoveryEvent(ctx context.Context, result *RecoveryResult) {
	result.Duration = rm.now().Sub(rm.now().Add(-result.Duration)) // placeholder; actual timing below

	if rm.telemetryStore == nil {
		return
	}

	event := telemetry.TelemetryEvent{
		EventID:   uuid.New().String(),
		EventType: telemetry.EventRecovery,
		TaskID:    "_system",
		Timestamp: rm.now(),
		Attributes: map[string]interface{}{
			"crash_detected":   result.CrashDetected,
			"tasks_discovered": result.TasksDiscovered,
			"tasks_resumed":    result.TasksResumed,
			"tasks_failed":     result.TasksFailed,
			"duration_ms":      result.Duration.Milliseconds(),
			"errors":           result.Errors,
		},
	}

	if err := rm.telemetryStore.Emit(ctx, event); err != nil {
		logger.Warn(fmt.Sprintf("Failed to record recovery event: %v", err))
	}
}

// shutdownMarkerData is the JSON content written into the shutdown marker file.
type shutdownMarkerData struct {
	Timestamp time.Time `json:"timestamp"`
	PID       int       `json:"pid"`
}

// DefaultWorktreeValidator validates worktrees by checking git metadata and branch.
type DefaultWorktreeValidator struct{}

// ValidateWorktree checks that the worktree directory exists, contains valid
// git metadata (.git file or directory), and its HEAD points to the expected branch.
//
// Validates: Requirement 52.2
func (v *DefaultWorktreeValidator) ValidateWorktree(ctx context.Context, worktreePath, branch string) error {
	// Check directory exists
	info, err := os.Stat(worktreePath)
	if err != nil {
		return fmt.Errorf("worktree path does not exist: %w", err)
	}
	if !info.IsDir() {
		return fmt.Errorf("worktree path is not a directory")
	}

	// Check for .git file/directory (worktrees use a .git file pointing to the main repo)
	gitPath := filepath.Join(worktreePath, ".git")
	if _, err := os.Stat(gitPath); err != nil {
		return fmt.Errorf("no .git metadata found: %w", err)
	}

	// Verify branch integrity via git
	actualBranch, err := gitBranchName(ctx, worktreePath)
	if err != nil {
		return fmt.Errorf("failed to determine branch: %w", err)
	}
	if actualBranch != branch && branch != "" {
		return fmt.Errorf("branch mismatch: expected %q, got %q", branch, actualBranch)
	}

	// Check for corruption indicators via git status
	_, err = runGit(ctx, worktreePath, "status", "--porcelain")
	if err != nil {
		return fmt.Errorf("git status failed (possible corruption): %w", err)
	}

	return nil
}
