package orchestratorhost

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// mockKnowledgeStore implements knowledgeStore for testing.
type mockKnowledgeStore struct {
	approaches []ApproachEffectivenessRecord
	patterns   []FailurePatternRecord
}

func (m *mockKnowledgeStore) GetApproachEffectiveness(repoPath, taskType, tier string) ([]ApproachEffectivenessRecord, error) {
	var result []ApproachEffectivenessRecord
	for _, a := range m.approaches {
		if repoPath != "" && a.RepoPath != repoPath {
			continue
		}
		if taskType != "" && a.TaskType != taskType {
			continue
		}
		if tier != "" && a.ComplexityTier != tier {
			continue
		}
		result = append(result, a)
	}
	return result, nil
}

func (m *mockKnowledgeStore) GetFailurePatterns(repoPath, failureClass, model string, minFrequency int) ([]FailurePatternRecord, error) {
	var result []FailurePatternRecord
	for _, p := range m.patterns {
		if repoPath != "" && p.RepoPath != repoPath {
			continue
		}
		if failureClass != "" && p.FailureClass != failureClass {
			continue
		}
		if minFrequency > 0 && p.Frequency < minFrequency {
			continue
		}
		result = append(result, p)
	}
	return result, nil
}

func setupKnowledgeTestServer(store *mockKnowledgeStore) *Server {
	root := "/tmp/knowledge-test"
	srv := NewServer(Config{
		SocketPath:    filepath.Join(root, "run", "vikramd.sock"),
		WorkspaceRoot: root,
	}, nil)
	srv.SetKnowledgeStore(store)
	return srv
}

// --- Knowledge Approaches Tests ---

func TestKnowledgeApproaches_ReturnsData(t *testing.T) {
	store := &mockKnowledgeStore{
		approaches: []ApproachEffectivenessRecord{
			{
				RepoPath:       "/repos/myapp",
				TaskType:       "bugfix",
				ComplexityTier: "moderate",
				TotalRecords:   25,
				SuccessRate:    0.80,
				CostEfficiency: 1.2,
				TimeEfficiency: 1.5,
				FirstPassRate:  0.60,
				CompositeScore: 0.72,
			},
			{
				RepoPath:       "/repos/myapp",
				TaskType:       "feature",
				ComplexityTier: "complex",
				TotalRecords:   10,
				SuccessRate:    0.70,
				CostEfficiency: 0.9,
				TimeEfficiency: 1.0,
				FirstPassRate:  0.40,
				CompositeScore: 0.58,
			},
		},
	}
	srv := setupKnowledgeTestServer(store)

	req := httptest.NewRequest(http.MethodGet, "/v1/knowledge/approaches", nil)
	w := httptest.NewRecorder()
	srv.handler().ServeHTTP(w, req)

	assert.Equal(t, http.StatusOK, w.Code)

	var resp map[string]interface{}
	require.NoError(t, json.Unmarshal(w.Body.Bytes(), &resp))
	approaches := resp["approaches"].([]interface{})
	assert.Len(t, approaches, 2)

	first := approaches[0].(map[string]interface{})
	assert.Equal(t, "/repos/myapp", first["repo_path"])
	assert.Equal(t, "bugfix", first["task_type"])
	assert.InDelta(t, 0.80, first["success_rate"], 0.001)
}

func TestKnowledgeApproaches_FiltersByRepoPath(t *testing.T) {
	store := &mockKnowledgeStore{
		approaches: []ApproachEffectivenessRecord{
			{RepoPath: "/repos/alpha", TaskType: "bugfix", ComplexityTier: "moderate", CompositeScore: 0.7},
			{RepoPath: "/repos/beta", TaskType: "bugfix", ComplexityTier: "moderate", CompositeScore: 0.6},
		},
	}
	srv := setupKnowledgeTestServer(store)

	req := httptest.NewRequest(http.MethodGet, "/v1/knowledge/approaches?repo_path=/repos/alpha", nil)
	w := httptest.NewRecorder()
	srv.handler().ServeHTTP(w, req)

	assert.Equal(t, http.StatusOK, w.Code)

	var resp map[string]interface{}
	require.NoError(t, json.Unmarshal(w.Body.Bytes(), &resp))
	approaches := resp["approaches"].([]interface{})
	assert.Len(t, approaches, 1)
	first := approaches[0].(map[string]interface{})
	assert.Equal(t, "/repos/alpha", first["repo_path"])
}

