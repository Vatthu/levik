package console

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// --- Archive unit tests ---

func TestArchiveRejectsNonTerminalStatus(t *testing.T) {
	a := NewArchive()
	err := a.ArchiveTask(ArchivedTask{
		TaskID: "t-1",
		Status: "running",
	})
	assert.Error(t, err)
	assert.Contains(t, err.Error(), "non-terminal status")
}

func TestArchiveAcceptsTerminalStatuses(t *testing.T) {
	a := NewArchive()
	for _, status := range []string{"merged", "completed", "failed-final", "cancelled"} {
		err := a.ArchiveTask(ArchivedTask{
			TaskID: "t-" + status,
			Status: status,
		})
		assert.NoError(t, err, "should accept status %q", status)
	}
	assert.Equal(t, 4, a.Count())
}

func TestArchiveSearchFullText(t *testing.T) {
	a := NewArchive()
	a.ArchiveTask(ArchivedTask{TaskID: "t-1", Status: "completed", Objective: "Implement user login"})
	a.ArchiveTask(ArchivedTask{TaskID: "t-2", Status: "completed", Objective: "Fix payment bug"})
	a.ArchiveTask(ArchivedTask{TaskID: "t-3", Status: "merged", Objective: "Add login tests"})

	result := a.Search(ArchiveFilter{Query: "login"})
	assert.Equal(t, 2, result.Total)
	assert.Equal(t, "t-1", result.Tasks[0].TaskID)
	assert.Equal(t, "t-3", result.Tasks[1].TaskID)
}

func TestArchiveSearchByStatus(t *testing.T) {
	a := NewArchive()
	a.ArchiveTask(ArchivedTask{TaskID: "t-1", Status: "completed"})
	a.ArchiveTask(ArchivedTask{TaskID: "t-2", Status: "cancelled"})
	a.ArchiveTask(ArchivedTask{TaskID: "t-3", Status: "completed"})

	result := a.Search(ArchiveFilter{Status: "cancelled"})
	assert.Equal(t, 1, result.Total)
	assert.Equal(t, "t-2", result.Tasks[0].TaskID)
}

func TestArchiveSearchByRepository(t *testing.T) {
	a := NewArchive()
	a.ArchiveTask(ArchivedTask{TaskID: "t-1", Status: "completed", Repository: "/home/dev/myapp"})
	a.ArchiveTask(ArchivedTask{TaskID: "t-2", Status: "completed", Repository: "/home/dev/other"})

	result := a.Search(ArchiveFilter{Repository: "myapp"})
	assert.Equal(t, 1, result.Total)
	assert.Equal(t, "t-1", result.Tasks[0].TaskID)
}

func TestArchiveSearchByDateRange(t *testing.T) {
	a := NewArchive()
	a.ArchiveTask(ArchivedTask{TaskID: "t-1", Status: "completed", CompletedAt: time.Date(2024, 1, 15, 0, 0, 0, 0, time.UTC)})
	a.ArchiveTask(ArchivedTask{TaskID: "t-2", Status: "completed", CompletedAt: time.Date(2024, 3, 20, 0, 0, 0, 0, time.UTC)})
	a.ArchiveTask(ArchivedTask{TaskID: "t-3", Status: "completed", CompletedAt: time.Date(2024, 6, 1, 0, 0, 0, 0, time.UTC)})

	result := a.Search(ArchiveFilter{DateFrom: "2024-02-01", DateTo: "2024-04-30"})
	assert.Equal(t, 1, result.Total)
	assert.Equal(t, "t-2", result.Tasks[0].TaskID)
}

func TestArchiveSearchByComplexityTier(t *testing.T) {
	a := NewArchive()
	a.ArchiveTask(ArchivedTask{TaskID: "t-1", Status: "completed", ComplexityTier: "trivial"})
	a.ArchiveTask(ArchivedTask{TaskID: "t-2", Status: "completed", ComplexityTier: "complex"})

	result := a.Search(ArchiveFilter{ComplexityTier: "complex"})
	assert.Equal(t, 1, result.Total)
	assert.Equal(t, "t-2", result.Tasks[0].TaskID)
}

func TestArchiveSearchByFormation(t *testing.T) {
	a := NewArchive()
	a.ArchiveTask(ArchivedTask{TaskID: "t-1", Status: "completed", Formation: "alpha-squad"})
	a.ArchiveTask(ArchivedTask{TaskID: "t-2", Status: "completed", Formation: "beta-team"})

	result := a.Search(ArchiveFilter{Formation: "alpha-squad"})
	assert.Equal(t, 1, result.Total)
	assert.Equal(t, "t-1", result.Tasks[0].TaskID)
}

