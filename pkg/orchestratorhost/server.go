package orchestratorhost

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"
	"unicode"

	"github.com/Vatthu/vikram/pkg/costledger"
	"github.com/Vatthu/vikram/pkg/logger"
	"github.com/Vatthu/vikram/pkg/orchestrator"
	"github.com/Vatthu/vikram/pkg/telemetry"
	"github.com/Vatthu/vikram/pkg/tools"
)

const maxInboundBodyBytes = 1 << 20
const maxRepoPreviewBytes = 1200
const defaultReadMaxBytes = 4000
const defaultArtifactReadMaxBytes = 32000
const maxArtifactReadBytes = 128 * 1024
const maxWriteBytes = 64 * 1024
const defaultTargetLimit = 6
const maxTargetDiscoveryFiles = 800
const maxTargetFileBytes = 256 * 1024
const defaultVerificationLimit = 6

var taskIDPattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$`)
var artifactIDPattern = regexp.MustCompile(`^[A-Za-z0-9._-]+$`)
var objectiveTokenPattern = regexp.MustCompile(`[A-Za-z0-9_]+`)
var gitSafeArgPattern = regexp.MustCompile(`^[A-Za-z0-9_./:@+-]+$`)

type notifier interface {
	SendToChannel(ctx context.Context, channelName, chatID, content string) error
}

type execTool interface {
	Execute(ctx context.Context, tc tools.ToolContext, args map[string]interface{}) *tools.ToolResult
}

// ReviewFunc is called by the /v1/review/change endpoint to obtain an
// independent LLM review verdict.  The caller provides task context; the
// implementation is responsible for selecting the reviewer model.
type ReviewFunc func(ctx context.Context, req orchestrator.ChangeReviewRequest) (orchestrator.ChangeReviewResponse, error)

// ThinkFunc is called by the /v1/agent/think endpoint to run a reasoning
// step requested by the Python orchestrator. The host executes the requested
// provider/model route and remains the native capability boundary.
type ThinkFunc func(ctx context.Context, req orchestrator.AgentThinkRequest) (orchestrator.AgentThinkResponse, error)

// Config defines the native host capability surface exposed to the orchestrator.
type Config struct {
	SocketPath          string
	WorkspaceRoot       string
	RestrictToWorkspace bool
	Sandboxed           bool
	TelegramEnabled     bool
	ReviewChange        ReviewFunc // optional LLM judge integration
	AgentThink          ThinkFunc  // optional per-role LLM reasoning
	AgentRoster         []orchestrator.AgentProfile
	TelemetryStore      telemetry.Store          // optional telemetry event store
	TelemetryStream     *telemetry.StreamHandler // optional WebSocket stream handler
}

// Server exposes the host daemon capability surface over a Unix domain socket.
type Server struct {
	cfg             Config
	notifier        notifier
	execTool        execTool
	ledger          costLedger
	traceVerifier   *telemetry.TraceVerifier
	formationStore  formationStore
	knowledgeStore  knowledgeStore
	lockRegistry    lockRegistry
	auditStore      auditStore
	configWatcher   *ConfigWatcher
	shutdownManager *ShutdownManager
	httpServer      *http.Server
	listener        net.Listener
	mu              sync.Mutex
}

// costLedger is the subset of costledger.Ledger used by the host server.
type costLedger interface {
	Record(ctx context.Context, rec costledger.CostRecord) error
	TaskCumulative(ctx context.Context, taskID string) (float64, error)
	DailyTotal(ctx context.Context) (float64, error)
	Forecast(ctx context.Context, complexity string, targetFiles int) (costledger.CostForecast, error)
}

// NewServer builds a host capability server around the current Vikram runtime.
func NewServer(cfg Config, notifier notifier) *Server {
	return &Server{
		cfg:      cfg,
		notifier: notifier,
		execTool: tools.NewExecToolForWorkspace(cfg.WorkspaceRoot, cfg.RestrictToWorkspace, cfg.Sandboxed, nil),
	}
}

// SetLedger configures the cost ledger used by the host server for cost
// recording and budget queries. Must be called before Start.
func (s *Server) SetLedger(l costLedger) {
	s.ledger = l
}

// SetTraceVerifier configures the execution trace verifier for hash chain
// integrity checks. Must be called before Start.
func (s *Server) SetTraceVerifier(tv *telemetry.TraceVerifier) {
	s.traceVerifier = tv
}

// SetLockRegistry configures the file-level lock registry for resource
// contention management. Must be called before Start.
func (s *Server) SetLockRegistry(lr lockRegistry) {
	s.lockRegistry = lr
}

// SetAuditStore configures the approval audit store for querying audit records.
// Must be called before Start.
func (s *Server) SetAuditStore(as auditStore) {
	s.auditStore = as
}

// SetConfigWatcher configures the filesystem config watcher for hot-reload.
// Must be called before Start.
func (s *Server) SetConfigWatcher(cw *ConfigWatcher) {
	s.configWatcher = cw
}

func (s *Server) handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", s.handleHealthz)
	mux.HandleFunc("/v1/system/health", s.handleHealthz)
	mux.HandleFunc("/v1/workspaces/provision", s.handleProvisionWorkspace)
	mux.HandleFunc("/v1/git/worktrees/create", s.handleCreateWorktree)
	mux.HandleFunc("/v1/git/worktrees/remove", s.handleRemoveWorktree)
	mux.HandleFunc("/v1/repos/inspect", s.handleInspectRepo)
	mux.HandleFunc("/v1/repos/discover-targets", s.handleDiscoverTargets)
	mux.HandleFunc("/v1/repos/discover-verification", s.handleDiscoverVerification)
	mux.HandleFunc("/v1/files/read", s.handleReadFile)
	mux.HandleFunc("/v1/files/write", s.handleWriteFile)
	mux.HandleFunc("/v1/files/replace", s.handleReplaceFile)
	mux.HandleFunc("/v1/artifacts/write", s.handleWriteArtifact)
	mux.HandleFunc("/v1/artifacts/read", s.handleReadArtifact)
	mux.HandleFunc("/v1/exec", s.handleExec)
	mux.HandleFunc("/v1/notify/telegram", s.handleNotifyTelegram)
	mux.HandleFunc("/v1/git/rollback", s.handleRollbackWorktree)
	mux.HandleFunc("/v1/review/change", s.handleReviewChange)
	mux.HandleFunc("/v1/agent/roster", s.handleAgentRoster)
	mux.HandleFunc("/v1/agent/think", s.handleAgentThink)
	mux.HandleFunc("/v1/repos/discover-lint", s.handleDiscoverLint)
	mux.HandleFunc("/v1/repos/run-lint", s.handleRunLint)
	mux.HandleFunc("/v1/browser/test", s.handleBrowserTest)
	mux.HandleFunc("/v1/telemetry/emit", s.handleTelemetryEmit)
	mux.HandleFunc("/v1/telemetry/summary", s.handleTelemetrySummary)
	mux.HandleFunc("/v1/telemetry/events", s.handleTelemetryEvents)
	mux.HandleFunc("/v1/telemetry/cost", s.handleTelemetryCost)
	if s.cfg.TelemetryStream != nil {
		mux.Handle("/v1/telemetry/stream", s.cfg.TelemetryStream)
	}
	mux.HandleFunc("POST /v1/cost/record", s.handleCostRecord)
	mux.HandleFunc("GET /v1/cost/task/{task_id}", s.handleCostTaskCumulative)
	mux.HandleFunc("POST /v1/cost/forecast", s.handleCostForecast)
	mux.HandleFunc("GET /v1/cost/daily", s.handleCostDaily)
	mux.HandleFunc("GET /v1/trace/query", s.handleTraceQuery)
	mux.HandleFunc("POST /v1/trace/replay", s.handleTraceReplay)
	mux.HandleFunc("GET /v1/models/performance", s.handleModelPerformance)
	mux.HandleFunc("GET /v1/formations/effectiveness", s.handleFormationEffectiveness)
	mux.HandleFunc("GET /v1/formations", s.handleFormations)
	mux.HandleFunc("POST /v1/formations", s.handleFormations)
	mux.HandleFunc("PUT /v1/formations/{name}", s.handleFormationByName)
	mux.HandleFunc("DELETE /v1/formations/{name}", s.handleFormationByName)
	mux.HandleFunc("POST /v1/locks/acquire", s.handleLockAcquire)
	mux.HandleFunc("POST /v1/locks/release", s.handleLockRelease)
	mux.HandleFunc("GET /v1/locks", s.handleLockQuery)
	mux.HandleFunc("GET /v1/approvals/audit", s.handleApprovalsAudit)
	mux.HandleFunc("GET /v1/knowledge/approaches", s.handleKnowledgeApproaches)
	mux.HandleFunc("GET /v1/knowledge/failures", s.handleKnowledgeFailures)
	mux.HandleFunc("POST /v1/workspaces/provision-multi", s.handleMultiRepoProvision)
	mux.HandleFunc("POST /v1/tasks/{task_id}/merge-atomic", s.handleAtomicMerge)
	mux.HandleFunc("POST /v1/tasks/{task_id}/detach-repo", s.handleDetachRepo)
	// Scheduler endpoints (Requirements 16.4, 18.1, 19.3)
	mux.HandleFunc("POST /v1/tasks", s.handleSchedulerCreateTask)
	mux.HandleFunc("PUT /v1/tasks/{task_id}/priority", s.handleUpdateTaskPriority)
	mux.HandleFunc("GET /v1/queue", s.handleGetQueue)
	return mux
}

// Start serves the host capability API on a Unix domain socket.
func (s *Server) Start(ctx context.Context) error {
	if strings.TrimSpace(s.cfg.SocketPath) == "" {
		return fmt.Errorf("socket path is required")
	}
	if strings.TrimSpace(s.cfg.WorkspaceRoot) == "" {
		return fmt.Errorf("workspace root is required")
	}

	if err := os.MkdirAll(filepath.Dir(s.cfg.SocketPath), 0o755); err != nil {
		return fmt.Errorf("create socket directory: %w", err)
	}
	if err := removeStaleSocket(s.cfg.SocketPath); err != nil {
		return fmt.Errorf("remove stale socket: %w", err)
	}

	listener, err := net.Listen("unix", s.cfg.SocketPath)
	if err != nil {
		return fmt.Errorf("listen on unix socket: %w", err)
	}
	if err := os.Chmod(s.cfg.SocketPath, 0o600); err != nil {
		listener.Close()
		return fmt.Errorf("chmod socket: %w", err)
	}

	s.mu.Lock()
	s.listener = listener
	s.httpServer = &http.Server{
		Handler:      s.handlerWithDrain(),
		ReadTimeout:  30 * time.Second,
		WriteTimeout: 30 * time.Second,
		IdleTimeout:  60 * time.Second,
	}
	s.mu.Unlock()

	go func() {
		<-ctx.Done()
		_ = s.Stop(context.Background())
	}()

	logger.InfoCF("orchestrator-host", "Host capability server starting", map[string]interface{}{
		"socket_path":           s.cfg.SocketPath,
		"workspace_root":        s.cfg.WorkspaceRoot,
		"restrict_to_workspace": s.cfg.RestrictToWorkspace,
		"sandboxed":             s.cfg.Sandboxed,
	})

	err = s.httpServer.Serve(listener)
	if errors.Is(err, http.ErrServerClosed) {
		return nil
	}
	return err
}

// Stop gracefully shuts down the host capability server.
func (s *Server) Stop(ctx context.Context) error {
	s.mu.Lock()
	defer s.mu.Unlock()

	var shutdownErr error
	if s.httpServer != nil {
		shutdownErr = s.httpServer.Shutdown(ctx)
		s.httpServer = nil
	}
	if s.listener != nil {
		_ = s.listener.Close()
		s.listener = nil
	}
	if s.cfg.SocketPath != "" {
		_ = os.Remove(s.cfg.SocketPath)
	}
	return shutdownErr
}

func (s *Server) handleHealthz(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "method not allowed"})
		return
	}

	writeJSON(w, http.StatusOK, orchestrator.SystemHealthResponse{
		Status:              "ok",
		WorkspaceRoot:       s.cfg.WorkspaceRoot,
		SocketPath:          s.cfg.SocketPath,
		RestrictToWorkspace: s.cfg.RestrictToWorkspace,
		Sandboxed:           s.cfg.Sandboxed,
		TelegramEnabled:     s.cfg.TelegramEnabled,
	})
}

func (s *Server) handleProvisionWorkspace(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "method not allowed"})
		return
	}

	var req orchestrator.WorkspaceProvisionRequest
	if err := decodeJSON(w, r, &req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	if !taskIDPattern.MatchString(req.TaskID) {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "task_id contains unsupported characters"})
		return
	}

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
	worktreePath, err := managedWorktreePathForTask(s.cfg.WorkspaceRoot, req.TaskID)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}

	for _, path := range []string{artifactsDir, logsDir, scratchDir, filepath.Dir(worktreePath)} {
		if err := os.MkdirAll(path, 0o755); err != nil {
			writeJSON(w, http.StatusInternalServerError, map[string]string{"error": fmt.Sprintf("failed to provision workspace: %v", err)})
			return
		}
	}

	writeJSON(w, http.StatusOK, orchestrator.WorkspaceProvisionResponse{
		TaskID:       req.TaskID,
		TaskRoot:     taskRoot,
		ArtifactsDir: artifactsDir,
		LogsDir:      logsDir,
		ScratchDir:   scratchDir,
		WorktreePath: worktreePath,
	})
}

func (s *Server) handleCreateWorktree(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "method not allowed"})
		return
	}

	var req orchestrator.GitWorktreeCreateRequest
	if err := decodeJSON(w, r, &req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	if !taskIDPattern.MatchString(req.TaskID) {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "task_id contains unsupported characters"})
		return
	}
	if strings.TrimSpace(req.Repo.Path) == "" || strings.TrimSpace(req.WorktreePath) == "" || strings.TrimSpace(req.Branch) == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "repo.path, worktree_path, and branch are required"})
		return
	}

	repoPath, err := validatedGitRepositoryPath(s.cfg.WorkspaceRoot, req.Repo.Path)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	worktreePath, err := managedWorktreePathForRequest(s.cfg.WorkspaceRoot, req.TaskID, req.WorktreePath)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	branch := strings.TrimSpace(req.Branch)
	if err := validateGitRefName(branch, "branch", false); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}

	baseRef := strings.TrimSpace(req.BaseRef)
	if baseRef == "" {
		baseRef = req.Repo.DefaultBranch
	}
	if strings.TrimSpace(baseRef) == "" {
		baseRef = "main"
	}
	if err := validateGitRefName(baseRef, "base_ref", true); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}

	if err := os.MkdirAll(filepath.Dir(worktreePath), 0o755); err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": fmt.Sprintf("failed to prepare worktree parent: %v", err)})
		return
	}

	if _, err := os.Stat(filepath.Join(worktreePath, ".git")); err == nil {
		headRef, headErr := gitHeadRef(r.Context(), worktreePath)
		if headErr != nil {
			writeJSON(w, http.StatusBadGateway, map[string]string{"error": fmt.Sprintf("failed to inspect existing worktree: %v", headErr)})
			return
		}
		writeJSON(w, http.StatusOK, orchestrator.GitWorktreeCreateResponse{
			TaskID:       req.TaskID,
			RepoPath:     repoPath,
			WorktreePath: worktreePath,
			Branch:       branch,
			BaseRef:      baseRef,
			HeadRef:      headRef,
			Created:      false,
		})
		return
	}

	output, err := runGit(r.Context(), repoPath, "worktree", "add", "-b", branch, worktreePath, baseRef)
	if err != nil {
		writeJSON(w, http.StatusBadGateway, map[string]string{"error": fmt.Sprintf("failed to create worktree: %v: %s", err, strings.TrimSpace(output))})
		return
	}

	headRef, headErr := gitHeadRef(r.Context(), worktreePath)
	if headErr != nil {
		writeJSON(w, http.StatusBadGateway, map[string]string{"error": fmt.Sprintf("failed to inspect worktree head: %v", headErr)})
		return
	}

	writeJSON(w, http.StatusOK, orchestrator.GitWorktreeCreateResponse{
		TaskID:       req.TaskID,
		RepoPath:     repoPath,
		WorktreePath: worktreePath,
		Branch:       branch,
		BaseRef:      baseRef,
		HeadRef:      headRef,
		Created:      true,
	})
}

func (s *Server) handleRemoveWorktree(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "method not allowed"})
		return
	}

	var req orchestrator.GitWorktreeRemoveRequest
	if err := decodeJSON(w, r, &req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	if !taskIDPattern.MatchString(req.TaskID) {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "task_id contains unsupported characters"})
		return
	}
	if strings.TrimSpace(req.WorktreePath) == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "worktree_path is required"})
		return
	}

	worktreePath, err := managedWorktreePathForRequest(s.cfg.WorkspaceRoot, req.TaskID, req.WorktreePath)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	if _, err := os.Stat(worktreePath); os.IsNotExist(err) {
		writeJSON(w, http.StatusOK, orchestrator.GitWorktreeRemoveResponse{
			TaskID:       req.TaskID,
			WorktreePath: worktreePath,
			Removed:      false,
		})
		return
	}

	args := []string{"worktree", "remove"}
	if req.Force {
		args = append(args, "--force")
	}
	args = append(args, worktreePath)
	output, err := runGit(r.Context(), worktreePath, args...)
	if err != nil {
		writeJSON(w, http.StatusBadGateway, map[string]string{"error": fmt.Sprintf("failed to remove worktree: %v: %s", err, strings.TrimSpace(output))})
		return
	}

	writeJSON(w, http.StatusOK, orchestrator.GitWorktreeRemoveResponse{
		TaskID:       req.TaskID,
		WorktreePath: worktreePath,
		Removed:      true,
	})
}

func (s *Server) handleInspectRepo(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "method not allowed"})
		return
	}

	var req orchestrator.RepoInspectRequest
	if err := decodeJSON(w, r, &req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	if !taskIDPattern.MatchString(req.TaskID) {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "task_id contains unsupported characters"})
		return
	}
	if strings.TrimSpace(req.WorktreePath) == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "worktree_path is required"})
		return
	}

	worktreePath, err := managedWorktreePathForRequest(s.cfg.WorkspaceRoot, req.TaskID, req.WorktreePath)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}

	branch, err := gitBranchName(r.Context(), worktreePath)
	if err != nil {
		writeJSON(w, http.StatusBadGateway, map[string]string{"error": fmt.Sprintf("failed to inspect branch: %v", err)})
		return
	}
	headRef, err := gitCommitRef(r.Context(), worktreePath)
	if err != nil {
		writeJSON(w, http.StatusBadGateway, map[string]string{"error": fmt.Sprintf("failed to inspect head: %v", err)})
		return
	}
	statusLines, err := gitStatusLines(r.Context(), worktreePath)
	if err != nil {
		writeJSON(w, http.StatusBadGateway, map[string]string{"error": fmt.Sprintf("failed to inspect git status: %v", err)})
		return
	}
	changedFiles, additions, deletions, diffShortStat, err := gitChangedFiles(r.Context(), worktreePath, statusLines)
	if err != nil {
		writeJSON(w, http.StatusBadGateway, map[string]string{"error": fmt.Sprintf("failed to inspect git diff summary: %v", err)})
		return
	}
	entries, err := topLevelEntries(worktreePath)
	if err != nil {
		writeJSON(w, http.StatusBadGateway, map[string]string{"error": fmt.Sprintf("failed to inspect repository entries: %v", err)})
		return
	}
	keyFiles, err := inspectKeyFiles(worktreePath)
	if err != nil {
		writeJSON(w, http.StatusBadGateway, map[string]string{"error": fmt.Sprintf("failed to inspect repository files: %v", err)})
		return
	}

	writeJSON(w, http.StatusOK, orchestrator.RepoInspectResponse{
		TaskID:           req.TaskID,
		RepoPath:         req.RepoPath,
		WorktreePath:     worktreePath,
		Branch:           branch,
		HeadRef:          headRef,
		Dirty:            len(statusLines) > 0,
		ChangedFileCount: len(changedFiles),
		Additions:        additions,
		Deletions:        deletions,
		DiffShortStat:    diffShortStat,
		TopLevelEntries:  entries,
		StatusLines:      statusLines,
		ChangedFiles:     changedFiles,
		KeyFiles:         keyFiles,
	})
}

func (s *Server) handleDiscoverTargets(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "method not allowed"})
		return
	}

	var req orchestrator.RepoTargetDiscoveryRequest
	if err := decodeJSON(w, r, &req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	if !taskIDPattern.MatchString(req.TaskID) {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "task_id contains unsupported characters"})
		return
	}

	worktreePath, err := managedWorktreePathForRequest(s.cfg.WorkspaceRoot, req.TaskID, req.WorktreePath)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}

	limit := req.Limit
	if limit <= 0 {
		limit = defaultTargetLimit
	}
	candidates, err := discoverRepoTargets(worktreePath, req.Objective, limit)
	if err != nil {
		writeJSON(w, http.StatusBadGateway, map[string]string{"error": fmt.Sprintf("failed to discover targets: %v", err)})
		return
	}

	writeJSON(w, http.StatusOK, orchestrator.RepoTargetDiscoveryResponse{
		TaskID:       req.TaskID,
		WorktreePath: worktreePath,
		Candidates:   candidates,
	})
}

func (s *Server) handleReadFile(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "method not allowed"})
		return
	}

	var req orchestrator.FileReadRequest
	if err := decodeJSON(w, r, &req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	if !taskIDPattern.MatchString(req.TaskID) {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "task_id contains unsupported characters"})
		return
	}
	if strings.TrimSpace(req.Path) == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "path is required"})
		return
	}

	worktreePath, err := managedWorktreePathForRequest(s.cfg.WorkspaceRoot, req.TaskID, req.WorktreePath)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	fullPath, err := resolveWorktreeFilePath(worktreePath, req.Path)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}

	maxBytes := req.MaxBytes
	if maxBytes <= 0 {
		maxBytes = defaultReadMaxBytes
	}
	content, bytesRead, truncated, err := readBoundedFile(worktreePath, fullPath, maxBytes)
	if err != nil {
		writeJSON(w, http.StatusBadGateway, map[string]string{"error": fmt.Sprintf("failed to read file: %v", err)})
		return
	}

	relativePath, relErr := filepath.Rel(worktreePath, fullPath)
	if relErr != nil {
		relativePath = req.Path
	}
	writeJSON(w, http.StatusOK, orchestrator.FileReadResponse{
		TaskID:    req.TaskID,
		Path:      relativePath,
		FullPath:  fullPath,
		Content:   content,
		BytesRead: bytesRead,
		Truncated: truncated,
	})
}

func (s *Server) handleWriteFile(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "method not allowed"})
		return
	}

	var req orchestrator.FileWriteRequest
	if err := decodeJSON(w, r, &req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	if !taskIDPattern.MatchString(req.TaskID) {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "task_id contains unsupported characters"})
		return
	}
	if strings.TrimSpace(req.Path) == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "path is required"})
		return
	}
	if len(req.Content) > maxWriteBytes {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": fmt.Sprintf("content exceeds %d bytes", maxWriteBytes)})
		return
	}

	worktreePath, err := managedWorktreePathForRequest(s.cfg.WorkspaceRoot, req.TaskID, req.WorktreePath)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	fullPath, err := resolveWorktreeFilePath(worktreePath, req.Path)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	// Verify rollback base before the first edit.
	if err := ensureSnapshot(r.Context(), worktreePath, req.TaskID); err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": fmt.Sprintf("failed to verify rollback base: %v", err)})
		return
	}
	if err := writeFileAtomically(worktreePath, fullPath, []byte(req.Content), 0o644); err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": fmt.Sprintf("failed to write file: %v", err)})
		return
	}

	relativePath, relErr := filepath.Rel(worktreePath, fullPath)
	if relErr != nil {
		relativePath = req.Path
	}
	writeJSON(w, http.StatusOK, orchestrator.FileWriteResponse{
		TaskID:       req.TaskID,
		Path:         relativePath,
		FullPath:     fullPath,
		BytesWritten: len(req.Content),
	})
}

func (s *Server) handleReplaceFile(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "method not allowed"})
		return
	}

	var req orchestrator.FileReplaceRequest
	if err := decodeJSON(w, r, &req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	if !taskIDPattern.MatchString(req.TaskID) {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "task_id contains unsupported characters"})
		return
	}
	if strings.TrimSpace(req.Path) == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "path is required"})
		return
	}
	if req.OldText == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "old_text is required"})
		return
	}
	if len(req.NewText) > maxWriteBytes {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": fmt.Sprintf("new_text exceeds %d bytes", maxWriteBytes)})
		return
	}

	worktreePath, err := managedWorktreePathForRequest(s.cfg.WorkspaceRoot, req.TaskID, req.WorktreePath)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	fullPath, err := resolveWorktreeFilePath(worktreePath, req.Path)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	// Verify rollback base before the first edit.
	if err := ensureSnapshot(r.Context(), worktreePath, req.TaskID); err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": fmt.Sprintf("failed to verify rollback base: %v", err)})
		return
	}
	bytesWritten, err := replaceUniqueText(worktreePath, fullPath, req.OldText, req.NewText)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}

	relativePath, relErr := filepath.Rel(worktreePath, fullPath)
	if relErr != nil {
		relativePath = req.Path
	}
	writeJSON(w, http.StatusOK, orchestrator.FileReplaceResponse{
		TaskID:       req.TaskID,
		Path:         relativePath,
		FullPath:     fullPath,
		BytesWritten: bytesWritten,
	})
}

func (s *Server) handleDiscoverVerification(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "method not allowed"})
		return
	}

	var req orchestrator.VerificationDiscoveryRequest
	if err := decodeJSON(w, r, &req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	if !taskIDPattern.MatchString(req.TaskID) {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "task_id contains unsupported characters"})
		return
	}

	worktreePath, err := managedWorktreePathForRequest(s.cfg.WorkspaceRoot, req.TaskID, req.WorktreePath)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	runtime, candidates, err := discoverVerificationCommands(worktreePath, req.TargetPaths)
	if err != nil {
		writeJSON(w, http.StatusBadGateway, map[string]string{"error": fmt.Sprintf("failed to discover verification commands: %v", err)})
		return
	}

	writeJSON(w, http.StatusOK, orchestrator.VerificationDiscoveryResponse{
		TaskID:       req.TaskID,
		WorktreePath: worktreePath,
		Runtime:      runtime,
		Candidates:   candidates,
	})
}

func (s *Server) handleWriteArtifact(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "method not allowed"})
		return
	}

	var req orchestrator.ArtifactWriteRequest
	if err := decodeJSON(w, r, &req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	if !taskIDPattern.MatchString(req.Artifact.TaskID) {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "artifact.task_id contains unsupported characters"})
		return
	}
	if !artifactIDPattern.MatchString(req.Artifact.ArtifactID) {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "artifact.artifact_id contains unsupported characters"})
		return
	}

	format := normalizedArtifactFormat(req.Format)
	tasksRoot, tasksRootReal, err := managedRootForWorkspace(s.cfg.WorkspaceRoot, "tasks")
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": err.Error()})
		return
	}
	taskRoot, err := resolvePathInsideRoot(tasksRoot, tasksRootReal, req.Artifact.TaskID, "task_id", true)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	artifactsDir := filepath.Join(taskRoot, "artifacts")

	artifactPath := filepath.Join(artifactsDir, req.Artifact.ArtifactID+"."+format)
	if err := writeFileAtomically(artifactsDir, artifactPath, []byte(req.Content), 0o644); err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": fmt.Sprintf("failed to write artifact: %v", err)})
		return
	}

	artifact := req.Artifact
	artifact.Path = artifactPath
	writeJSON(w, http.StatusOK, orchestrator.ArtifactWriteResponse{
		Artifact:     artifact,
		Path:         artifactPath,
		BytesWritten: len(req.Content),
	})
}

func (s *Server) handleReadArtifact(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "method not allowed"})
		return
	}

	var req orchestrator.ArtifactReadRequest
	if err := decodeJSON(w, r, &req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	if !taskIDPattern.MatchString(req.TaskID) {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "task_id contains unsupported characters"})
		return
	}
	if strings.TrimSpace(req.Path) == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "path is required"})
		return
	}

	tasksRoot, tasksRootReal, err := managedRootForWorkspace(s.cfg.WorkspaceRoot, "tasks")
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": err.Error()})
		return
	}
	taskRoot, err := resolvePathInsideRoot(tasksRoot, tasksRootReal, req.TaskID, "task_id", false)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	artifactsRoot := filepath.Join(taskRoot, "artifacts")
	artifactPath, err := resolveArtifactPath(artifactsRoot, req.Path)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}

	maxBytes := req.MaxBytes
	if maxBytes <= 0 {
		maxBytes = defaultArtifactReadMaxBytes
	}
	if maxBytes > maxArtifactReadBytes {
		maxBytes = maxArtifactReadBytes
	}

	content, bytesRead, truncated, err := readBoundedFile(artifactsRoot, artifactPath, maxBytes)
	if err != nil {
		writeJSON(w, http.StatusBadGateway, map[string]string{"error": fmt.Sprintf("failed to read artifact: %v", err)})
		return
	}

	writeJSON(w, http.StatusOK, orchestrator.ArtifactReadResponse{
		TaskID:    req.TaskID,
		Path:      artifactPath,
		Content:   content,
		BytesRead: bytesRead,
		Truncated: truncated,
	})
}

func (s *Server) handleExec(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "method not allowed"})
		return
	}

	var req orchestrator.HostActionRequest
	if err := decodeJSON(w, r, &req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	if req.ActionName != "exec" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "action_name must be exec"})
		return
	}

	commandValue, ok := req.Arguments["command"]
	command, commandOK := commandValue.(string)
	if !ok || !commandOK || strings.TrimSpace(command) == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "arguments.command is required"})
		return
	}

	args := map[string]interface{}{
		"command": command,
	}
	if req.WorkingDir != "" {
		args["working_dir"] = req.WorkingDir
	}

	execStart := time.Now().UTC()
	result := s.execTool.Execute(r.Context(), tools.ToolContext{}, args)
	execDuration := time.Since(execStart)

	state := map[string]interface{}{}
	if req.WorkingDir != "" {
		state["working_dir"] = req.WorkingDir
	}

	// Shape the observation with SWE-agent-style templates that guide
	// the model toward better behaviour on empty/truncated output.
	shaped := shapeObservation(result.ForLLM, result.IsError)
	if req.WorkingDir != "" {
		shaped = shaped + fmt.Sprintf("\n[working_dir: %s]", req.WorkingDir)
	}

	obs := orchestrator.HostObservation{
		TaskID:     req.TaskID,
		ActionName: req.ActionName,
		Success:    !result.IsError,
		Summary:    summarizeObservation(result.ForLLM, result.IsError),
		Output:     shaped,
		State:      state,
	}
	// Populate exit code when available from the exec tool.
	if result.ExitCode != nil {
		exitCode := int(*result.ExitCode)
		obs.ExitCode = &exitCode
	}

	// Emit host_action telemetry event.
	if s.cfg.TelemetryStore != nil {
		actionAttrs := map[string]interface{}{
			"action_name": req.ActionName,
			"duration_ms": execDuration.Milliseconds(),
			"success":     !result.IsError,
		}
		if result.ExitCode != nil {
			actionAttrs["exit_code"] = int(*result.ExitCode)
		}
		if len(result.ForLLM) > 0 {
			actionAttrs["bytes_transferred"] = len(result.ForLLM)
		}
		actionEvent := telemetry.TelemetryEvent{
			EventType:  telemetry.EventHostAction,
			TaskID:     req.TaskID,
			Timestamp:  time.Now().UTC(),
			Attributes: actionAttrs,
		}
		_ = s.cfg.TelemetryStore.Emit(r.Context(), actionEvent)
	}

	writeJSON(w, http.StatusOK, obs)
}

func (s *Server) handleNotifyTelegram(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "method not allowed"})
		return
	}
	if s.notifier == nil {
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{"error": "telegram notifier is not available"})
		return
	}

	var req orchestrator.ChannelNotificationRequest
	if err := decodeJSON(w, r, &req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	if strings.TrimSpace(req.ChatID) == "" || strings.TrimSpace(req.Content) == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "chat_id and content are required"})
		return
	}

	channel := req.Channel
	if strings.TrimSpace(channel) == "" {
		channel = "telegram"
	}
	if channel != "telegram" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "only telegram notifications are supported by this endpoint"})
		return
	}

	if err := s.notifier.SendToChannel(r.Context(), channel, req.ChatID, req.Content); err != nil {
		writeJSON(w, http.StatusBadGateway, map[string]string{"error": fmt.Sprintf("failed to deliver telegram message: %v", err)})
		return
	}

	writeJSON(w, http.StatusOK, orchestrator.ChannelNotificationResponse{
		Delivered: true,
		Summary:   "telegram notification delivered",
	})
}

// handleRollbackWorktree reverts a managed task worktree to its current HEAD.
// Task worktrees are created per task, so rollback must remove both tracked
// edits and new untracked files instead of relying on git stash snapshots.
func (s *Server) handleRollbackWorktree(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "method not allowed"})
		return
	}

	var req orchestrator.GitRollbackRequest
	if err := decodeJSON(w, r, &req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	if !taskIDPattern.MatchString(req.TaskID) {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "task_id contains unsupported characters"})
		return
	}
	if strings.TrimSpace(req.WorktreePath) == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "worktree_path is required"})
		return
	}

	worktreePath, err := managedWorktreePathForRequest(s.cfg.WorkspaceRoot, req.TaskID, req.WorktreePath)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}

	if _, err := runGit(r.Context(), worktreePath, "reset", "--hard", "HEAD"); err != nil {
		writeJSON(w, http.StatusBadGateway, map[string]string{"error": fmt.Sprintf("failed to reset worktree: %v", err)})
		return
	}
	if _, err := runGit(r.Context(), worktreePath, "clean", "-fd"); err != nil {
		writeJSON(w, http.StatusBadGateway, map[string]string{"error": fmt.Sprintf("failed to clean worktree: %v", err)})
		return
	}

	headRef, _ := gitHeadRef(r.Context(), worktreePath)
	writeJSON(w, http.StatusOK, orchestrator.GitRollbackResponse{
		TaskID:       req.TaskID,
		WorktreePath: worktreePath,
		RolledBack:   true,
		HeadRef:      headRef,
	})
}

// handleBrowserTest is intentionally disabled until Vikram has a constrained
// browser QA runner. Executing model-generated Node/Playwright scripts on the
// host is not acceptable for the native Mac daemon threat model.
func (s *Server) handleBrowserTest(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusNotImplemented, map[string]string{
		"error": "browser QA requires a constrained runner and is not enabled in this host build",
	})
}

// ensureSnapshot verifies the managed worktree has a HEAD that rollback can
// reset to. A clean task worktree has no stashable changes before the first edit,
// so git stash is the wrong rollback primitive here.
func ensureSnapshot(ctx context.Context, worktreePath, taskID string) error {
	if _, err := runGit(ctx, worktreePath, "rev-parse", "--verify", "HEAD"); err != nil {
		return fmt.Errorf("failed to verify rollback base for task %s: %w", taskID, err)
	}
	return nil
}

// discoverLintCommands returns lint command candidates for the detected runtime.
func discoverLintCommands(worktreePath string, runtime string) []orchestrator.LintCommandCandidate {
	var candidates []orchestrator.LintCommandCandidate

	add := func(command, reason string) {
		command = strings.TrimSpace(command)
		if command == "" {
			return
		}
		candidates = append(candidates, orchestrator.LintCommandCandidate{
			Command:    command,
			WorkingDir: worktreePath,
			Runtime:    runtime,
			Reason:     reason,
		})
	}

	switch runtime {
	case "go":
		add("go vet ./...", "Go static analysis")
	case "python":
		if fileExistsInRoot(worktreePath, "pyproject.toml") {
			add("ruff check .", "Python linter (ruff via pyproject.toml)")
		} else {
			add("ruff check .", "Python linter (ruff)")
		}
		add("flake8 .", "Python style checker (flake8)")
	case "node":
		add("eslint .", "JavaScript/TypeScript linter (eslint)")
	case "rust":
		add("cargo clippy -- -D warnings", "Rust linter (clippy)")
	default:
		// No language-specific lint tool; check for Makefile lint target
		if fileExistsInRoot(worktreePath, "Makefile") {
			add("make lint", "Makefile lint target")
		}
	}

	return candidates
}

// handleDiscoverLint responds with available lint commands for a worktree.
func (s *Server) handleDiscoverLint(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "method not allowed"})
		return
	}
	var req orchestrator.LintDiscoveryRequest
	if err := decodeJSON(w, r, &req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	if !taskIDPattern.MatchString(req.TaskID) {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "task_id contains unsupported characters"})
		return
	}

	worktreePath, err := managedWorktreePathForRequest(s.cfg.WorkspaceRoot, req.TaskID, req.WorktreePath)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}

	runtime := detectRepoRuntime(worktreePath)
	candidates := discoverLintCommands(worktreePath, runtime)

	writeJSON(w, http.StatusOK, orchestrator.LintDiscoveryResponse{
		TaskID:       req.TaskID,
		WorktreePath: worktreePath,
		Runtime:      runtime,
		Candidates:   candidates,
	})
}

// handleRunLint executes a lint command and returns structured results.
// When a baseline is provided, only new errors (lines not in baseline) are returned.
func (s *Server) handleRunLint(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "method not allowed"})
		return
	}
	var req orchestrator.LintRunRequest
	if err := decodeJSON(w, r, &req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	if !taskIDPattern.MatchString(req.TaskID) {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "task_id contains unsupported characters"})
		return
	}
	if strings.TrimSpace(req.Command) == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "command is required"})
		return
	}

	worktreePath, err := managedWorktreePathForRequest(s.cfg.WorkspaceRoot, req.TaskID, req.WorktreePath)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}

	args := map[string]interface{}{
		"command":     req.Command,
		"working_dir": worktreePath,
	}
	result := s.execTool.Execute(r.Context(), tools.ToolContext{}, args)

	var exitCode int
	if result.ExitCode != nil {
		exitCode = int(*result.ExitCode)
	} else if result.IsError {
		exitCode = 1
	}

	newErrors := diffLintOutput(req.Baseline, result.ForLLM)

	writeJSON(w, http.StatusOK, orchestrator.LintRunResponse{
		TaskID:    req.TaskID,
		Command:   req.Command,
		Success:   !result.IsError,
		ExitCode:  exitCode,
		Output:    result.ForLLM,
		NewErrors: newErrors,
	})
}

// diffLintOutput compares current lint output against a baseline and returns
// only lines that are new (not present in the baseline).
func diffLintOutput(baseline, current string) []string {
	if baseline == "" {
		return nil // no baseline: caller gets full output, not diffed
	}
	baselineLines := strings.Split(baseline, "\n")
	baselineSet := make(map[string]struct{}, len(baselineLines))
	for _, line := range baselineLines {
		line = strings.TrimSpace(line)
		if line != "" {
			baselineSet[line] = struct{}{}
		}
	}
	var newErrors []string
	for _, line := range strings.Split(current, "\n") {
		line = strings.TrimSpace(line)
		if line != "" {
			if _, exists := baselineSet[line]; !exists {
				newErrors = append(newErrors, line)
			}
		}
	}
	return newErrors
}

// handleAgentRoster returns non-secret team routing metadata for the Python
// orchestrator. Secrets stay in Go provider config; Python only sees IDs,
// roles, provider names, models, and coarse capabilities.
func (s *Server) handleAgentRoster(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "method not allowed"})
		return
	}
	agents := append([]orchestrator.AgentProfile(nil), s.cfg.AgentRoster...)
	writeJSON(w, http.StatusOK, orchestrator.AgentRosterResponse{Agents: agents})
}

// handleAgentThink executes a reasoning request selected by the Python orchestrator.
func (s *Server) handleAgentThink(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "method not allowed"})
		return
	}
	if s.cfg.AgentThink == nil {
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{"error": "agent think not configured"})
		return
	}

	var req orchestrator.AgentThinkRequest
	if err := decodeJSON(w, r, &req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	if !taskIDPattern.MatchString(req.TaskID) {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "task_id contains unsupported characters"})
		return
	}
	if strings.TrimSpace(req.Role) == "" || strings.TrimSpace(req.Prompt) == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "role and prompt are required"})
		return
	}

	// Emit agent_call_start telemetry event.
	startTime := time.Now().UTC()
	if s.cfg.TelemetryStore != nil {
		startEvent := telemetry.TelemetryEvent{
			EventType: telemetry.EventAgentCallStart,
			TaskID:    req.TaskID,
			Timestamp: startTime,
			Attributes: map[string]interface{}{
				"role":     req.Role,
				"model":    req.Model,
				"provider": req.ProviderName,
			},
		}
		_ = s.cfg.TelemetryStore.Emit(r.Context(), startEvent)
	}

	resp, err := s.cfg.AgentThink(r.Context(), req)

	// Emit agent_call_end telemetry event.
	if s.cfg.TelemetryStore != nil {
		endTime := time.Now().UTC()
		latencyMS := endTime.Sub(startTime).Milliseconds()
		endAttrs := map[string]interface{}{
			"role":       req.Role,
			"model":      req.Model,
			"provider":   req.ProviderName,
			"latency_ms": latencyMS,
			"success":    err == nil,
		}
		if err != nil {
			endAttrs["error"] = err.Error()
		}
		endEvent := telemetry.TelemetryEvent{
			EventType:  telemetry.EventAgentCallEnd,
			TaskID:     req.TaskID,
			Timestamp:  endTime,
			Attributes: endAttrs,
		}
		_ = s.cfg.TelemetryStore.Emit(r.Context(), endEvent)
	}

	if err != nil {
		writeJSON(w, http.StatusBadGateway, map[string]string{"error": fmt.Sprintf("agent think failed: %v", err)})
		return
	}

	// Record cost via the ledger after each successful provider call.
	if s.ledger != nil {
		durationMS := time.Since(startTime).Milliseconds()
		rec := costledger.CostRecord{
			RecordID:     fmt.Sprintf("%s-%s-%d", req.TaskID, req.Role, startTime.UnixNano()),
			TaskID:       req.TaskID,
			Role:         req.Role,
			Model:        req.Model,
			Provider:     req.ProviderName,
			InputTokens:  len(req.Prompt) / 4, // approximate token count
			OutputTokens: len(resp.Content) / 4,
			CostUSD:      0, // actual cost computed by pricing table or set by provider
			Estimated:    true,
			DurationMS:   durationMS,
			InvocationID: fmt.Sprintf("%s-%s-%d", req.TaskID, req.Role, startTime.UnixNano()),
			Timestamp:    startTime,
		}
		// Best-effort: do not fail the think response on ledger errors.
		_ = s.ledger.Record(r.Context(), rec)
	}

	writeJSON(w, http.StatusOK, resp)
}

// handleReviewChange asks the reviewer agent (a different model) to evaluate
// a set of code changes.  The review function is injected via Config so the
// gateway can wire the reviewer provider from the multi-agent registry.
func (s *Server) handleReviewChange(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "method not allowed"})
		return
	}
	if s.cfg.ReviewChange == nil {
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{"error": "review function not configured"})
		return
	}

	var req orchestrator.ChangeReviewRequest
	if err := decodeJSON(w, r, &req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	if !taskIDPattern.MatchString(req.TaskID) {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "task_id contains unsupported characters"})
		return
	}
	if strings.TrimSpace(req.Objective) == "" || strings.TrimSpace(req.Diff) == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "objective and diff are required"})
		return
	}

	resp, err := s.cfg.ReviewChange(r.Context(), req)
	if err != nil {
		writeJSON(w, http.StatusBadGateway, map[string]string{"error": fmt.Sprintf("review failed: %v", err)})
		return
	}
	writeJSON(w, http.StatusOK, resp)
}

func removeStaleSocket(path string) error {
	info, err := os.Lstat(path)
	if err != nil {
		if os.IsNotExist(err) {
			return nil
		}
		return err
	}
	if info.Mode()&os.ModeSocket == 0 {
		return fmt.Errorf("refusing to remove non-socket path %s", path)
	}
	return os.Remove(path)
}

func decodeJSON(w http.ResponseWriter, r *http.Request, target interface{}) error {
	r.Body = http.MaxBytesReader(w, r.Body, maxInboundBodyBytes)
	defer r.Body.Close()
	if err := json.NewDecoder(r.Body).Decode(target); err != nil {
		return fmt.Errorf("invalid request body")
	}
	return nil
}

func writeJSON(w http.ResponseWriter, status int, payload interface{}) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(payload)
}

const obsMaxChars = 8000

// shapeObservation produces SWE-agent-style observation messages that guide
// the model toward better behaviour instead of just dumping raw output.
// Three templates: empty output, truncated output, normal output.
func shapeObservation(output string, isError bool) string {
	trimmed := strings.TrimSpace(output)
	if trimmed == "" {
		if isError {
			return "Your command failed and produced no output. Check the command syntax and try again."
		}
		return "Your command ran successfully and did not produce any output."
	}
	if len(trimmed) > obsMaxChars {
		elided := len(trimmed) - obsMaxChars
		preview := trimmed[:obsMaxChars]
		return fmt.Sprintf("Observation:\n%s\n<response clipped>\n<NOTE>%d more characters were elided. The output exceeded %d characters. To see the full output, redirect to a file or use head/tail/grep to narrow the result.</NOTE>",
			preview, elided, obsMaxChars)
	}
	return "Observation:\n" + trimmed
}

func summarizeObservation(output string, isError bool) string {
	trimmed := strings.TrimSpace(output)
	if trimmed == "" {
		if isError {
			return "command failed with no output"
		}
		return "command completed with no output"
	}
	lines := strings.Split(trimmed, "\n")
	summary := strings.TrimSpace(lines[0])
	if len(summary) > 160 {
		summary = summary[:160] + "..."
	}
	return summary
}

func isWithinRoot(candidate, root string) bool {
	absCandidate, err := filepath.Abs(candidate)
	if err != nil {
		return false
	}
	absRoot, err := filepath.Abs(root)
	if err != nil {
		return false
	}
	rel, err := filepath.Rel(absRoot, absCandidate)
	if err != nil {
		return false
	}
	return rel == "." || (rel != ".." && !strings.HasPrefix(rel, ".."+string(os.PathSeparator)))
}

func managedRootForWorkspace(workspaceRoot, child string) (string, string, error) {
	if strings.TrimSpace(workspaceRoot) == "" {
		return "", "", fmt.Errorf("workspace root is required")
	}
	if strings.Contains(workspaceRoot, "\x00") || strings.Contains(child, "\x00") {
		return "", "", fmt.Errorf("path contains unsupported characters")
	}
	absWorkspace, err := filepath.Abs(workspaceRoot)
	if err != nil {
		return "", "", fmt.Errorf("invalid workspace root: %w", err)
	}
	workspaceReal, err := filepath.EvalSymlinks(absWorkspace)
	if err != nil {
		return "", "", fmt.Errorf("failed to resolve workspace root: %w", err)
	}
	rootAbs := filepath.Join(absWorkspace, child)
	rootReal := filepath.Join(workspaceReal, child)
	if resolved, err := filepath.EvalSymlinks(rootAbs); err == nil {
		rootReal = resolved
	} else if !os.IsNotExist(err) {
		return "", "", fmt.Errorf("failed to resolve managed root: %w", err)
	}
	if !isWithinRoot(rootReal, workspaceReal) {
		return "", "", fmt.Errorf("managed root must remain inside workspace root")
	}
	return rootAbs, rootReal, nil
}

func resolvePathInsideRoot(rootAbs, rootReal, candidate, label string, allowMissing bool) (string, error) {
	candidate = strings.TrimSpace(candidate)
	if candidate == "" {
		return "", fmt.Errorf("%s is required", label)
	}
	if strings.Contains(candidate, "\x00") {
		return "", fmt.Errorf("%s contains unsupported characters", label)
	}
	var fullPath string
	if filepath.IsAbs(candidate) {
		fullPath = filepath.Clean(candidate)
	} else {
		fullPath = filepath.Join(rootAbs, candidate)
	}
	absPath, err := filepath.Abs(fullPath)
	if err != nil {
		return "", fmt.Errorf("invalid %s", label)
	}

	pathReal, err := resolvePathForRootContainment(absPath, allowMissing)
	if err != nil {
		return "", fmt.Errorf("failed to resolve %s: %w", label, err)
	}
	if !isWithinRoot(pathReal, rootReal) {
		return "", fmt.Errorf("%s must remain inside the managed root", label)
	}
	return absPath, nil
}

func resolvePathForRootContainment(absPath string, allowMissing bool) (string, error) {
	if resolved, err := filepath.EvalSymlinks(absPath); err == nil {
		return resolved, nil
	} else if !os.IsNotExist(err) || !allowMissing {
		return "", err
	}

	absDir := filepath.Dir(absPath)
	suffix := filepath.Base(absPath)
	for {
		if resolved, err := filepath.EvalSymlinks(absDir); err == nil {
			return filepath.Join(resolved, suffix), nil
		} else if !os.IsNotExist(err) {
			return "", err
		}
		parent := filepath.Dir(absDir)
		if parent == absDir {
			return "", os.ErrNotExist
		}
		suffix = filepath.Join(filepath.Base(absDir), suffix)
		absDir = parent
	}
}

func validatedGitRepositoryPath(workspaceRoot, candidate string) (string, error) {
	candidate = strings.TrimSpace(candidate)
	if candidate == "" {
		return "", fmt.Errorf("repo.path is required")
	}
	if strings.Contains(candidate, "\x00") {
		return "", fmt.Errorf("repo.path contains unsupported characters")
	}
	absPath, err := filepath.Abs(candidate)
	if err != nil {
		return "", fmt.Errorf("invalid repo.path")
	}
	realPath, err := filepath.EvalSymlinks(absPath)
	if err != nil {
		return "", fmt.Errorf("repo.path must be an existing local git worktree: %w", err)
	}
	workspaceAbs, err := filepath.Abs(workspaceRoot)
	if err != nil {
		return "", fmt.Errorf("invalid workspace root: %w", err)
	}
	workspaceReal, err := filepath.EvalSymlinks(workspaceAbs)
	if err != nil {
		return "", fmt.Errorf("failed to resolve workspace root: %w", err)
	}
	if !isWithinRoot(realPath, workspaceReal) {
		return "", fmt.Errorf("repo.path must remain inside the Vikram workspace root")
	}
	info, err := os.Stat(realPath)
	if err != nil {
		return "", fmt.Errorf("repo.path must be an existing local git worktree: %w", err)
	}
	if !info.IsDir() {
		return "", fmt.Errorf("repo.path must be a directory")
	}
	if !hasGitMetadata(realPath) {
		return "", fmt.Errorf("repo.path must be an existing local git worktree")
	}
	return realPath, nil
}

func hasGitMetadata(repoPath string) bool {
	info, err := os.Stat(filepath.Join(repoPath, ".git"))
	return err == nil && (info.IsDir() || info.Mode().IsRegular())
}

func validateGitRefName(ref, field string, allowHEAD bool) error {
	ref = strings.TrimSpace(ref)
	if ref == "" {
		return fmt.Errorf("%s is required", field)
	}
	if strings.EqualFold(ref, "HEAD") {
		if allowHEAD {
			return nil
		}
		return fmt.Errorf("%s must not be HEAD", field)
	}
	if strings.HasPrefix(ref, "-") || strings.HasPrefix(ref, "/") || strings.HasSuffix(ref, "/") || strings.HasSuffix(ref, ".") {
		return fmt.Errorf("%s is not a safe git ref", field)
	}
	if strings.Contains(ref, "..") || strings.Contains(ref, "//") || strings.Contains(ref, "@{") {
		return fmt.Errorf("%s is not a safe git ref", field)
	}
	for _, r := range ref {
		if unicode.IsControl(r) || unicode.IsSpace(r) || strings.ContainsRune(`~^:?*[\`, r) {
			return fmt.Errorf("%s is not a safe git ref", field)
		}
	}
	for _, part := range strings.Split(ref, "/") {
		if part == "" || part == "." || part == ".." || strings.HasPrefix(part, ".") || strings.HasSuffix(part, ".lock") {
			return fmt.Errorf("%s is not a safe git ref", field)
		}
	}
	return nil
}

func runGit(ctx context.Context, repoPath string, args ...string) (string, error) {
	safeRepoPath, err := filepath.EvalSymlinks(repoPath)
	if err != nil {
		return "", err
	}
	if !gitSafeArgPattern.MatchString(safeRepoPath) {
		return "", fmt.Errorf("git repo path contains unsupported characters")
	}
	for _, arg := range args {
		if strings.Contains(arg, "\x00") {
			return "", fmt.Errorf("git argument contains unsupported characters")
		}
		if !gitSafeArgPattern.MatchString(arg) {
			return "", fmt.Errorf("git argument contains unsupported characters")
		}
	}
	commandArgs := append([]string{"-C", safeRepoPath}, args...)
	cmd := exec.CommandContext(ctx, "git", commandArgs...)
	output, err := cmd.CombinedOutput()
	return string(output), err
}

func gitHeadRef(ctx context.Context, worktreePath string) (string, error) {
	output, err := runGit(ctx, worktreePath, "rev-parse", "--abbrev-ref", "HEAD")
	if err != nil {
		return "", err
	}
	return strings.TrimSpace(output), nil
}

func gitBranchName(ctx context.Context, worktreePath string) (string, error) {
	return gitHeadRef(ctx, worktreePath)
}

func gitCommitRef(ctx context.Context, worktreePath string) (string, error) {
	output, err := runGit(ctx, worktreePath, "rev-parse", "HEAD")
	if err != nil {
		return "", err
	}
	return strings.TrimSpace(output), nil
}

func gitStatusLines(ctx context.Context, worktreePath string) ([]string, error) {
	output, err := runGit(ctx, worktreePath, "status", "--short")
	if err != nil {
		return nil, err
	}
	trimmed := strings.TrimSpace(output)
	if trimmed == "" {
		return nil, nil
	}
	lines := strings.Split(trimmed, "\n")
	if len(lines) > 20 {
		lines = append(lines[:20], fmt.Sprintf("... %d more", len(lines)-20))
	}
	return lines, nil
}

func gitChangedFiles(ctx context.Context, worktreePath string, statusLines []string) ([]orchestrator.RepoChangedFile, int, int, string, error) {
	statusByPath := parseGitStatusByPath(statusLines)
	output, err := runGit(ctx, worktreePath, "diff", "--numstat", "HEAD", "--")
	if err != nil {
		return nil, 0, 0, "", err
	}
	shortStatOutput, err := runGit(ctx, worktreePath, "diff", "--shortstat", "HEAD", "--")
	if err != nil {
		return nil, 0, 0, "", err
	}

	changed := make([]orchestrator.RepoChangedFile, 0)
	seen := map[string]struct{}{}
	totalAdditions := 0
	totalDeletions := 0

	for _, line := range strings.Split(strings.TrimSpace(output), "\n") {
		line = strings.TrimSpace(line)
		if line == "" {
			continue
		}
		parts := strings.Split(line, "\t")
		if len(parts) < 3 {
			continue
		}
		path := normalizeGitPath(parts[len(parts)-1])
		if path == "" {
			continue
		}
		file := orchestrator.RepoChangedFile{
			Path:   path,
			Status: statusByPath[path],
		}
		if file.Status == "" {
			file.Status = "M"
		}
		if parts[0] == "-" || parts[1] == "-" {
			file.Binary = true
		} else {
			file.Additions, _ = strconv.Atoi(parts[0])
			file.Deletions, _ = strconv.Atoi(parts[1])
			totalAdditions += file.Additions
			totalDeletions += file.Deletions
		}
		changed = append(changed, file)
		seen[path] = struct{}{}
	}

	for _, line := range statusLines {
		path, status := parseGitStatusLine(line)
		if path == "" {
			continue
		}
		if _, exists := seen[path]; exists {
			continue
		}
		changed = append(changed, orchestrator.RepoChangedFile{
			Path:   path,
			Status: status,
		})
	}

	sort.SliceStable(changed, func(i, j int) bool {
		return changed[i].Path < changed[j].Path
	})
	if len(changed) > 50 {
		changed = changed[:50]
	}
	return changed, totalAdditions, totalDeletions, strings.TrimSpace(shortStatOutput), nil
}

func parseGitStatusByPath(lines []string) map[string]string {
	result := map[string]string{}
	for _, line := range lines {
		path, status := parseGitStatusLine(line)
		if path != "" {
			result[path] = status
		}
	}
	return result
}

func parseGitStatusLine(line string) (string, string) {
	if len(line) < 4 {
		return "", ""
	}
	status := strings.TrimSpace(line[:2])
	path := strings.TrimSpace(line[3:])
	if path == "" {
		return "", ""
	}
	if strings.Contains(path, " -> ") {
		parts := strings.Split(path, " -> ")
		path = strings.TrimSpace(parts[len(parts)-1])
	}
	return normalizeGitPath(path), status
}

func normalizeGitPath(path string) string {
	path = strings.TrimSpace(path)
	path = strings.Trim(path, `"`)
	return filepath.ToSlash(path)
}