func TestKnowledgeApproaches_FiltersByTaskType(t *testing.T) {
	store := &mockKnowledgeStore{
		approaches: []ApproachEffectivenessRecord{
			{RepoPath: "/repos/app", TaskType: "bugfix", ComplexityTier: "moderate", CompositeScore: 0.7},
			{RepoPath: "/repos/app", TaskType: "feature", ComplexityTier: "complex", CompositeScore: 0.6},
		},
	}
	srv := setupKnowledgeTestServer(store)

	req := httptest.NewRequest(http.MethodGet, "/v1/knowledge/approaches?task_type=feature", nil)
	w := httptest.NewRecorder()
	srv.handler().ServeHTTP(w, req)

	assert.Equal(t, http.StatusOK, w.Code)

	var resp map[string]interface{}
	require.NoError(t, json.Unmarshal(w.Body.Bytes(), &resp))
	approaches := resp["approaches"].([]interface{})
	assert.Len(t, approaches, 1)
	first := approaches[0].(map[string]interface{})
	assert.Equal(t, "feature", first["task_type"])
}

func TestKnowledgeApproaches_FiltersByComplexityTier(t *testing.T) {
	store := &mockKnowledgeStore{
		approaches: []ApproachEffectivenessRecord{
			{RepoPath: "/repos/app", TaskType: "bugfix", ComplexityTier: "routine", CompositeScore: 0.9},
			{RepoPath: "/repos/app", TaskType: "bugfix", ComplexityTier: "critical", CompositeScore: 0.5},
		},
	}
	srv := setupKnowledgeTestServer(store)

	req := httptest.NewRequest(http.MethodGet, "/v1/knowledge/approaches?complexity_tier=critical", nil)
	w := httptest.NewRecorder()
	srv.handler().ServeHTTP(w, req)

	assert.Equal(t, http.StatusOK, w.Code)

	var resp map[string]interface{}
	require.NoError(t, json.Unmarshal(w.Body.Bytes(), &resp))
	approaches := resp["approaches"].([]interface{})
	assert.Len(t, approaches, 1)
	first := approaches[0].(map[string]interface{})
	assert.Equal(t, "critical", first["complexity_tier"])
}

func TestKnowledgeApproaches_Returns503WithoutStore(t *testing.T) {
	root := "/tmp/knowledge-test"
	srv := NewServer(Config{
		SocketPath:    filepath.Join(root, "run", "vikramd.sock"),
		WorkspaceRoot: root,
	}, nil)
	// No knowledge store set

	req := httptest.NewRequest(http.MethodGet, "/v1/knowledge/approaches", nil)
	w := httptest.NewRecorder()
	srv.handler().ServeHTTP(w, req)

	assert.Equal(t, http.StatusServiceUnavailable, w.Code)
}

func TestKnowledgeApproaches_RejectsNonGET(t *testing.T) {
	store := &mockKnowledgeStore{}
	srv := setupKnowledgeTestServer(store)

	req := httptest.NewRequest(http.MethodPost, "/v1/knowledge/approaches", nil)
	w := httptest.NewRecorder()
	srv.handler().ServeHTTP(w, req)

	assert.Equal(t, http.StatusMethodNotAllowed, w.Code)
}

// --- Knowledge Failures Tests ---

func TestKnowledgeFailures_ReturnsData(t *testing.T) {
	store := &mockKnowledgeStore{
		patterns: []FailurePatternRecord{
			{
				PatternID:              "fp-001",
				RepoPath:               "/repos/myapp",
				FailureClass:           "model_limitation",
				ErrorSignature:         "context_window_exceeded",
				Frequency:              5,
				LastSeen:               1700000000.0,
				SuccessfulAlternatives: []string{"use_claude_opus", "split_task"},
			},
			{
				PatternID:              "fp-002",
				RepoPath:               "/repos/myapp",
				FailureClass:           "timeout",
				ErrorSignature:         "verification_timeout_300s",
				Frequency:              3,
				LastSeen:               1700001000.0,
				SuccessfulAlternatives: []string{"increase_timeout", "skip_integration_tests"},
			},
		},
	}
	srv := setupKnowledgeTestServer(store)

	req := httptest.NewRequest(http.MethodGet, "/v1/knowledge/failures", nil)
	w := httptest.NewRecorder()
	srv.handler().ServeHTTP(w, req)

	assert.Equal(t, http.StatusOK, w.Code)

	var resp map[string]interface{}
	require.NoError(t, json.Unmarshal(w.Body.Bytes(), &resp))
	patterns := resp["patterns"].([]interface{})
	assert.Len(t, patterns, 2)

	first := patterns[0].(map[string]interface{})
	assert.Equal(t, "fp-001", first["pattern_id"])
	assert.Equal(t, "model_limitation", first["failure_class"])
	assert.InDelta(t, 5.0, first["frequency"], 0.001)
}