func TestArchiveSearchByCostRange(t *testing.T) {
	a := NewArchive()
	a.ArchiveTask(ArchivedTask{TaskID: "t-1", Status: "completed", TotalCostUSD: 0.50})
	a.ArchiveTask(ArchivedTask{TaskID: "t-2", Status: "completed", TotalCostUSD: 2.00})
	a.ArchiveTask(ArchivedTask{TaskID: "t-3", Status: "completed", TotalCostUSD: 5.00})

	result := a.Search(ArchiveFilter{CostMin: 1.0, CostMax: 3.0})
	assert.Equal(t, 1, result.Total)
	assert.Equal(t, "t-2", result.Tasks[0].TaskID)
}

func TestArchiveSearchPagination(t *testing.T) {
	a := NewArchive()
	for i := 0; i < 25; i++ {
		a.ArchiveTask(ArchivedTask{TaskID: "t-" + string(rune('a'+i)), Status: "completed"})
	}

	// Default page size is 20.
	result := a.Search(ArchiveFilter{})
	assert.Equal(t, 25, result.Total)
	assert.Equal(t, 20, len(result.Tasks))
	assert.Equal(t, 1, result.Page)
	assert.Equal(t, 2, result.TotalPages)

	// Page 2.
	result = a.Search(ArchiveFilter{Page: 2})
	assert.Equal(t, 25, result.Total)
	assert.Equal(t, 5, len(result.Tasks))
	assert.Equal(t, 2, result.Page)
}

func TestArchiveSearchCustomPageSize(t *testing.T) {
	a := NewArchive()
	for i := 0; i < 10; i++ {
		a.ArchiveTask(ArchivedTask{TaskID: "t-x", Status: "completed"})
	}

	result := a.Search(ArchiveFilter{PageSize: 3, Page: 2})
	assert.Equal(t, 10, result.Total)
	assert.Equal(t, 3, len(result.Tasks))
	assert.Equal(t, 4, result.TotalPages)
}

func TestArchiveExport(t *testing.T) {
	a := NewArchive()
	a.ArchiveTask(ArchivedTask{TaskID: "t-1", Status: "completed", Repository: "repo-a"})
	a.ArchiveTask(ArchivedTask{TaskID: "t-2", Status: "cancelled", Repository: "repo-b"})
	a.ArchiveTask(ArchivedTask{TaskID: "t-3", Status: "completed", Repository: "repo-a"})

	tasks := a.Export(ArchiveFilter{Repository: "repo-a"})
	assert.Len(t, tasks, 2)
}

func TestArchiveRetentionPolicyDefault(t *testing.T) {
	a := NewArchive()
	policy := a.GetRetentionPolicy()
	assert.Equal(t, 365, policy.MinRetentionDays)
	assert.False(t, policy.AutoPurge)
}

func TestArchiveRetentionPolicyClampedTo365(t *testing.T) {
	a := NewArchive()
	a.SetRetentionPolicy(RetentionPolicy{MinRetentionDays: 30, AutoPurge: true})
	policy := a.GetRetentionPolicy()
	assert.Equal(t, 365, policy.MinRetentionDays)
	assert.True(t, policy.AutoPurge)
}

func TestArchivePurgeExpired(t *testing.T) {
	a := NewArchive()
	old := time.Now().AddDate(-2, 0, 0)
	recent := time.Now().Add(-24 * time.Hour)

	a.ArchiveTask(ArchivedTask{TaskID: "t-old", Status: "completed", ArchivedAt: old})
	a.ArchiveTask(ArchivedTask{TaskID: "t-recent", Status: "completed", ArchivedAt: recent})

	// Manually set the older task's archived time.
	a.mu.Lock()
	a.tasks[0].ArchivedAt = old
	a.mu.Unlock()

	purged := a.PurgeExpired()
	assert.Equal(t, 1, purged)
	assert.Equal(t, 1, a.Count())
}

// --- HTTP handler tests ---

func TestHandleArchiveSearchRejectsNonGET(t *testing.T) {
	s := testConsoleServerWithArchive()
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/api/archive", nil)
	s.handleArchiveSearch(rec, req)
	assert.Equal(t, http.StatusMethodNotAllowed, rec.Code)
}

