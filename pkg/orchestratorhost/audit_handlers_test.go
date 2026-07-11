package orchestratorhost

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"testing"

	"github.com/stretchr/testify/require"
)

// mockAuditStore is a test implementation of the auditStore interface.
type mockAuditStore struct {
	records []AuditRecord
	// Capture query params for assertions
	lastTaskID         string
	lastRoutingOutcome string
	lastRule           string
	lastTimeStart      float64
	lastTimeEnd        float64
	lastLimit          int
}

func (m *mockAuditStore) QueryAuditRecords(
	taskID, routingOutcome, rule string,
	timeRangeStart, timeRangeEnd float64,
	limit int,
) ([]AuditRecord, error) {
	m.lastTaskID = taskID
	m.lastRoutingOutcome = routingOutcome
	m.lastRule = rule
	m.lastTimeStart = timeRangeStart
	m.lastTimeEnd = timeRangeEnd
	m.lastLimit = limit

	// Apply filters for test accuracy
	var filtered []AuditRecord
	for _, r := range m.records {
		if taskID != "" && r.TaskID != taskID {
			continue
		}
		if routingOutcome != "" && r.RoutingOutcome != routingOutcome {
			continue
		}
		if rule != "" && (r.RuleMatched == nil || *r.RuleMatched != rule) {
			continue
		}
		if timeRangeStart > 0 && r.Timestamp < timeRangeStart {
			continue
		}
		if timeRangeEnd > 0 && r.Timestamp > timeRangeEnd {
			continue
		}
		filtered = append(filtered, r)
		if len(filtered) >= limit {
			break
		}
	}
	return filtered, nil
}

func newTestServerWithAudit(store auditStore) *Server {
	root := "/tmp/test-workspace"
	s := NewServer(Config{
		SocketPath:    filepath.Join(root, "run", "vikramd.sock"),
		WorkspaceRoot: root,
	}, nil)
	s.auditStore = store
	return s
}

func TestApprovalsAudit_NoStore(t *testing.T) {
	s := NewServer(Config{
		SocketPath:    "/tmp/test.sock",
		WorkspaceRoot: "/tmp",
	}, nil)

	req := httptest.NewRequest(http.MethodGet, "/v1/approvals/audit", nil)
	rec := httptest.NewRecorder()
	s.handler().ServeHTTP(rec, req)

	require.Equal(t, http.StatusServiceUnavailable, rec.Code)
}

func TestApprovalsAudit_MethodNotAllowed(t *testing.T) {
	store := &mockAuditStore{}
	s := newTestServerWithAudit(store)

	req := httptest.NewRequest(http.MethodPost, "/v1/approvals/audit", nil)
	rec := httptest.NewRecorder()
	s.handler().ServeHTTP(rec, req)

	require.Equal(t, http.StatusMethodNotAllowed, rec.Code)
}

func TestApprovalsAudit_ReturnsAllRecords(t *testing.T) {
	ruleName := "docs-auto-approve"
	conf := 5.0
	store := &mockAuditStore{
		records: []AuditRecord{
			{
				AuditID:              "audit-1",
				TaskID:               "task-1",
				Timestamp:            1700000000.0,
				ChangeContextJSON:    `{"risk_level":"low"}`,
				RuleMatched:          &ruleName,
				RoutingOutcome:       "auto_approve",
				ConfidenceAtDecision: &conf,
			},
			{
				AuditID:              "audit-2",
				TaskID:               "task-2",
				Timestamp:            1700001000.0,
				ChangeContextJSON:    `{"risk_level":"high"}`,
				RuleMatched:          nil,
				RoutingOutcome:       "founder_review",
				ConfidenceAtDecision: nil,
			},
		},
	}
	s := newTestServerWithAudit(store)

	req := httptest.NewRequest(http.MethodGet, "/v1/approvals/audit", nil)
	rec := httptest.NewRecorder()
	s.handler().ServeHTTP(rec, req)

	require.Equal(t, http.StatusOK, rec.Code)

	var resp AuditQueryResponse
	err := json.Unmarshal(rec.Body.Bytes(), &resp)
	require.NoError(t, err)
	require.Equal(t, 2, resp.Total)
	require.Len(t, resp.Records, 2)
}

func TestApprovalsAudit_FilterByTaskID(t *testing.T) {
	ruleName := "docs-auto-approve"
	store := &mockAuditStore{
		records: []AuditRecord{
			{AuditID: "a1", TaskID: "task-1", Timestamp: 1700000000.0, RoutingOutcome: "auto_approve", RuleMatched: &ruleName},
			{AuditID: "a2", TaskID: "task-2", Timestamp: 1700001000.0, RoutingOutcome: "founder_review", RuleMatched: nil},
		},
	}
	s := newTestServerWithAudit(store)

	req := httptest.NewRequest(http.MethodGet, "/v1/approvals/audit?task_id=task-1", nil)
	rec := httptest.NewRecorder()
	s.handler().ServeHTTP(rec, req)

	require.Equal(t, http.StatusOK, rec.Code)
	var resp AuditQueryResponse
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &resp))
	require.Equal(t, 1, resp.Total)
	require.Equal(t, "task-1", resp.Records[0].TaskID)
}

