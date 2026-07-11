package orchestrator

// TaskStatus describes the lifecycle state of a task session.
type TaskStatus string

const (
	TaskStatusQueued           TaskStatus = "queued"
	TaskStatusRunning          TaskStatus = "running"
	TaskStatusPaused           TaskStatus = "paused"
	TaskStatusAwaitingApproval TaskStatus = "awaiting_approval"
	TaskStatusCompleted        TaskStatus = "completed"
	TaskStatusFailed           TaskStatus = "failed"
)

// ApprovalDecisionKind describes the founder decision that resumes a paused task.
type ApprovalDecisionKind string

const (
	ApprovalDecisionApprove        ApprovalDecisionKind = "approve"
	ApprovalDecisionReject         ApprovalDecisionKind = "reject"
	ApprovalDecisionEditAndApprove ApprovalDecisionKind = "edit_and_approve"
	ApprovalDecisionClarify        ApprovalDecisionKind = "clarify"
)

// ApprovalPolicy controls whether a host action needs founder approval.
type ApprovalPolicy string

const (
	ApprovalPolicyNever     ApprovalPolicy = "never"
	ApprovalPolicyOnRequest ApprovalPolicy = "on_request"
	ApprovalPolicyAlways    ApprovalPolicy = "always"
)

// HostActionKind identifies the host capability class that will execute an action.
type HostActionKind string

const (
	HostActionKindShell      HostActionKind = "shell"
	HostActionKindFilesystem HostActionKind = "filesystem"
	HostActionKindGit        HostActionKind = "git"
	HostActionKindWorkspace  HostActionKind = "workspace"
	HostActionKindNotify     HostActionKind = "notify"
)

// SideEffectLevel captures the risk profile of a host action.
type SideEffectLevel string

const (
	SideEffectReadOnly       SideEffectLevel = "read_only"
	SideEffectWorkspaceWrite SideEffectLevel = "workspace_write"
	SideEffectRepoWrite      SideEffectLevel = "repo_write"
	SideEffectExternal       SideEffectLevel = "external"
)

// ArtifactKind describes the kind of structured output a workflow phase produces.
type ArtifactKind string

const (
	ArtifactKindTaskSpec        ArtifactKind = "task_spec"
	ArtifactKindPlan            ArtifactKind = "plan"
	ArtifactKindImplementation  ArtifactKind = "implementation"
	ArtifactKindVerification    ArtifactKind = "verification"
	ArtifactKindReview          ArtifactKind = "review"
	ArtifactKindFounderApproval ArtifactKind = "founder_approval"
	ArtifactKindBlocker         ArtifactKind = "blocker"
	ArtifactKindArchive         ArtifactKind = "archive"
)

// RepoRef identifies the repository a task operates on.
type RepoRef struct {
	Path          string `json:"path"`
	DefaultBranch string `json:"default_branch"`
}

// SystemHealthResponse reports whether the host daemon is ready for orchestrator requests.
type SystemHealthResponse struct {
	Status              string `json:"status"`
	WorkspaceRoot       string `json:"workspace_root"`
	SocketPath          string `json:"socket_path"`
	RestrictToWorkspace bool   `json:"restrict_to_workspace"`
	Sandboxed           bool   `json:"sandboxed"`
	TelegramEnabled     bool   `json:"telegram_enabled"`
}

// TaskConstraints limit autonomy, budget, and concurrency for a task.
type TaskConstraints struct {
	RequireHumanApproval bool    `json:"require_human_approval"`
	MaxParallelWorkers   int     `json:"max_parallel_workers"`
	MaxCostUSD           float64 `json:"max_cost_usd,omitempty"`
	AllowNetwork         bool    `json:"allow_network,omitempty"`
}