func topLevelEntries(worktreePath string) ([]string, error) {
	worktreeReal, err := filepath.EvalSymlinks(worktreePath)
	if err != nil {
		return nil, err
	}
	entries, err := os.ReadDir(worktreeReal)
	if err != nil {
		return nil, err
	}
	names := make([]string, 0, len(entries))
	for _, entry := range entries {
		name := entry.Name()
		if name == ".git" {
			continue
		}
		if entry.IsDir() {
			name += "/"
		}
		names = append(names, name)
	}
	sort.Strings(names)
	if len(names) > 20 {
		names = append(names[:20], fmt.Sprintf("... %d more", len(names)-20))
	}
	return names, nil
}

func inspectKeyFiles(worktreePath string) ([]orchestrator.RepoFileSummary, error) {
	candidates := []string{
		"README.md",
		"README",
		"Makefile",
		"go.mod",
		"package.json",
		"pyproject.toml",
		"requirements.txt",
		"Cargo.toml",
		"docker-compose.yml",
		"docker-compose.yaml",
	}
	keyFiles := make([]orchestrator.RepoFileSummary, 0, len(candidates))
	for _, relativePath := range candidates {
		fullPath, err := resolveWorktreeFilePath(worktreePath, relativePath)
		if err != nil {
			return nil, err
		}
		if fullPath != worktreePath && !strings.HasPrefix(fullPath, worktreePath+string(os.PathSeparator)) {
			return nil, fmt.Errorf("path must remain inside the managed worktree")
		}
		info, err := os.Lstat(fullPath)
		if err != nil {
			if os.IsNotExist(err) {
				continue
			}
			return nil, err
		}
		if info.IsDir() || info.Mode()&os.ModeSymlink != 0 {
			continue
		}
		data, _, truncated, err := readBoundedFile(worktreePath, fullPath, maxRepoPreviewBytes+1)
		if err != nil {
			return nil, err
		}
		preview := string(data)
		if truncated || len(preview) > maxRepoPreviewBytes {
			preview = preview[:maxRepoPreviewBytes] + "\n... (truncated)"
		}
		keyFiles = append(keyFiles, orchestrator.RepoFileSummary{
			Path:    relativePath,
			Preview: preview,
			Bytes:   len(data),
		})
	}
	return keyFiles, nil
}

