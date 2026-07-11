package orchestratorhost

import (
	"context"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"strings"

	"github.com/Vatthu/vikram/pkg/logger"
	"github.com/Vatthu/vikram/pkg/orchestrator"
)

// handleMultiRepoProvision provisions worktrees for multiple repositories under
// a single task workspace root. Each repo gets its own worktree directory named
// after the repository basename within the task's worktree area.
func (s *Server) handleMultiRepoProvision(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "method not allowed"})
		return
	}

	var req orchestrator.MultiRepoProvisionRequest
	if err := decodeJSON(w, r, &req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	if !taskIDPattern.MatchString(req.TaskID) {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "task_id contains unsupported characters"})
		return
	}
	if len(req.Repos) == 0 {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "repos must contain at least one repository"})
		return
	}
	if len(req.Repos) > 8 {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "repos must contain at most 8 repositories"})
		return
	}

	// Provision the task root directories.
	tasksRoot, tasksRootReal, err := managedRootForWorkspace(s.cfg.WorkspaceRoot, "tasks")
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": err.Error()})
		return
	}
	taskRoot, err := resolvePathInsideRoot(tasksRoot, tasksRootReal, req.TaskID, "task_id", true)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	artifactsDir := filepath.Join(taskRoot, "artifacts")
	logsDir := filepath.Join(taskRoot, "logs")
	scratchDir := filepath.Join(taskRoot, "scratch")

	for _, path := range []string{artifactsDir, logsDir, scratchDir} {
		if err := os.MkdirAll(path, 0o755); err != nil {
			writeJSON(w, http.StatusInternalServerError, map[string]string{"error": fmt.Sprintf("failed to provision workspace: %v", err)})
			return
		}
	}

	// Provision worktrees for each repo.
	worktrees := make([]orchestrator.MultiRepoWorktreeResult, 0, len(req.Repos))
	for _, repo := range req.Repos {
		result := s.provisionRepoWorktree(r.Context(), req.TaskID, repo)
		worktrees = append(worktrees, result)
	}

	writeJSON(w, http.StatusOK, orchestrator.MultiRepoProvisionResponse{
		TaskID:       req.TaskID,
		TaskRoot:     taskRoot,
		ArtifactsDir: artifactsDir,
		LogsDir:      logsDir,
		ScratchDir:   scratchDir,
		Worktrees:    worktrees,
	})
}