// TaskSession is the shared session object between the Go daemon and Python orchestrator.
type TaskSession struct {
	TaskID          string          `json:"task_id"`
	Source          string          `json:"source"`
	RequestedBy     string          `json:"requested_by"`
	Objective       string          `json:"objective"`
	Repo            RepoRef         `json:"repo"`
	Constraints     TaskConstraints `json:"constraints"`
	OperatorChannel string          `json:"operator_channel,omitempty"`
	OperatorChatID  string          `json:"operator_chat_id,omitempty"`
	Status          TaskStatus      `json:"status"`
	Phase           string          `json:"phase"`
	Summary         string          `json:"summary"`
}

// HostActionSpec declares a host capability exposed to the orchestrator.
type HostActionSpec struct {
	Name              string                 `json:"name"`
	Kind              HostActionKind         `json:"kind"`
	Description       string                 `json:"description"`
	ArgumentsSchema   map[string]interface{} `json:"arguments_schema,omitempty"`
	ApprovalPolicy    ApprovalPolicy         `json:"approval_policy"`
	TimeoutSeconds    int                    `json:"timeout_seconds"`
	StateProbe        []string               `json:"state_probe,omitempty"`
	ObservationPolicy map[string]interface{} `json:"observation_policy,omitempty"`
	SideEffectLevel   SideEffectLevel        `json:"side_effect_level"`
}

// HostActionRequest asks the Go daemon to execute one declared host action.
type HostActionRequest struct {
	TaskID         string                 `json:"task_id"`
	ActionName     string                 `json:"action_name"`
	Arguments      map[string]interface{} `json:"arguments,omitempty"`
	WorkingDir     string                 `json:"working_dir,omitempty"`
	IdempotencyKey string                 `json:"idempotency_key,omitempty"`
}

// HostObservation is the normalized result of a host action.
type HostObservation struct {
	TaskID     string                 `json:"task_id"`
	ActionName string                 `json:"action_name"`
	Success    bool                   `json:"success"`
	ExitCode   *int                   `json:"exit_code,omitempty"`
	Summary    string                 `json:"summary"`
	Output     string                 `json:"output,omitempty"`
	State      map[string]interface{} `json:"state,omitempty"`
}

// WorkspaceProvisionRequest creates the stable on-disk task layout for a session.
type WorkspaceProvisionRequest struct {
	TaskID string  `json:"task_id"`
	Repo   RepoRef `json:"repo"`
}

// WorkspaceProvisionResponse returns the task-scoped paths created for a session.
type WorkspaceProvisionResponse struct {
	TaskID       string `json:"task_id"`
	TaskRoot     string `json:"task_root"`
	ArtifactsDir string `json:"artifacts_dir"`
	LogsDir      string `json:"logs_dir"`
	ScratchDir   string `json:"scratch_dir"`
	WorktreePath string `json:"worktree_path"`
}

// GitWorktreeCreateRequest asks the host daemon to create or reuse a task worktree.
type GitWorktreeCreateRequest struct {
	TaskID       string  `json:"task_id"`
	Repo         RepoRef `json:"repo"`
	WorktreePath string  `json:"worktree_path"`
	Branch       string  `json:"branch"`
	BaseRef      string  `json:"base_ref,omitempty"`
}

// GitWorktreeCreateResponse reports the prepared worktree details for a task.
type GitWorktreeCreateResponse struct {
	TaskID       string `json:"task_id"`
	RepoPath     string `json:"repo_path"`
	WorktreePath string `json:"worktree_path"`
	Branch       string `json:"branch"`
	BaseRef      string `json:"base_ref"`
	HeadRef      string `json:"head_ref,omitempty"`
	Created      bool   `json:"created"`
}

// GitWorktreeRemoveRequest asks the host daemon to remove a managed task worktree.
type GitWorktreeRemoveRequest struct {
	TaskID       string `json:"task_id"`
	WorktreePath string `json:"worktree_path"`
	Force        bool   `json:"force,omitempty"`
}

// GitWorktreeRemoveResponse reports whether the managed task worktree was removed.
type GitWorktreeRemoveResponse struct {
	TaskID       string `json:"task_id"`
	WorktreePath string `json:"worktree_path"`
	Removed      bool   `json:"removed"`
}

