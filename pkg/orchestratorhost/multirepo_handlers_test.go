package orchestratorhost

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	"github.com/Vatthu/vikram/pkg/orchestrator"
	"github.com/stretchr/testify/require"
)

// initTestRepo creates a git repo with an initial commit inside the given root.
func initTestRepo(t *testing.T, root, name string) string {
	t.Helper()

	repoDir := filepath.Join(root, name)
	require.NoError(t, os.MkdirAll(repoDir, 0o755))
	runGitCommand(t, repoDir, "init")
	runGitCommand(t, repoDir, "config", "user.email", "test@test.com")
	runGitCommand(t, repoDir, "config", "user.name", "Test")

	// Create an initial commit on main branch.
	require.NoError(t, os.WriteFile(filepath.Join(repoDir, "README.md"), []byte("# "+name), 0o644))
	runGitCommand(t, repoDir, "add", ".")
	runGitCommand(t, repoDir, "commit", "-m", "initial commit")

	// Ensure the default branch is named "main".
	cmd := exec.Command("git", "-C", repoDir, "branch", "-M", "main")
	_ = cmd.Run() // ignore error if already named main

	return repoDir
}

func TestMultiRepoProvision(t *testing.T) {
	root := t.TempDir()
	workspace := filepath.Join(root, "workspace")
	require.NoError(t, os.MkdirAll(workspace, 0o755))

	// Create two test repos inside the workspace.
	repoA := initTestRepo(t, workspace, "repo-alpha")
	repoB := initTestRepo(t, workspace, "repo-beta")

	server := NewServer(Config{
		SocketPath:          filepath.Join(root, "run", "vikramd.sock"),
		WorkspaceRoot:       workspace,
		RestrictToWorkspace: true,
	}, nil)

	reqBody, err := json.Marshal(orchestrator.MultiRepoProvisionRequest{
		TaskID: "multi-task-1",
		Repos: []orchestrator.RepoRef{
			{Path: repoA, DefaultBranch: "main"},
			{Path: repoB, DefaultBranch: "main"},
		},
	})
	require.NoError(t, err)

	req := httptest.NewRequest(http.MethodPost, "/v1/workspaces/provision-multi", bytes.NewReader(reqBody))
	rec := httptest.NewRecorder()
	server.handler().ServeHTTP(rec, req)

	require.Equal(t, http.StatusOK, rec.Code, "body: %s", rec.Body.String())

	var resp orchestrator.MultiRepoProvisionResponse
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &resp))
	require.Equal(t, "multi-task-1", resp.TaskID)
	require.DirExists(t, resp.ArtifactsDir)
	require.DirExists(t, resp.LogsDir)
	require.DirExists(t, resp.ScratchDir)
	require.Len(t, resp.Worktrees, 2)

	// Both worktrees should be created successfully.
	for _, wt := range resp.Worktrees {
		require.Empty(t, wt.Error, "worktree error for %s: %s", wt.RepoPath, wt.Error)
		require.True(t, wt.Created)
		require.NotEmpty(t, wt.WorktreePath)
		require.NotEmpty(t, wt.Branch)
		require.NotEmpty(t, wt.HeadRef)
		require.DirExists(t, wt.WorktreePath)
	}
}