func TestApprovalsAudit_FilterByTimeRange(t *testing.T) {
	store := &mockAuditStore{
		records: []AuditRecord{
			{AuditID: "a1", TaskID: "task-1", Timestamp: 1700000000.0, RoutingOutcome: "auto_approve"},
			{AuditID: "a2", TaskID: "task-2", Timestamp: 1700005000.0, RoutingOutcome: "founder_review"},
			{AuditID: "a3", TaskID: "task-3", Timestamp: 1700010000.0, RoutingOutcome: "auto_approve"},
		},
	}
	s := newTestServerWithAudit(store)

	req := httptest.NewRequest(http.MethodGet, "/v1/approvals/audit?time_start=1700004000&time_end=1700006000", nil)
	rec := httptest.NewRecorder()
	s.handler().ServeHTTP(rec, req)

	require.Equal(t, http.StatusOK, rec.Code)
	var resp AuditQueryResponse
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &resp))
	require.Equal(t, 1, resp.Total)
	require.Equal(t, "task-2", resp.Records[0].TaskID)
}

func TestApprovalsAudit_FilterByRule(t *testing.T) {
	rule1 := "security-always-review"
	rule2 := "docs-auto-approve"
	store := &mockAuditStore{
		records: []AuditRecord{
			{AuditID: "a1", TaskID: "task-1", Timestamp: 1700000000.0, RoutingOutcome: "founder_review", RuleMatched: &rule1},
			{AuditID: "a2", TaskID: "task-2", Timestamp: 1700001000.0, RoutingOutcome: "auto_approve", RuleMatched: &rule2},
		},
	}
	s := newTestServerWithAudit(store)

	req := httptest.NewRequest(http.MethodGet, "/v1/approvals/audit?rule=docs-auto-approve", nil)
	rec := httptest.NewRecorder()
	s.handler().ServeHTTP(rec, req)

	require.Equal(t, http.StatusOK, rec.Code)
	var resp AuditQueryResponse
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &resp))
	require.Equal(t, 1, resp.Total)
	require.Equal(t, "task-2", resp.Records[0].TaskID)
}

func TestApprovalsAudit_FilterByRoutingOutcome(t *testing.T) {
	store := &mockAuditStore{
		records: []AuditRecord{
			{AuditID: "a1", TaskID: "task-1", Timestamp: 1700000000.0, RoutingOutcome: "auto_approve"},
			{AuditID: "a2", TaskID: "task-2", Timestamp: 1700001000.0, RoutingOutcome: "founder_review"},
			{AuditID: "a3", TaskID: "task-3", Timestamp: 1700002000.0, RoutingOutcome: "auto_approve"},
		},
	}
	s := newTestServerWithAudit(store)

	req := httptest.NewRequest(http.MethodGet, "/v1/approvals/audit?routing_outcome=auto_approve", nil)
	rec := httptest.NewRecorder()
	s.handler().ServeHTTP(rec, req)

	require.Equal(t, http.StatusOK, rec.Code)
	var resp AuditQueryResponse
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &resp))
	require.Equal(t, 2, resp.Total)
}

func TestApprovalsAudit_LimitEnforced(t *testing.T) {
	records := make([]AuditRecord, 50)
	for i := range records {
		records[i] = AuditRecord{
			AuditID:        "a" + string(rune('0'+i)),
			TaskID:         "task-1",
			Timestamp:      float64(1700000000 + i),
			RoutingOutcome: "auto_approve",
		}
	}
	store := &mockAuditStore{records: records}
	s := newTestServerWithAudit(store)

	req := httptest.NewRequest(http.MethodGet, "/v1/approvals/audit?limit=5", nil)
	rec := httptest.NewRecorder()
	s.handler().ServeHTTP(rec, req)

	require.Equal(t, http.StatusOK, rec.Code)
	var resp AuditQueryResponse
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &resp))
	require.Equal(t, 5, resp.Total)
	require.Len(t, resp.Records, 5)
}

func TestApprovalsAudit_CombinedFilters(t *testing.T) {
	rule := "docs-auto-approve"
	store := &mockAuditStore{
		records: []AuditRecord{
			{AuditID: "a1", TaskID: "task-1", Timestamp: 1700000000.0, RoutingOutcome: "auto_approve", RuleMatched: &rule},
			{AuditID: "a2", TaskID: "task-1", Timestamp: 1700005000.0, RoutingOutcome: "founder_review", RuleMatched: nil},
			{AuditID: "a3", TaskID: "task-2", Timestamp: 1700005000.0, RoutingOutcome: "auto_approve", RuleMatched: &rule},
		},
	}
	s := newTestServerWithAudit(store)

	// Filter: task-1 + auto_approve + rule=docs-auto-approve
	req := httptest.NewRequest(http.MethodGet, "/v1/approvals/audit?task_id=task-1&routing_outcome=auto_approve&rule=docs-auto-approve", nil)
	rec := httptest.NewRecorder()
	s.handler().ServeHTTP(rec, req)

	require.Equal(t, http.StatusOK, rec.Code)
	var resp AuditQueryResponse
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &resp))
	require.Equal(t, 1, resp.Total)
	require.Equal(t, "a1", resp.Records[0].AuditID)
}