// provisionRepoWorktree provisions a single repository worktree within the task's
// workspace area. Each repo is placed at worktrees/{task_id}/{repo_basename}.
func (s *Server) provisionRepoWorktree(ctx context.Context, taskID string, repo orchestrator.RepoRef) orchestrator.MultiRepoWorktreeResult {
	// Validate taskID format to prevent path traversal (also validated by caller).
	if !taskIDPattern.MatchString(taskID) {
		return orchestrator.MultiRepoWorktreeResult{
			RepoPath: repo.Path,
			Error:    "task_id contains unsupported characters",
		}
	}

	repoPath, err := validatedGitRepositoryPath(s.cfg.WorkspaceRoot, repo.Path)
	if err != nil {
		return orchestrator.MultiRepoWorktreeResult{
			RepoPath: repo.Path,
			Error:    fmt.Sprintf("invalid repo path: %v", err),
		}
	}

	// Derive worktree directory name from repository basename.
	repoBaseName := filepath.Base(repoPath)
	if repoBaseName == "" || repoBaseName == "." || repoBaseName == "/" {
		return orchestrator.MultiRepoWorktreeResult{
			RepoPath: repoPath,
			Error:    "cannot determine repository basename",
		}
	}

	worktreeRoot, _, err := managedRootForWorkspace(s.cfg.WorkspaceRoot, "worktrees")
	if err != nil {
		return orchestrator.MultiRepoWorktreeResult{
			RepoPath: repoPath,
			Error:    fmt.Sprintf("failed to resolve worktree root: %v", err),
		}
	}
	worktreePath := filepath.Join(worktreeRoot, taskID, repoBaseName)

	// Verify the derived path is within the managed worktree root (defense in depth).
	absWorktree, _ := filepath.Abs(worktreePath)
	absRoot, _ := filepath.Abs(worktreeRoot)
	if !strings.HasPrefix(absWorktree, absRoot) {
		return orchestrator.MultiRepoWorktreeResult{
			RepoPath: repoPath,
			Error:    "derived worktree path escapes managed root",
		}
	}

	if err := os.MkdirAll(filepath.Dir(worktreePath), 0o755); err != nil {
		return orchestrator.MultiRepoWorktreeResult{
			RepoPath: repoPath,
			Error:    fmt.Sprintf("failed to create worktree parent: %v", err),
		}
	}

	// Check if the worktree already exists.
	if _, err := os.Stat(filepath.Join(worktreePath, ".git")); err == nil {
		headRef, headErr := gitHeadRef(ctx, worktreePath)
		if headErr != nil {
			return orchestrator.MultiRepoWorktreeResult{
				RepoPath:     repoPath,
				WorktreePath: worktreePath,
				Error:        fmt.Sprintf("existing worktree unreadable: %v", headErr),
			}
		}
		branch, _ := gitBranchName(ctx, worktreePath)
		return orchestrator.MultiRepoWorktreeResult{
			RepoPath:     repoPath,
			WorktreePath: worktreePath,
			Branch:       branch,
			HeadRef:      headRef,
			Created:      false,
		}
	}

	// Create the worktree with a task-scoped branch.
	branch := fmt.Sprintf("vikram/%s/%s", taskID, repoBaseName)
	baseRef := repo.DefaultBranch
	if baseRef == "" {
		baseRef = "main"
	}

	output, err := runGit(ctx, repoPath, "worktree", "add", "-b", branch, worktreePath, baseRef)
	if err != nil {
		return orchestrator.MultiRepoWorktreeResult{
			RepoPath:     repoPath,
			WorktreePath: worktreePath,
			Branch:       branch,
			Error:        fmt.Sprintf("git worktree add failed: %v: %s", err, strings.TrimSpace(output)),
		}
	}

	headRef, _ := gitCommitRef(ctx, worktreePath)

	logger.InfoCF("multi-repo", "Provisioned worktree for repo", map[string]interface{}{
		"task_id":       taskID,
		"repo_path":     repoPath,
		"worktree_path": worktreePath,
		"branch":        branch,
		"head_ref":      headRef,
	})

	return orchestrator.MultiRepoWorktreeResult{
		RepoPath:     repoPath,
		WorktreePath: worktreePath,
		Branch:       branch,
		HeadRef:      headRef,
		Created:      true,
	}
}

// handleAtomicMerge performs sequential fast-forward merges across multiple
// repositories. If any merge fails, all previously merged repositories are
// rolled back to their pre-merge state.
func (s *Server) handleAtomicMerge(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "method not allowed"})
		return
	}

	// Extract task_id from path pattern: /v1/tasks/{task_id}/merge-atomic
	taskID := r.PathValue("task_id")
	if taskID == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "task_id is required in path"})
		return
	}
	if !taskIDPattern.MatchString(taskID) {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "task_id contains unsupported characters"})
		return
	}

	var req orchestrator.AtomicMergeRequest
	if err := decodeJSON(w, r, &req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	// Use task_id from path.
	req.TaskID = taskID

	if len(req.Repos) == 0 {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "repos must contain at least one entry"})
		return
	}

	results := make([]orchestrator.AtomicMergeResult, 0, len(req.Repos))
	mergedRepos := make([]mergeRollbackInfo, 0, len(req.Repos))
	var failedAt string

	for _, entry := range req.Repos {
		result, rollbackInfo := s.mergeOneRepo(r.Context(), entry)
		results = append(results, result)

		if result.Error != "" {
			failedAt = entry.RepoPath
			// Rollback all previously merged repos.
			s.rollbackMergedRepos(r.Context(), mergedRepos, results)
			break
		}
		rollbackInfo.index = len(results) - 1
		mergedRepos = append(mergedRepos, rollbackInfo)
	}

	success := failedAt == ""
	writeJSON(w, http.StatusOK, orchestrator.AtomicMergeResponse{
		TaskID:   req.TaskID,
		Success:  success,
		Results:  results,
		FailedAt: failedAt,
	})
}

// mergeRollbackInfo tracks the pre-merge state for rollback purposes.
type mergeRollbackInfo struct {
	repoPath     string
	targetBranch string
	preMergeRef  string
	index        int // index into results slice
}

