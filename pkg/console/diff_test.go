package console

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestHandleAPITaskDiffReturnsUnifiedDiff(t *testing.T) {
	server := testConsoleServer(roundTripFunc(func(r *http.Request) (*http.Response, error) {
		if r.Method != http.MethodGet || r.URL.Path != "/v1/tasks/task-001/diff" {
			return nil, unexpectedRequestError(r)
		}
		if r.URL.Query().Get("mode") != "unified" {
			t.Fatalf("expected mode=unified, got %q", r.URL.Query().Get("mode"))
		}
		if r.URL.Query().Get("context") != "3" {
			t.Fatalf("expected context=3, got %q", r.URL.Query().Get("context"))
		}
		return testJSONResponse(t, http.StatusOK, DiffResponse{
			TaskID:       "task-001",
			Mode:         DiffModeUnified,
			ContextLines: 3,
			Files: []FileDiff{
				{
					Path:      "pkg/console/diff.go",
					Patch:     "@@ -1,3 +1,5 @@\n+package console\n",
					Additions: 5,
					Deletions: 0,
				},
			},
		}), nil
	}))

	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodGet, "/api/tasks/task-001/diff?mode=unified", nil)
	request.SetPathValue("task_id", "task-001")

	server.handleAPITaskDiff(recorder, request)

	if recorder.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d: %s", recorder.Code, recorder.Body.String())
	}

	var resp DiffResponse
	decodeTestJSON(t, recorder, &resp)

	if resp.TaskID != "task-001" {
		t.Fatalf("expected task_id=task-001, got %q", resp.TaskID)
	}
	if resp.Mode != DiffModeUnified {
		t.Fatalf("expected mode=unified, got %q", resp.Mode)
	}
	if resp.ContextLines != 3 {
		t.Fatalf("expected context_lines=3, got %d", resp.ContextLines)
	}
	if len(resp.Files) != 1 {
		t.Fatalf("expected 1 file diff, got %d", len(resp.Files))
	}
	if resp.Files[0].Path != "pkg/console/diff.go" {
		t.Fatalf("unexpected file path: %q", resp.Files[0].Path)
	}
	if resp.Files[0].Language != "go" {
		t.Fatalf("expected language=go, got %q", resp.Files[0].Language)
	}
	if resp.Files[0].Additions != 5 || resp.Files[0].Deletions != 0 {
		t.Fatalf("unexpected additions/deletions: +%d/-%d", resp.Files[0].Additions, resp.Files[0].Deletions)
	}
}

func TestHandleAPITaskDiffDefaultsToUnifiedMode(t *testing.T) {
	var capturedMode string
	var capturedContext string
	server := testConsoleServer(roundTripFunc(func(r *http.Request) (*http.Response, error) {
		capturedMode = r.URL.Query().Get("mode")
		capturedContext = r.URL.Query().Get("context")
		return testJSONResponse(t, http.StatusOK, DiffResponse{
			TaskID:       "task-002",
			Mode:         DiffModeUnified,
			ContextLines: 3,
			Files:        []FileDiff{},
		}), nil
	}))

	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodGet, "/api/tasks/task-002/diff", nil)
	request.SetPathValue("task_id", "task-002")

	server.handleAPITaskDiff(recorder, request)

	if recorder.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d: %s", recorder.Code, recorder.Body.String())
	}
	if capturedMode != "unified" {
		t.Fatalf("expected default mode=unified sent upstream, got %q", capturedMode)
	}
	if capturedContext != "3" {
		t.Fatalf("expected default context=3 sent upstream, got %q", capturedContext)
	}
}

func TestHandleAPITaskDiffSideBySideMode(t *testing.T) {
	server := testConsoleServer(roundTripFunc(func(r *http.Request) (*http.Response, error) {
		if r.URL.Query().Get("mode") != "side-by-side" {
			t.Fatalf("expected mode=side-by-side, got %q", r.URL.Query().Get("mode"))
		}
		return testJSONResponse(t, http.StatusOK, DiffResponse{
			TaskID:       "task-003",
			Mode:         DiffModeSideBySide,
			ContextLines: 3,
			Files: []FileDiff{
				{
					Path:       "main.go",
					OldContent: "old",
					NewContent: "new",
					Additions:  1,
					Deletions:  1,
				},
			},
		}), nil
	}))

	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodGet, "/api/tasks/task-003/diff?mode=side-by-side", nil)
	request.SetPathValue("task_id", "task-003")

	server.handleAPITaskDiff(recorder, request)

	if recorder.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d: %s", recorder.Code, recorder.Body.String())
	}

	var resp DiffResponse
	decodeTestJSON(t, recorder, &resp)
	if resp.Mode != DiffModeSideBySide {
		t.Fatalf("expected mode=side-by-side, got %q", resp.Mode)
	}
	if resp.Files[0].Language != "go" {
		t.Fatalf("expected language=go for main.go, got %q", resp.Files[0].Language)
	}
}

