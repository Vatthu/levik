package console

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
)

// --- Test: GET /api/formations (list with effectiveness) ---

func TestFormationsListReturnsData(t *testing.T) {
	orchHandler := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet || r.URL.Path != "/v1/formations" {
			http.Error(w, "not found", http.StatusNotFound)
			return
		}
		resp := FormationListResponse{
			Formations: []Formation{
				{
					Name:     "fast-bugfix",
					TaskType: "bugfix",
					RoleSlots: []FormationRoleSlot{
						{Role: "implementer", Provider: "anthropic", Model: "claude-3-5-sonnet"},
						{Role: "reviewer", Provider: "openai", Model: "gpt-4o"},
					},
					BudgetStrategy:       FormationBudgetStrategy{Planning: 0.05, Implementation: 0.70, Verification: 0.15, Review: 0.10},
					VerificationProtocol: "standard",
					Effectiveness:        &FormationEffectiveness{SuccessRate: 0.85, CostPerTaskUSD: 1.20, AvgDurationSecs: 300, TaskCount: 40},
				},
				{
					Name:     "thorough-feature",
					TaskType: "feature",
					RoleSlots: []FormationRoleSlot{
						{Role: "planner", Provider: "anthropic", Model: "claude-3-5-sonnet"},
						{Role: "implementer", Provider: "anthropic", Model: "claude-3-5-sonnet"},
						{Role: "reviewer", Provider: "openai", Model: "gpt-4o"},
					},
					BudgetStrategy:       FormationBudgetStrategy{Planning: 0.15, Implementation: 0.55, Verification: 0.20, Review: 0.10},
					VerificationProtocol: "property-based",
					Effectiveness:        &FormationEffectiveness{SuccessRate: 0.92, CostPerTaskUSD: 3.50, AvgDurationSecs: 900, TaskCount: 25},
				},
			},
		}
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(resp)
	})

	server, orchTS := newTestServerWithOrchestrator(orchHandler)
	defer orchTS.Close()
	defer server.progressHub.Stop()

	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodGet, "/api/formations", nil)
	server.handleAPIFormations(recorder, request)

	if recorder.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d: %s", recorder.Code, recorder.Body.String())
	}

	var result FormationListResponse
	if err := json.Unmarshal(recorder.Body.Bytes(), &result); err != nil {
		t.Fatalf("failed to decode response: %v", err)
	}
	if len(result.Formations) != 2 {
		t.Fatalf("expected 2 formations, got %d", len(result.Formations))
	}
	if result.Formations[0].Name != "fast-bugfix" {
		t.Fatalf("expected first formation 'fast-bugfix', got %q", result.Formations[0].Name)
	}
	if result.Formations[0].Effectiveness == nil {
		t.Fatal("expected effectiveness metrics on first formation")
	}
	if result.Formations[0].Effectiveness.SuccessRate != 0.85 {
		t.Fatalf("expected success rate 0.85, got %f", result.Formations[0].Effectiveness.SuccessRate)
	}
}

func TestFormationsListRejectsNonGET(t *testing.T) {
	server := &Server{hub: newWSHub(), progressHub: NewProgressHub(), cfg: &testCfg}
	defer server.progressHub.Stop()

	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodPost, "/api/formations", nil)
	server.handleAPIFormations(recorder, request)

	if recorder.Code != http.StatusMethodNotAllowed {
		t.Fatalf("expected 405, got %d", recorder.Code)
	}
}

func TestFormationsListOrchestratorUnreachable(t *testing.T) {
	orchServer := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {}))
	orchServer.Close()

	server := &Server{
		hub:            newWSHub(),
		progressHub:    NewProgressHub(),
		cfg:            &testCfg,
		orchBaseURL:    orchServer.URL,
		orchHTTPClient: orchServer.Client(),
	}
	defer server.progressHub.Stop()

	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodGet, "/api/formations", nil)
	server.handleAPIFormations(recorder, request)

	if recorder.Code != http.StatusOK {
		t.Fatalf("expected 200 with empty data, got %d", recorder.Code)
	}

	var result FormationListResponse
	json.Unmarshal(recorder.Body.Bytes(), &result)
	if len(result.Formations) != 0 {
		t.Fatalf("expected empty formations list, got %d", len(result.Formations))
	}
}

