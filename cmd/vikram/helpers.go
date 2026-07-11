package main

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/Vatthu/vikram/pkg/bus"
	"github.com/Vatthu/vikram/pkg/config"
	"github.com/Vatthu/vikram/pkg/orchestrator"
	"github.com/Vatthu/vikram/pkg/providers"
	"github.com/Vatthu/vikram/pkg/state"
)
func copyDirectory(src, dst string) error {
	return filepath.Walk(src, func(path string, info os.FileInfo, err error) error {
		if err != nil {
			return err
		}

		relPath, err := filepath.Rel(src, path)
		if err != nil {
			return err
		}

		dstPath := filepath.Join(dst, relPath)

		if info.IsDir() {
			return os.MkdirAll(dstPath, info.Mode())
		}

		srcFile, err := os.Open(path)
		if err != nil {
			return err
		}
		defer srcFile.Close()

		dstFile, err := os.OpenFile(dstPath, os.O_WRONLY|os.O_CREATE|os.O_TRUNC, info.Mode())
		if err != nil {
			return err
		}
		defer dstFile.Close()

		_, err = io.Copy(dstFile, srcFile)
		return err
	})
}

func expandHome(path string) string {
	if strings.HasPrefix(path, "~") {
		home, err := os.UserHomeDir()
		if err == nil {
			return filepath.Join(home, path[1:])
		}
	}
	return path
}

// sanitizeOnboardingField strips newlines and control characters from a
// user-supplied onboarding string before it is written into MEMORY.md.
// MEMORY.md is loaded into the LLM system prompt on every request, so any
// injected markdown headings or instruction text would be interpreted by the
// model.  Single-line fields (aiName, aiRole, userName) are restricted to
// printable non-newline characters.  The multi-line userPrefs field allows
// newlines but strips NUL and other control characters.
func sanitizeOnboardingField(s string, allowNewlines bool) string {
	return strings.Map(func(r rune) rune {
		if r == '\x00' {
			return -1 // drop NUL bytes entirely
		}
		if !allowNewlines && (r == '\n' || r == '\r') {
			return ' '
		}
		if r < 0x20 && r != '\n' && r != '\r' && r != '\t' {
			return ' ' // replace other control chars with space
		}
		return r
	}, s)
}

func initMemory(workspace, aiName, aiRole, userName, userPrefs string) {
	memoryDir := filepath.Join(workspace, "memory")
	os.MkdirAll(memoryDir, 0700)
	memoryFile := filepath.Join(memoryDir, "MEMORY.md")

	// Sanitise all user-supplied strings to prevent markdown/prompt injection.
	safeName := sanitizeOnboardingField(aiName, false)
	safeRole := sanitizeOnboardingField(aiRole, false)
	safeUser := sanitizeOnboardingField(userName, false)
	safePrefs := sanitizeOnboardingField(userPrefs, true)

	memoryContent := fmt.Sprintf(`# Long-term Memory

This file stores important information that should persist across sessions.

## Core Identity (Soul)
- Name: %s
- Core Purpose: %s

## User Information
- Name: %s

## Preferences
- %s

## Important Notes
- Initialized configuration defaults.
`, safeName, safeRole, safeUser, safePrefs)

	_ = os.WriteFile(memoryFile, []byte(memoryContent), 0600)
}