// RepoFileSummary is a bounded preview of a key repository file.
type RepoFileSummary struct {
	Path    string `json:"path"`
	Preview string `json:"preview"`
	Bytes   int    `json:"bytes"`
}

// RepoChangedFile summarizes one changed file in a task worktree.
type RepoChangedFile struct {
	Path      string `json:"path"`
	Status    string `json:"status"`
	Additions int    `json:"additions,omitempty"`
	Deletions int    `json:"deletions,omitempty"`
	Binary    bool   `json:"binary,omitempty"`
}

// RepoInspectRequest asks the host daemon to summarize repository state for planning.
type RepoInspectRequest struct {
	TaskID       string `json:"task_id"`
	RepoPath     string `json:"repo_path"`
	WorktreePath string `json:"worktree_path"`
}

// RepoInspectResponse returns a bounded summary of repository state for planning.
type RepoInspectResponse struct {
	TaskID           string            `json:"task_id"`
	RepoPath         string            `json:"repo_path"`
	WorktreePath     string            `json:"worktree_path"`
	Branch           string            `json:"branch"`
	HeadRef          string            `json:"head_ref"`
	Dirty            bool              `json:"dirty"`
	ChangedFileCount int               `json:"changed_file_count"`
	Additions        int               `json:"additions,omitempty"`
	Deletions        int               `json:"deletions,omitempty"`
	DiffShortStat    string            `json:"diff_short_stat,omitempty"`
	TopLevelEntries  []string          `json:"top_level_entries,omitempty"`
	StatusLines      []string          `json:"status_lines,omitempty"`
	ChangedFiles     []RepoChangedFile `json:"changed_files,omitempty"`
	KeyFiles         []RepoFileSummary `json:"key_files,omitempty"`
}

// RepoTargetCandidate is a scored candidate file or path for the next implementation step.
type RepoTargetCandidate struct {
	Path   string `json:"path"`
	Score  int    `json:"score"`
	Reason string `json:"reason"`
}

// RepoTargetDiscoveryRequest asks the host daemon to localize candidate targets for an objective.
type RepoTargetDiscoveryRequest struct {
	TaskID       string `json:"task_id"`
	WorktreePath string `json:"worktree_path"`
	Objective    string `json:"objective"`
	Limit        int    `json:"limit,omitempty"`
}

// RepoTargetDiscoveryResponse returns scored target candidates for the objective.
type RepoTargetDiscoveryResponse struct {
	TaskID       string                `json:"task_id"`
	WorktreePath string                `json:"worktree_path"`
	Candidates   []RepoTargetCandidate `json:"candidates,omitempty"`
}

// FileReadRequest asks the host daemon to read a bounded file slice from a worktree.
type FileReadRequest struct {
	TaskID       string `json:"task_id"`
	WorktreePath string `json:"worktree_path"`
	Path         string `json:"path"`
	MaxBytes     int    `json:"max_bytes,omitempty"`
}

// FileReadResponse returns bounded file content for planning or inspection.
type FileReadResponse struct {
	TaskID    string `json:"task_id"`
	Path      string `json:"path"`
	FullPath  string `json:"full_path"`
	Content   string `json:"content"`
	BytesRead int    `json:"bytes_read"`
	Truncated bool   `json:"truncated"`
}

// FileWriteRequest asks the host daemon to write bounded content into a managed worktree.
type FileWriteRequest struct {
	TaskID       string `json:"task_id"`
	WorktreePath string `json:"worktree_path"`
	Path         string `json:"path"`
	Content      string `json:"content"`
}

// FileWriteResponse reports the result of a bounded file write.
type FileWriteResponse struct {
	TaskID       string `json:"task_id"`
	Path         string `json:"path"`
	FullPath     string `json:"full_path"`
	BytesWritten int    `json:"bytes_written"`
}

// FileReplaceRequest asks the host daemon to replace one exact text span in a managed worktree file.
type FileReplaceRequest struct {
	TaskID       string `json:"task_id"`
	WorktreePath string `json:"worktree_path"`
	Path         string `json:"path"`
	OldText      string `json:"old_text"`
	NewText      string `json:"new_text"`
}