func TestMultiRepoProvisionValidation(t *testing.T) {
	root := t.TempDir()
	server := NewServer(Config{
		SocketPath:    filepath.Join(root, "run", "vikramd.sock"),
		WorkspaceRoot: root,
	}, nil)

	t.Run("empty repos", func(t *testing.T) {
		reqBody, _ := json.Marshal(orchestrator.MultiRepoProvisionRequest{
			TaskID: "task-1",
			Repos:  []orchestrator.RepoRef{},
		})
		req := httptest.NewRequest(http.MethodPost, "/v1/workspaces/provision-multi", bytes.NewReader(reqBody))
		rec := httptest.NewRecorder()
		server.handler().ServeHTTP(rec, req)
		require.Equal(t, http.StatusBadRequest, rec.Code)
	})

	t.Run("too many repos", func(t *testing.T) {
		repos := make([]orchestrator.RepoRef, 9)
		for i := range repos {
			repos[i] = orchestrator.RepoRef{Path: "/tmp/fake", DefaultBranch: "main"}
		}
		reqBody, _ := json.Marshal(orchestrator.MultiRepoProvisionRequest{
			TaskID: "task-1",
			Repos:  repos,
		})
		req := httptest.NewRequest(http.MethodPost, "/v1/workspaces/provision-multi", bytes.NewReader(reqBody))
		rec := httptest.NewRecorder()
		server.handler().ServeHTTP(rec, req)
		require.Equal(t, http.StatusBadRequest, rec.Code)
	})

	t.Run("invalid task_id", func(t *testing.T) {
		reqBody, _ := json.Marshal(orchestrator.MultiRepoProvisionRequest{
			TaskID: "../escape",
			Repos:  []orchestrator.RepoRef{{Path: "/tmp/r", DefaultBranch: "main"}},
		})
		req := httptest.NewRequest(http.MethodPost, "/v1/workspaces/provision-multi", bytes.NewReader(reqBody))
		rec := httptest.NewRecorder()
		server.handler().ServeHTTP(rec, req)
		require.Equal(t, http.StatusBadRequest, rec.Code)
	})
}

func TestMultiRepoProvisionIdempotent(t *testing.T) {
	root := t.TempDir()
	workspace := filepath.Join(root, "workspace")
	require.NoError(t, os.MkdirAll(workspace, 0o755))

	repoA := initTestRepo(t, workspace, "repo-idem")

	server := NewServer(Config{
		SocketPath:          filepath.Join(root, "run", "vikramd.sock"),
		WorkspaceRoot:       workspace,
		RestrictToWorkspace: true,
	}, nil)

	reqBody, _ := json.Marshal(orchestrator.MultiRepoProvisionRequest{
		TaskID: "idem-task",
		Repos:  []orchestrator.RepoRef{{Path: repoA, DefaultBranch: "main"}},
	})

	// First call creates.
	req := httptest.NewRequest(http.MethodPost, "/v1/workspaces/provision-multi", bytes.NewReader(reqBody))
	rec := httptest.NewRecorder()
	server.handler().ServeHTTP(rec, req)
	require.Equal(t, http.StatusOK, rec.Code)
	var resp1 orchestrator.MultiRepoProvisionResponse
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &resp1))
	require.True(t, resp1.Worktrees[0].Created)

	// Second call reuses.
	req = httptest.NewRequest(http.MethodPost, "/v1/workspaces/provision-multi", bytes.NewReader(reqBody))
	rec = httptest.NewRecorder()
	server.handler().ServeHTTP(rec, req)
	require.Equal(t, http.StatusOK, rec.Code)
	var resp2 orchestrator.MultiRepoProvisionResponse
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &resp2))
	require.False(t, resp2.Worktrees[0].Created) // Not created again.
	require.Equal(t, resp1.Worktrees[0].WorktreePath, resp2.Worktrees[0].WorktreePath)
}

func TestAtomicMergeSuccess(t *testing.T) {
	root := t.TempDir()
	workspace := filepath.Join(root, "workspace")
	require.NoError(t, os.MkdirAll(workspace, 0o755))

	repoA := initTestRepo(t, workspace, "merge-repo-a")
	repoB := initTestRepo(t, workspace, "merge-repo-b")

	// Create feature branches with commits in each repo.
	addFeatureBranch(t, repoA, "feature/task-1")
	addFeatureBranch(t, repoB, "feature/task-1")

	server := NewServer(Config{
		SocketPath:    filepath.Join(root, "run", "vikramd.sock"),
		WorkspaceRoot: workspace,
	}, nil)

	reqBody, _ := json.Marshal(orchestrator.AtomicMergeRequest{
		TaskID: "merge-task-1",
		Repos: []orchestrator.AtomicMergeEntry{
			{RepoPath: repoA, Branch: "feature/task-1", TargetBranch: "main"},
			{RepoPath: repoB, Branch: "feature/task-1", TargetBranch: "main"},
		},
	})

	req := httptest.NewRequest(http.MethodPost, "/v1/tasks/merge-task-1/merge-atomic", bytes.NewReader(reqBody))
	req.SetPathValue("task_id", "merge-task-1")
	rec := httptest.NewRecorder()
	server.handler().ServeHTTP(rec, req)

	require.Equal(t, http.StatusOK, rec.Code, "body: %s", rec.Body.String())

	var resp orchestrator.AtomicMergeResponse
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &resp))
	require.True(t, resp.Success)
	require.Empty(t, resp.FailedAt)
	require.Len(t, resp.Results, 2)

	for _, r := range resp.Results {
		require.True(t, r.Merged)
		require.Empty(t, r.Error)
		require.NotEmpty(t, r.PreMergeRef)
		require.NotEmpty(t, r.PostMergeRef)
		require.NotEqual(t, r.PreMergeRef, r.PostMergeRef)
	}
}