func writePersonalizedBootstrapFiles(workspace, aiName, aiRole, userName, userPrefs string) {
	safeName := strings.TrimSpace(sanitizeOnboardingField(aiName, false))
	if safeName == "" {
		safeName = "V1"
	}
	safeRole := strings.TrimSpace(sanitizeOnboardingField(aiRole, false))
	if safeRole == "" {
		safeRole = "Your personal AI assistant"
	}
	safeUser := strings.TrimSpace(sanitizeOnboardingField(userName, false))
	if safeUser == "" {
		safeUser = "User"
	}
	safePrefs := strings.TrimSpace(sanitizeOnboardingField(userPrefs, true))
	if safePrefs == "" {
		safePrefs = "- Keep replies direct and natural.\n- Prefer acting like a present assistant, not a product brochure."
	} else if !strings.HasPrefix(safePrefs, "-") {
		safePrefs = "- " + strings.ReplaceAll(safePrefs, "\n", "\n- ")
	}

	files := map[string]string{
		"AGENT.md": fmt.Sprintf(`# Agent Instructions

You are %s, %s for %s.

## Operating Rules

- Act like a present, awake assistant for %s, not like a README or marketing page.
- When asked about yourself, answer in first person as %s and describe your current role, behavior, and practical capabilities.
- Use the identity and personality defined in IDENTITY.md, SOUL.md, and USER.md as the source of truth.
- Use tools when action is required; do not pretend that something was done.
- Keep replies direct, natural, and grounded in the current conversation.
`, safeName, safeRole, safeUser, safeUser, safeName),
		"IDENTITY.md": fmt.Sprintf(`# Identity

## Name
%s

## Role
%s

## Relationship
You assist %s directly on their machine and channels.

## How to Speak
- Speak like a real assistant in the room.
- Be clear, calm, practical, and concise.
- Do not default to product pitches, GitHub blurbs, or README-style summaries unless %s asks about the project itself.
`, safeName, safeRole, safeUser, safeUser),
		"SOUL.md": `# Soul

## Personality

- Alert and grounded
- Helpful without sounding generic
- Calm under pressure
- Honest about what is working, what is broken, and what you are doing next

## Values

- Protect the user's trust
- Prefer clear action over vague promises
- Stay practical and reality-based
- Do not slip into marketing language
`,
		"USER.md": fmt.Sprintf(`# User

## Primary Operator
- Name: %s

## Preferences
%s
`, safeUser, safePrefs),
		"TOOLS.md": `# Tools

## Guidance

- Use tools to do real work; do not claim an action happened unless a tool actually completed it.
- Prefer the smallest safe action that solves the user's request.
- If a tool fails, say what failed and what you will try next.
- Keep file and shell work grounded in the current workspace unless the user explicitly wants broader access.
`,
	}

	for name, content := range files {
		writeBootstrapFileIfTemplate(workspace, name, content)
	}
}

func writeBootstrapFileIfTemplate(workspace, name, content string) {
	targetPath := filepath.Join(workspace, name)

	existing, err := os.ReadFile(targetPath)
	switch {
	case err == nil:
		templateData, templateErr := embeddedFiles.ReadFile(filepath.Join("workspace", name))
		if templateErr == nil && string(existing) != string(templateData) {
			return
		}
	case os.IsNotExist(err):
		if mkErr := os.MkdirAll(filepath.Dir(targetPath), 0755); mkErr != nil {
			return
		}
	default:
		return
	}

	_ = os.WriteFile(targetPath, []byte(content), 0644)
}

func setProviderKey(cfg *config.Config, provider, key string) {
	switch provider {
	case "gemini":
		cfg.Providers.Gemini.APIKey = key
	case "openai":
		cfg.Providers.OpenAI.APIKey = key
	case "anthropic":
		cfg.Providers.Anthropic.APIKey = key
	case "groq":
		cfg.Providers.Groq.APIKey = key
	case "deepseek":
		cfg.Providers.DeepSeek.APIKey = key
	case "openrouter":
		cfg.Providers.OpenRouter.APIKey = key
	case "zhipu", "glm":
		cfg.Providers.Zhipu.APIKey = key
	case "moonshot":
		cfg.Providers.Moonshot.APIKey = key
	case "nvidia":
		cfg.Providers.Nvidia.APIKey = key
	case "vllm":
		cfg.Providers.VLLM.APIKey = key
	case "ollama":
		cfg.Providers.Ollama.APIKey = key
	case "github_copilot":
		cfg.Providers.GitHubCopilot.APIKey = key
	case "azure_openai", "azure":
		cfg.Providers.AzureOpenAI.APIKey = key
	case "mistral":
		cfg.Providers.Mistral.APIKey = key
	case "xai", "grok":
		cfg.Providers.XAI.APIKey = key
	case "cerebras":
		cfg.Providers.Cerebras.APIKey = key
	case "sambanova":
		cfg.Providers.SambaNova.APIKey = key
	case "github_models":
		cfg.Providers.GitHubModels.APIKey = key
	}
}