// FileReplaceResponse reports the result of an exact text replacement in a managed worktree file.
type FileReplaceResponse struct {
	TaskID       string `json:"task_id"`
	Path         string `json:"path"`
	FullPath     string `json:"full_path"`
	BytesWritten int    `json:"bytes_written"`
}

// VerificationCommandCandidate is a candidate verification command for the current task.
type VerificationCommandCandidate struct {
	Command    string `json:"command"`
	WorkingDir string `json:"working_dir"`
	Runtime    string `json:"runtime"`
	Reason     string `json:"reason"`
}

// VerificationDiscoveryRequest asks the host daemon to discover focused verification commands.
type VerificationDiscoveryRequest struct {
	TaskID       string   `json:"task_id"`
	WorktreePath string   `json:"worktree_path"`
	TargetPaths  []string `json:"target_paths,omitempty"`
}

// VerificationDiscoveryResponse returns verification command candidates for the task.
type VerificationDiscoveryResponse struct {
	TaskID       string                         `json:"task_id"`
	WorktreePath string                         `json:"worktree_path"`
	Runtime      string                         `json:"runtime"`
	Candidates   []VerificationCommandCandidate `json:"candidates,omitempty"`
}

// Artifact is a structured workflow output stored outside the conversation transcript.
type Artifact struct {
	TaskID     string       `json:"task_id"`
	ArtifactID string       `json:"artifact_id"`
	Kind       ArtifactKind `json:"kind"`
	Title      string       `json:"title"`
	Summary    string       `json:"summary"`
	Path       string       `json:"path,omitempty"`
}

// ApprovalRequest asks the founder to decide whether a paused task may continue.
type ApprovalRequest struct {
	TaskID           string                 `json:"task_id"`
	Phase            string                 `json:"phase"`
	Reason           string                 `json:"reason"`
	Summary          string                 `json:"summary"`
	Options          []ApprovalDecisionKind `json:"options"`
	RelatedArtifacts []string               `json:"related_artifacts,omitempty"`
}

// ApprovalDecision resumes a paused task with a founder decision.
type ApprovalDecision struct {
	TaskID        string                 `json:"task_id"`
	Decision      ApprovalDecisionKind   `json:"decision"`
	Comment       string                 `json:"comment,omitempty"`
	ProposedEdits map[string]interface{} `json:"proposed_edits,omitempty"`
}

// ArtifactWriteRequest asks the host daemon to persist a structured artifact.
type ArtifactWriteRequest struct {
	Artifact Artifact `json:"artifact"`
	Content  string   `json:"content"`
	Format   string   `json:"format,omitempty"`
}

// ArtifactWriteResponse reports the persisted artifact path and metadata.
type ArtifactWriteResponse struct {
	Artifact     Artifact `json:"artifact"`
	Path         string   `json:"path"`
	BytesWritten int      `json:"bytes_written"`
}

// ArtifactReadRequest asks the host daemon to read a persisted task artifact.
type ArtifactReadRequest struct {
	TaskID   string `json:"task_id"`
	Path     string `json:"path"`
	MaxBytes int    `json:"max_bytes,omitempty"`
}

// ArtifactReadResponse returns bounded artifact content for founder review.
type ArtifactReadResponse struct {
	TaskID    string `json:"task_id"`
	Path      string `json:"path"`
	Content   string `json:"content"`
	BytesRead int    `json:"bytes_read"`
	Truncated bool   `json:"truncated"`
}

// ChannelNotificationRequest asks the host daemon to send a message to a channel.
type ChannelNotificationRequest struct {
	Channel string `json:"channel"`
	ChatID  string `json:"chat_id"`
	Content string `json:"content"`
}

// ChannelNotificationResponse reports whether a channel notification was delivered.
type ChannelNotificationResponse struct {
	Delivered bool   `json:"delivered"`
	Summary   string `json:"summary"`
}