func normalizedArtifactFormat(format string) string {
	switch strings.ToLower(strings.TrimSpace(format)) {
	case "", "md", "markdown":
		return "md"
	case "json":
		return "json"
	case "txt", "text":
		return "txt"
	default:
		return "txt"
	}
}

func validatedManagedWorktreePath(workspaceRoot, candidate string) (string, error) {
	worktreeRoot, worktreeRootReal, err := managedRootForWorkspace(workspaceRoot, "worktrees")
	if err != nil {
		return "", err
	}
	worktreePath, err := resolvePathInsideRoot(worktreeRoot, worktreeRootReal, candidate, "worktree_path", true)
	if err != nil {
		return "", err
	}
	return worktreePath, nil
}

func managedWorktreePathForTask(workspaceRoot, taskID string) (string, error) {
	if !taskIDPattern.MatchString(taskID) || !filepath.IsLocal(taskID) {
		return "", fmt.Errorf("task_id contains unsupported characters")
	}
	worktreeRoot, _, err := managedRootForWorkspace(workspaceRoot, "worktrees")
	if err != nil {
		return "", err
	}
	return filepath.Join(worktreeRoot, taskID), nil
}

func managedWorktreePathForRequest(workspaceRoot, taskID, provided string) (string, error) {
	expected, err := managedWorktreePathForTask(workspaceRoot, taskID)
	if err != nil {
		return "", err
	}
	provided = strings.TrimSpace(provided)
	if provided == "" {
		return expected, nil
	}
	absProvided, err := filepath.Abs(provided)
	if err != nil {
		return "", fmt.Errorf("invalid worktree_path")
	}
	if filepath.Clean(absProvided) != filepath.Clean(expected) {
		return "", fmt.Errorf("worktree_path must match the managed path for task_id")
	}
	return expected, nil
}

