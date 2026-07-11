package orchestratorhost

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

func newTestSchedulerServer(t *testing.T) *Server {
	t.Helper()
	return &Server{
		cfg: Config{
			WorkspaceRoot: t.TempDir(),
		},
	}
}

// --- POST /v1/tasks (scheduler extended) ---

func TestHandleSchedulerCreateTask_Success(t *testing.T) {
	srv := newTestSchedulerServer(t)

	body := `{
		"task_id": "task-sched-1",
		"objective": "implement feature X",
		"repo": {"path": "/repos/main", "default_branch": "main"},
		"priority": "high",
		"depends_on": ["task-0"],
		"repos": [{"path": "/repos/main"}, {"path": "/repos/lib"}]
	}`
	req := httptest.NewRequest(http.MethodPost, "/v1/tasks", bytes.NewBufferString(body))
	req.Header.Set("Content-Type", "application/json")
	w := httptest.NewRecorder()

	srv.handleSchedulerCreateTask(w, req)

	assert.Equal(t, http.StatusAccepted, w.Code)

	var resp map[string]interface{}
	err := json.Unmarshal(w.Body.Bytes(), &resp)
	require.NoError(t, err)
	assert.Equal(t, "task-sched-1", resp["task_id"])
	assert.Equal(t, "high", resp["priority"])
	assert.Equal(t, "queued", resp["status"])
}

func TestHandleSchedulerCreateTask_DefaultPriority(t *testing.T) {
	srv := newTestSchedulerServer(t)

	body := `{
		"task_id": "task-default",
		"objective": "fix bug"
	}`
	req := httptest.NewRequest(http.MethodPost, "/v1/tasks", bytes.NewBufferString(body))
	req.Header.Set("Content-Type", "application/json")
	w := httptest.NewRecorder()

	srv.handleSchedulerCreateTask(w, req)

	assert.Equal(t, http.StatusAccepted, w.Code)

	var resp map[string]interface{}
	err := json.Unmarshal(w.Body.Bytes(), &resp)
	require.NoError(t, err)
	assert.Equal(t, "normal", resp["priority"])
}

func TestHandleSchedulerCreateTask_InvalidPriority(t *testing.T) {
	srv := newTestSchedulerServer(t)

	body := `{
		"task_id": "task-bad",
		"objective": "do something",
		"priority": "ultra"
	}`
	req := httptest.NewRequest(http.MethodPost, "/v1/tasks", bytes.NewBufferString(body))
	req.Header.Set("Content-Type", "application/json")
	w := httptest.NewRecorder()

	srv.handleSchedulerCreateTask(w, req)

	assert.Equal(t, http.StatusBadRequest, w.Code)

	var resp map[string]string
	err := json.Unmarshal(w.Body.Bytes(), &resp)
	require.NoError(t, err)
	assert.Contains(t, resp["error"], "invalid priority")
}

func TestHandleSchedulerCreateTask_MissingObjective(t *testing.T) {
	srv := newTestSchedulerServer(t)

	body := `{"task_id": "task-no-obj"}`
	req := httptest.NewRequest(http.MethodPost, "/v1/tasks", bytes.NewBufferString(body))
	req.Header.Set("Content-Type", "application/json")
	w := httptest.NewRecorder()

	srv.handleSchedulerCreateTask(w, req)

	assert.Equal(t, http.StatusBadRequest, w.Code)
}

func TestHandleSchedulerCreateTask_MissingTaskID(t *testing.T) {
	srv := newTestSchedulerServer(t)

	body := `{"objective": "something"}`
	req := httptest.NewRequest(http.MethodPost, "/v1/tasks", bytes.NewBufferString(body))
	req.Header.Set("Content-Type", "application/json")
	w := httptest.NewRecorder()

	srv.handleSchedulerCreateTask(w, req)

	assert.Equal(t, http.StatusBadRequest, w.Code)
}

