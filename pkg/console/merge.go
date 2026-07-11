package console

import (
	"context"
	"encoding/json"
	"net/http"
	"net/url"
	"time"
)

// MergeStrategy identifies the git merge strategy to apply.
type MergeStrategy string

const (
	MergeStrategyFastForward MergeStrategy = "fast-forward"
	MergeStrategySquash      MergeStrategy = "squash"
	MergeStrategyRebase      MergeStrategy = "rebase"
)

// MergeRequest is the body sent by the console to initiate a merge.
type MergeRequest struct {
	Strategy MergeStrategy `json:"strategy"`
}

// MergeConflict describes a single file conflict encountered during merge.
type MergeConflict struct {
	Path    string `json:"path"`
	Markers string `json:"markers,omitempty"` // conflict marker text
	Ours    string `json:"ours,omitempty"`
	Theirs  string `json:"theirs,omitempty"`
}

// MergeResolutionOption describes a possible action to resolve merge conflicts.
type MergeResolutionOption struct {
	Action      string `json:"action"`      // "manual", "rebase_retry", "cancel"
	Description string `json:"description"` // human-readable description
}

// MergeResponse is the envelope returned after a merge operation.
type MergeResponse struct {
	TaskID            string                  `json:"task_id"`
	Status            string                  `json:"status"` // "merged", "conflict", "failed"
	Strategy          MergeStrategy           `json:"strategy"`
	MergeCommit       string                  `json:"merge_commit,omitempty"`
	Conflicts         []MergeConflict         `json:"conflicts,omitempty"`
	ResolutionOptions []MergeResolutionOption `json:"resolution_options,omitempty"`
	Error             string                  `json:"error,omitempty"`
}

// handleAPITaskMerge serves POST /api/tasks/{task_id}/merge.
// It forwards the merge request to the orchestrator host which executes
// the actual git operation on the task worktree.
func (s *Server) handleAPITaskMerge(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		s.writeError(w, http.StatusMethodNotAllowed, "POST only")
		return
	}

	taskID := r.PathValue("task_id")
	if taskID == "" {
		s.writeError(w, http.StatusBadRequest, "task_id is required")
		return
	}

	var req MergeRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		s.writeError(w, http.StatusBadRequest, "invalid JSON body")
		return
	}

	if !isValidMergeStrategy(req.Strategy) {
		s.writeError(w, http.StatusBadRequest, "strategy must be 'fast-forward', 'squash', or 'rebase'")
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), 2*time.Minute)
	defer cancel()

	var resp MergeResponse
	path := "/v1/tasks/" + url.PathEscape(taskID) + "/merge"
	if err := s.orchestratorJSON(ctx, http.MethodPost, path, req, &resp); err != nil {
		s.writeError(w, http.StatusBadGateway, err.Error())
		return
	}

	resp.TaskID = taskID
	resp.Strategy = req.Strategy

	// When conflicts are detected, populate resolution options per Req 46.4.
	if resp.Status == "conflict" && len(resp.ResolutionOptions) == 0 {
		resp.ResolutionOptions = []MergeResolutionOption{
			{
				Action:      "manual",
				Description: "Resolve conflicts manually in the worktree and retry the merge",
			},
			{
				Action:      "rebase_retry",
				Description: "Rebase the branch onto the latest target and retry the merge",
			},
			{
				Action:      "cancel",
				Description: "Cancel the merge and leave the branch as-is",
			},
		}
	}

	s.writeOK(w, resp)
}

func isValidMergeStrategy(s MergeStrategy) bool {
	switch s {
	case MergeStrategyFastForward, MergeStrategySquash, MergeStrategyRebase:
		return true
	}
	return false
}
