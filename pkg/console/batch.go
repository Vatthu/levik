package console

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"time"
)

// BatchAction identifies the type of batch operation to apply.
type BatchAction string

const (
	BatchActionApprove      BatchAction = "approve"
	BatchActionReject       BatchAction = "reject"
	BatchActionReprioritize BatchAction = "reprioritize"
	BatchActionCancel       BatchAction = "cancel"
	BatchActionResume       BatchAction = "resume"
)

// destructiveBatchActions are actions that require confirmation before execution.
var destructiveBatchActions = map[BatchAction]bool{
	BatchActionReject: true,
	BatchActionCancel: true,
}

// BatchRequest is the payload for POST /api/tasks/batch.
type BatchRequest struct {
	TaskIDs   []string    `json:"task_ids"`
	Action    BatchAction `json:"action"`
	Confirmed bool        `json:"confirmed"`
	// Priority is used only for reprioritize action.
	Priority int    `json:"priority,omitempty"`
	Comment  string `json:"comment,omitempty"`
}

// BatchTaskResult reports the outcome of a batch action on a single task.
type BatchTaskResult struct {
	TaskID  string `json:"task_id"`
	Success bool   `json:"success"`
	Error   string `json:"error,omitempty"`
}

// BatchResponse is the response payload for batch operations.
type BatchResponse struct {
	Action          BatchAction       `json:"action"`
	TotalRequested  int               `json:"total_requested"`
	Succeeded       int               `json:"succeeded"`
	Failed          int               `json:"failed"`
	Results         []BatchTaskResult `json:"results"`
	RequiresConfirm bool              `json:"requires_confirm,omitempty"`
	ConfirmationMsg string            `json:"confirmation_msg,omitempty"`
	AffectedTaskIDs []string          `json:"affected_task_ids,omitempty"`
}

// validBatchActions lists all recognized batch actions.
var validBatchActions = map[BatchAction]bool{
	BatchActionApprove:      true,
	BatchActionReject:       true,
	BatchActionReprioritize: true,
	BatchActionCancel:       true,
	BatchActionResume:       true,
}

// handleBatchTasks handles POST /api/tasks/batch.
// It applies batch actions to multiple tasks, requiring confirmation for destructive operations.
func (s *Server) handleBatchTasks(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		s.writeError(w, http.StatusMethodNotAllowed, "POST only")
		return
	}

	var req BatchRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		s.writeError(w, http.StatusBadRequest, "invalid JSON body")
		return
	}

	// Validate action.
	if !validBatchActions[req.Action] {
		s.writeError(w, http.StatusBadRequest, fmt.Sprintf("invalid action: %q; valid actions: approve, reject, reprioritize, cancel, resume", req.Action))
		return
	}

	// Validate task IDs.
	if len(req.TaskIDs) == 0 {
		s.writeError(w, http.StatusBadRequest, "task_ids must not be empty")
		return
	}

	// If destructive and not confirmed, return a confirmation prompt.
	if destructiveBatchActions[req.Action] && !req.Confirmed {
		msg := fmt.Sprintf("You are about to %s %d task(s). This action cannot be undone. Please confirm.", req.Action, len(req.TaskIDs))
		s.writeJSON(w, http.StatusOK, BatchResponse{
			Action:          req.Action,
			TotalRequested:  len(req.TaskIDs),
			RequiresConfirm: true,
			ConfirmationMsg: msg,
			AffectedTaskIDs: req.TaskIDs,
		})
		return
	}

	// Execute the batch action against the orchestrator for each task.
	ctx, cancel := context.WithTimeout(r.Context(), 30*time.Second)
	defer cancel()

	results := make([]BatchTaskResult, 0, len(req.TaskIDs))
	succeeded := 0
	failed := 0

	for _, taskID := range req.TaskIDs {
		err := s.executeBatchAction(ctx, taskID, req.Action, req.Priority, req.Comment)
		if err != nil {
			results = append(results, BatchTaskResult{TaskID: taskID, Success: false, Error: err.Error()})
			failed++
		} else {
			results = append(results, BatchTaskResult{TaskID: taskID, Success: true})
			succeeded++
		}
	}

	s.writeJSON(w, http.StatusOK, BatchResponse{
		Action:         req.Action,
		TotalRequested: len(req.TaskIDs),
		Succeeded:      succeeded,
		Failed:         failed,
		Results:        results,
	})
}

// executeBatchAction applies a single batch action to a task via the orchestrator.
func (s *Server) executeBatchAction(ctx context.Context, taskID string, action BatchAction, priority int, comment string) error {
	switch action {
	case BatchActionApprove:
		body := map[string]interface{}{
			"task_id":  taskID,
			"decision": "approve",
			"comment":  comment,
		}
		return s.orchestratorJSON(ctx, http.MethodPost, "/v1/tasks/"+taskID+"/resume", body, nil)

	case BatchActionReject:
		body := map[string]interface{}{
			"task_id":  taskID,
			"decision": "reject",
			"comment":  comment,
		}
		return s.orchestratorJSON(ctx, http.MethodPost, "/v1/tasks/"+taskID+"/resume", body, nil)

	case BatchActionReprioritize:
		body := map[string]interface{}{
			"task_id":  taskID,
			"priority": priority,
		}
		return s.orchestratorJSON(ctx, http.MethodPost, "/v1/tasks/"+taskID+"/priority", body, nil)

	case BatchActionCancel:
		body := map[string]interface{}{
			"task_id": taskID,
			"reason":  comment,
		}
		return s.orchestratorJSON(ctx, http.MethodPost, "/v1/tasks/"+taskID+"/cancel", body, nil)

	case BatchActionResume:
		body := map[string]interface{}{
			"task_id":  taskID,
			"decision": "approve",
			"comment":  comment,
		}
		return s.orchestratorJSON(ctx, http.MethodPost, "/v1/tasks/"+taskID+"/resume", body, nil)

	default:
		return fmt.Errorf("unsupported batch action: %s", action)
	}
}