func TestHandleArchiveSearchEmptyArchive(t *testing.T) {
	s := testConsoleServerWithArchive()
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/archive", nil)
	s.handleArchiveSearch(rec, req)

	assert.Equal(t, http.StatusOK, rec.Code)
	var resp ArchiveSearchResponse
	require.NoError(t, json.NewDecoder(rec.Body).Decode(&resp))
	assert.Equal(t, 0, resp.Total)
	assert.Empty(t, resp.Tasks)
}

func TestHandleArchiveSearchWithFilters(t *testing.T) {
	s := testConsoleServerWithArchive()
	s.archive.ArchiveTask(ArchivedTask{TaskID: "t-1", Status: "completed", Objective: "fix login"})
	s.archive.ArchiveTask(ArchivedTask{TaskID: "t-2", Status: "merged", Objective: "add tests"})

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/archive?q=login", nil)
	s.handleArchiveSearch(rec, req)

	assert.Equal(t, http.StatusOK, rec.Code)
	var resp ArchiveSearchResponse
	require.NoError(t, json.NewDecoder(rec.Body).Decode(&resp))
	assert.Equal(t, 1, resp.Total)
	assert.Equal(t, "t-1", resp.Tasks[0].TaskID)
}

func TestHandleArchiveSearchWithCostFilter(t *testing.T) {
	s := testConsoleServerWithArchive()
	s.archive.ArchiveTask(ArchivedTask{TaskID: "t-1", Status: "completed", TotalCostUSD: 1.0})
	s.archive.ArchiveTask(ArchivedTask{TaskID: "t-2", Status: "completed", TotalCostUSD: 5.0})

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/archive?cost_min=2.0&cost_max=10.0", nil)
	s.handleArchiveSearch(rec, req)

	assert.Equal(t, http.StatusOK, rec.Code)
	var resp ArchiveSearchResponse
	require.NoError(t, json.NewDecoder(rec.Body).Decode(&resp))
	assert.Equal(t, 1, resp.Total)
	assert.Equal(t, "t-2", resp.Tasks[0].TaskID)
}

func TestHandleArchiveExportRejectsNonPOST(t *testing.T) {
	s := testConsoleServerWithArchive()
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/archive/export", nil)
	s.handleArchiveExport(rec, req)
	assert.Equal(t, http.StatusMethodNotAllowed, rec.Code)
}

func TestHandleArchiveExportReturnsJSON(t *testing.T) {
	s := testConsoleServerWithArchive()
	s.archive.ArchiveTask(ArchivedTask{TaskID: "t-1", Status: "completed", Objective: "task one"})
	s.archive.ArchiveTask(ArchivedTask{TaskID: "t-2", Status: "cancelled", Objective: "task two"})

	exportReq := ArchiveExportRequest{
		Filter: ArchiveFilter{Status: "completed"},
		Format: "json",
	}
	data, _ := json.Marshal(exportReq)
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/api/archive/export", bytes.NewReader(data))
	s.handleArchiveExport(rec, req)

	assert.Equal(t, http.StatusOK, rec.Code)
	assert.Contains(t, rec.Header().Get("Content-Disposition"), "archive_export.json")

	var result map[string]interface{}
	require.NoError(t, json.NewDecoder(rec.Body).Decode(&result))
	assert.Equal(t, float64(1), result["exported"])
	assert.Equal(t, "json", result["format"])
	tasks := result["tasks"].([]interface{})
	assert.Len(t, tasks, 1)
}

func TestHandleArchiveExportRejectsInvalidFormat(t *testing.T) {
	s := testConsoleServerWithArchive()
	exportReq := ArchiveExportRequest{Format: "csv"}
	data, _ := json.Marshal(exportReq)
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/api/archive/export", bytes.NewReader(data))
	s.handleArchiveExport(rec, req)
	assert.Equal(t, http.StatusBadRequest, rec.Code)
}

func TestHandleArchiveExportEmptyArchive(t *testing.T) {
	s := testConsoleServerWithArchive()
	exportReq := ArchiveExportRequest{Format: "json"}
	data, _ := json.Marshal(exportReq)
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/api/archive/export", bytes.NewReader(data))
	s.handleArchiveExport(rec, req)

	assert.Equal(t, http.StatusOK, rec.Code)
	var result map[string]interface{}
	require.NoError(t, json.NewDecoder(rec.Body).Decode(&result))
	assert.Equal(t, float64(0), result["exported"])
}

// --- Test helpers ---

func testConsoleServerWithArchive() *Server {
	s := testConsoleServer(nil)
	s.archive = NewArchive()
	return s
}