// TextReplacement defines one bounded in-file text replacement.
type TextReplacement struct {
	Path      string `json:"path"`
	OldText   string `json:"old_text"`
	NewText   string `json:"new_text"`
	Rationale string `json:"rationale,omitempty"`
}

// TaskChangeRequest asks the orchestrator to apply bounded edits and run focused verification.
type TaskChangeRequest struct {
	TaskID               string            `json:"task_id"`
	Summary              string            `json:"summary,omitempty"`
	Edits                []TextReplacement `json:"edits"`
	VerificationCommands []string          `json:"verification_commands,omitempty"`
}

// GitRollbackRequest asks the host daemon to revert a worktree to its pre-edit snapshot.
type GitRollbackRequest struct {
	TaskID       string `json:"task_id"`
	WorktreePath string `json:"worktree_path"`
}

// GitRollbackResponse reports whether a rollback was performed.
type GitRollbackResponse struct {
	TaskID       string `json:"task_id"`
	WorktreePath string `json:"worktree_path"`
	RolledBack   bool   `json:"rolled_back"`
	HeadRef      string `json:"head_ref,omitempty"`
}

// LintDiscoveryRequest asks the host daemon to discover available lint commands.
type LintDiscoveryRequest struct {
	TaskID       string `json:"task_id"`
	WorktreePath string `json:"worktree_path"`
}

// LintDiscoveryResponse returns lint command candidates for the worktree runtime.
type LintDiscoveryResponse struct {
	TaskID       string                 `json:"task_id"`
	WorktreePath string                 `json:"worktree_path"`
	Runtime      string                 `json:"runtime"`
	Candidates   []LintCommandCandidate `json:"candidates,omitempty"`
}

// LintCommandCandidate describes one available lint command.
type LintCommandCandidate struct {
	Command    string `json:"command"`
	WorkingDir string `json:"working_dir"`
	Runtime    string `json:"runtime"`
	Reason     string `json:"reason"`
}

// LintRunRequest asks the host daemon to execute a lint command and return
// structured results so new errors can be differentiated from pre-existing ones.
type LintRunRequest struct {
	TaskID       string `json:"task_id"`
	WorktreePath string `json:"worktree_path"`
	Command      string `json:"command"`
	// Baseline is optional previous lint output; only NEW errors are returned.
	Baseline string `json:"baseline,omitempty"`
}

// LintRunResponse reports the result of a lint command run.
type LintRunResponse struct {
	TaskID    string   `json:"task_id"`
	Command   string   `json:"command"`
	Success   bool     `json:"success"`
	ExitCode  int      `json:"exit_code"`
	Output    string   `json:"output"`
	NewErrors []string `json:"new_errors,omitempty"`
}

// ChangeReviewRequest asks the host daemon to have the reviewer agent
// evaluate a set of code changes against the original task objective.
type ChangeReviewRequest struct {
	TaskID     string   `json:"task_id"`
	Objective  string   `json:"objective"`
	Diff       string   `json:"diff"`
	TestOutput string   `json:"test_output,omitempty"`
	LintErrors []string `json:"lint_errors,omitempty"`
}

// ChangeReviewVerdict is the structured review decision from the LLM judge.
type ChangeReviewVerdict string

const (
	ReviewVerdictApprove          ChangeReviewVerdict = "APPROVE"
	ReviewVerdictChangesRequested ChangeReviewVerdict = "CHANGES_REQUESTED"
	ReviewVerdictReject           ChangeReviewVerdict = "REJECT"
)

// ChangeReviewResponse is the structured result from the reviewer agent.
type ChangeReviewResponse struct {
	TaskID  string              `json:"task_id"`
	Verdict ChangeReviewVerdict `json:"verdict"`
	Issues  []string            `json:"issues,omitempty"`
	Summary string              `json:"summary"`
}

// AgentProfile exposes non-secret routing metadata for an available team
// member. The Python orchestrator uses this roster to choose a role/model
// assignment, while the Go host only executes the requested route.
type AgentProfile struct {
	ID           string   `json:"id"`
	Name         string   `json:"name,omitempty"`
	Role         string   `json:"role"`
	ProviderName string   `json:"provider,omitempty"`
	Model        string   `json:"model,omitempty"`
	Capabilities []string `json:"capabilities,omitempty"`
}