func setProviderAPIBase(cfg *config.Config, provider, apiBase string) {
	switch provider {
	case "gemini":
		cfg.Providers.Gemini.APIBase = apiBase
	case "openai":
		cfg.Providers.OpenAI.APIBase = apiBase
	case "anthropic":
		cfg.Providers.Anthropic.APIBase = apiBase
	case "groq":
		cfg.Providers.Groq.APIBase = apiBase
	case "deepseek":
		cfg.Providers.DeepSeek.APIBase = apiBase
	case "openrouter":
		cfg.Providers.OpenRouter.APIBase = apiBase
	case "zhipu", "glm":
		cfg.Providers.Zhipu.APIBase = apiBase
	case "moonshot":
		cfg.Providers.Moonshot.APIBase = apiBase
	case "nvidia":
		cfg.Providers.Nvidia.APIBase = apiBase
	case "vllm":
		cfg.Providers.VLLM.APIBase = apiBase
	case "ollama":
		cfg.Providers.Ollama.APIBase = apiBase
	case "github_copilot":
		cfg.Providers.GitHubCopilot.APIBase = apiBase
	case "mistral":
		cfg.Providers.Mistral.APIBase = apiBase
	case "xai", "grok":
		cfg.Providers.XAI.APIBase = apiBase
	case "cerebras":
		cfg.Providers.Cerebras.APIBase = apiBase
	case "sambanova":
		cfg.Providers.SambaNova.APIBase = apiBase
	case "github_models":
		cfg.Providers.GitHubModels.APIBase = apiBase
	}
}

func setProviderConnectMode(cfg *config.Config, provider, connectMode string) {
	switch provider {
	case "github_copilot", "copilot":
		cfg.Providers.GitHubCopilot.ConnectMode = connectMode
	}
}

func gatewayProviderConfigError(cfg *config.Config) error {
	providerName := strings.TrimSpace(cfg.Agents.Defaults.Provider)
	if providerName == "" {
		return nil
	}

	_, ready, hint := providerCredentialStatus(cfg, providerName)
	if ready {
		return nil
	}
	if hint == "" {
		hint = "Run  vikram onboard  or  vikram configure → Brain  to finish setup."
	}
	return fmt.Errorf("provider %q is not ready. %s", providerName, hint)
}

func copyEmbeddedToTarget(targetDir string) error {
	// Ensure target directory exists
	if err := os.MkdirAll(targetDir, 0755); err != nil {
		return fmt.Errorf("Failed to create target directory: %w", err)
	}

	// Walk through all files in embed.FS
	err := fs.WalkDir(embeddedFiles, "workspace", func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}

		// Skip directories
		if d.IsDir() {
			return nil
		}

		// Read embedded file
		data, err := embeddedFiles.ReadFile(path)
		if err != nil {
			return fmt.Errorf("Failed to read embedded file %s: %w", path, err)
		}

		newPath, err := filepath.Rel("workspace", path)
		if err != nil {
			return fmt.Errorf("Failed to get relative path for %s: %v\n", path, err)
		}

		// Build target file path
		targetPath := filepath.Join(targetDir, newPath)

		// Never clobber an existing workspace file. Users are expected to
		// customize these templates, so only seed missing files.
		if _, err := os.Stat(targetPath); err == nil {
			return nil
		} else if !os.IsNotExist(err) {
			return fmt.Errorf("Failed to stat existing file %s: %w", targetPath, err)
		}

		// Ensure target file's directory exists
		if err := os.MkdirAll(filepath.Dir(targetPath), 0755); err != nil {
			return fmt.Errorf("Failed to create directory %s: %w", filepath.Dir(targetPath), err)
		}

		// Write file
		if err := os.WriteFile(targetPath, data, 0644); err != nil {
			return fmt.Errorf("Failed to write file %s: %w", targetPath, err)
		}

		return nil
	})

	return err
}

