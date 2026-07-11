package console

import (
	"encoding/json"
	"fmt"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"time"
)

// TerminalStatuses are task statuses that trigger automatic archival.
var TerminalStatuses = map[string]bool{
	"merged":       true,
	"completed":    true,
	"failed-final": true,
	"cancelled":    true,
}

// ArchivedTask represents a task record stored in the archive.
type ArchivedTask struct {
	TaskID         string                 `json:"task_id"`
	Objective      string                 `json:"objective"`
	Status         string                 `json:"status"`
	Repository     string                 `json:"repository"`
	ComplexityTier string                 `json:"complexity_tier"`
	Formation      string                 `json:"formation"`
	TotalCostUSD   float64                `json:"total_cost_usd"`
	TotalTokens    int64                  `json:"total_tokens"`
	TotalDuration  time.Duration          `json:"total_duration_ms"`
	FilesChanged   []FileChange           `json:"files_changed,omitempty"`
	Verifications  []VerificationResult   `json:"verifications,omitempty"`
	Approvals      []ApprovalRecord       `json:"approvals,omitempty"`
	Phases         []PhaseTransition      `json:"phases,omitempty"`
	Artifacts      []string               `json:"artifacts,omitempty"`
	ArchivedAt     time.Time              `json:"archived_at"`
	CompletedAt    time.Time              `json:"completed_at"`
	CreatedAt      time.Time              `json:"created_at"`
	Metadata       map[string]interface{} `json:"metadata,omitempty"`
}

// FileChange describes a file modification in the archived task.
type FileChange struct {
	Path   string `json:"path"`
	Action string `json:"action"` // added, modified, deleted
	Diff   string `json:"diff,omitempty"`
}

// VerificationResult stores a verification outcome.
type VerificationResult struct {
	Type    string `json:"type"`
	Passed  bool   `json:"passed"`
	Details string `json:"details,omitempty"`
}

// ApprovalRecord stores a founder approval decision.
type ApprovalRecord struct {
	Decision  string    `json:"decision"`
	Comment   string    `json:"comment,omitempty"`
	Timestamp time.Time `json:"timestamp"`
}

// PhaseTransition records a work phase change.
type PhaseTransition struct {
	From      string    `json:"from"`
	To        string    `json:"to"`
	Reason    string    `json:"reason,omitempty"`
	Timestamp time.Time `json:"timestamp"`
}

// ArchiveFilter defines the query parameters for searching the archive.
type ArchiveFilter struct {
	Query          string  `json:"query,omitempty"`      // full-text search
	Status         string  `json:"status,omitempty"`     // filter by terminal status
	Repository     string  `json:"repository,omitempty"` // filter by repo
	DateFrom       string  `json:"date_from,omitempty"`  // ISO 8601
	DateTo         string  `json:"date_to,omitempty"`    // ISO 8601
	ComplexityTier string  `json:"complexity_tier,omitempty"`
	Formation      string  `json:"formation,omitempty"`
	CostMin        float64 `json:"cost_min,omitempty"`
	CostMax        float64 `json:"cost_max,omitempty"`
	Page           int     `json:"page,omitempty"`
	PageSize       int     `json:"page_size,omitempty"`
}

// ArchiveSearchResponse is the paginated response for archive searches.
type ArchiveSearchResponse struct {
	Tasks      []ArchivedTask `json:"tasks"`
	Total      int            `json:"total"`
	Page       int            `json:"page"`
	PageSize   int            `json:"page_size"`
	TotalPages int            `json:"total_pages"`
}

// ArchiveExportRequest defines the export criteria.
type ArchiveExportRequest struct {
	Filter ArchiveFilter `json:"filter"`
	Format string        `json:"format"` // only "json" supported
}

// RetentionPolicy configures archive retention rules.
type RetentionPolicy struct {
	MinRetentionDays int  `json:"min_retention_days"` // minimum 365
	AutoPurge        bool `json:"auto_purge"`
}

// DefaultRetentionPolicy returns the platform default (365-day minimum retention).
func DefaultRetentionPolicy() RetentionPolicy {
	return RetentionPolicy{
		MinRetentionDays: 365,
		AutoPurge:        false,
	}
}