func TestHandleAPITaskDiffRejectsInvalidMode(t *testing.T) {
	server := testConsoleServer(nil)

	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodGet, "/api/tasks/task-001/diff?mode=invalid", nil)
	request.SetPathValue("task_id", "task-001")

	server.handleAPITaskDiff(recorder, request)

	if recorder.Code != http.StatusBadRequest {
		t.Fatalf("expected 400, got %d: %s", recorder.Code, recorder.Body.String())
	}
}

func TestHandleAPITaskDiffRejectsNonGET(t *testing.T) {
	server := testConsoleServer(nil)

	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodPost, "/api/tasks/task-001/diff", nil)
	request.SetPathValue("task_id", "task-001")

	server.handleAPITaskDiff(recorder, request)

	if recorder.Code != http.StatusMethodNotAllowed {
		t.Fatalf("expected 405, got %d: %s", recorder.Code, recorder.Body.String())
	}
}

func TestHandleAPITaskDiffRequiresTaskID(t *testing.T) {
	server := testConsoleServer(nil)

	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodGet, "/api/tasks//diff", nil)
	// task_id is empty string

	server.handleAPITaskDiff(recorder, request)

	if recorder.Code != http.StatusBadRequest {
		t.Fatalf("expected 400, got %d: %s", recorder.Code, recorder.Body.String())
	}
}

func TestHandleAPITaskDiffReturnsGatewayErrorOnOrchestratorFailure(t *testing.T) {
	// nil transport means orchestrator unreachable
	server := testConsoleServer(roundTripFunc(func(r *http.Request) (*http.Response, error) {
		body := bytes.NewBufferString(`{"error": "not found"}`)
		return &http.Response{
			StatusCode: http.StatusNotFound,
			Header:     http.Header{"Content-Type": []string{"application/json"}},
			Body:       nopCloser(body),
		}, nil
	}))

	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodGet, "/api/tasks/missing-task/diff", nil)
	request.SetPathValue("task_id", "missing-task")

	server.handleAPITaskDiff(recorder, request)

	if recorder.Code != http.StatusBadGateway {
		t.Fatalf("expected 502, got %d: %s", recorder.Code, recorder.Body.String())
	}
}

func nopCloser(b *bytes.Buffer) *nopReadCloser {
	return &nopReadCloser{Buffer: b}
}

type nopReadCloser struct {
	*bytes.Buffer
}

func (n *nopReadCloser) Close() error { return nil }

// --- File Tree Tests ---

func TestHandleAPITaskFilesReturnsFileTree(t *testing.T) {
	server := testConsoleServer(roundTripFunc(func(r *http.Request) (*http.Response, error) {
		if r.Method != http.MethodGet || r.URL.Path != "/v1/tasks/task-001/files" {
			return nil, unexpectedRequestError(r)
		}
		return testJSONResponse(t, http.StatusOK, FileTreeResponse{
			TaskID: "task-001",
			Files: []FileTreeEntry{
				{Path: "pkg/console/diff.go", Status: "added", Additions: 80, Deletions: 0},
				{Path: "pkg/console/merge.go", Status: "added", Additions: 60, Deletions: 0},
				{Path: "pkg/console/console.go", Status: "modified", Additions: 3, Deletions: 0},
			},
		}), nil
	}))

	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodGet, "/api/tasks/task-001/files", nil)
	request.SetPathValue("task_id", "task-001")

	server.handleAPITaskFiles(recorder, request)

	if recorder.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d: %s", recorder.Code, recorder.Body.String())
	}

	var resp FileTreeResponse
	decodeTestJSON(t, recorder, &resp)

	if resp.TaskID != "task-001" {
		t.Fatalf("expected task_id=task-001, got %q", resp.TaskID)
	}
	if len(resp.Files) != 3 {
		t.Fatalf("expected 3 files, got %d", len(resp.Files))
	}
	if resp.Total.FilesChanged != 3 {
		t.Fatalf("expected total.files_changed=3, got %d", resp.Total.FilesChanged)
	}
	if resp.Total.Additions != 143 {
		t.Fatalf("expected total.additions=143, got %d", resp.Total.Additions)
	}
	if resp.Total.Deletions != 0 {
		t.Fatalf("expected total.deletions=0, got %d", resp.Total.Deletions)
	}
}