func TestHandleSchedulerCreateTask_EmptyDependsOnEntry(t *testing.T) {
	srv := newTestSchedulerServer(t)

	body := `{
		"task_id": "task-dep",
		"objective": "something",
		"depends_on": ["task-1", ""]
	}`
	req := httptest.NewRequest(http.MethodPost, "/v1/tasks", bytes.NewBufferString(body))
	req.Header.Set("Content-Type", "application/json")
	w := httptest.NewRecorder()

	srv.handleSchedulerCreateTask(w, req)

	assert.Equal(t, http.StatusBadRequest, w.Code)
}

// --- PUT /v1/tasks/{task_id}/priority ---

func TestHandleUpdateTaskPriority_Success(t *testing.T) {
	srv := newTestSchedulerServer(t)

	body := `{"priority": "critical"}`
	req := httptest.NewRequest(http.MethodPut, "/v1/tasks/task-1/priority", bytes.NewBufferString(body))
	req.Header.Set("Content-Type", "application/json")
	req.SetPathValue("task_id", "task-1")
	w := httptest.NewRecorder()

	srv.handleUpdateTaskPriority(w, req)

	assert.Equal(t, http.StatusOK, w.Code)

	var resp map[string]interface{}
	err := json.Unmarshal(w.Body.Bytes(), &resp)
	require.NoError(t, err)
	assert.Equal(t, "task-1", resp["task_id"])
	assert.Equal(t, "critical", resp["new_priority"])
}

func TestHandleUpdateTaskPriority_InvalidPriority(t *testing.T) {
	srv := newTestSchedulerServer(t)

	body := `{"priority": "mega"}`
	req := httptest.NewRequest(http.MethodPut, "/v1/tasks/task-1/priority", bytes.NewBufferString(body))
	req.Header.Set("Content-Type", "application/json")
	req.SetPathValue("task_id", "task-1")
	w := httptest.NewRecorder()

	srv.handleUpdateTaskPriority(w, req)

	assert.Equal(t, http.StatusBadRequest, w.Code)
}

func TestHandleUpdateTaskPriority_EmptyPriority(t *testing.T) {
	srv := newTestSchedulerServer(t)

	body := `{"priority": ""}`
	req := httptest.NewRequest(http.MethodPut, "/v1/tasks/task-1/priority", bytes.NewBufferString(body))
	req.Header.Set("Content-Type", "application/json")
	req.SetPathValue("task_id", "task-1")
	w := httptest.NewRecorder()

	srv.handleUpdateTaskPriority(w, req)

	assert.Equal(t, http.StatusBadRequest, w.Code)
}

func TestHandleUpdateTaskPriority_MissingTaskID(t *testing.T) {
	srv := newTestSchedulerServer(t)

	body := `{"priority": "high"}`
	req := httptest.NewRequest(http.MethodPut, "/v1/tasks//priority", bytes.NewBufferString(body))
	req.Header.Set("Content-Type", "application/json")
	// No path value set — simulates missing task_id
	w := httptest.NewRecorder()

	srv.handleUpdateTaskPriority(w, req)

	assert.Equal(t, http.StatusBadRequest, w.Code)
}

// --- GET /v1/queue ---

func TestHandleGetQueue_EmptyQueue(t *testing.T) {
	srv := newTestSchedulerServer(t)

	req := httptest.NewRequest(http.MethodGet, "/v1/queue", nil)
	w := httptest.NewRecorder()

	srv.handleGetQueue(w, req)

	assert.Equal(t, http.StatusOK, w.Code)

	var resp map[string]interface{}
	err := json.Unmarshal(w.Body.Bytes(), &resp)
	require.NoError(t, err)
	assert.NotNil(t, resp["queue"])
	assert.Equal(t, float64(0), resp["running"])
	assert.Equal(t, float64(0), resp["queued"])
	assert.Equal(t, float64(0), resp["blocked"])
}