func TestKnowledgeFailures_FiltersByRepoPath(t *testing.T) {
	store := &mockKnowledgeStore{
		patterns: []FailurePatternRecord{
			{PatternID: "fp-001", RepoPath: "/repos/alpha", FailureClass: "timeout", Frequency: 3},
			{PatternID: "fp-002", RepoPath: "/repos/beta", FailureClass: "timeout", Frequency: 2},
		},
	}
	srv := setupKnowledgeTestServer(store)

	req := httptest.NewRequest(http.MethodGet, "/v1/knowledge/failures?repo_path=/repos/alpha", nil)
	w := httptest.NewRecorder()
	srv.handler().ServeHTTP(w, req)

	assert.Equal(t, http.StatusOK, w.Code)

	var resp map[string]interface{}
	require.NoError(t, json.Unmarshal(w.Body.Bytes(), &resp))
	patterns := resp["patterns"].([]interface{})
	assert.Len(t, patterns, 1)
	first := patterns[0].(map[string]interface{})
	assert.Equal(t, "/repos/alpha", first["repo_path"])
}

func TestKnowledgeFailures_FiltersByFailureClass(t *testing.T) {
	store := &mockKnowledgeStore{
		patterns: []FailurePatternRecord{
			{PatternID: "fp-001", RepoPath: "/repos/app", FailureClass: "model_limitation", Frequency: 5},
			{PatternID: "fp-002", RepoPath: "/repos/app", FailureClass: "timeout", Frequency: 3},
		},
	}
	srv := setupKnowledgeTestServer(store)

	req := httptest.NewRequest(http.MethodGet, "/v1/knowledge/failures?failure_class=timeout", nil)
	w := httptest.NewRecorder()
	srv.handler().ServeHTTP(w, req)

	assert.Equal(t, http.StatusOK, w.Code)

	var resp map[string]interface{}
	require.NoError(t, json.Unmarshal(w.Body.Bytes(), &resp))
	patterns := resp["patterns"].([]interface{})
	assert.Len(t, patterns, 1)
	first := patterns[0].(map[string]interface{})
	assert.Equal(t, "timeout", first["failure_class"])
}

func TestKnowledgeFailures_FiltersByMinFrequency(t *testing.T) {
	store := &mockKnowledgeStore{
		patterns: []FailurePatternRecord{
			{PatternID: "fp-001", RepoPath: "/repos/app", FailureClass: "timeout", Frequency: 10},
			{PatternID: "fp-002", RepoPath: "/repos/app", FailureClass: "timeout", Frequency: 2},
		},
	}
	srv := setupKnowledgeTestServer(store)

	req := httptest.NewRequest(http.MethodGet, "/v1/knowledge/failures?min_frequency=5", nil)
	w := httptest.NewRecorder()
	srv.handler().ServeHTTP(w, req)

	assert.Equal(t, http.StatusOK, w.Code)

	var resp map[string]interface{}
	require.NoError(t, json.Unmarshal(w.Body.Bytes(), &resp))
	patterns := resp["patterns"].([]interface{})
	assert.Len(t, patterns, 1)
	first := patterns[0].(map[string]interface{})
	assert.InDelta(t, 10.0, first["frequency"], 0.001)
}

func TestKnowledgeFailures_InvalidMinFrequencyReturns400(t *testing.T) {
	store := &mockKnowledgeStore{}
	srv := setupKnowledgeTestServer(store)

	req := httptest.NewRequest(http.MethodGet, "/v1/knowledge/failures?min_frequency=abc", nil)
	w := httptest.NewRecorder()
	srv.handler().ServeHTTP(w, req)

	assert.Equal(t, http.StatusBadRequest, w.Code)
	assert.Contains(t, w.Body.String(), "min_frequency must be an integer")
}

func TestKnowledgeFailures_Returns503WithoutStore(t *testing.T) {
	root := "/tmp/knowledge-test"
	srv := NewServer(Config{
		SocketPath:    filepath.Join(root, "run", "vikramd.sock"),
		WorkspaceRoot: root,
	}, nil)

	req := httptest.NewRequest(http.MethodGet, "/v1/knowledge/failures", nil)
	w := httptest.NewRecorder()
	srv.handler().ServeHTTP(w, req)

	assert.Equal(t, http.StatusServiceUnavailable, w.Code)
}

func TestKnowledgeFailures_RejectsNonGET(t *testing.T) {
	store := &mockKnowledgeStore{}
	srv := setupKnowledgeTestServer(store)

	req := httptest.NewRequest(http.MethodPost, "/v1/knowledge/failures", nil)
	w := httptest.NewRecorder()
	srv.handler().ServeHTTP(w, req)

	assert.Equal(t, http.StatusMethodNotAllowed, w.Code)
}