// AgentRosterResponse lists available non-secret team members.
type AgentRosterResponse struct {
	Agents []AgentProfile `json:"agents"`
}

// AgentThinkRequest asks the host daemon to run a reasoning step through a
// team route chosen by the Python orchestrator. Role is still required as the
// budget and audit key; Provider/Model are optional explicit routing choices.
type AgentThinkRequest struct {
	TaskID       string `json:"task_id"`
	Role         string `json:"role"`
	Prompt       string `json:"prompt"`
	Model        string `json:"model,omitempty"`
	ProviderName string `json:"provider,omitempty"`
}

// AgentThinkResponse returns the LLM response from the role-specific model.
type AgentThinkResponse struct {
	TaskID  string `json:"task_id"`
	Role    string `json:"role"`
	Content string `json:"content"`
}

// InvocationContext carries per-invocation state through an agent call tree,
// following Google ADK's pattern of a single state carrier with copy-on-write
// semantics for sub-agent calls.
type InvocationContext struct {
	InvocationID string            `json:"invocation_id"`
	TaskID       string            `json:"task_id"`
	AgentID      string            `json:"agent_id"`
	Role         string            `json:"role"`
	WorkingDir   string            `json:"working_dir,omitempty"`
	State        map[string]string `json:"state,omitempty"`
}

// NewInvocationContext creates a shallow-copyable context for an agent call.
func NewInvocationContext(taskID, agentID, role string) InvocationContext {
	return InvocationContext{
		InvocationID: agentID + "-" + taskID,
		TaskID:       taskID,
		AgentID:      agentID,
		Role:         role,
		State:        make(map[string]string),
	}
}

// WithWorkingDir returns a copy with the working directory set.
func (c InvocationContext) WithWorkingDir(dir string) InvocationContext {
	c.WorkingDir = dir
	return c
}

// WithState returns a copy with an additional state key set.
func (c InvocationContext) WithState(key, value string) InvocationContext {
	if c.State == nil {
		c.State = make(map[string]string)
	}
	c.State[key] = value
	return c
}

// MultiRepoProvisionRequest asks the host daemon to provision worktrees for
// multiple repositories under a single task workspace root.
type MultiRepoProvisionRequest struct {
	TaskID string    `json:"task_id"`
	Repos  []RepoRef `json:"repos"`
}

// MultiRepoWorktreeResult describes the provisioned worktree for one repository
// within a multi-repo task.
type MultiRepoWorktreeResult struct {
	RepoPath     string `json:"repo_path"`
	WorktreePath string `json:"worktree_path"`
	Branch       string `json:"branch"`
	HeadRef      string `json:"head_ref,omitempty"`
	Created      bool   `json:"created"`
	Error        string `json:"error,omitempty"`
}

// MultiRepoProvisionResponse returns provisioned worktree paths for all repos.
type MultiRepoProvisionResponse struct {
	TaskID       string                    `json:"task_id"`
	TaskRoot     string                    `json:"task_root"`
	ArtifactsDir string                    `json:"artifacts_dir"`
	LogsDir      string                    `json:"logs_dir"`
	ScratchDir   string                    `json:"scratch_dir"`
	Worktrees    []MultiRepoWorktreeResult `json:"worktrees"`
}

// AtomicMergeRequest asks the host daemon to perform sequential fast-forward
// merges across multiple repositories with rollback on any failure.
type AtomicMergeRequest struct {
	TaskID string             `json:"task_id"`
	Repos  []AtomicMergeEntry `json:"repos"`
}

// AtomicMergeEntry describes one repository merge within an atomic multi-repo merge.
type AtomicMergeEntry struct {
	RepoPath     string `json:"repo_path"`
	WorktreePath string `json:"worktree_path"`
	Branch       string `json:"branch"`
	TargetBranch string `json:"target_branch"`
}