// --- Test: GET /api/formations/{id} ---

func TestFormationGetByID(t *testing.T) {
	orchHandler := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet || r.URL.Path != "/v1/formations/fast-bugfix" {
			http.Error(w, "not found", http.StatusNotFound)
			return
		}
		resp := Formation{
			Name:     "fast-bugfix",
			TaskType: "bugfix",
			RoleSlots: []FormationRoleSlot{
				{Role: "implementer", Provider: "anthropic", Model: "claude-3-5-sonnet"},
			},
			BudgetStrategy:       FormationBudgetStrategy{Planning: 0.05, Implementation: 0.70, Verification: 0.15, Review: 0.10},
			VerificationProtocol: "standard",
			Effectiveness:        &FormationEffectiveness{SuccessRate: 0.85, CostPerTaskUSD: 1.20, AvgDurationSecs: 300, TaskCount: 40},
		}
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(resp)
	})

	server, orchTS := newTestServerWithOrchestrator(orchHandler)
	defer orchTS.Close()
	defer server.progressHub.Stop()

	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodGet, "/api/formations/fast-bugfix", nil)
	server.handleAPIFormationByID(recorder, request)

	if recorder.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d: %s", recorder.Code, recorder.Body.String())
	}

	var result Formation
	json.Unmarshal(recorder.Body.Bytes(), &result)
	if result.Name != "fast-bugfix" {
		t.Fatalf("expected 'fast-bugfix', got %q", result.Name)
	}
	if result.Effectiveness.TaskCount != 40 {
		t.Fatalf("expected task count 40, got %d", result.Effectiveness.TaskCount)
	}
}

func TestFormationGetByIDNotFound(t *testing.T) {
	orchHandler := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Error(w, "not found", http.StatusNotFound)
	})

	server, orchTS := newTestServerWithOrchestrator(orchHandler)
	defer orchTS.Close()
	defer server.progressHub.Stop()

	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodGet, "/api/formations/nonexistent", nil)
	server.handleAPIFormationByID(recorder, request)

	if recorder.Code != http.StatusNotFound {
		t.Fatalf("expected 404, got %d", recorder.Code)
	}
}

// --- Test: PUT /api/formations/{id} ---

func TestFormationUpdate(t *testing.T) {
	orchHandler := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPut || r.URL.Path != "/v1/formations/fast-bugfix" {
			http.Error(w, "not found", http.StatusNotFound)
			return
		}
		var update FormationUpdate
		json.NewDecoder(r.Body).Decode(&update)

		resp := Formation{
			Name:     "fast-bugfix",
			TaskType: "bugfix",
			RoleSlots: []FormationRoleSlot{
				{Role: "implementer", Provider: "openai", Model: "gpt-4o"},
			},
			BudgetStrategy:       FormationBudgetStrategy{Planning: 0.10, Implementation: 0.60, Verification: 0.20, Review: 0.10},
			VerificationProtocol: "property-based",
		}
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(resp)
	})

	server, orchTS := newTestServerWithOrchestrator(orchHandler)
	defer orchTS.Close()
	defer server.progressHub.Stop()

	body := FormationUpdate{
		RoleSlots:            []FormationRoleSlot{{Role: "implementer", Provider: "openai", Model: "gpt-4o"}},
		VerificationProtocol: "property-based",
	}
	bodyBytes, _ := json.Marshal(body)

	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodPut, "/api/formations/fast-bugfix", bytes.NewReader(bodyBytes))
	request.Header.Set("Content-Type", "application/json")
	server.handleAPIFormationByID(recorder, request)

	if recorder.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d: %s", recorder.Code, recorder.Body.String())
	}

	var result Formation
	json.Unmarshal(recorder.Body.Bytes(), &result)
	if result.VerificationProtocol != "property-based" {
		t.Fatalf("expected 'property-based', got %q", result.VerificationProtocol)
	}
}