func createWorkspaceTemplates(workspace string) {
	err := copyEmbeddedToTarget(workspace)
	if err != nil {
		fmt.Printf("Error copying workspace templates: %v\n", err)
	}
}

func agentCapabilitiesForRole(role string) []string {
	switch strings.ToLower(strings.TrimSpace(role)) {
	case "lead", "planner", "architect":
		return []string{"planning", "architecture", "integration"}
	case "engineer", "implementer", "coder":
		return []string{"implementation", "code_editing", "documentation"}
	case "reviewer", "critic":
		return []string{"code_review", "adversarial_review", "risk_analysis"}
	case "runner", "verifier":
		return []string{"verification", "test_analysis", "execution_review"}
	case "qa", "browser", "visual":
		return []string{"qa", "browser_review", "visual_review"}
	default:
		if strings.TrimSpace(role) == "" {
			return nil
		}
		return []string{strings.ToLower(strings.TrimSpace(role))}
	}
}

// callReviewer builds a review prompt and asks the reviewer model to evaluate
// a set of code changes.  The reviewer MUST be a different model than the
// implementer — independent review is the point.
func callReviewer(ctx context.Context, reviewer providers.LLMProvider, model string, req orchestrator.ChangeReviewRequest) (orchestrator.ChangeReviewResponse, error) {
	prompt := fmt.Sprintf(`You are a code reviewer evaluating changes made by another AI agent.

TASK OBJECTIVE: %s

CHANGES (diff):
%s

TEST OUTPUT:
%s

LINT ERRORS:
%s

Review the changes against the objective. Consider:
- Does this change actually address the objective?
- Are there scope creep or unrelated changes?
- Are there security concerns, bugs, or design issues?
- Does the code follow best practices?

Respond with ONLY a JSON object:
{
  "verdict": "APPROVE" | "CHANGES_REQUESTED" | "REJECT",
  "issues": ["issue 1", "issue 2"],
  "summary": "brief explanation"
}`, req.Objective, req.Diff, req.TestOutput, strings.Join(req.LintErrors, "\n"))

	messages := []providers.Message{
		{Role: "user", Content: prompt},
	}

	resp, err := reviewer.Chat(ctx, messages, nil, model, map[string]interface{}{
		"max_tokens":  1024,
		"temperature": 0.3,
	})
	if err != nil {
		return orchestrator.ChangeReviewResponse{
			TaskID:  req.TaskID,
			Verdict: orchestrator.ReviewVerdictApprove,
			Summary: fmt.Sprintf("Review unavailable: %v", err),
		}, nil // degrade gracefully — don't block the task on review failure
	}

	// Parse the JSON response.
	var result struct {
		Verdict string   `json:"verdict"`
		Issues  []string `json:"issues"`
		Summary string   `json:"summary"`
	}
	if err := json.Unmarshal([]byte(resp.Content), &result); err != nil {
		// Try to extract JSON from the response if the model added markdown fences.
		cleaned := strings.TrimSpace(resp.Content)
		cleaned = strings.TrimPrefix(cleaned, "```json")
		cleaned = strings.TrimPrefix(cleaned, "```")
		cleaned = strings.TrimSuffix(cleaned, "```")
		cleaned = strings.TrimSpace(cleaned)
		if err2 := json.Unmarshal([]byte(cleaned), &result); err2 != nil {
			return orchestrator.ChangeReviewResponse{
				TaskID:  req.TaskID,
				Verdict: orchestrator.ReviewVerdictApprove,
				Summary: fmt.Sprintf("Could not parse review response: %v", err),
			}, nil
		}
	}

	verdict := orchestrator.ChangeReviewVerdict(strings.ToUpper(strings.TrimSpace(result.Verdict)))
	if verdict != orchestrator.ReviewVerdictApprove &&
		verdict != orchestrator.ReviewVerdictChangesRequested &&
		verdict != orchestrator.ReviewVerdictReject {
		verdict = orchestrator.ReviewVerdictApprove
	}

	return orchestrator.ChangeReviewResponse{
		TaskID:  req.TaskID,
		Verdict: verdict,
		Issues:  result.Issues,
		Summary: result.Summary,
	}, nil
}