func TestHandleAPITaskFilesComputesSummaryWhenMissing(t *testing.T) {
	server := testConsoleServer(roundTripFunc(func(r *http.Request) (*http.Response, error) {
		// Return file tree without pre-computed Total
		return testJSONResponse(t, http.StatusOK, map[string]interface{}{
			"task_id": "task-002",
			"files": []map[string]interface{}{
				{"path": "a.go", "status": "added", "additions": 10, "deletions": 0},
				{"path": "b.go", "status": "deleted", "additions": 0, "deletions": 25},
			},
			"total": map[string]interface{}{
				"files_changed": 0,
				"additions":     0,
				"deletions":     0,
			},
		}), nil
	}))

	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodGet, "/api/tasks/task-002/files", nil)
	request.SetPathValue("task_id", "task-002")

	server.handleAPITaskFiles(recorder, request)

	if recorder.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d: %s", recorder.Code, recorder.Body.String())
	}

	var resp FileTreeResponse
	decodeTestJSON(t, recorder, &resp)

	if resp.Total.FilesChanged != 2 {
		t.Fatalf("expected computed files_changed=2, got %d", resp.Total.FilesChanged)
	}
	if resp.Total.Additions != 10 {
		t.Fatalf("expected computed additions=10, got %d", resp.Total.Additions)
	}
	if resp.Total.Deletions != 25 {
		t.Fatalf("expected computed deletions=25, got %d", resp.Total.Deletions)
	}
}

func TestHandleAPITaskFilesRejectsNonGET(t *testing.T) {
	server := testConsoleServer(nil)

	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodPost, "/api/tasks/task-001/files", nil)
	request.SetPathValue("task_id", "task-001")

	server.handleAPITaskFiles(recorder, request)

	if recorder.Code != http.StatusMethodNotAllowed {
		t.Fatalf("expected 405, got %d: %s", recorder.Code, recorder.Body.String())
	}
}

// --- Merge Tests ---

func TestHandleAPITaskMergeWithFastForward(t *testing.T) {
	var upstreamReq MergeRequest
	server := testConsoleServer(roundTripFunc(func(r *http.Request) (*http.Response, error) {
		if r.Method != http.MethodPost || r.URL.Path != "/v1/tasks/task-001/merge" {
			return nil, unexpectedRequestError(r)
		}
		if err := json.NewDecoder(r.Body).Decode(&upstreamReq); err != nil {
			return nil, err
		}
		return testJSONResponse(t, http.StatusOK, MergeResponse{
			TaskID:      "task-001",
			Status:      "merged",
			Strategy:    MergeStrategyFastForward,
			MergeCommit: "abc123def456",
		}), nil
	}))

	body := bytes.NewBufferString(`{"strategy":"fast-forward"}`)
	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodPost, "/api/tasks/task-001/merge", body)
	request.SetPathValue("task_id", "task-001")

	server.handleAPITaskMerge(recorder, request)

	if recorder.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d: %s", recorder.Code, recorder.Body.String())
	}

	if upstreamReq.Strategy != MergeStrategyFastForward {
		t.Fatalf("expected strategy=fast-forward upstream, got %q", upstreamReq.Strategy)
	}

	var resp MergeResponse
	decodeTestJSON(t, recorder, &resp)

	if resp.Status != "merged" {
		t.Fatalf("expected status=merged, got %q", resp.Status)
	}
	if resp.MergeCommit != "abc123def456" {
		t.Fatalf("expected merge_commit=abc123def456, got %q", resp.MergeCommit)
	}
	if resp.Strategy != MergeStrategyFastForward {
		t.Fatalf("expected strategy=fast-forward, got %q", resp.Strategy)
	}
}