func resolveWorktreeFilePath(worktreePath, candidate string) (string, error) {
	candidate = strings.TrimSpace(candidate)
	if filepath.IsAbs(candidate) || !filepath.IsLocal(candidate) {
		return "", fmt.Errorf("path must remain inside the managed worktree")
	}
	worktreeAbs, err := filepath.Abs(worktreePath)
	if err != nil {
		return "", err
	}
	worktreeReal, err := filepath.EvalSymlinks(worktreeAbs)
	if err != nil {
		return "", fmt.Errorf("failed to resolve worktree_path: %w", err)
	}
	absPath, err := resolvePathInsideRoot(worktreeAbs, worktreeReal, candidate, "path", true)
	if err != nil {
		return "", fmt.Errorf("path must remain inside the managed worktree: %w", err)
	}
	rel, relErr := filepath.Rel(worktreeAbs, absPath)
	if relErr == nil {
		parts := strings.Split(filepath.ToSlash(rel), "/")
		for _, part := range parts {
			if part == ".git" {
				return "", fmt.Errorf("path must not target .git internals")
			}
		}
	}
	return absPath, nil
}

func resolveArtifactPath(artifactsRoot, candidate string) (string, error) {
	candidate = filepath.Base(strings.TrimSpace(candidate))
	if candidate == "." || candidate == "" || !filepath.IsLocal(candidate) {
		return "", fmt.Errorf("path must remain inside the managed artifact root")
	}
	artifactsAbs, err := filepath.Abs(artifactsRoot)
	if err != nil {
		return "", err
	}
	artifactsReal, err := filepath.EvalSymlinks(artifactsAbs)
	if err != nil {
		if os.IsNotExist(err) {
			return "", fmt.Errorf("artifact not found")
		}
		return "", err
	}
	absPath, err := resolvePathInsideRoot(artifactsAbs, artifactsReal, candidate, "path", false)
	if err != nil {
		return "", fmt.Errorf("path must remain inside the managed artifact root: %w", err)
	}
	artifactsAbs = filepath.Clean(artifactsAbs)
	absPath = filepath.Clean(absPath)
	if absPath != artifactsAbs && !strings.HasPrefix(absPath, artifactsAbs+string(os.PathSeparator)) {
		return "", fmt.Errorf("path must remain inside the managed artifact root")
	}
	info, err := os.Lstat(absPath)
	if err != nil {
		if os.IsNotExist(err) {
			return "", fmt.Errorf("artifact not found")
		}
		return "", err
	}
	if info.IsDir() || info.Mode()&os.ModeSymlink != 0 {
		return "", fmt.Errorf("path must be an artifact file")
	}
	return absPath, nil
}