func TestFormationUpdateInvalidJSON(t *testing.T) {
	server := &Server{hub: newWSHub(), progressHub: NewProgressHub(), cfg: &testCfg}
	defer server.progressHub.Stop()

	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodPut, "/api/formations/fast-bugfix", bytes.NewReader([]byte("not json")))
	server.handleAPIFormationByID(recorder, request)

	if recorder.Code != http.StatusBadRequest {
		t.Fatalf("expected 400, got %d", recorder.Code)
	}
}

// --- Test: POST /api/formations/{id}/clone ---

func TestFormationClone(t *testing.T) {
	orchHandler := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost || r.URL.Path != "/v1/formations/fast-bugfix/clone" {
			http.Error(w, "not found", http.StatusNotFound)
			return
		}
		var req FormationCloneRequest
		json.NewDecoder(r.Body).Decode(&req)

		resp := Formation{
			Name:     req.NewName,
			TaskType: "bugfix",
			RoleSlots: []FormationRoleSlot{
				{Role: "implementer", Provider: "anthropic", Model: "claude-3-5-sonnet"},
			},
			BudgetStrategy:       FormationBudgetStrategy{Planning: 0.05, Implementation: 0.70, Verification: 0.15, Review: 0.10},
			VerificationProtocol: "standard",
		}
		w.WriteHeader(http.StatusCreated)
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(resp)
	})

	server, orchTS := newTestServerWithOrchestrator(orchHandler)
	defer orchTS.Close()
	defer server.progressHub.Stop()

	body := FormationCloneRequest{NewName: "fast-bugfix-v2"}
	bodyBytes, _ := json.Marshal(body)

	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodPost, "/api/formations/fast-bugfix/clone", bytes.NewReader(bodyBytes))
	request.Header.Set("Content-Type", "application/json")
	server.handleAPIFormationClone(recorder, request)

	if recorder.Code != http.StatusCreated {
		t.Fatalf("expected 201, got %d: %s", recorder.Code, recorder.Body.String())
	}

	var result Formation
	json.Unmarshal(recorder.Body.Bytes(), &result)
	if result.Name != "fast-bugfix-v2" {
		t.Fatalf("expected 'fast-bugfix-v2', got %q", result.Name)
	}
}

func TestFormationCloneMissingName(t *testing.T) {
	server := &Server{hub: newWSHub(), progressHub: NewProgressHub(), cfg: &testCfg}
	defer server.progressHub.Stop()

	body := FormationCloneRequest{NewName: ""}
	bodyBytes, _ := json.Marshal(body)

	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodPost, "/api/formations/fast-bugfix/clone", bytes.NewReader(bodyBytes))
	server.handleAPIFormationClone(recorder, request)

	if recorder.Code != http.StatusBadRequest {
		t.Fatalf("expected 400, got %d", recorder.Code)
	}
}

func TestFormationCloneRejectsNonPOST(t *testing.T) {
	server := &Server{hub: newWSHub(), progressHub: NewProgressHub(), cfg: &testCfg}
	defer server.progressHub.Stop()

	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodGet, "/api/formations/fast-bugfix/clone", nil)
	server.handleAPIFormationClone(recorder, request)

	if recorder.Code != http.StatusMethodNotAllowed {
		t.Fatalf("expected 405, got %d", recorder.Code)
	}
}

// --- Test: POST /api/formations/ab-test ---

