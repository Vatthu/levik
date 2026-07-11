// Package locks provides file-level lock management for resource contention
// control across concurrent task sessions.
package locks

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"time"
)

// ErrAlreadyLocked is returned when a file path is already locked by a different task.
var ErrAlreadyLocked = errors.New("path is already locked by another task")

// FileLock represents a file-level lock held by a task session.
type FileLock struct {
	Path      string    `json:"path"`
	TaskID    string    `json:"task_id"`
	Acquired  time.Time `json:"acquired"`
	ExpiresAt time.Time `json:"expires_at"`
}

// Registry defines the interface for managing file-level locks.
type Registry interface {
	Acquire(ctx context.Context, taskID, path string, ttl time.Duration) error
	Release(ctx context.Context, taskID, path string) error
	Query(ctx context.Context) ([]FileLock, error)
	IsLocked(ctx context.Context, path string) (bool, string, error)
}

// InMemoryRegistry is a thread-safe, in-memory implementation of the lock Registry
// with TTL-based automatic expiry.
type InMemoryRegistry struct {
	mu     sync.RWMutex
	locks  map[string]FileLock
	stopCh chan struct{}
}

// NewInMemoryRegistry creates a new InMemoryRegistry and starts a background
// goroutine that prunes expired locks every 30 seconds.
func NewInMemoryRegistry() *InMemoryRegistry {
	r := &InMemoryRegistry{
		locks:  make(map[string]FileLock),
		stopCh: make(chan struct{}),
	}
	go r.pruneLoop()
	return r
}

// Acquire locks a file path for a given task with the specified TTL duration.
// If the path is already locked by the same task, the lock is re-acquired (TTL refreshed).
// If locked by a different task, ErrAlreadyLocked is returned.
func (r *InMemoryRegistry) Acquire(_ context.Context, taskID, path string, ttl time.Duration) error {
	if taskID == "" {
		return fmt.Errorf("task_id is required")
	}
	if path == "" {
		return fmt.Errorf("path is required")
	}
	if ttl <= 0 {
		return fmt.Errorf("ttl must be positive")
	}

	now := time.Now().UTC()

	r.mu.Lock()
	defer r.mu.Unlock()

	if existing, ok := r.locks[path]; ok {
		// Expired locks can be overwritten
		if now.After(existing.ExpiresAt) {
			// Lock expired, allow re-acquisition
		} else if existing.TaskID != taskID {
			return fmt.Errorf("%w: path %q locked by task %q until %s",
				ErrAlreadyLocked, path, existing.TaskID, existing.ExpiresAt.Format(time.RFC3339))
		}
		// Same task re-acquiring — refresh TTL
	}

	r.locks[path] = FileLock{
		Path:      path,
		TaskID:    taskID,
		Acquired:  now,
		ExpiresAt: now.Add(ttl),
	}
	return nil
}

// Release releases a lock on a file path for a given task.
// Returns an error if the path is not locked or is locked by a different task.
func (r *InMemoryRegistry) Release(_ context.Context, taskID, path string) error {
	if taskID == "" {
		return fmt.Errorf("task_id is required")
	}
	if path == "" {
		return fmt.Errorf("path is required")
	}

	r.mu.Lock()
	defer r.mu.Unlock()

	existing, ok := r.locks[path]
	if !ok {
		return fmt.Errorf("path %q is not locked", path)
	}
	if existing.TaskID != taskID {
		return fmt.Errorf("path %q is locked by task %q, not %q", path, existing.TaskID, taskID)
	}

	delete(r.locks, path)
	return nil
}

// Query returns all currently active (non-expired) locks.
func (r *InMemoryRegistry) Query(_ context.Context) ([]FileLock, error) {
	now := time.Now().UTC()

	r.mu.RLock()
	defer r.mu.RUnlock()

	result := make([]FileLock, 0, len(r.locks))
	for _, lock := range r.locks {
		if now.Before(lock.ExpiresAt) {
			result = append(result, lock)
		}
	}
	return result, nil
}

// IsLocked checks whether a file path is currently locked (and not expired).
// Returns (locked, taskID, error).
func (r *InMemoryRegistry) IsLocked(_ context.Context, path string) (bool, string, error) {
	if path == "" {
		return false, "", fmt.Errorf("path is required")
	}

	now := time.Now().UTC()

	r.mu.RLock()
	defer r.mu.RUnlock()

	lock, ok := r.locks[path]
	if !ok || now.After(lock.ExpiresAt) {
		return false, "", nil
	}
	return true, lock.TaskID, nil
}

// Stop terminates the background pruning goroutine.
func (r *InMemoryRegistry) Stop() {
	close(r.stopCh)
}

// pruneLoop runs every 30 seconds to remove expired locks.
func (r *InMemoryRegistry) pruneLoop() {
	ticker := time.NewTicker(30 * time.Second)
	defer ticker.Stop()

	for {
		select {
		case <-r.stopCh:
			return
		case <-ticker.C:
			r.pruneExpired()
		}
	}
}

// pruneExpired removes all expired locks from the registry.
func (r *InMemoryRegistry) pruneExpired() {
	now := time.Now().UTC()

	r.mu.Lock()
	defer r.mu.Unlock()

	for path, lock := range r.locks {
		if now.After(lock.ExpiresAt) {
			delete(r.locks, path)
		}
	}
}