func readBoundedFile(root, fullPath string, maxBytes int) (string, int, bool, error) {
	if maxBytes <= 0 {
		return "", 0, false, fmt.Errorf("maxBytes must be positive")
	}
	root = filepath.Clean(root)
	fullPath = filepath.Clean(fullPath)
	if fullPath != root && !strings.HasPrefix(fullPath, root+string(os.PathSeparator)) {
		return "", 0, false, fmt.Errorf("path must remain inside the managed root")
	}
	f, err := os.Open(fullPath)
	if err != nil {
		return "", 0, false, err
	}
	defer f.Close()
	info, err := f.Stat()
	if err != nil {
		return "", 0, false, err
	}
	if info.IsDir() {
		return "", 0, false, fmt.Errorf("path must be a file")
	}
	linkInfo, err := os.Lstat(fullPath)
	if err != nil {
		return "", 0, false, err
	}
	if linkInfo.Mode()&os.ModeSymlink != 0 || !os.SameFile(info, linkInfo) {
		return "", 0, false, fmt.Errorf("access denied: symlink race detected")
	}
	data, err := io.ReadAll(io.LimitReader(f, int64(maxBytes)+1))
	if err != nil {
		return "", 0, false, err
	}
	truncated := false
	if len(data) > maxBytes {
		data = data[:maxBytes]
		truncated = true
	}
	return string(data), len(data), truncated, nil
}