// mergeOneRepo performs a fast-forward merge for a single repository entry.
// Returns the result and rollback info needed if a subsequent repo fails.
func (s *Server) mergeOneRepo(ctx context.Context, entry orchestrator.AtomicMergeEntry) (orchestrator.AtomicMergeResult, mergeRollbackInfo) {
	repoPath := entry.RepoPath
	branch := entry.Branch
	targetBranch := entry.TargetBranch

	if strings.TrimSpace(repoPath) == "" || strings.TrimSpace(branch) == "" || strings.TrimSpace(targetBranch) == "" {
		return orchestrator.AtomicMergeResult{
			RepoPath: repoPath,
			Error:    "repo_path, branch, and target_branch are required",
		}, mergeRollbackInfo{}
	}

	// Record pre-merge ref on target branch.
	preMergeRef, err := resolveRef(ctx, repoPath, targetBranch)
	if err != nil {
		return orchestrator.AtomicMergeResult{
			RepoPath: repoPath,
			Error:    fmt.Sprintf("failed to resolve target branch %s: %v", targetBranch, err),
		}, mergeRollbackInfo{}
	}

	// Perform fast-forward merge: update target branch to point to source branch head.
	sourceBranchRef, err := resolveRef(ctx, repoPath, branch)
	if err != nil {
		return orchestrator.AtomicMergeResult{
			RepoPath:    repoPath,
			PreMergeRef: preMergeRef,
			Error:       fmt.Sprintf("failed to resolve source branch %s: %v", branch, err),
		}, mergeRollbackInfo{}
	}

	// Check if fast-forward is possible: source must be a descendant of target.
	if !isFastForwardable(ctx, repoPath, preMergeRef, sourceBranchRef) {
		return orchestrator.AtomicMergeResult{
			RepoPath:    repoPath,
			PreMergeRef: preMergeRef,
			Error:       fmt.Sprintf("cannot fast-forward %s to %s: not a linear descendant", targetBranch, branch),
		}, mergeRollbackInfo{}
	}

	// Execute the fast-forward by updating the target branch ref directly.
	// We use update-ref instead of branch -f to avoid the "cannot force update
	// the branch used by worktree" error when the target is checked out.
	targetRefPath := "refs/heads/" + targetBranch
	output, err := runGit(ctx, repoPath, "update-ref", targetRefPath, sourceBranchRef, preMergeRef)
	if err != nil {
		return orchestrator.AtomicMergeResult{
			RepoPath:    repoPath,
			PreMergeRef: preMergeRef,
			Error:       fmt.Sprintf("fast-forward failed: %v: %s", err, strings.TrimSpace(output)),
		}, mergeRollbackInfo{}
	}

	logger.InfoCF("multi-repo", "Merged repo via fast-forward", map[string]interface{}{
		"repo_path":      repoPath,
		"branch":         branch,
		"target_branch":  targetBranch,
		"pre_merge_ref":  preMergeRef,
		"post_merge_ref": sourceBranchRef,
	})

	return orchestrator.AtomicMergeResult{
			RepoPath:     repoPath,
			Merged:       true,
			PreMergeRef:  preMergeRef,
			PostMergeRef: sourceBranchRef,
		}, mergeRollbackInfo{
			repoPath:     repoPath,
			targetBranch: targetBranch,
			preMergeRef:  preMergeRef,
		}
}

// rollbackMergedRepos resets all previously merged repositories to their
// pre-merge state. Updates the results slice to mark rolled-back entries.
func (s *Server) rollbackMergedRepos(ctx context.Context, merged []mergeRollbackInfo, results []orchestrator.AtomicMergeResult) {
	for _, info := range merged {
		targetRefPath := "refs/heads/" + info.targetBranch
		output, err := runGit(ctx, info.repoPath, "update-ref", targetRefPath, info.preMergeRef)
		if err != nil {
			logger.InfoCF("multi-repo", "Rollback failed for repo", map[string]interface{}{
				"repo_path": info.repoPath,
				"error":     fmt.Sprintf("%v: %s", err, strings.TrimSpace(output)),
			})
			// Mark the result as having a rollback error.
			results[info.index].Error = fmt.Sprintf("rollback failed: %v", err)
		} else {
			results[info.index].RolledBack = true
			logger.InfoCF("multi-repo", "Rolled back repo", map[string]interface{}{
				"repo_path":     info.repoPath,
				"target_branch": info.targetBranch,
				"restored_ref":  info.preMergeRef,
			})
		}
	}
}

