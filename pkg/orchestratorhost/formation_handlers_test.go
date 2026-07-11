package orchestratorhost

import (
	"bytes"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// stubFormationStore implements formationStore for testing.
type stubFormationStore struct {
	formations    map[string]Formation
	performance   []ModelPerformanceRecord
	effectiveness []FormationEffectivenessEntry
}

func newStubFormationStore() *stubFormationStore {
	return &stubFormationStore{
		formations: make(map[string]Formation),
	}
}

func (s *stubFormationStore) GetModelPerformance() ([]ModelPerformanceRecord, error) {
	return s.performance, nil
}

func (s *stubFormationStore) ListFormations() ([]Formation, error) {
	formations := make([]Formation, 0, len(s.formations))
	for _, f := range s.formations {
		formations = append(formations, f)
	}
	return formations, nil
}

func (s *stubFormationStore) CreateFormation(f Formation) (Formation, error) {
	if _, exists := s.formations[f.Name]; exists {
		return Formation{}, fmt.Errorf("formation '%s' already exists", f.Name)
	}
	s.formations[f.Name] = f
	return f, nil
}

func (s *stubFormationStore) UpdateFormation(name string, f Formation) (Formation, error) {
	if _, exists := s.formations[name]; !exists {
		return Formation{}, fmt.Errorf("formation '%s' not found", name)
	}
	if f.Name != name {
		delete(s.formations, name)
	}
	s.formations[f.Name] = f
	return f, nil
}

func (s *stubFormationStore) DeleteFormation(name string) (bool, error) {
	if _, exists := s.formations[name]; !exists {
		return false, nil
	}
	delete(s.formations, name)
	return true, nil
}

func (s *stubFormationStore) GetEffectiveness() ([]FormationEffectivenessEntry, error) {
	return s.effectiveness, nil
}

func setupFormationTestServer(store *stubFormationStore) *Server {
	root := "/tmp/formation-test"
	srv := NewServer(Config{
		SocketPath:    filepath.Join(root, "run", "vikramd.sock"),
		WorkspaceRoot: root,
	}, nil)
	srv.SetFormationStore(store)
	return srv
}

// --- Model Performance Tests ---

func TestModelPerformance_ReturnsRecords(t *testing.T) {
	store := newStubFormationStore()
	store.performance = []ModelPerformanceRecord{
		{
			Model:          "claude-sonnet-4-20250514",
			Provider:       "anthropic",
			Role:           "implementer",
			ComplexityTier: "moderate",
			SuccessRate:    0.85,
			AvgLatencyMS:   1200.0,
			CostPerSuccess: 0.05,
			TotalCalls:     150,
		},
	}
	srv := setupFormationTestServer(store)

	req := httptest.NewRequest(http.MethodGet, "/v1/models/performance", nil)
	w := httptest.NewRecorder()
	srv.handler().ServeHTTP(w, req)

	assert.Equal(t, http.StatusOK, w.Code)

	var resp map[string]interface{}
	require.NoError(t, json.Unmarshal(w.Body.Bytes(), &resp))
	records := resp["records"].([]interface{})
	assert.Len(t, records, 1)

	first := records[0].(map[string]interface{})
	assert.Equal(t, "claude-sonnet-4-20250514", first["model"])
	assert.Equal(t, "anthropic", first["provider"])
	assert.InDelta(t, 0.85, first["success_rate"], 0.001)
}

func TestModelPerformance_Returns503WithoutStore(t *testing.T) {
	root := "/tmp/formation-test"
	srv := NewServer(Config{
		SocketPath:    filepath.Join(root, "run", "vikramd.sock"),
		WorkspaceRoot: root,
	}, nil)
	// No formation store set

	req := httptest.NewRequest(http.MethodGet, "/v1/models/performance", nil)
	w := httptest.NewRecorder()
	srv.handler().ServeHTTP(w, req)

	assert.Equal(t, http.StatusServiceUnavailable, w.Code)
}

func TestModelPerformance_RejectsNonGET(t *testing.T) {
	store := newStubFormationStore()
	srv := setupFormationTestServer(store)

	req := httptest.NewRequest(http.MethodPost, "/v1/models/performance", nil)
	w := httptest.NewRecorder()
	srv.handler().ServeHTTP(w, req)

	assert.Equal(t, http.StatusMethodNotAllowed, w.Code)
}

// --- List Formations Tests ---

func TestListFormations_ReturnsAll(t *testing.T) {
	store := newStubFormationStore()
	store.formations["bugfix-standard"] = Formation{
		Name:     "bugfix-standard",
		TaskType: "bugfix",
		BudgetStrategy: BudgetStrategy{
			Planning:       0.15,
			Implementation: 0.50,
			Verification:   0.25,
			Review:         0.10,
		},
		VerificationProtocol: "property-based",
	}
	srv := setupFormationTestServer(store)

	req := httptest.NewRequest(http.MethodGet, "/v1/formations", nil)
	w := httptest.NewRecorder()
	srv.handler().ServeHTTP(w, req)

	assert.Equal(t, http.StatusOK, w.Code)

	var resp map[string]interface{}
	require.NoError(t, json.Unmarshal(w.Body.Bytes(), &resp))
	formations := resp["formations"].([]interface{})
	assert.Len(t, formations, 1)
}

func TestListFormations_Returns503WithoutStore(t *testing.T) {
	root := "/tmp/formation-test"
	srv := NewServer(Config{
		SocketPath:    filepath.Join(root, "run", "vikramd.sock"),
		WorkspaceRoot: root,
	}, nil)

	req := httptest.NewRequest(http.MethodGet, "/v1/formations", nil)
	w := httptest.NewRecorder()
	srv.handler().ServeHTTP(w, req)

	assert.Equal(t, http.StatusServiceUnavailable, w.Code)
}

// --- Create Formation Tests ---

func TestCreateFormation_Success(t *testing.T) {
	store := newStubFormationStore()
	srv := setupFormationTestServer(store)

	body := Formation{
		Name:     "custom-bugfix",
		TaskType: "bugfix",
		BudgetStrategy: BudgetStrategy{
			Planning:       0.10,
			Implementation: 0.60,
			Verification:   0.20,
			Review:         0.10,
		},
		VerificationProtocol: "standard",
	}
	reqBody, err := json.Marshal(body)
	require.NoError(t, err)

	req := httptest.NewRequest(http.MethodPost, "/v1/formations", bytes.NewReader(reqBody))
	w := httptest.NewRecorder()
	srv.handler().ServeHTTP(w, req)

	assert.Equal(t, http.StatusCreated, w.Code)

	var resp Formation
	require.NoError(t, json.Unmarshal(w.Body.Bytes(), &resp))
	assert.Equal(t, "custom-bugfix", resp.Name)
	assert.Equal(t, "bugfix", resp.TaskType)

	// Verify it was stored
	assert.Contains(t, store.formations, "custom-bugfix")
}

func TestCreateFormation_DuplicateReturns409(t *testing.T) {
	store := newStubFormationStore()
	store.formations["existing"] = Formation{Name: "existing", TaskType: "feature"}
	srv := setupFormationTestServer(store)

	body := Formation{Name: "existing", TaskType: "feature"}
	reqBody, err := json.Marshal(body)
	require.NoError(t, err)

	req := httptest.NewRequest(http.MethodPost, "/v1/formations", bytes.NewReader(reqBody))
	w := httptest.NewRecorder()
	srv.handler().ServeHTTP(w, req)

	assert.Equal(t, http.StatusConflict, w.Code)
}

func TestCreateFormation_MissingNameReturns400(t *testing.T) {
	store := newStubFormationStore()
	srv := setupFormationTestServer(store)

	body := Formation{TaskType: "bugfix"}
	reqBody, err := json.Marshal(body)
	require.NoError(t, err)

	req := httptest.NewRequest(http.MethodPost, "/v1/formations", bytes.NewReader(reqBody))
	w := httptest.NewRecorder()
	srv.handler().ServeHTTP(w, req)

	assert.Equal(t, http.StatusBadRequest, w.Code)
	assert.Contains(t, w.Body.String(), "name is required")
}

func TestCreateFormation_MissingTaskTypeReturns400(t *testing.T) {
	store := newStubFormationStore()
	srv := setupFormationTestServer(store)

	body := Formation{Name: "my-formation"}
	reqBody, err := json.Marshal(body)
	require.NoError(t, err)

	req := httptest.NewRequest(http.MethodPost, "/v1/formations", bytes.NewReader(reqBody))
	w := httptest.NewRecorder()
	srv.handler().ServeHTTP(w, req)

	assert.Equal(t, http.StatusBadRequest, w.Code)
	assert.Contains(t, w.Body.String(), "task_type is required")
}

// --- Update Formation Tests ---

func TestUpdateFormation_Success(t *testing.T) {
	store := newStubFormationStore()
	store.formations["bugfix-standard"] = Formation{
		Name:                 "bugfix-standard",
		TaskType:             "bugfix",
		VerificationProtocol: "standard",
	}
	srv := setupFormationTestServer(store)

	body := Formation{
		Name:                 "bugfix-standard",
		TaskType:             "bugfix",
		VerificationProtocol: "property-based",
	}
	reqBody, err := json.Marshal(body)
	require.NoError(t, err)

	req := httptest.NewRequest(http.MethodPut, "/v1/formations/bugfix-standard", bytes.NewReader(reqBody))
	req.SetPathValue("name", "bugfix-standard")
	w := httptest.NewRecorder()
	srv.handler().ServeHTTP(w, req)

	assert.Equal(t, http.StatusOK, w.Code)

	var resp Formation
	require.NoError(t, json.Unmarshal(w.Body.Bytes(), &resp))
	assert.Equal(t, "property-based", resp.VerificationProtocol)
}

func TestUpdateFormation_NotFoundReturns404(t *testing.T) {
	store := newStubFormationStore()
	srv := setupFormationTestServer(store)

	body := Formation{Name: "nonexistent", TaskType: "bugfix"}
	reqBody, err := json.Marshal(body)
	require.NoError(t, err)

	req := httptest.NewRequest(http.MethodPut, "/v1/formations/nonexistent", bytes.NewReader(reqBody))
	req.SetPathValue("name", "nonexistent")
	w := httptest.NewRecorder()
	srv.handler().ServeHTTP(w, req)

	assert.Equal(t, http.StatusNotFound, w.Code)
}

// --- Delete Formation Tests ---

func TestDeleteFormation_Success(t *testing.T) {
	store := newStubFormationStore()
	store.formations["to-delete"] = Formation{Name: "to-delete", TaskType: "feature"}
	srv := setupFormationTestServer(store)

	req := httptest.NewRequest(http.MethodDelete, "/v1/formations/to-delete", nil)
	req.SetPathValue("name", "to-delete")
	w := httptest.NewRecorder()
	srv.handler().ServeHTTP(w, req)

	assert.Equal(t, http.StatusOK, w.Code)

	var resp map[string]interface{}
	require.NoError(t, json.Unmarshal(w.Body.Bytes(), &resp))
	assert.Equal(t, true, resp["deleted"])
	assert.Equal(t, "to-delete", resp["name"])

	// Verify removed from store
	assert.NotContains(t, store.formations, "to-delete")
}

func TestDeleteFormation_NotFoundReturns404(t *testing.T) {
	store := newStubFormationStore()
	srv := setupFormationTestServer(store)

	req := httptest.NewRequest(http.MethodDelete, "/v1/formations/nonexistent", nil)
	req.SetPathValue("name", "nonexistent")
	w := httptest.NewRecorder()
	srv.handler().ServeHTTP(w, req)

	assert.Equal(t, http.StatusNotFound, w.Code)
}

// --- Formation Effectiveness Tests ---

func TestFormationEffectiveness_ReturnsData(t *testing.T) {
	store := newStubFormationStore()
	store.effectiveness = []FormationEffectivenessEntry{
		{
			FormationName: "bugfix-standard",
			Scores: map[string]float64{
				"bugfix": 0.72,
			},
		},
		{
			FormationName: "feature-standard",
			Scores: map[string]float64{
				"feature": 0.65,
			},
		},
	}
	srv := setupFormationTestServer(store)

	req := httptest.NewRequest(http.MethodGet, "/v1/formations/effectiveness", nil)
	w := httptest.NewRecorder()
	srv.handler().ServeHTTP(w, req)

	assert.Equal(t, http.StatusOK, w.Code)

	var resp map[string]interface{}
	require.NoError(t, json.Unmarshal(w.Body.Bytes(), &resp))
	formations := resp["formations"].([]interface{})
	assert.Len(t, formations, 2)
}

func TestFormationEffectiveness_Returns503WithoutStore(t *testing.T) {
	root := "/tmp/formation-test"
	srv := NewServer(Config{
		SocketPath:    filepath.Join(root, "run", "vikramd.sock"),
		WorkspaceRoot: root,
	}, nil)

	req := httptest.NewRequest(http.MethodGet, "/v1/formations/effectiveness", nil)
	w := httptest.NewRecorder()
	srv.handler().ServeHTTP(w, req)

	assert.Equal(t, http.StatusServiceUnavailable, w.Code)
}

func TestFormationEffectiveness_RejectsNonGET(t *testing.T) {
	store := newStubFormationStore()
	srv := setupFormationTestServer(store)

	req := httptest.NewRequest(http.MethodPost, "/v1/formations/effectiveness", nil)
	w := httptest.NewRecorder()
	srv.handler().ServeHTTP(w, req)

	assert.Equal(t, http.StatusMethodNotAllowed, w.Code)
}