func writeFileAtomically(root, fullPath string, content []byte, defaultMode os.FileMode) error {
	root = filepath.Clean(root)
	fullPath = filepath.Clean(fullPath)
	if fullPath != root && !strings.HasPrefix(fullPath, root+string(os.PathSeparator)) {
		return fmt.Errorf("path must remain inside the managed root")
	}
	dir := filepath.Dir(fullPath)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return err
	}

	mode := defaultMode
	if info, err := os.Lstat(fullPath); err == nil {
		if info.Mode()&os.ModeSymlink != 0 {
			return fmt.Errorf("path must not be a symlink")
		}
		if info.IsDir() {
			return fmt.Errorf("path must be a file")
		}
		mode = info.Mode().Perm()
	} else if !os.IsNotExist(err) {
		return err
	}

	tmp, err := os.CreateTemp(dir, ".vikram-write-*")
	if err != nil {
		return err
	}
	tmpPath := tmp.Name()
	cleanup := true
	defer func() {
		if cleanup {
			_ = os.Remove(tmpPath)
		}
	}()

	if err := tmp.Chmod(mode); err != nil {
		_ = tmp.Close()
		return err
	}
	if _, err := tmp.Write(content); err != nil {
		_ = tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	if err := os.Rename(tmpPath, fullPath); err != nil {
		return err
	}
	cleanup = false
	return nil
}

