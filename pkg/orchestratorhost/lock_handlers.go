package orchestratorhost

import (
	"context"
	"net/http"
	"time"

	"github.com/Vatthu/vikram/pkg/locks"
)

// lockRegistry is the subset of locks.Registry used by the host server handlers.
type lockRegistry interface {
	Acquire(ctx context.Context, taskID, path string, ttl time.Duration) error
	Release(ctx context.Context, taskID, path string) error
	Query(ctx context.Context) ([]locks.FileLock, error)
	IsLocked(ctx context.Context, path string) (bool, string, error)
}

// lockAcquireRequest is the JSON body for POST /v1/locks/acquire.
type lockAcquireRequest struct {
	TaskID     string `json:"task_id"`
	Path       string `json:"path"`
	TTLSeconds int    `json:"ttl_seconds"`
}

// lockReleaseRequest is the JSON body for POST /v1/locks/release.
type lockReleaseRequest struct {
	TaskID string `json:"task_id"`
	Path   string `json:"path"`
}

// handleLockAcquire handles POST /v1/locks/acquire — acquires a file-level lock.
func (s *Server) handleLockAcquire(w http.ResponseWriter, r *http.Request) {
	if s.lockRegistry == nil {
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{"error": "lock registry not configured"})
		return
	}

	var req lockAcquireRequest
	if err := decodeJSON(w, r, &req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	if req.TaskID == "" || req.Path == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "task_id and path are required"})
		return
	}
	if req.TTLSeconds <= 0 {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "ttl_seconds must be positive"})
		return
	}

	ttl := time.Duration(req.TTLSeconds) * time.Second
	err := s.lockRegistry.Acquire(r.Context(), req.TaskID, req.Path, ttl)
	if err != nil {
		writeJSON(w, http.StatusConflict, map[string]string{"error": err.Error()})
		return
	}

	writeJSON(w, http.StatusOK, map[string]interface{}{
		"acquired":    true,
		"task_id":     req.TaskID,
		"path":        req.Path,
		"ttl_seconds": req.TTLSeconds,
	})
}

// handleLockRelease handles POST /v1/locks/release — releases a file-level lock.
func (s *Server) handleLockRelease(w http.ResponseWriter, r *http.Request) {
	if s.lockRegistry == nil {
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{"error": "lock registry not configured"})
		return
	}

	var req lockReleaseRequest
	if err := decodeJSON(w, r, &req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	if req.TaskID == "" || req.Path == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "task_id and path are required"})
		return
	}

	err := s.lockRegistry.Release(r.Context(), req.TaskID, req.Path)
	if err != nil {
		writeJSON(w, http.StatusConflict, map[string]string{"error": err.Error()})
		return
	}

	writeJSON(w, http.StatusOK, map[string]interface{}{
		"released": true,
		"task_id":  req.TaskID,
		"path":     req.Path,
	})
}

// handleLockQuery handles GET /v1/locks — returns all active locks.
func (s *Server) handleLockQuery(w http.ResponseWriter, r *http.Request) {
	if s.lockRegistry == nil {
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{"error": "lock registry not configured"})
		return
	}

	activeLocks, err := s.lockRegistry.Query(r.Context())
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": err.Error()})
		return
	}

	writeJSON(w, http.StatusOK, map[string]interface{}{
		"locks": activeLocks,
		"count": len(activeLocks),
	})
}