// handleDetachRepo detaches a blocked repository from a multi-repo task.
// It removes the worktree and marks the repo as detached, allowing remaining
// repositories to proceed to merge independently.
func (s *Server) handleDetachRepo(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "method not allowed"})
		return
	}

	// Extract task_id from path pattern: /v1/tasks/{task_id}/detach-repo
	taskID := r.PathValue("task_id")
	if taskID == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "task_id is required in path"})
		return
	}
	if !taskIDPattern.MatchString(taskID) {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "task_id contains unsupported characters"})
		return
	}

	var req orchestrator.DetachRepoRequest
	if err := decodeJSON(w, r, &req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	// Override task_id from path.
	req.TaskID = taskID

	if strings.TrimSpace(req.RepoPath) == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "repo_path is required"})
		return
	}

	// Determine the worktree path for this repo under the task.
	repoBaseName := filepath.Base(strings.TrimSpace(req.RepoPath))
	if repoBaseName == "" || repoBaseName == "." || repoBaseName == "/" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "cannot determine repository basename from repo_path"})
		return
	}

	worktreeRoot, _, err := managedRootForWorkspace(s.cfg.WorkspaceRoot, "worktrees")
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": err.Error()})
		return
	}
	worktreePath := filepath.Join(worktreeRoot, taskID, repoBaseName)

	// Verify the derived path is within the managed worktree root (defense in depth).
	absWorktree, _ := filepath.Abs(worktreePath)
	absRoot, _ := filepath.Abs(worktreeRoot)
	if !strings.HasPrefix(absWorktree, absRoot) {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "derived worktree path escapes managed root"})
		return
	}

	// Check if the worktree exists.
	cleaned := false
	if _, err := os.Stat(worktreePath); err == nil {
		// Remove the worktree via git.
		repoPath := strings.TrimSpace(req.RepoPath)
		output, err := runGit(r.Context(), repoPath, "worktree", "remove", "--force", worktreePath)
		if err != nil {
			// If git worktree remove fails, try direct removal.
			logger.InfoCF("multi-repo", "git worktree remove failed, attempting direct cleanup", map[string]interface{}{
				"task_id":       taskID,
				"repo_path":     repoPath,
				"worktree_path": worktreePath,
				"error":         fmt.Sprintf("%v: %s", err, strings.TrimSpace(output)),
			})
			if removeErr := os.RemoveAll(worktreePath); removeErr != nil {
				writeJSON(w, http.StatusInternalServerError, map[string]string{
					"error": fmt.Sprintf("failed to remove worktree: %v", removeErr),
				})
				return
			}
		}
		cleaned = true
	}

	logger.InfoCF("multi-repo", "Detached repo from task", map[string]interface{}{
		"task_id":       taskID,
		"repo_path":     req.RepoPath,
		"reason":        req.Reason,
		"worktree_path": worktreePath,
		"cleaned":       cleaned,
	})

	writeJSON(w, http.StatusOK, orchestrator.DetachRepoResponse{
		TaskID:       taskID,
		RepoPath:     req.RepoPath,
		Detached:     true,
		WorktreePath: worktreePath,
		Cleaned:      cleaned,
	})
}

// resolveRef resolves a branch name or ref to its full SHA commit hash.
func resolveRef(ctx context.Context, repoPath, ref string) (string, error) {
	output, err := runGit(ctx, repoPath, "rev-parse", ref)
	if err != nil {
		return "", fmt.Errorf("rev-parse %s: %v: %s", ref, err, strings.TrimSpace(output))
	}
	return strings.TrimSpace(output), nil
}

// isFastForwardable checks whether targetRef is an ancestor of sourceRef,
// meaning a fast-forward from target to source is possible.
func isFastForwardable(ctx context.Context, repoPath, targetRef, sourceRef string) bool {
	output, err := runGit(ctx, repoPath, "merge-base", "--is-ancestor", targetRef, sourceRef)
	if err != nil {
		// If there's an error with output containing anything, log it but return false.
		_ = output
		return false
	}
	return true
}