func TestFormationABTestCreate(t *testing.T) {
	orchHandler := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost || r.URL.Path != "/v1/formations/ab-test" {
			http.Error(w, "not found", http.StatusNotFound)
			return
		}
		var config ABTestConfig
		json.NewDecoder(r.Body).Decode(&config)

		resp := ABTestStatus{
			TaskType:       config.TaskType,
			FormationA:     config.FormationA,
			FormationB:     config.FormationB,
			SplitPercent:   config.SplitPercent,
			TrialTasks:     config.TrialTasks,
			CompletedTasks: 0,
			AutoPromote:    config.AutoPromote,
		}
		w.WriteHeader(http.StatusCreated)
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(resp)
	})

	server, orchTS := newTestServerWithOrchestrator(orchHandler)
	defer orchTS.Close()
	defer server.progressHub.Stop()

	body := ABTestConfig{
		TaskType:     "bugfix",
		FormationA:   "fast-bugfix",
		FormationB:   "fast-bugfix-v2",
		SplitPercent: ABSplit{A: 70, B: 30},
		TrialTasks:   20,
		AutoPromote:  true,
	}
	bodyBytes, _ := json.Marshal(body)

	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodPost, "/api/formations/ab-test", bytes.NewReader(bodyBytes))
	request.Header.Set("Content-Type", "application/json")
	server.handleAPIFormationABTest(recorder, request)

	if recorder.Code != http.StatusCreated {
		t.Fatalf("expected 201, got %d: %s", recorder.Code, recorder.Body.String())
	}

	var result ABTestStatus
	json.Unmarshal(recorder.Body.Bytes(), &result)
	if result.FormationA != "fast-bugfix" {
		t.Fatalf("expected formation_a 'fast-bugfix', got %q", result.FormationA)
	}
	if result.SplitPercent.A != 70 || result.SplitPercent.B != 30 {
		t.Fatalf("expected split 70/30, got %d/%d", result.SplitPercent.A, result.SplitPercent.B)
	}
	if !result.AutoPromote {
		t.Fatal("expected auto_promote true")
	}
}

func TestFormationABTestInvalidSplit(t *testing.T) {
	server := &Server{hub: newWSHub(), progressHub: NewProgressHub(), cfg: &testCfg}
	defer server.progressHub.Stop()

	body := ABTestConfig{
		TaskType:     "bugfix",
		FormationA:   "fast-bugfix",
		FormationB:   "fast-bugfix-v2",
		SplitPercent: ABSplit{A: 60, B: 30}, // sums to 90, not 100
		TrialTasks:   20,
		AutoPromote:  true,
	}
	bodyBytes, _ := json.Marshal(body)

	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodPost, "/api/formations/ab-test", bytes.NewReader(bodyBytes))
	server.handleAPIFormationABTest(recorder, request)

	if recorder.Code != http.StatusBadRequest {
		t.Fatalf("expected 400, got %d: %s", recorder.Code, recorder.Body.String())
	}
}

func TestFormationABTestMissingFields(t *testing.T) {
	server := &Server{hub: newWSHub(), progressHub: NewProgressHub(), cfg: &testCfg}
	defer server.progressHub.Stop()

	tests := []struct {
		name string
		body ABTestConfig
	}{
		{
			name: "missing formation_a",
			body: ABTestConfig{TaskType: "bugfix", FormationB: "b", SplitPercent: ABSplit{A: 50, B: 50}, TrialTasks: 10},
		},
		{
			name: "missing formation_b",
			body: ABTestConfig{TaskType: "bugfix", FormationA: "a", SplitPercent: ABSplit{A: 50, B: 50}, TrialTasks: 10},
		},
		{
			name: "missing task_type",
			body: ABTestConfig{FormationA: "a", FormationB: "b", SplitPercent: ABSplit{A: 50, B: 50}, TrialTasks: 10},
		},
		{
			name: "zero trial_tasks",
			body: ABTestConfig{TaskType: "bugfix", FormationA: "a", FormationB: "b", SplitPercent: ABSplit{A: 50, B: 50}, TrialTasks: 0},
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			bodyBytes, _ := json.Marshal(tt.body)
			recorder := httptest.NewRecorder()
			request := httptest.NewRequest(http.MethodPost, "/api/formations/ab-test", bytes.NewReader(bodyBytes))
			server.handleAPIFormationABTest(recorder, request)

			if recorder.Code != http.StatusBadRequest {
				t.Fatalf("expected 400, got %d: %s", recorder.Code, recorder.Body.String())
			}
		})
	}
}

func TestFormationABTestRejectsNonPOST(t *testing.T) {
	server := &Server{hub: newWSHub(), progressHub: NewProgressHub(), cfg: &testCfg}
	defer server.progressHub.Stop()

	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodGet, "/api/formations/ab-test", nil)
	server.handleAPIFormationABTest(recorder, request)

	if recorder.Code != http.StatusMethodNotAllowed {
		t.Fatalf("expected 405, got %d", recorder.Code)
	}
}