// agentCheckpoint stores enough state to resume an interrupted agent invocation.
type agentCheckpoint struct {
	TaskID      string    `json:"task_id"`
	Role        string    `json:"role"`
	Phase       string    `json:"phase"`
	LastSummary string    `json:"last_summary"`
	Timestamp   time.Time `json:"timestamp"`
}

// resumeIncompleteSessions scans for checkpoint files from a previous run
// and logs them.  The actual resume happens when the orchestrator replays
// the orchestrator workflow from its own checkpoints.
func resumeIncompleteSessions(workspaceRoot string) {
	tasksDir := filepath.Join(workspaceRoot, "tasks")
	entries, err := os.ReadDir(tasksDir)
	if err != nil {
		return
	}
	for _, entry := range entries {
		if !entry.IsDir() {
			continue
		}
		cp, err := loadCheckpoint(workspaceRoot, entry.Name())
		if err != nil {
			continue
		}
		fmt.Printf("📋 Incomplete session: %s (role=%s phase=%s at %s)\n",
			cp.TaskID, cp.Role, cp.Phase, cp.Timestamp.Format("15:04:05"))
	}
}

func saveCheckpoint(workspaceRoot, taskID, role, phase, summary string) {
	dir := filepath.Join(workspaceRoot, "tasks", taskID)
	os.MkdirAll(dir, 0700)
	cp := agentCheckpoint{
		TaskID:      taskID,
		Role:        role,
		Phase:       phase,
		LastSummary: summary,
		Timestamp:   time.Now(),
	}
	data, _ := json.Marshal(cp)
	os.WriteFile(filepath.Join(dir, "checkpoint.json"), data, 0600)
}

func loadCheckpoint(workspaceRoot, taskID string) (*agentCheckpoint, error) {
	path := filepath.Join(workspaceRoot, "tasks", taskID, "checkpoint.json")
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var cp agentCheckpoint
	if err := json.Unmarshal(data, &cp); err != nil {
		return nil, err
	}
	return &cp, nil
}

func sendTeamSummary(cfg *config.Config, msgBus *bus.MessageBus) {
	stateManager := state.NewManager(cfg.WorkspacePath())
	lastChannel := stateManager.GetLastChannel()
	if lastChannel == "" {
		return
	}
	parts := strings.SplitN(lastChannel, ":", 2)
	if len(parts) != 2 {
		return
	}

	var lines []string
	lines = append(lines, "📊 Vikram Team Summary")
	lines = append(lines, "")
	for _, a := range cfg.Agents.List {
		role := a.Role
		if role == "" {
			role = a.ID
		}
		provider := a.Provider
		if provider == "" {
			provider = "default"
		}
		limit := "unlimited"
		if a.MaxTokensPerDay > 0 {
			limit = fmt.Sprintf("%dK/day", a.MaxTokensPerDay/1000)
		}
		lines = append(lines, fmt.Sprintf("• %s (%s/%s) — budget: %s", role, provider, a.Model, limit))
	}
	lines = append(lines, "")
	lines = append(lines, fmt.Sprintf("Gateway: %s:%d", cfg.Gateway.Host, cfg.Gateway.Port))

	msgBus.PublishOutbound(bus.OutboundMessage{
		Channel: parts[0],
		ChatID:  parts[1],
		Content: strings.Join(lines, "\n"),
	})
}