// AtomicMergeResult describes the outcome of merging one repository.
type AtomicMergeResult struct {
	RepoPath     string `json:"repo_path"`
	Merged       bool   `json:"merged"`
	RolledBack   bool   `json:"rolled_back"`
	PreMergeRef  string `json:"pre_merge_ref,omitempty"`
	PostMergeRef string `json:"post_merge_ref,omitempty"`
	Error        string `json:"error,omitempty"`
}

// AtomicMergeResponse reports the result of an atomic multi-repo merge.
type AtomicMergeResponse struct {
	TaskID   string              `json:"task_id"`
	Success  bool                `json:"success"`
	Results  []AtomicMergeResult `json:"results"`
	FailedAt string              `json:"failed_at,omitempty"`
}

// DetachRepoRequest asks the host daemon to detach a blocked repository from
// a multi-repo task, allowing remaining repos to proceed independently.
type DetachRepoRequest struct {
	TaskID   string `json:"task_id"`
	RepoPath string `json:"repo_path"`
	Reason   string `json:"reason,omitempty"`
}

// DetachRepoResponse reports the result of detaching a repository.
type DetachRepoResponse struct {
	TaskID       string `json:"task_id"`
	RepoPath     string `json:"repo_path"`
	Detached     bool   `json:"detached"`
	WorktreePath string `json:"worktree_path,omitempty"`
	Cleaned      bool   `json:"cleaned"`
}

// BrowserTestRequest asks the host daemon to run a browser test using Playwright.
type BrowserTestRequest struct {
	TaskID       string `json:"task_id"`
	WorktreePath string `json:"worktree_path"`
	TestScript   string `json:"test_script"`
	URL          string `json:"url,omitempty"`
}

// BrowserTestResponse reports the result of a browser test run.
type BrowserTestResponse struct {
	TaskID     string `json:"task_id"`
	Success    bool   `json:"success"`
	ExitCode   int    `json:"exit_code"`
	Output     string `json:"output"`
	Screenshot string `json:"screenshot,omitempty"` // base64 PNG
}

// ---------------------------------------------------------------------------
// Extended Task Create (Requirements 14.2, 16.1, 17.1, 21.1)
// ---------------------------------------------------------------------------

// TaskCreateRequest is the extended task creation payload that includes
// multi-repo, priority, dependency, and formation fields for the scheduler.
type TaskCreateRequest struct {
	TaskID          string          `json:"task_id"`
	Source          string          `json:"source,omitempty"`
	RequestedBy     string          `json:"requested_by,omitempty"`
	Objective       string          `json:"objective"`
	Repo            RepoRef         `json:"repo"`
	Constraints     TaskConstraints `json:"constraints"`
	OperatorChannel string          `json:"operator_channel,omitempty"`
	OperatorChatID  string          `json:"operator_chat_id,omitempty"`
	// Scheduler extensions
	Repos     []RepoRef `json:"repos,omitempty"`
	Priority  string    `json:"priority,omitempty"` // "critical", "high", "normal", "low"
	DependsOn []string  `json:"depends_on,omitempty"`
	Formation string    `json:"formation,omitempty"`
}

// PriorityUpdateRequest is the payload for PUT /v1/tasks/{task_id}/priority.
type PriorityUpdateRequest struct {
	Priority string `json:"priority"` // "critical", "high", "normal", "low"
}

// QueueEntry represents a single task in the scheduler queue response.
type QueueEntry struct {
	TaskID     string   `json:"task_id"`
	Priority   string   `json:"priority"`
	Status     string   `json:"status"`
	EnqueuedAt float64  `json:"enqueued_at"`
	DependsOn  []string `json:"depends_on,omitempty"`
	Repos      []string `json:"repos,omitempty"`
	Formation  string   `json:"formation,omitempty"`
}

// QueueResponse is the response for GET /v1/queue.
type QueueResponse struct {
	Queue   []QueueEntry `json:"queue"`
	Running int          `json:"running"`
	Queued  int          `json:"queued"`
	Blocked int          `json:"blocked"`
}