func TestAtomicMergeRollbackOnFailure(t *testing.T) {
	root := t.TempDir()
	workspace := filepath.Join(root, "workspace")
	require.NoError(t, os.MkdirAll(workspace, 0o755))

	repoA := initTestRepo(t, workspace, "rollback-repo-a")
	repoB := initTestRepo(t, workspace, "rollback-repo-b")

	// Create a feature branch in repo A.
	addFeatureBranch(t, repoA, "feature/rollback-test")
	// Repo B: create a divergent commit on main, making fast-forward impossible.
	addDivergentMain(t, repoB, "feature/rollback-test")

	// Record pre-merge state of repoA main.
	preMergeRefA := getRef(t, repoA, "main")

	server := NewServer(Config{
		SocketPath:    filepath.Join(root, "run", "vikramd.sock"),
		WorkspaceRoot: workspace,
	}, nil)

	reqBody, _ := json.Marshal(orchestrator.AtomicMergeRequest{
		TaskID: "rollback-task",
		Repos: []orchestrator.AtomicMergeEntry{
			{RepoPath: repoA, Branch: "feature/rollback-test", TargetBranch: "main"},
			{RepoPath: repoB, Branch: "feature/rollback-test", TargetBranch: "main"},
		},
	})

	req := httptest.NewRequest(http.MethodPost, "/v1/tasks/rollback-task/merge-atomic", bytes.NewReader(reqBody))
	req.SetPathValue("task_id", "rollback-task")
	rec := httptest.NewRecorder()
	server.handler().ServeHTTP(rec, req)

	require.Equal(t, http.StatusOK, rec.Code, "body: %s", rec.Body.String())

	var resp orchestrator.AtomicMergeResponse
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &resp))
	require.False(t, resp.Success)
	require.Equal(t, repoB, resp.FailedAt)

	// Repo A should have been rolled back.
	require.True(t, resp.Results[0].RolledBack)
	postRollbackRefA := getRef(t, repoA, "main")
	require.Equal(t, preMergeRefA, postRollbackRefA, "repoA main should be restored after rollback")

	// Repo B should have an error.
	require.NotEmpty(t, resp.Results[1].Error)
}

func TestDetachRepo(t *testing.T) {
	root := t.TempDir()
	workspace := filepath.Join(root, "workspace")
	require.NoError(t, os.MkdirAll(workspace, 0o755))

	repoA := initTestRepo(t, workspace, "detach-repo")

	server := NewServer(Config{
		SocketPath:          filepath.Join(root, "run", "vikramd.sock"),
		WorkspaceRoot:       workspace,
		RestrictToWorkspace: true,
	}, nil)

	// First provision the multi-repo workspace.
	provBody, _ := json.Marshal(orchestrator.MultiRepoProvisionRequest{
		TaskID: "detach-task",
		Repos:  []orchestrator.RepoRef{{Path: repoA, DefaultBranch: "main"}},
	})
	provReq := httptest.NewRequest(http.MethodPost, "/v1/workspaces/provision-multi", bytes.NewReader(provBody))
	provRec := httptest.NewRecorder()
	server.handler().ServeHTTP(provRec, provReq)
	require.Equal(t, http.StatusOK, provRec.Code, "provision body: %s", provRec.Body.String())

	var provResp orchestrator.MultiRepoProvisionResponse
	require.NoError(t, json.Unmarshal(provRec.Body.Bytes(), &provResp))
	require.Len(t, provResp.Worktrees, 1)
	require.Empty(t, provResp.Worktrees[0].Error)
	require.DirExists(t, provResp.Worktrees[0].WorktreePath)

	// Now detach the repo.
	detachBody, _ := json.Marshal(orchestrator.DetachRepoRequest{
		RepoPath: repoA,
		Reason:   "repo is blocked",
	})
	detachReq := httptest.NewRequest(http.MethodPost, "/v1/tasks/detach-task/detach-repo", bytes.NewReader(detachBody))
	detachReq.SetPathValue("task_id", "detach-task")
	detachRec := httptest.NewRecorder()
	server.handler().ServeHTTP(detachRec, detachReq)

	require.Equal(t, http.StatusOK, detachRec.Code, "body: %s", detachRec.Body.String())

	var detachResp orchestrator.DetachRepoResponse
	require.NoError(t, json.Unmarshal(detachRec.Body.Bytes(), &detachResp))
	require.True(t, detachResp.Detached)
	require.True(t, detachResp.Cleaned)
	require.Equal(t, "detach-task", detachResp.TaskID)
	require.Equal(t, repoA, detachResp.RepoPath)

	// Worktree directory should no longer exist.
	require.NoDirExists(t, provResp.Worktrees[0].WorktreePath)
}

