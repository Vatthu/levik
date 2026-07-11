package console

import (
	"context"
	"net/http"
	"net/url"
	"path/filepath"
	"strconv"
	"time"
)

// DiffMode controls the output format of a diff response.
type DiffMode string

const (
	DiffModeUnified    DiffMode = "unified"
	DiffModeSideBySide DiffMode = "side-by-side"
)

// DefaultContextLines is the default number of surrounding context lines in diffs.
const DefaultContextLines = 3

// FileDiff represents the diff for a single file within a task worktree.
type FileDiff struct {
	Path       string `json:"path"`
	Language   string `json:"language,omitempty"`
	OldContent string `json:"old_content,omitempty"`
	NewContent string `json:"new_content,omitempty"`
	Patch      string `json:"patch,omitempty"`
	Additions  int    `json:"additions"`
	Deletions  int    `json:"deletions"`
	Binary     bool   `json:"binary,omitempty"`
}

// DiffResponse is the envelope returned by the diff endpoint.
type DiffResponse struct {
	TaskID       string     `json:"task_id"`
	Mode         DiffMode   `json:"mode"`
	ContextLines int        `json:"context_lines"`
	Files        []FileDiff `json:"files"`
}

// handleAPITaskDiff serves GET /api/tasks/{task_id}/diff.
// It proxies to the orchestrator and returns per-file diffs with syntax
// highlighting metadata and configurable context lines.
//
// Query params:
//   - mode=unified|side-by-side (default: unified)
//   - context=N (default: 3, use 0 for full file content)
func (s *Server) handleAPITaskDiff(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		s.writeError(w, http.StatusMethodNotAllowed, "GET only")
		return
	}

	taskID := r.PathValue("task_id")
	if taskID == "" {
		s.writeError(w, http.StatusBadRequest, "task_id is required")
		return
	}

	mode := DiffMode(r.URL.Query().Get("mode"))
	if mode == "" {
		mode = DiffModeUnified
	}
	if mode != DiffModeUnified && mode != DiffModeSideBySide {
		s.writeError(w, http.StatusBadRequest, "mode must be 'unified' or 'side-by-side'")
		return
	}

	contextLines := DefaultContextLines
	if ctxParam := r.URL.Query().Get("context"); ctxParam != "" {
		n, err := strconv.Atoi(ctxParam)
		if err != nil || n < 0 {
			s.writeError(w, http.StatusBadRequest, "context must be a non-negative integer")
			return
		}
		contextLines = n
	}

	ctx, cancel := context.WithTimeout(r.Context(), 30*time.Second)
	defer cancel()

	var diff DiffResponse
	path := "/v1/tasks/" + url.PathEscape(taskID) + "/diff?mode=" + url.QueryEscape(string(mode)) +
		"&context=" + strconv.Itoa(contextLines)
	if err := s.orchestratorJSON(ctx, http.MethodGet, path, nil, &diff); err != nil {
		s.writeError(w, http.StatusBadGateway, err.Error())
		return
	}

	// Ensure mode and context are set in the response regardless of what the orchestrator returns.
	diff.TaskID = taskID
	diff.Mode = mode
	diff.ContextLines = contextLines

	// Populate language from file extension for syntax highlighting.
	for i := range diff.Files {
		if diff.Files[i].Language == "" && !diff.Files[i].Binary {
			diff.Files[i].Language = detectLanguage(diff.Files[i].Path)
		}
	}

	s.writeOK(w, diff)
}

// detectLanguage infers a syntax highlighting language identifier from the file extension.
func detectLanguage(path string) string {
	ext := filepath.Ext(path)
	switch ext {
	case ".go":
		return "go"
	case ".py":
		return "python"
	case ".ts":
		return "typescript"
	case ".tsx":
		return "typescriptreact"
	case ".js":
		return "javascript"
	case ".jsx":
		return "javascriptreact"
	case ".rs":
		return "rust"
	case ".java":
		return "java"
	case ".rb":
		return "ruby"
	case ".c", ".h":
		return "c"
	case ".cpp", ".cc", ".cxx", ".hpp":
		return "cpp"
	case ".cs":
		return "csharp"
	case ".swift":
		return "swift"
	case ".kt", ".kts":
		return "kotlin"
	case ".sql":
		return "sql"
	case ".html", ".htm":
		return "html"
	case ".css":
		return "css"
	case ".scss":
		return "scss"
	case ".json":
		return "json"
	case ".yaml", ".yml":
		return "yaml"
	case ".toml":
		return "toml"
	case ".xml":
		return "xml"
	case ".sh", ".bash":
		return "shell"
	case ".md":
		return "markdown"
	case ".dockerfile":
		return "dockerfile"
	default:
		// Check for Dockerfile without extension
		if filepath.Base(path) == "Dockerfile" {
			return "dockerfile"
		}
		if filepath.Base(path) == "Makefile" || filepath.Base(path) == "GNUmakefile" {
			return "makefile"
		}
		return ""
	}
}
