package locks

import (
	"context"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

func TestAcquire_Basic(t *testing.T) {
	r := NewInMemoryRegistry()
	defer r.Stop()

	ctx := context.Background()
	err := r.Acquire(ctx, "task-1", "src/main.go", 5*time.Minute)
	require.NoError(t, err)

	locks, err := r.Query(ctx)
	require.NoError(t, err)
	assert.Len(t, locks, 1)
	assert.Equal(t, "src/main.go", locks[0].Path)
	assert.Equal(t, "task-1", locks[0].TaskID)
}

func TestAcquire_MutualExclusion(t *testing.T) {
	r := NewInMemoryRegistry()
	defer r.Stop()

	ctx := context.Background()

	// Task 1 acquires lock
	err := r.Acquire(ctx, "task-1", "src/main.go", 5*time.Minute)
	require.NoError(t, err)

	// Task 2 tries to acquire same path — should fail
	err = r.Acquire(ctx, "task-2", "src/main.go", 5*time.Minute)
	require.Error(t, err)
	assert.ErrorIs(t, err, ErrAlreadyLocked)
}

func TestAcquire_SameTaskReacquire(t *testing.T) {
	r := NewInMemoryRegistry()
	defer r.Stop()

	ctx := context.Background()

	// Task 1 acquires lock
	err := r.Acquire(ctx, "task-1", "src/main.go", 1*time.Minute)
	require.NoError(t, err)

	// Same task re-acquires — should succeed and refresh TTL
	err = r.Acquire(ctx, "task-1", "src/main.go", 10*time.Minute)
	require.NoError(t, err)

	// Verify the lock is still there with updated expiry
	locked, taskID, err := r.IsLocked(ctx, "src/main.go")
	require.NoError(t, err)
	assert.True(t, locked)
	assert.Equal(t, "task-1", taskID)
}

func TestAcquire_AfterTTLExpiry(t *testing.T) {
	r := NewInMemoryRegistry()
	defer r.Stop()

	ctx := context.Background()

	// Task 1 acquires lock with very short TTL
	err := r.Acquire(ctx, "task-1", "src/main.go", 1*time.Millisecond)
	require.NoError(t, err)

	// Wait for expiry
	time.Sleep(5 * time.Millisecond)

	// Task 2 can now acquire the expired lock
	err = r.Acquire(ctx, "task-2", "src/main.go", 5*time.Minute)
	require.NoError(t, err)

	locked, taskID, err := r.IsLocked(ctx, "src/main.go")
	require.NoError(t, err)
	assert.True(t, locked)
	assert.Equal(t, "task-2", taskID)
}

func TestRelease_Basic(t *testing.T) {
	r := NewInMemoryRegistry()
	defer r.Stop()

	ctx := context.Background()

	err := r.Acquire(ctx, "task-1", "src/main.go", 5*time.Minute)
	require.NoError(t, err)

	err = r.Release(ctx, "task-1", "src/main.go")
	require.NoError(t, err)

	locked, _, err := r.IsLocked(ctx, "src/main.go")
	require.NoError(t, err)
	assert.False(t, locked)
}

func TestRelease_WrongTask(t *testing.T) {
	r := NewInMemoryRegistry()
	defer r.Stop()

	ctx := context.Background()

	err := r.Acquire(ctx, "task-1", "src/main.go", 5*time.Minute)
	require.NoError(t, err)

	// Different task tries to release — should fail
	err = r.Release(ctx, "task-2", "src/main.go")
	require.Error(t, err)
	assert.Contains(t, err.Error(), "locked by task")
}

func TestRelease_NotLocked(t *testing.T) {
	r := NewInMemoryRegistry()
	defer r.Stop()

	ctx := context.Background()

	err := r.Release(ctx, "task-1", "src/main.go")
	require.Error(t, err)
	assert.Contains(t, err.Error(), "not locked")
}

func TestQuery_FiltersExpired(t *testing.T) {
	r := NewInMemoryRegistry()
	defer r.Stop()

	ctx := context.Background()

	// Create one expired and one active lock
	err := r.Acquire(ctx, "task-1", "src/expired.go", 1*time.Millisecond)
	require.NoError(t, err)

	err = r.Acquire(ctx, "task-2", "src/active.go", 5*time.Minute)
	require.NoError(t, err)

	// Wait for first lock to expire
	time.Sleep(5 * time.Millisecond)

	locks, err := r.Query(ctx)
	require.NoError(t, err)
	assert.Len(t, locks, 1)
	assert.Equal(t, "src/active.go", locks[0].Path)
}

func TestIsLocked_ExpiredLock(t *testing.T) {
	r := NewInMemoryRegistry()
	defer r.Stop()

	ctx := context.Background()

	err := r.Acquire(ctx, "task-1", "src/main.go", 1*time.Millisecond)
	require.NoError(t, err)

	time.Sleep(5 * time.Millisecond)

	locked, taskID, err := r.IsLocked(ctx, "src/main.go")
	require.NoError(t, err)
	assert.False(t, locked)
	assert.Empty(t, taskID)
}

func TestIsLocked_NotLocked(t *testing.T) {
	r := NewInMemoryRegistry()
	defer r.Stop()

	ctx := context.Background()

	locked, taskID, err := r.IsLocked(ctx, "src/main.go")
	require.NoError(t, err)
	assert.False(t, locked)
	assert.Empty(t, taskID)
}

func TestAcquire_ValidationErrors(t *testing.T) {
	r := NewInMemoryRegistry()
	defer r.Stop()

	ctx := context.Background()

	err := r.Acquire(ctx, "", "src/main.go", 5*time.Minute)
	assert.Error(t, err)
	assert.Contains(t, err.Error(), "task_id is required")

	err = r.Acquire(ctx, "task-1", "", 5*time.Minute)
	assert.Error(t, err)
	assert.Contains(t, err.Error(), "path is required")

	err = r.Acquire(ctx, "task-1", "src/main.go", 0)
	assert.Error(t, err)
	assert.Contains(t, err.Error(), "ttl must be positive")
}

func TestPruneExpired(t *testing.T) {
	r := NewInMemoryRegistry()
	defer r.Stop()

	ctx := context.Background()

	// Create expired lock
	err := r.Acquire(ctx, "task-1", "src/main.go", 1*time.Millisecond)
	require.NoError(t, err)

	time.Sleep(5 * time.Millisecond)

	// Manually trigger prune
	r.pruneExpired()

	r.mu.RLock()
	count := len(r.locks)
	r.mu.RUnlock()
	assert.Equal(t, 0, count)
}

func TestMultipleLocks_DifferentPaths(t *testing.T) {
	r := NewInMemoryRegistry()
	defer r.Stop()

	ctx := context.Background()

	// Same task can lock multiple paths
	err := r.Acquire(ctx, "task-1", "src/a.go", 5*time.Minute)
	require.NoError(t, err)

	err = r.Acquire(ctx, "task-1", "src/b.go", 5*time.Minute)
	require.NoError(t, err)

	// Different tasks can lock different paths
	err = r.Acquire(ctx, "task-2", "src/c.go", 5*time.Minute)
	require.NoError(t, err)

	locks, err := r.Query(ctx)
	require.NoError(t, err)
	assert.Len(t, locks, 3)
}
