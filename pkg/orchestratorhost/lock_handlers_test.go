package orchestratorhost

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/Vatthu/vikram/pkg/locks"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

func newTestServerWithLocks(t *testing.T) (*Server, *locks.InMemoryRegistry) {
	t.Helper()
	reg := locks.NewInMemoryRegistry()
	t.Cleanup(reg.Stop)

	srv := &Server{
		cfg: Config{
			WorkspaceRoot: t.TempDir(),
		},
		lockRegistry: reg,
	}
	return srv, reg
}

func TestHandleLockAcquire_Success(t *testing.T) {
	srv, _ := newTestServerWithLocks(t)

	body := `{"task_id":"task-1","path":"src/main.go","ttl_seconds":300}`
	req := httptest.NewRequest(http.MethodPost, "/v1/locks/acquire", bytes.NewBufferString(body))
	req.Header.Set("Content-Type", "application/json")
	w := httptest.NewRecorder()

	srv.handleLockAcquire(w, req)

	assert.Equal(t, http.StatusOK, w.Code)
	var resp map[string]interface{}
	err := json.Unmarshal(w.Body.Bytes(), &resp)
	require.NoError(t, err)
	assert.Equal(t, true, resp["acquired"])
	assert.Equal(t, "task-1", resp["task_id"])
	assert.Equal(t, "src/main.go", resp["path"])
}

func TestHandleLockAcquire_Conflict(t *testing.T) {
	srv, _ := newTestServerWithLocks(t)

	// First acquire succeeds
	body := `{"task_id":"task-1","path":"src/main.go","ttl_seconds":300}`
	req := httptest.NewRequest(http.MethodPost, "/v1/locks/acquire", bytes.NewBufferString(body))
	req.Header.Set("Content-Type", "application/json")
	w := httptest.NewRecorder()
	srv.handleLockAcquire(w, req)
	assert.Equal(t, http.StatusOK, w.Code)

	// Second acquire by different task fails
	body = `{"task_id":"task-2","path":"src/main.go","ttl_seconds":300}`
	req = httptest.NewRequest(http.MethodPost, "/v1/locks/acquire", bytes.NewBufferString(body))
	req.Header.Set("Content-Type", "application/json")
	w = httptest.NewRecorder()
	srv.handleLockAcquire(w, req)

	assert.Equal(t, http.StatusConflict, w.Code)
	var resp map[string]interface{}
	err := json.Unmarshal(w.Body.Bytes(), &resp)
	require.NoError(t, err)
	assert.Contains(t, resp["error"], "already locked")
}

func TestHandleLockAcquire_MissingFields(t *testing.T) {
	srv, _ := newTestServerWithLocks(t)

	body := `{"task_id":"","path":"src/main.go","ttl_seconds":300}`
	req := httptest.NewRequest(http.MethodPost, "/v1/locks/acquire", bytes.NewBufferString(body))
	req.Header.Set("Content-Type", "application/json")
	w := httptest.NewRecorder()
	srv.handleLockAcquire(w, req)

	assert.Equal(t, http.StatusBadRequest, w.Code)
}

func TestHandleLockAcquire_InvalidTTL(t *testing.T) {
	srv, _ := newTestServerWithLocks(t)

	body := `{"task_id":"task-1","path":"src/main.go","ttl_seconds":0}`
	req := httptest.NewRequest(http.MethodPost, "/v1/locks/acquire", bytes.NewBufferString(body))
	req.Header.Set("Content-Type", "application/json")
	w := httptest.NewRecorder()
	srv.handleLockAcquire(w, req)

	assert.Equal(t, http.StatusBadRequest, w.Code)
}

func TestHandleLockRelease_Success(t *testing.T) {
	srv, _ := newTestServerWithLocks(t)

	// First acquire
	body := `{"task_id":"task-1","path":"src/main.go","ttl_seconds":300}`
	req := httptest.NewRequest(http.MethodPost, "/v1/locks/acquire", bytes.NewBufferString(body))
	req.Header.Set("Content-Type", "application/json")
	w := httptest.NewRecorder()
	srv.handleLockAcquire(w, req)
	require.Equal(t, http.StatusOK, w.Code)

	// Release
	body = `{"task_id":"task-1","path":"src/main.go"}`
	req = httptest.NewRequest(http.MethodPost, "/v1/locks/release", bytes.NewBufferString(body))
	req.Header.Set("Content-Type", "application/json")
	w = httptest.NewRecorder()
	srv.handleLockRelease(w, req)

	assert.Equal(t, http.StatusOK, w.Code)
	var resp map[string]interface{}
	err := json.Unmarshal(w.Body.Bytes(), &resp)
	require.NoError(t, err)
	assert.Equal(t, true, resp["released"])
}

