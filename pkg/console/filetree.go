package console

import (
	"context"
	"net/http"
	"net/url"
	"time"
)

// FileTreeEntry represents a single file in the changed-file tree for a task.
type FileTreeEntry struct {
	Path      string `json:"path"`
	Status    string `json:"status"` // "added", "modified", "deleted", "renamed"
	Additions int    `json:"additions"`
	Deletions int    `json:"deletions"`
	Binary    bool   `json:"binary,omitempty"`
}

// FileTreeResponse is the envelope returned by the file tree endpoint.
type FileTreeResponse struct {
	TaskID string          `json:"task_id"`
	Files  []FileTreeEntry `json:"files"`
	Total  FileTreeSummary `json:"total"`
}

// FileTreeSummary aggregates addition/deletion counts across all changed files.
type FileTreeSummary struct {
	FilesChanged int `json:"files_changed"`
	Additions    int `json:"additions"`
	Deletions    int `json:"deletions"`
}

// handleAPITaskFiles serves GET /api/tasks/{task_id}/files.
// It returns the changed file tree with per-file addition/deletion counts.
func (s *Server) handleAPITaskFiles(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		s.writeError(w, http.StatusMethodNotAllowed, "GET only")
		return
	}

	taskID := r.PathValue("task_id")
	if taskID == "" {
		s.writeError(w, http.StatusBadRequest, "task_id is required")
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), 15*time.Second)
	defer cancel()

	var tree FileTreeResponse
	path := "/v1/tasks/" + url.PathEscape(taskID) + "/files"
	if err := s.orchestratorJSON(ctx, http.MethodGet, path, nil, &tree); err != nil {
		s.writeError(w, http.StatusBadGateway, err.Error())
		return
	}

	tree.TaskID = taskID

	// Compute summary if not provided by orchestrator.
	if tree.Total.FilesChanged == 0 && len(tree.Files) > 0 {
		tree.Total.FilesChanged = len(tree.Files)
		for _, f := range tree.Files {
			tree.Total.Additions += f.Additions
			tree.Total.Deletions += f.Deletions
		}
	}

	s.writeOK(w, tree)
}