func TestHandleAPITaskMergeWithSquash(t *testing.T) {
	server := testConsoleServer(roundTripFunc(func(r *http.Request) (*http.Response, error) {
		return testJSONResponse(t, http.StatusOK, MergeResponse{
			TaskID:      "task-002",
			Status:      "merged",
			Strategy:    MergeStrategySquash,
			MergeCommit: "squashed123",
		}), nil
	}))

	body := bytes.NewBufferString(`{"strategy":"squash"}`)
	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodPost, "/api/tasks/task-002/merge", body)
	request.SetPathValue("task_id", "task-002")

	server.handleAPITaskMerge(recorder, request)

	if recorder.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d: %s", recorder.Code, recorder.Body.String())
	}

	var resp MergeResponse
	decodeTestJSON(t, recorder, &resp)
	if resp.Strategy != MergeStrategySquash {
		t.Fatalf("expected strategy=squash, got %q", resp.Strategy)
	}
}

func TestHandleAPITaskMergeWithRebase(t *testing.T) {
	server := testConsoleServer(roundTripFunc(func(r *http.Request) (*http.Response, error) {
		return testJSONResponse(t, http.StatusOK, MergeResponse{
			TaskID:      "task-003",
			Status:      "merged",
			Strategy:    MergeStrategyRebase,
			MergeCommit: "rebased456",
		}), nil
	}))

	body := bytes.NewBufferString(`{"strategy":"rebase"}`)
	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodPost, "/api/tasks/task-003/merge", body)
	request.SetPathValue("task_id", "task-003")

	server.handleAPITaskMerge(recorder, request)

	if recorder.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d: %s", recorder.Code, recorder.Body.String())
	}

	var resp MergeResponse
	decodeTestJSON(t, recorder, &resp)
	if resp.Strategy != MergeStrategyRebase {
		t.Fatalf("expected strategy=rebase, got %q", resp.Strategy)
	}
}

func TestHandleAPITaskMergeReturnsConflicts(t *testing.T) {
	server := testConsoleServer(roundTripFunc(func(r *http.Request) (*http.Response, error) {
		return testJSONResponse(t, http.StatusOK, MergeResponse{
			TaskID:   "task-004",
			Status:   "conflict",
			Strategy: MergeStrategyRebase,
			Conflicts: []MergeConflict{
				{
					Path:    "pkg/console/merge.go",
					Markers: "<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> branch",
					Ours:    "ours",
					Theirs:  "theirs",
				},
			},
		}), nil
	}))

	body := bytes.NewBufferString(`{"strategy":"rebase"}`)
	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodPost, "/api/tasks/task-004/merge", body)
	request.SetPathValue("task_id", "task-004")

	server.handleAPITaskMerge(recorder, request)

	if recorder.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d: %s", recorder.Code, recorder.Body.String())
	}

	var resp MergeResponse
	decodeTestJSON(t, recorder, &resp)

	if resp.Status != "conflict" {
		t.Fatalf("expected status=conflict, got %q", resp.Status)
	}
	if len(resp.Conflicts) != 1 {
		t.Fatalf("expected 1 conflict, got %d", len(resp.Conflicts))
	}
	if resp.Conflicts[0].Path != "pkg/console/merge.go" {
		t.Fatalf("unexpected conflict path: %q", resp.Conflicts[0].Path)
	}
	if resp.Conflicts[0].Ours != "ours" || resp.Conflicts[0].Theirs != "theirs" {
		t.Fatalf("unexpected conflict content: ours=%q theirs=%q", resp.Conflicts[0].Ours, resp.Conflicts[0].Theirs)
	}
	// Verify resolution options are populated for conflicts
	if len(resp.ResolutionOptions) != 3 {
		t.Fatalf("expected 3 resolution options, got %d", len(resp.ResolutionOptions))
	}
	actions := map[string]bool{}
	for _, opt := range resp.ResolutionOptions {
		actions[opt.Action] = true
		if opt.Description == "" {
			t.Fatalf("resolution option %q has empty description", opt.Action)
		}
	}
	if !actions["manual"] || !actions["rebase_retry"] || !actions["cancel"] {
		t.Fatalf("missing expected resolution actions: %v", actions)
	}
}

func TestHandleAPITaskMergeRejectsInvalidStrategy(t *testing.T) {
	server := testConsoleServer(nil)

	body := bytes.NewBufferString(`{"strategy":"merge-commit"}`)
	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodPost, "/api/tasks/task-001/merge", body)
	request.SetPathValue("task_id", "task-001")

	server.handleAPITaskMerge(recorder, request)

	if recorder.Code != http.StatusBadRequest {
		t.Fatalf("expected 400, got %d: %s", recorder.Code, recorder.Body.String())
	}
}