func TestHandleLockRelease_WrongTask(t *testing.T) {
	srv, _ := newTestServerWithLocks(t)

	// First acquire by task-1
	body := `{"task_id":"task-1","path":"src/main.go","ttl_seconds":300}`
	req := httptest.NewRequest(http.MethodPost, "/v1/locks/acquire", bytes.NewBufferString(body))
	req.Header.Set("Content-Type", "application/json")
	w := httptest.NewRecorder()
	srv.handleLockAcquire(w, req)
	require.Equal(t, http.StatusOK, w.Code)

	// Release by task-2 — should fail
	body = `{"task_id":"task-2","path":"src/main.go"}`
	req = httptest.NewRequest(http.MethodPost, "/v1/locks/release", bytes.NewBufferString(body))
	req.Header.Set("Content-Type", "application/json")
	w = httptest.NewRecorder()
	srv.handleLockRelease(w, req)

	assert.Equal(t, http.StatusConflict, w.Code)
}

func TestHandleLockRelease_MissingFields(t *testing.T) {
	srv, _ := newTestServerWithLocks(t)

	body := `{"task_id":"task-1","path":""}`
	req := httptest.NewRequest(http.MethodPost, "/v1/locks/release", bytes.NewBufferString(body))
	req.Header.Set("Content-Type", "application/json")
	w := httptest.NewRecorder()
	srv.handleLockRelease(w, req)

	assert.Equal(t, http.StatusBadRequest, w.Code)
}

func TestHandleLockQuery_Empty(t *testing.T) {
	srv, _ := newTestServerWithLocks(t)

	req := httptest.NewRequest(http.MethodGet, "/v1/locks", nil)
	w := httptest.NewRecorder()
	srv.handleLockQuery(w, req)

	assert.Equal(t, http.StatusOK, w.Code)
	var resp map[string]interface{}
	err := json.Unmarshal(w.Body.Bytes(), &resp)
	require.NoError(t, err)
	assert.Equal(t, float64(0), resp["count"])
}

func TestHandleLockQuery_WithLocks(t *testing.T) {
	srv, _ := newTestServerWithLocks(t)

	// Acquire two locks
	body := `{"task_id":"task-1","path":"src/a.go","ttl_seconds":300}`
	req := httptest.NewRequest(http.MethodPost, "/v1/locks/acquire", bytes.NewBufferString(body))
	req.Header.Set("Content-Type", "application/json")
	w := httptest.NewRecorder()
	srv.handleLockAcquire(w, req)
	require.Equal(t, http.StatusOK, w.Code)

	body = `{"task_id":"task-2","path":"src/b.go","ttl_seconds":300}`
	req = httptest.NewRequest(http.MethodPost, "/v1/locks/acquire", bytes.NewBufferString(body))
	req.Header.Set("Content-Type", "application/json")
	w = httptest.NewRecorder()
	srv.handleLockAcquire(w, req)
	require.Equal(t, http.StatusOK, w.Code)

	// Query
	req = httptest.NewRequest(http.MethodGet, "/v1/locks", nil)
	w = httptest.NewRecorder()
	srv.handleLockQuery(w, req)

	assert.Equal(t, http.StatusOK, w.Code)
	var resp map[string]interface{}
	err := json.Unmarshal(w.Body.Bytes(), &resp)
	require.NoError(t, err)
	assert.Equal(t, float64(2), resp["count"])
}

func TestHandleLockAcquire_NoRegistry(t *testing.T) {
	srv := &Server{
		cfg: Config{
			WorkspaceRoot: t.TempDir(),
		},
		// lockRegistry is nil
	}

	body := `{"task_id":"task-1","path":"src/main.go","ttl_seconds":300}`
	req := httptest.NewRequest(http.MethodPost, "/v1/locks/acquire", bytes.NewBufferString(body))
	req.Header.Set("Content-Type", "application/json")
	w := httptest.NewRecorder()
	srv.handleLockAcquire(w, req)

	assert.Equal(t, http.StatusServiceUnavailable, w.Code)
}

func TestHandleLockRelease_NoRegistry(t *testing.T) {
	srv := &Server{
		cfg: Config{
			WorkspaceRoot: t.TempDir(),
		},
	}

	body := `{"task_id":"task-1","path":"src/main.go"}`
	req := httptest.NewRequest(http.MethodPost, "/v1/locks/release", bytes.NewBufferString(body))
	req.Header.Set("Content-Type", "application/json")
	w := httptest.NewRecorder()
	srv.handleLockRelease(w, req)

	assert.Equal(t, http.StatusServiceUnavailable, w.Code)
}

func TestHandleLockQuery_NoRegistry(t *testing.T) {
	srv := &Server{
		cfg: Config{
			WorkspaceRoot: t.TempDir(),
		},
	}

	req := httptest.NewRequest(http.MethodGet, "/v1/locks", nil)
	w := httptest.NewRecorder()
	srv.handleLockQuery(w, req)

	assert.Equal(t, http.StatusServiceUnavailable, w.Code)
}
