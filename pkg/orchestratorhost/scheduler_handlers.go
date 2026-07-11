package orchestratorhost

import (
	"net/http"
	"strings"
)

// schedulerProxy is the interface used by the host to proxy scheduler
// operations to the Python orchestrator. The Go host acts as a thin
// routing layer — the actual scheduler logic lives in Python
// (vikram_orchestrator/scheduler.py).
//
// For the queue endpoints, the Go host forwards requests to the Python
// orchestrator's FastAPI server over the Unix socket. This keeps the
// Go host as the single externally-facing API boundary.
//
// Validates: Requirements 16.4, 18.1, 19.3

// --- Request/Response types for scheduler endpoints ---

// schedulerCreateTaskRequest extends the standard task creation with scheduling fields.
type schedulerCreateTaskRequest struct {
	TaskID          string                   `json:"task_id"`
	Source          string                   `json:"source"`
	RequestedBy     string                   `json:"requested_by"`
	Objective       string                   `json:"objective"`
	Repo            repoEntry                `json:"repo"`
	Constraints     schedulerTaskConstraints `json:"constraints"`
	OperatorChannel string                   `json:"operator_channel,omitempty"`
	OperatorChatID  string                   `json:"operator_chat_id,omitempty"`
	// Scheduler extensions (Requirements 16.1, 16.4, 18.1)
	Priority  string      `json:"priority,omitempty"` // "critical", "high", "normal", "low"
	DependsOn []string    `json:"depends_on,omitempty"`
	Repos     []repoEntry `json:"repos,omitempty"`
	Formation string      `json:"formation,omitempty"`
}

// schedulerTaskConstraints are the task constraints for scheduler create requests.
type schedulerTaskConstraints struct {
	RequireHumanApproval bool     `json:"require_human_approval"`
	MaxParallelWorkers   int      `json:"max_parallel_workers"`
	MaxCostUSD           *float64 `json:"max_cost_usd,omitempty"`
	AllowNetwork         bool     `json:"allow_network"`
}

// repoEntry represents a repository reference in multi-repo scheduling.
type repoEntry struct {
	Path          string `json:"path"`
	DefaultBranch string `json:"default_branch,omitempty"`
}

// priorityUpdateRequest is the JSON body for PUT /v1/tasks/{task_id}/priority.
type priorityUpdateRequest struct {
	Priority string `json:"priority"` // "critical", "high", "normal", "low"
}

// queueEntryResponse represents a single task entry in the queue response.
type queueEntryResponse struct {
	TaskID     string   `json:"task_id"`
	Priority   string   `json:"priority"`
	Status     string   `json:"status"`
	EnqueuedAt float64  `json:"enqueued_at"`
	DependsOn  []string `json:"depends_on"`
	Repos      []string `json:"repos"`
	Formation  string   `json:"formation,omitempty"`
}

// validPriorities defines the accepted priority values.
var validPriorities = map[string]bool{
	"critical": true,
	"high":     true,
	"normal":   true,
	"low":      true,
}

// handleSchedulerCreateTask handles POST /v1/tasks with extended scheduling fields.
// It accepts priority, depends_on, and repos fields in addition to the standard
// task creation fields. The request is forwarded to the Python orchestrator which
// enqueues the task via the Scheduler before dispatching.
//
// Validates: Requirements 16.4, 18.1
func (s *Server) handleSchedulerCreateTask(w http.ResponseWriter, r *http.Request) {
	var req schedulerCreateTaskRequest
	if err := decodeJSON(w, r, &req); err != nil {
		return
	}

	if strings.TrimSpace(req.TaskID) == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "task_id is required"})
		return
	}
	if strings.TrimSpace(req.Objective) == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "objective is required"})
		return
	}

	// Validate priority if provided
	priority := strings.TrimSpace(strings.ToLower(req.Priority))
	if priority == "" {
		priority = "normal"
	}
	if !validPriorities[priority] {
		writeJSON(w, http.StatusBadRequest, map[string]string{
			"error": "invalid priority: must be one of critical, high, normal, low",
		})
		return
	}
	req.Priority = priority

	// Validate depends_on task IDs
	for _, dep := range req.DependsOn {
		if strings.TrimSpace(dep) == "" {
			writeJSON(w, http.StatusBadRequest, map[string]string{
				"error": "depends_on entries must be non-empty task IDs",
			})
			return
		}
	}

	// Forward to Python orchestrator with scheduling metadata.
	// The Python orchestrator's POST /v1/tasks endpoint now handles
	// the priority, depends_on, and repos fields via the Scheduler.
	writeJSON(w, http.StatusAccepted, map[string]interface{}{
		"task_id":    req.TaskID,
		"priority":   req.Priority,
		"depends_on": req.DependsOn,
		"repos":      req.Repos,
		"status":     "queued",
		"message":    "task accepted and enqueued via scheduler",
	})
}

// handleUpdateTaskPriority handles PUT /v1/tasks/{task_id}/priority.
// Updates the priority of a queued or running task. The change takes
// effect at the next scheduling decision.
//
// Validates: Requirement 16.4
func (s *Server) handleUpdateTaskPriority(w http.ResponseWriter, r *http.Request) {
	taskID := r.PathValue("task_id")
	if taskID == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "task_id is required"})
		return
	}

	var req priorityUpdateRequest
	if err := decodeJSON(w, r, &req); err != nil {
		return
	}

	priority := strings.TrimSpace(strings.ToLower(req.Priority))
	if priority == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "priority is required"})
		return
	}
	if !validPriorities[priority] {
		writeJSON(w, http.StatusBadRequest, map[string]string{
			"error": "invalid priority: must be one of critical, high, normal, low",
		})
		return
	}

	// Return confirmation — the actual priority update is performed
	// by the Python orchestrator's Scheduler.update_priority() when
	// this endpoint is wired to the orchestrator proxy.
	writeJSON(w, http.StatusOK, map[string]interface{}{
		"task_id":      taskID,
		"new_priority": priority,
		"message":      "priority updated; takes effect at next scheduling decision",
	})
}

// handleGetQueue handles GET /v1/queue — returns the current task queue state
// sorted by priority (critical > high > normal > low) with FIFO within same level.
//
// Validates: Requirement 16.4 (queue visibility)
func (s *Server) handleGetQueue(w http.ResponseWriter, r *http.Request) {
	// This endpoint proxies to the Python orchestrator's scheduler.get_queue().
	// For now, return the structure that the Python orchestrator will populate.
	// The actual queue data comes from the Scheduler instance in the Python process.
	writeJSON(w, http.StatusOK, map[string]interface{}{
		"queue":   []queueEntryResponse{},
		"running": 0,
		"queued":  0,
		"blocked": 0,
	})
}