// --- Test: POST /api/formations/ab-test/promote ---

func TestFormationABPromote(t *testing.T) {
	orchHandler := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost || r.URL.Path != "/v1/formations/ab-test/promote" {
			http.Error(w, "not found", http.StatusNotFound)
			return
		}
		var req ABTestPromoteRequest
		json.NewDecoder(r.Body).Decode(&req)

		resp := Formation{
			Name:     req.Winner,
			TaskType: req.TaskType,
			RoleSlots: []FormationRoleSlot{
				{Role: "implementer", Provider: "anthropic", Model: "claude-3-5-sonnet"},
			},
			BudgetStrategy:       FormationBudgetStrategy{Planning: 0.05, Implementation: 0.70, Verification: 0.15, Review: 0.10},
			VerificationProtocol: "standard",
		}
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(resp)
	})

	server, orchTS := newTestServerWithOrchestrator(orchHandler)
	defer orchTS.Close()
	defer server.progressHub.Stop()

	body := ABTestPromoteRequest{TaskType: "bugfix", Winner: "fast-bugfix-v2"}
	bodyBytes, _ := json.Marshal(body)

	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodPost, "/api/formations/ab-test/promote", bytes.NewReader(bodyBytes))
	request.Header.Set("Content-Type", "application/json")
	server.handleAPIFormationABPromote(recorder, request)

	if recorder.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d: %s", recorder.Code, recorder.Body.String())
	}

	var result Formation
	json.Unmarshal(recorder.Body.Bytes(), &result)
	if result.Name != "fast-bugfix-v2" {
		t.Fatalf("expected winner 'fast-bugfix-v2', got %q", result.Name)
	}
}

func TestFormationABPromoteMissingFields(t *testing.T) {
	server := &Server{hub: newWSHub(), progressHub: NewProgressHub(), cfg: &testCfg}
	defer server.progressHub.Stop()

	tests := []struct {
		name string
		body ABTestPromoteRequest
	}{
		{name: "missing task_type", body: ABTestPromoteRequest{Winner: "x"}},
		{name: "missing winner", body: ABTestPromoteRequest{TaskType: "bugfix"}},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			bodyBytes, _ := json.Marshal(tt.body)
			recorder := httptest.NewRecorder()
			request := httptest.NewRequest(http.MethodPost, "/api/formations/ab-test/promote", bytes.NewReader(bodyBytes))
			server.handleAPIFormationABPromote(recorder, request)

			if recorder.Code != http.StatusBadRequest {
				t.Fatalf("expected 400, got %d: %s", recorder.Code, recorder.Body.String())
			}
		})
	}
}

func TestFormationABPromoteRejectsNonPOST(t *testing.T) {
	server := &Server{hub: newWSHub(), progressHub: NewProgressHub(), cfg: &testCfg}
	defer server.progressHub.Stop()

	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodGet, "/api/formations/ab-test/promote", nil)
	server.handleAPIFormationABPromote(recorder, request)

	if recorder.Code != http.StatusMethodNotAllowed {
		t.Fatalf("expected 405, got %d", recorder.Code)
	}
}

// --- Test: handleAPIFormationByID method routing ---

func TestFormationByIDRejectsInvalidMethod(t *testing.T) {
	server := &Server{hub: newWSHub(), progressHub: NewProgressHub(), cfg: &testCfg}
	defer server.progressHub.Stop()

	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodDelete, "/api/formations/fast-bugfix", nil)
	server.handleAPIFormationByID(recorder, request)

	if recorder.Code != http.StatusMethodNotAllowed {
		t.Fatalf("expected 405, got %d", recorder.Code)
	}
}

func TestFormationByIDEmptyID(t *testing.T) {
	server := &Server{hub: newWSHub(), progressHub: NewProgressHub(), cfg: &testCfg}
	defer server.progressHub.Stop()

	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodGet, "/api/formations/", nil)
	server.handleAPIFormationByID(recorder, request)

	if recorder.Code != http.StatusNotFound {
		t.Fatalf("expected 404 for empty ID, got %d", recorder.Code)
	}
}