func TestHandleAPITaskMergeRejectsNonPOST(t *testing.T) {
	server := testConsoleServer(nil)

	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodGet, "/api/tasks/task-001/merge", nil)
	request.SetPathValue("task_id", "task-001")

	server.handleAPITaskMerge(recorder, request)

	if recorder.Code != http.StatusMethodNotAllowed {
		t.Fatalf("expected 405, got %d: %s", recorder.Code, recorder.Body.String())
	}
}

func TestHandleAPITaskMergeRejectsInvalidJSON(t *testing.T) {
	server := testConsoleServer(nil)

	body := bytes.NewBufferString(`not json`)
	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodPost, "/api/tasks/task-001/merge", body)
	request.SetPathValue("task_id", "task-001")

	server.handleAPITaskMerge(recorder, request)

	if recorder.Code != http.StatusBadRequest {
		t.Fatalf("expected 400, got %d: %s", recorder.Code, recorder.Body.String())
	}
}

func TestHandleAPITaskMergeRequiresTaskID(t *testing.T) {
	server := testConsoleServer(nil)

	body := bytes.NewBufferString(`{"strategy":"squash"}`)
	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodPost, "/api/tasks//merge", body)
	// task_id is empty

	server.handleAPITaskMerge(recorder, request)

	if recorder.Code != http.StatusBadRequest {
		t.Fatalf("expected 400, got %d: %s", recorder.Code, recorder.Body.String())
	}
}

func TestHandleAPITaskDiffCustomContextLines(t *testing.T) {
	var capturedContext string
	server := testConsoleServer(roundTripFunc(func(r *http.Request) (*http.Response, error) {
		capturedContext = r.URL.Query().Get("context")
		return testJSONResponse(t, http.StatusOK, DiffResponse{
			TaskID:       "task-ctx",
			Mode:         DiffModeUnified,
			ContextLines: 10,
			Files:        []FileDiff{},
		}), nil
	}))

	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodGet, "/api/tasks/task-ctx/diff?context=10", nil)
	request.SetPathValue("task_id", "task-ctx")

	server.handleAPITaskDiff(recorder, request)

	if recorder.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d: %s", recorder.Code, recorder.Body.String())
	}
	if capturedContext != "10" {
		t.Fatalf("expected context=10 upstream, got %q", capturedContext)
	}

	var resp DiffResponse
	decodeTestJSON(t, recorder, &resp)
	if resp.ContextLines != 10 {
		t.Fatalf("expected context_lines=10, got %d", resp.ContextLines)
	}
}

func TestHandleAPITaskDiffZeroContextForFullFile(t *testing.T) {
	var capturedContext string
	server := testConsoleServer(roundTripFunc(func(r *http.Request) (*http.Response, error) {
		capturedContext = r.URL.Query().Get("context")
		return testJSONResponse(t, http.StatusOK, DiffResponse{
			TaskID: "task-full",
			Mode:   DiffModeUnified,
			Files:  []FileDiff{},
		}), nil
	}))

	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodGet, "/api/tasks/task-full/diff?context=0", nil)
	request.SetPathValue("task_id", "task-full")

	server.handleAPITaskDiff(recorder, request)

	if recorder.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d: %s", recorder.Code, recorder.Body.String())
	}
	if capturedContext != "0" {
		t.Fatalf("expected context=0 upstream, got %q", capturedContext)
	}

	var resp DiffResponse
	decodeTestJSON(t, recorder, &resp)
	if resp.ContextLines != 0 {
		t.Fatalf("expected context_lines=0, got %d", resp.ContextLines)
	}
}

func TestHandleAPITaskDiffRejectsNegativeContext(t *testing.T) {
	server := testConsoleServer(nil)

	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodGet, "/api/tasks/task-001/diff?context=-1", nil)
	request.SetPathValue("task_id", "task-001")

	server.handleAPITaskDiff(recorder, request)

	if recorder.Code != http.StatusBadRequest {
		t.Fatalf("expected 400, got %d: %s", recorder.Code, recorder.Body.String())
	}
}

