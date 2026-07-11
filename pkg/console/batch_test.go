package console

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

func TestHandleBatchTasksRejectsNonPOST(t *testing.T) {
	s := testConsoleServer(nil)
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/tasks/batch", nil)
	s.handleBatchTasks(rec, req)
	assert.Equal(t, http.StatusMethodNotAllowed, rec.Code)
}

func TestHandleBatchTasksRejectsInvalidJSON(t *testing.T) {
	s := testConsoleServer(nil)
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/api/tasks/batch", bytes.NewBufferString("not json"))
	s.handleBatchTasks(rec, req)
	assert.Equal(t, http.StatusBadRequest, rec.Code)
}

func TestHandleBatchTasksRejectsEmptyTaskIDs(t *testing.T) {
	s := testConsoleServer(nil)
	body := BatchRequest{Action: BatchActionApprove, TaskIDs: []string{}}
	data, _ := json.Marshal(body)
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/api/tasks/batch", bytes.NewReader(data))
	s.handleBatchTasks(rec, req)
	assert.Equal(t, http.StatusBadRequest, rec.Code)
}

func TestHandleBatchTasksRejectsInvalidAction(t *testing.T) {
	s := testConsoleServer(nil)
	body := BatchRequest{Action: "invalid_action", TaskIDs: []string{"task-1"}}
	data, _ := json.Marshal(body)
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/api/tasks/batch", bytes.NewReader(data))
	s.handleBatchTasks(rec, req)
	assert.Equal(t, http.StatusBadRequest, rec.Code)
}

func TestHandleBatchTasksRequiresConfirmForDestructiveActions(t *testing.T) {
	s := testConsoleServer(nil)

	for _, action := range []BatchAction{BatchActionReject, BatchActionCancel} {
		body := BatchRequest{
			Action:  action,
			TaskIDs: []string{"task-1", "task-2", "task-3"},
		}
		data, _ := json.Marshal(body)
		rec := httptest.NewRecorder()
		req := httptest.NewRequest(http.MethodPost, "/api/tasks/batch", bytes.NewReader(data))
		s.handleBatchTasks(rec, req)

		assert.Equal(t, http.StatusOK, rec.Code)
		var resp BatchResponse
		require.NoError(t, json.NewDecoder(rec.Body).Decode(&resp))
		assert.True(t, resp.RequiresConfirm)
		assert.Contains(t, resp.ConfirmationMsg, "3 task(s)")
		assert.Equal(t, 3, resp.TotalRequested)
		assert.Equal(t, []string{"task-1", "task-2", "task-3"}, resp.AffectedTaskIDs)
	}
}

func TestHandleBatchTasksNonDestructiveSkipsConfirmation(t *testing.T) {
	// Approve is non-destructive; will fail at orchestrator level but should not require confirm.
	transport := roundTripFunc(func(r *http.Request) (*http.Response, error) {
		return testJSONResponse(t, http.StatusOK, map[string]string{"status": "ok"}), nil
	})
	s := testConsoleServer(transport)

	body := BatchRequest{
		Action:  BatchActionApprove,
		TaskIDs: []string{"task-1"},
	}
	data, _ := json.Marshal(body)
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/api/tasks/batch", bytes.NewReader(data))
	s.handleBatchTasks(rec, req)

	assert.Equal(t, http.StatusOK, rec.Code)
	var resp BatchResponse
	require.NoError(t, json.NewDecoder(rec.Body).Decode(&resp))
	assert.False(t, resp.RequiresConfirm)
	assert.Equal(t, 1, resp.Succeeded)
	assert.Equal(t, 0, resp.Failed)
}