func TestDetachRepoNonExistentWorktree(t *testing.T) {
	root := t.TempDir()
	workspace := filepath.Join(root, "workspace")
	require.NoError(t, os.MkdirAll(workspace, 0o755))
	// Create worktrees directory.
	require.NoError(t, os.MkdirAll(filepath.Join(workspace, "worktrees"), 0o755))

	server := NewServer(Config{
		SocketPath:    filepath.Join(root, "run", "vikramd.sock"),
		WorkspaceRoot: workspace,
	}, nil)

	detachBody, _ := json.Marshal(orchestrator.DetachRepoRequest{
		RepoPath: "/some/nonexistent/repo",
		Reason:   "test",
	})
	detachReq := httptest.NewRequest(http.MethodPost, "/v1/tasks/no-task/detach-repo", bytes.NewReader(detachBody))
	detachReq.SetPathValue("task_id", "no-task")
	detachRec := httptest.NewRecorder()
	server.handler().ServeHTTP(detachRec, detachReq)

	require.Equal(t, http.StatusOK, detachRec.Code)

	var resp orchestrator.DetachRepoResponse
	require.NoError(t, json.Unmarshal(detachRec.Body.Bytes(), &resp))
	require.True(t, resp.Detached)
	require.False(t, resp.Cleaned) // Nothing to clean.
}

// --- Test Helpers ---

func addFeatureBranch(t *testing.T, repoPath, branchName string) {
	t.Helper()
	runGitCommand(t, repoPath, "checkout", "-b", branchName)
	require.NoError(t, os.WriteFile(filepath.Join(repoPath, "feature.txt"), []byte("feature content for "+branchName), 0o644))
	runGitCommand(t, repoPath, "add", ".")
	runGitCommand(t, repoPath, "commit", "-m", "add feature on "+branchName)
	runGitCommand(t, repoPath, "checkout", "main")
}

func addDivergentMain(t *testing.T, repoPath, branchName string) {
	t.Helper()
	// Create the feature branch first.
	runGitCommand(t, repoPath, "checkout", "-b", branchName)
	require.NoError(t, os.WriteFile(filepath.Join(repoPath, "feature.txt"), []byte("feature content"), 0o644))
	runGitCommand(t, repoPath, "add", ".")
	runGitCommand(t, repoPath, "commit", "-m", "add feature on "+branchName)

	// Go back to main and add a divergent commit.
	runGitCommand(t, repoPath, "checkout", "main")
	require.NoError(t, os.WriteFile(filepath.Join(repoPath, "divergent.txt"), []byte("divergent commit"), 0o644))
	runGitCommand(t, repoPath, "add", ".")
	runGitCommand(t, repoPath, "commit", "-m", "divergent commit on main")
}

func getRef(t *testing.T, repoPath, ref string) string {
	t.Helper()
	out, err := runGit(context.Background(), repoPath, "rev-parse", ref)
	require.NoError(t, err)
	return strings.TrimSpace(out)
}