// Archive manages the task archive with search, retention, and export.
type Archive struct {
	mu              sync.RWMutex
	tasks           []ArchivedTask
	retentionPolicy RetentionPolicy
}

// NewArchive creates an Archive with the default retention policy.
func NewArchive() *Archive {
	return &Archive{
		tasks:           make([]ArchivedTask, 0),
		retentionPolicy: DefaultRetentionPolicy(),
	}
}

// SetRetentionPolicy updates the retention policy. MinRetentionDays is clamped to >= 365.
func (a *Archive) SetRetentionPolicy(policy RetentionPolicy) {
	if policy.MinRetentionDays < 365 {
		policy.MinRetentionDays = 365
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	a.retentionPolicy = policy
}

// GetRetentionPolicy returns the current retention policy.
func (a *Archive) GetRetentionPolicy() RetentionPolicy {
	a.mu.RLock()
	defer a.mu.RUnlock()
	return a.retentionPolicy
}

// ArchiveTask adds a task to the archive. It only accepts tasks in terminal status.
func (a *Archive) ArchiveTask(task ArchivedTask) error {
	if !TerminalStatuses[task.Status] {
		return fmt.Errorf("cannot archive task with non-terminal status %q", task.Status)
	}
	if task.ArchivedAt.IsZero() {
		task.ArchivedAt = time.Now()
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	a.tasks = append(a.tasks, task)
	return nil
}

// Search finds archived tasks matching the given filter with pagination.
func (a *Archive) Search(filter ArchiveFilter) ArchiveSearchResponse {
	a.mu.RLock()
	defer a.mu.RUnlock()

	if filter.PageSize <= 0 {
		filter.PageSize = 20
	}
	if filter.Page <= 0 {
		filter.Page = 1
	}

	var matched []ArchivedTask
	for _, task := range a.tasks {
		if matchesFilter(task, filter) {
			matched = append(matched, task)
		}
	}

	total := len(matched)
	totalPages := (total + filter.PageSize - 1) / filter.PageSize
	if totalPages < 1 {
		totalPages = 1
	}

	start := (filter.Page - 1) * filter.PageSize
	if start >= total {
		return ArchiveSearchResponse{
			Tasks:      []ArchivedTask{},
			Total:      total,
			Page:       filter.Page,
			PageSize:   filter.PageSize,
			TotalPages: totalPages,
		}
	}
	end := start + filter.PageSize
	if end > total {
		end = total
	}

	return ArchiveSearchResponse{
		Tasks:      matched[start:end],
		Total:      total,
		Page:       filter.Page,
		PageSize:   filter.PageSize,
		TotalPages: totalPages,
	}
}

// Export returns all archived tasks matching the filter (no pagination) for bulk export.
func (a *Archive) Export(filter ArchiveFilter) []ArchivedTask {
	a.mu.RLock()
	defer a.mu.RUnlock()

	var matched []ArchivedTask
	for _, task := range a.tasks {
		if matchesFilter(task, filter) {
			matched = append(matched, task)
		}
	}
	if matched == nil {
		matched = []ArchivedTask{}
	}
	return matched
}

// PurgeExpired removes tasks older than the retention policy allows.
// Returns the number of tasks purged.
func (a *Archive) PurgeExpired() int {
	a.mu.Lock()
	defer a.mu.Unlock()

	cutoff := time.Now().AddDate(0, 0, -a.retentionPolicy.MinRetentionDays)
	var kept []ArchivedTask
	purged := 0
	for _, task := range a.tasks {
		if task.ArchivedAt.Before(cutoff) {
			purged++
		} else {
			kept = append(kept, task)
		}
	}
	a.tasks = kept
	return purged
}

// Count returns the total number of archived tasks.
func (a *Archive) Count() int {
	a.mu.RLock()
	defer a.mu.RUnlock()
	return len(a.tasks)
}

// matchesFilter checks if an archived task matches the given filter criteria.
func matchesFilter(task ArchivedTask, filter ArchiveFilter) bool {
	// Full-text search on objective and task ID.
	if filter.Query != "" {
		q := strings.ToLower(filter.Query)
		if !strings.Contains(strings.ToLower(task.Objective), q) &&
			!strings.Contains(strings.ToLower(task.TaskID), q) {
			return false
		}
	}

	// Status filter.
	if filter.Status != "" && task.Status != filter.Status {
		return false
	}

	// Repository filter.
	if filter.Repository != "" && !strings.Contains(strings.ToLower(task.Repository), strings.ToLower(filter.Repository)) {
		return false
	}

	// Date range filter (on CompletedAt).
	if filter.DateFrom != "" {
		from, err := time.Parse("2006-01-02", filter.DateFrom)
		if err == nil && task.CompletedAt.Before(from) {
			return false
		}
	}
	if filter.DateTo != "" {
		to, err := time.Parse("2006-01-02", filter.DateTo)
		if err == nil && task.CompletedAt.After(to.Add(24*time.Hour-time.Nanosecond)) {
			return false
		}
	}

	// Complexity tier filter.
	if filter.ComplexityTier != "" && task.ComplexityTier != filter.ComplexityTier {
		return false
	}

	// Formation filter.
	if filter.Formation != "" && task.Formation != filter.Formation {
		return false
	}

	// Cost range filter.
	if filter.CostMin > 0 && task.TotalCostUSD < filter.CostMin {
		return false
	}
	if filter.CostMax > 0 && task.TotalCostUSD > filter.CostMax {
		return false
	}

	return true
}

// handleArchiveSearch handles GET /api/archive.
// Supports query params: q, status, repo, date_from, date_to, tier, formation, cost_min, cost_max, page, page_size.
func (s *Server) handleArchiveSearch(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		s.writeError(w, http.StatusMethodNotAllowed, "GET only")
		return
	}

	q := r.URL.Query()
	filter := ArchiveFilter{
		Query:          q.Get("q"),
		Status:         q.Get("status"),
		Repository:     q.Get("repo"),
		DateFrom:       q.Get("date_from"),
		DateTo:         q.Get("date_to"),
		ComplexityTier: q.Get("tier"),
		Formation:      q.Get("formation"),
	}

	if v := q.Get("cost_min"); v != "" {
		if f, err := strconv.ParseFloat(v, 64); err == nil {
			filter.CostMin = f
		}
	}
	if v := q.Get("cost_max"); v != "" {
		if f, err := strconv.ParseFloat(v, 64); err == nil {
			filter.CostMax = f
		}
	}
	if v := q.Get("page"); v != "" {
		if p, err := strconv.Atoi(v); err == nil {
			filter.Page = p
		}
	}
	if v := q.Get("page_size"); v != "" {
		if ps, err := strconv.Atoi(v); err == nil {
			filter.PageSize = ps
		}
	}

	if s.archive == nil {
		s.writeOK(w, ArchiveSearchResponse{
			Tasks:      []ArchivedTask{},
			Total:      0,
			Page:       1,
			PageSize:   20,
			TotalPages: 1,
		})
		return
	}

	result := s.archive.Search(filter)
	s.writeOK(w, result)
}

// handleArchiveExport handles POST /api/archive/export.
// Returns a JSON bulk export of archived tasks matching the filter.
func (s *Server) handleArchiveExport(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		s.writeError(w, http.StatusMethodNotAllowed, "POST only")
		return
	}

	var req ArchiveExportRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		s.writeError(w, http.StatusBadRequest, "invalid JSON body")
		return
	}

	if req.Format != "" && req.Format != "json" {
		s.writeError(w, http.StatusBadRequest, "only json format is supported")
		return
	}

	if s.archive == nil {
		s.writeOK(w, map[string]interface{}{
			"tasks":    []ArchivedTask{},
			"exported": 0,
			"format":   "json",
		})
		return
	}

	tasks := s.archive.Export(req.Filter)
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Content-Disposition", "attachment; filename=archive_export.json")
	w.WriteHeader(http.StatusOK)
	json.NewEncoder(w).Encode(map[string]interface{}{
		"tasks":       tasks,
		"exported":    len(tasks),
		"format":      "json",
		"exported_at": time.Now().UTC().Format(time.RFC3339),
	})
}