func TestHandleBatchTasksExecutesConfirmedDestructiveAction(t *testing.T) {
	var requestedPaths []string
	transport := roundTripFunc(func(r *http.Request) (*http.Response, error) {
		requestedPaths = append(requestedPaths, r.URL.Path)
		return testJSONResponse(t, http.StatusOK, map[string]string{"status": "ok"}), nil
	})
	s := testConsoleServer(transport)

	body := BatchRequest{
		Action:    BatchActionCancel,
		TaskIDs:   []string{"task-1", "task-2"},
		Confirmed: true,
		Comment:   "cleanup",
	}
	data, _ := json.Marshal(body)
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/api/tasks/batch", bytes.NewReader(data))
	s.handleBatchTasks(rec, req)

	assert.Equal(t, http.StatusOK, rec.Code)
	var resp BatchResponse
	require.NoError(t, json.NewDecoder(rec.Body).Decode(&resp))
	assert.Equal(t, 2, resp.TotalRequested)
	assert.Equal(t, 2, resp.Succeeded)
	assert.Equal(t, 0, resp.Failed)
	assert.Len(t, requestedPaths, 2)
	assert.Equal(t, "/v1/tasks/task-1/cancel", requestedPaths[0])
	assert.Equal(t, "/v1/tasks/task-2/cancel", requestedPaths[1])
}

func TestHandleBatchTasksReportsPartialSuccess(t *testing.T) {
	callCount := 0
	transport := roundTripFunc(func(r *http.Request) (*http.Response, error) {
		callCount++
		if callCount == 2 {
			return testJSONResponse(t, http.StatusInternalServerError, map[string]string{"error": "task stuck"}), nil
		}
		return testJSONResponse(t, http.StatusOK, map[string]string{"status": "ok"}), nil
	})
	s := testConsoleServer(transport)

	body := BatchRequest{
		Action:  BatchActionApprove,
		TaskIDs: []string{"task-1", "task-2", "task-3"},
	}
	data, _ := json.Marshal(body)
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/api/tasks/batch", bytes.NewReader(data))
	s.handleBatchTasks(rec, req)

	assert.Equal(t, http.StatusOK, rec.Code)
	var resp BatchResponse
	require.NoError(t, json.NewDecoder(rec.Body).Decode(&resp))
	assert.Equal(t, 3, resp.TotalRequested)
	assert.Equal(t, 2, resp.Succeeded)
	assert.Equal(t, 1, resp.Failed)

	// Check individual results.
	assert.True(t, resp.Results[0].Success)
	assert.False(t, resp.Results[1].Success)
	assert.NotEmpty(t, resp.Results[1].Error)
	assert.True(t, resp.Results[2].Success)
}

func TestHandleBatchTasksReprioritize(t *testing.T) {
	var receivedBody map[string]interface{}
	transport := roundTripFunc(func(r *http.Request) (*http.Response, error) {
		assert.Contains(t, r.URL.Path, "/priority")
		json.NewDecoder(r.Body).Decode(&receivedBody)
		return testJSONResponse(t, http.StatusOK, map[string]string{"status": "ok"}), nil
	})
	s := testConsoleServer(transport)

	body := BatchRequest{
		Action:   BatchActionReprioritize,
		TaskIDs:  []string{"task-1"},
		Priority: 5,
	}
	data, _ := json.Marshal(body)
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/api/tasks/batch", bytes.NewReader(data))
	s.handleBatchTasks(rec, req)

	assert.Equal(t, http.StatusOK, rec.Code)
	var resp BatchResponse
	require.NoError(t, json.NewDecoder(rec.Body).Decode(&resp))
	assert.Equal(t, 1, resp.Succeeded)
	assert.Equal(t, float64(5), receivedBody["priority"])
}

func TestHandleBatchTasksResume(t *testing.T) {
	var receivedBody map[string]interface{}
	transport := roundTripFunc(func(r *http.Request) (*http.Response, error) {
		assert.Contains(t, r.URL.Path, "/resume")
		json.NewDecoder(r.Body).Decode(&receivedBody)
		return testJSONResponse(t, http.StatusOK, map[string]string{"status": "ok"}), nil
	})
	s := testConsoleServer(transport)

	body := BatchRequest{
		Action:  BatchActionResume,
		TaskIDs: []string{"task-1"},
		Comment: "budget increased",
	}
	data, _ := json.Marshal(body)
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/api/tasks/batch", bytes.NewReader(data))
	s.handleBatchTasks(rec, req)

	assert.Equal(t, http.StatusOK, rec.Code)
	var resp BatchResponse
	require.NoError(t, json.NewDecoder(rec.Body).Decode(&resp))
	assert.Equal(t, 1, resp.Succeeded)
	assert.Equal(t, "approve", receivedBody["decision"])
	assert.Equal(t, "budget increased", receivedBody["comment"])
}