func replaceUniqueText(root, fullPath, oldText, newText string) (int, error) {
	root = filepath.Clean(root)
	fullPath = filepath.Clean(fullPath)
	if fullPath != root && !strings.HasPrefix(fullPath, root+string(os.PathSeparator)) {
		return 0, fmt.Errorf("path must remain inside the managed root")
	}
	info, err := os.Lstat(fullPath)
	if err != nil {
		if os.IsNotExist(err) {
			return 0, fmt.Errorf("file not found")
		}
		return 0, fmt.Errorf("failed to stat file: %w", err)
	}
	if info.IsDir() {
		return 0, fmt.Errorf("path must be a file")
	}
	if info.Mode()&os.ModeSymlink != 0 {
		return 0, fmt.Errorf("path must not be a symlink")
	}

	content, _, truncated, err := readBoundedFile(root, fullPath, maxTargetFileBytes+1)
	if err != nil {
		return 0, fmt.Errorf("failed to read file: %w", err)
	}
	if truncated {
		return 0, fmt.Errorf("file too large to replace safely")
	}
	contentStr := string(content)
	if !strings.Contains(contentStr, oldText) {
		return 0, fmt.Errorf("old_text was not found in the file")
	}
	if count := strings.Count(contentStr, oldText); count > 1 {
		return 0, fmt.Errorf("old_text appears %d times in the file; provide a more specific span", count)
	}

	updated := strings.Replace(contentStr, oldText, newText, 1)
	if err := writeFileAtomically(root, fullPath, []byte(updated), info.Mode().Perm()); err != nil {
		return 0, fmt.Errorf("failed to write file: %w", err)
	}
	return len(updated), nil
}