func TestHandleAPITaskDiffRejectsInvalidContext(t *testing.T) {
	server := testConsoleServer(nil)

	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodGet, "/api/tasks/task-001/diff?context=abc", nil)
	request.SetPathValue("task_id", "task-001")

	server.handleAPITaskDiff(recorder, request)

	if recorder.Code != http.StatusBadRequest {
		t.Fatalf("expected 400, got %d: %s", recorder.Code, recorder.Body.String())
	}
}

func TestDetectLanguageFromFilePath(t *testing.T) {
	cases := []struct {
		path     string
		expected string
	}{
		{"main.go", "go"},
		{"script.py", "python"},
		{"app.ts", "typescript"},
		{"component.tsx", "typescriptreact"},
		{"index.js", "javascript"},
		{"component.jsx", "javascriptreact"},
		{"lib.rs", "rust"},
		{"Main.java", "java"},
		{"app.rb", "ruby"},
		{"main.c", "c"},
		{"algo.cpp", "cpp"},
		{"Program.cs", "csharp"},
		{"app.swift", "swift"},
		{"main.kt", "kotlin"},
		{"query.sql", "sql"},
		{"page.html", "html"},
		{"styles.css", "css"},
		{"theme.scss", "scss"},
		{"config.json", "json"},
		{"deploy.yaml", "yaml"},
		{"settings.yml", "yaml"},
		{"config.toml", "toml"},
		{"layout.xml", "xml"},
		{"run.sh", "shell"},
		{"README.md", "markdown"},
		{"Dockerfile", "dockerfile"},
		{"build.dockerfile", "dockerfile"},
		{"Makefile", "makefile"},
		{"unknown.xyz", ""},
	}

	for _, tc := range cases {
		t.Run(tc.path, func(t *testing.T) {
			got := detectLanguage(tc.path)
			if got != tc.expected {
				t.Fatalf("detectLanguage(%q) = %q, want %q", tc.path, got, tc.expected)
			}
		})
	}
}

func TestHandleAPITaskDiffPopulatesLanguage(t *testing.T) {
	server := testConsoleServer(roundTripFunc(func(r *http.Request) (*http.Response, error) {
		return testJSONResponse(t, http.StatusOK, DiffResponse{
			TaskID: "task-lang",
			Mode:   DiffModeUnified,
			Files: []FileDiff{
				{Path: "src/main.py", Additions: 10, Deletions: 2},
				{Path: "config.yaml", Additions: 3, Deletions: 0},
				{Path: "logo.png", Binary: true},
			},
		}), nil
	}))

	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodGet, "/api/tasks/task-lang/diff", nil)
	request.SetPathValue("task_id", "task-lang")

	server.handleAPITaskDiff(recorder, request)

	if recorder.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d: %s", recorder.Code, recorder.Body.String())
	}

	var resp DiffResponse
	decodeTestJSON(t, recorder, &resp)

	if resp.Files[0].Language != "python" {
		t.Fatalf("expected python for .py file, got %q", resp.Files[0].Language)
	}
	if resp.Files[1].Language != "yaml" {
		t.Fatalf("expected yaml for .yaml file, got %q", resp.Files[1].Language)
	}
	// Binary files should not get language set
	if resp.Files[2].Language != "" {
		t.Fatalf("expected empty language for binary file, got %q", resp.Files[2].Language)
	}
}

func TestHandleAPITaskMergeNoResolutionOptionsOnSuccess(t *testing.T) {
	server := testConsoleServer(roundTripFunc(func(r *http.Request) (*http.Response, error) {
		return testJSONResponse(t, http.StatusOK, MergeResponse{
			TaskID:      "task-ok",
			Status:      "merged",
			Strategy:    MergeStrategySquash,
			MergeCommit: "abc123",
		}), nil
	}))

	body := bytes.NewBufferString(`{"strategy":"squash"}`)
	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodPost, "/api/tasks/task-ok/merge", body)
	request.SetPathValue("task_id", "task-ok")

	server.handleAPITaskMerge(recorder, request)

	if recorder.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d: %s", recorder.Code, recorder.Body.String())
	}

	var resp MergeResponse
	decodeTestJSON(t, recorder, &resp)

	if resp.Status != "merged" {
		t.Fatalf("expected status=merged, got %q", resp.Status)
	}
	if len(resp.ResolutionOptions) != 0 {
		t.Fatalf("expected no resolution options on success, got %d", len(resp.ResolutionOptions))
	}
}