func discoverRepoTargets(worktreePath, objective string, limit int) ([]orchestrator.RepoTargetCandidate, error) {
	worktreeReal, err := filepath.EvalSymlinks(worktreePath)
	if err != nil {
		return nil, err
	}
	tokens := objectiveTokens(objective)
	type candidate struct {
		path    string
		score   int
		reasons []string
	}
	candidates := make([]candidate, 0, limit*2)
	inspectedFiles := 0
	stopWalk := fmt.Errorf("stop-walk")

	err = filepath.WalkDir(worktreeReal, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.IsDir() {
			if shouldSkipRepoDir(d.Name()) {
				return filepath.SkipDir
			}
			return nil
		}
		if inspectedFiles >= maxTargetDiscoveryFiles {
			return stopWalk
		}
		inspectedFiles++

		relPath, err := filepath.Rel(worktreeReal, path)
		if err != nil {
			return nil
		}
		if !looksInspectableFile(path, relPath, d) {
			return nil
		}

		score, reasons, err := scoreRepoTarget(worktreeReal, path, relPath, tokens)
		if err != nil {
			return nil
		}
		if score <= 0 {
			return nil
		}
		candidates = append(candidates, candidate{path: relPath, score: score, reasons: reasons})
		return nil
	})
	if err != nil && !errors.Is(err, stopWalk) {
		return nil, err
	}

	if len(candidates) == 0 {
		keyFiles, err := inspectKeyFiles(worktreePath)
		if err != nil {
			return nil, err
		}
		for _, item := range keyFiles {
			candidates = append(candidates, candidate{
				path:    item.Path,
				score:   1,
				reasons: []string{"fallback to key repository file"},
			})
		}
	}

	sort.SliceStable(candidates, func(i, j int) bool {
		if candidates[i].score == candidates[j].score {
			return candidates[i].path < candidates[j].path
		}
		return candidates[i].score > candidates[j].score
	})
	if len(candidates) > limit {
		candidates = candidates[:limit]
	}

	result := make([]orchestrator.RepoTargetCandidate, 0, len(candidates))
	for _, item := range candidates {
		result = append(result, orchestrator.RepoTargetCandidate{
			Path:   item.path,
			Score:  item.score,
			Reason: strings.Join(uniqueStrings(item.reasons), "; "),
		})
	}
	return result, nil
}

func discoverVerificationCommands(worktreePath string, targetPaths []string) (string, []orchestrator.VerificationCommandCandidate, error) {
	runtime := detectRepoRuntime(worktreePath)
	candidates := make([]orchestrator.VerificationCommandCandidate, 0, defaultVerificationLimit)
	seen := map[string]struct{}{}

	addCandidate := func(command, reason string) {
		command = strings.TrimSpace(command)
		if command == "" {
			return
		}
		if _, exists := seen[command]; exists {
			return
		}
		seen[command] = struct{}{}
		candidates = append(candidates, orchestrator.VerificationCommandCandidate{
			Command:    command,
			WorkingDir: worktreePath,
			Runtime:    runtime,
			Reason:     reason,
		})
	}

	switch runtime {
	case "go":
		for _, target := range targetPaths {
			if pkg := goTestPackageForPath(target); pkg != "" {
				addCandidate("go test "+pkg, fmt.Sprintf("target-scoped Go package for `%s`", target))
			}
		}
		addCandidate("go test ./...", "full Go repository verification")
	case "python":
		for _, target := range targetPaths {
			if strings.HasSuffix(target, "_test.py") || strings.Contains(target, "/tests/") || strings.HasPrefix(target, "tests/") {
				addCandidate("pytest "+target, fmt.Sprintf("target-specific pytest path for `%s`", target))
			}
		}
		addCandidate("pytest", "default Python test command")
	case "node":
		addCandidate("npm test", "default Node test command from package.json")
	case "rust":
		addCandidate("cargo test", "default Rust test command")
	default:
		if fileExistsInRoot(worktreePath, "Makefile") {
			addCandidate("make test", "Makefile-based test target")
		}
	}

	if fileExistsInRoot(worktreePath, "Makefile") && runtime != "node" {
		addCandidate("make test", "Makefile-based test target")
	}
	if len(candidates) > defaultVerificationLimit {
		candidates = candidates[:defaultVerificationLimit]
	}
	return runtime, candidates, nil
}

func scoreRepoTarget(root, path, relPath string, tokens []string) (int, []string, error) {
	if len(tokens) == 0 {
		return 0, nil, nil
	}
	content, _, _, err := readBoundedFile(root, path, 4096)
	if err != nil {
		return 0, nil, err
	}
	if !looksText(content) {
		return 0, nil, nil
	}

	lowerPath := strings.ToLower(relPath)
	lowerContent := strings.ToLower(content)
	score := 0
	reasons := []string{}
	for _, token := range tokens {
		if strings.Contains(lowerPath, token) {
			score += 5
			reasons = append(reasons, fmt.Sprintf("path matched `%s`", token))
		}
		if strings.Contains(lowerContent, token) {
			score += 3
			reasons = append(reasons, fmt.Sprintf("content matched `%s`", token))
		}
	}
	if strings.HasSuffix(lowerPath, "/test") || strings.Contains(lowerPath, "test") {
		score += 1
		reasons = append(reasons, "testing-related path")
	}
	return score, reasons, nil
}

func objectiveTokens(objective string) []string {
	raw := objectiveTokenPattern.FindAllString(strings.ToLower(objective), -1)
	seen := map[string]struct{}{}
	stopwords := map[string]struct{}{
		"the": {}, "and": {}, "for": {}, "with": {}, "that": {}, "this": {}, "from": {},
		"into": {}, "your": {}, "their": {}, "have": {}, "will": {}, "should": {}, "need": {},
		"make": {}, "best": {}, "possible": {}, "agent": {}, "team": {}, "enterprise": {},
	}
	tokens := make([]string, 0, len(raw))
	for _, token := range raw {
		if len(token) < 3 {
			continue
		}
		if _, blocked := stopwords[token]; blocked {
			continue
		}
		if _, exists := seen[token]; exists {
			continue
		}
		seen[token] = struct{}{}
		tokens = append(tokens, token)
	}
	return tokens
}

func detectRepoRuntime(worktreePath string) string {
	switch {
	case fileExistsInRoot(worktreePath, "go.mod"):
		return "go"
	case fileExistsInRoot(worktreePath, "package.json"):
		return "node"
	case fileExistsInRoot(worktreePath, "pyproject.toml") || fileExistsInRoot(worktreePath, "requirements.txt"):
		return "python"
	case fileExistsInRoot(worktreePath, "Cargo.toml"):
		return "rust"
	default:
		return ""
	}
}

func goTestPackageForPath(target string) string {
	cleaned := filepath.Clean(strings.TrimSpace(target))
	if cleaned == "." || cleaned == "" {
		return ""
	}
	dir := cleaned
	if filepath.Ext(cleaned) != "" {
		dir = filepath.Dir(cleaned)
	}
	if dir == "." {
		return "./..."
	}
	return "./" + filepath.ToSlash(dir)
}

func fileExistsInRoot(root, rel string) bool {
	if !filepath.IsLocal(rel) {
		return false
	}
	root = filepath.Clean(root)
	path := filepath.Clean(filepath.Join(root, rel))
	if path != root && !strings.HasPrefix(path, root+string(os.PathSeparator)) {
		return false
	}
	info, err := os.Lstat(path)
	return err == nil && !info.IsDir() && info.Mode()&os.ModeSymlink == 0
}

func shouldSkipRepoDir(name string) bool {
	switch name {
	case ".git", "node_modules", "dist", "build", "coverage", "__pycache__", ".venv", "vendor":
		return true
	default:
		return false
	}
}

func looksInspectableFile(fullPath, relPath string, entry fs.DirEntry) bool {
	if relPath == ".git" {
		return false
	}
	for _, part := range strings.Split(filepath.ToSlash(relPath), "/") {
		if part == ".git" {
			return false
		}
	}
	info, err := entry.Info()
	if err != nil {
		return false
	}
	if info.Size() > maxTargetFileBytes {
		return false
	}
	ext := strings.ToLower(filepath.Ext(relPath))
	switch ext {
	case ".png", ".jpg", ".jpeg", ".gif", ".pdf", ".zip", ".tar", ".gz", ".class", ".jar", ".so", ".dylib", ".exe", ".bin", ".mp4", ".mp3", ".wav":
		return false
	}
	return true
}

func looksText(content string) bool {
	for _, r := range content {
		if r == 0 {
			return false
		}
		if unicode.IsControl(r) && r != '\n' && r != '\r' && r != '\t' {
			return false
		}
	}
	return true
}

func uniqueStrings(values []string) []string {
	seen := map[string]struct{}{}
	result := make([]string, 0, len(values))
	for _, value := range values {
		if _, exists := seen[value]; exists {
			continue
		}
		seen[value] = struct{}{}
		result = append(result, value)
	}
	return result
}
