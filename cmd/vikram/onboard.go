package main

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/charmbracelet/huh"
	"github.com/charmbracelet/lipgloss"
	"github.com/Vatthu/vikram/pkg/config"
	"github.com/Vatthu/vikram/pkg/providers"
)

// ─── Styles ──────────────────────────────────────────────────────────────────

var (
	titleStyle   = lipgloss.NewStyle().Bold(true).Foreground(lipgloss.Color("12"))
	stepStyle    = lipgloss.NewStyle().Foreground(lipgloss.Color("8"))
	successStyle = lipgloss.NewStyle().Foreground(lipgloss.Color("10")).Bold(true)
	warnStyle    = lipgloss.NewStyle().Foreground(lipgloss.Color("11"))
	errorStyle   = lipgloss.NewStyle().Foreground(lipgloss.Color("9"))
	boxStyle     = lipgloss.NewStyle().
			Border(lipgloss.RoundedBorder()).
			BorderForeground(lipgloss.Color("12")).
			Padding(1, 3).
			MarginTop(1).
			MarginBottom(1)
	demoBoxStyle = lipgloss.NewStyle().
			Border(lipgloss.RoundedBorder()).
			BorderForeground(lipgloss.Color("10")).
			Padding(1, 3).
			MarginTop(1).
			MarginBottom(1)
)

// ─── Entry point ─────────────────────────────────────────────────────────────

func onboardCmd() {
	args := os.Args[2:]

	// Fast-exit flags that don't need the wizard.
	for _, arg := range args {
		if arg == "--refresh" {
			onboardRefresh()
			return
		}
	}

	// --auto mode: non-interactive setup via flags.
	//   vikram onboard --auto --provider gemini --api-key sk-...
	//   vikram onboard --auto --provider gemini --api-key sk-... --model gemini-3.1-pro-preview
	//   vikram onboard --auto --provider gemini --api-key sk-... --workspace /my/path
	//   vikram onboard --auto --provider gemini --api-key sk-... --skip-test
	autoMode := false
	for _, arg := range args {
		if arg == "--auto" {
			autoMode = true
			break
		}
	}
	if autoMode {
		onboardAuto(args)
		return
	}
	configPath := getConfigPath()
	cfg := config.DefaultConfig()
	hasExistingConfig := false
	if _, err := os.Stat(configPath); err == nil {
		if loaded, err := loadConfig(); err == nil {
			cfg = loaded
			hasExistingConfig = true
		}
	}

	printOnboardWelcome()

	if hasExistingConfig {
		choice := onboardExistingConfigChoice(cfg, configPath)
		if choice == "" {
			return
		}
		if choice == configHandlingReset {
			cfg = config.DefaultConfig()
		}
	}

	if !onboardSecurityAcknowledgement() {
		return
	}

	mode := onboardChooseMode()
	if mode == "" {
		return
	}

	target := onboardChooseTarget(cfg)
	if target == "" {
		return
	}
	applySetupTargetDefaults(cfg, target)

	switch mode {
	case onboardModeQuick:
		runQuickStartOnboarding(cfg, configPath)
	case onboardModeManual:
		runManualOnboarding(cfg, configPath)
	}
}

func runQuickStartOnboarding(cfg *config.Config, configPath string) {
	fmt.Println(stepStyle.Render("\n  Step 1 of 4 — Workspace"))
	if !onboardWorkspace(cfg) {
		return
	}

	fmt.Println(stepStyle.Render("\n  Step 2 of 4 — AI provider"))
	providerID, providerURL := onboardProvider(cfg)
	if providerID == "" {
		return
	}

	fmt.Println(stepStyle.Render("\n  Step 3 of 4 — Provider access"))
	if !onboardAPIKey(cfg, providerID, providerURL) {
		return
	}
	if !onboardModel(cfg, providerID) {
		return
	}
	if providerSupportsLiveValidation(providerID) {
		fmt.Println(stepStyle.Render("\n  Checking provider connection…"))
		if !onboardValidateProvider(cfg, providerID) {
			return
		}
	}

	aiName, userName := "", ""
	fmt.Println(stepStyle.Render("\n  Step 4 of 5 — Founder alerts (optional)"))
	onboardFounderAlerts(cfg)

	fmt.Println(stepStyle.Render("\n  Step 5 of 5 — Save and test"))
	if !onboardSaveAndTest(cfg, configPath, aiName, userName) {
		return
	}
	printOnboardSuccess(cfg, configPath, aiName)
}

func runManualOnboarding(cfg *config.Config, configPath string) {
	fmt.Println(stepStyle.Render("\n  Step 1 of 9 — Workspace and file access"))
	configureWorkspace(cfg)

	fmt.Println(stepStyle.Render("\n  Step 2 of 9 — AI provider"))
	providerID, providerURL := onboardProvider(cfg)
	if providerID == "" {
		return
	}

	fmt.Println(stepStyle.Render("\n  Step 3 of 9 — Provider access"))
	if !onboardAPIKey(cfg, providerID, providerURL) {
		return
	}
	if !onboardModel(cfg, providerID) {
		return
	}
	if providerSupportsLiveValidation(providerID) {
		fmt.Println(stepStyle.Render("\n  Checking provider connection…"))
		if !onboardValidateProvider(cfg, providerID) {
			return
		}
	}

	fmt.Println(stepStyle.Render("\n  Step 4 of 9 — High availability"))
	onboardCouncil(cfg)

	fmt.Println(stepStyle.Render("\n  Step 5 of 9 — First-run bootstrap"))
	onboardBootstrap(cfg)
	aiName, userName := "", ""

	fmt.Println(stepStyle.Render("\n  Step 6 of 9 — Web tools"))
	onboardTools(cfg)

	fmt.Println(stepStyle.Render("\n  Step 7 of 9 — Permissions"))
	configurePermissions(cfg)

	fmt.Println(stepStyle.Render("\n  Step 8 of 9 — Gateway and multi-device access"))
	configureGateway(cfg)

	var configureChannelsNow bool
	form := huh.NewForm(
		huh.NewGroup(
			huh.NewConfirm().
				Title("Do you want to configure messaging channels now?").
				Description("You can always do this later with vikram configure.").
				Affirmative("Yes").
				Negative("Later").
				Value(&configureChannelsNow),
		),
	)
	if err := form.Run(); err == nil && configureChannelsNow {
		fmt.Println(stepStyle.Render("\n  Step 9 of 9 — Channels"))
		configureChannels(cfg)
	}

	fmt.Println(stepStyle.Render("\n  Saving and running a live test…"))
	if !onboardSaveAndTest(cfg, configPath, aiName, userName) {
		return
	}
	printOnboardSuccess(cfg, configPath, aiName)
}

func onboardCouncil(cfg *config.Config) {
	var enableCouncil bool
	form := huh.NewForm(
		huh.NewGroup(
			huh.NewConfirm().
				Title("Enable High Availability (The Council)?").
				Description("Automatically fail over to a fallback model if the primary provider becomes unavailable.").
				Affirmative("Yes").
				Negative("No").
				Value(&enableCouncil),
		),
	)
	if err := form.Run(); err != nil {
		fmt.Println("Setup cancelled.")
		return
	}
	if enableCouncil {
		configureCouncil(cfg)
		return
	}
	cfg.Council.Enabled = false
	cfg.Council.Persona = ""
	cfg.Council.Primary = ""
	cfg.Council.PrimaryModel = ""
	cfg.Council.Fallback = ""
	cfg.Council.FallbackModel = ""
}

func onboardFounderAlerts(cfg *config.Config) {
	var connectTelegram bool
	form := huh.NewForm(
		huh.NewGroup(
			huh.NewConfirm().
				Title("Connect Telegram for founder approvals and blocker alerts?").
				Description("You can skip this now and configure it later with vikram configure.").
				Affirmative("Yes").
				Negative("Later").
				Value(&connectTelegram),
		),
	)
	if err := form.Run(); err != nil || !connectTelegram {
		return
	}
	configureTelegramChannel(cfg)
}

// ─── Welcome screen ───────────────────────────────────────────────────────────

func printOnboardWelcome() {
	welcome := `
 Welcome to Vikram 🤖

 Vikram is your personal AI assistant that runs on your own computer.
 This wizard will get you to a working setup first, then you can tune
 the details later with  vikram configure.

 Choose Quick Start for the fastest path.
 Choose Manual if you want to review security, gateway, and permissions now.

 Press Ctrl+C at any time to quit.`

	fmt.Println(boxStyle.Render(titleStyle.Render(welcome)))
}

// ─── Step 1: Workspace ───────────────────────────────────────────────────────

func onboardWorkspace(cfg *config.Config) bool {
	defaultPath := config.DefaultWorkspaceDir()

	recommended := fmt.Sprintf("%s  %s", defaultPath, stepStyle.Render("← recommended"))

	var choice string
	form := huh.NewForm(
		huh.NewGroup(
			huh.NewSelect[string]().
				Title("Where should the AI store its memory and workspace files?").
				Description("This folder holds conversation memory, skills, and workspace files.").
				Options(
					huh.NewOption(recommended, "default"),
					huh.NewOption("Current folder  "+stepStyle.Render("(./workspace)"), "current"),
					huh.NewOption("Custom path…", "custom"),
				).
				Value(&choice),
		),
	)

	if err := form.Run(); err != nil {
		fmt.Println("Setup cancelled.")
		return false
	}

	switch choice {
	case "default":
		cfg.Workspace.Path = defaultPath
	case "current":
		cwd, _ := os.Getwd()
		cfg.Workspace.Path = filepath.Join(cwd, "workspace")
	case "custom":
		var customPath string
		customForm := huh.NewForm(
			huh.NewGroup(
				huh.NewInput().
					Title("Enter the full path for the workspace:").
					Placeholder(defaultPath).
					Value(&customPath),
			),
		)
		if err := customForm.Run(); err != nil || strings.TrimSpace(customPath) == "" {
			cfg.Workspace.Path = defaultPath
		} else {
			cfg.Workspace.Path = expandHome(strings.TrimSpace(customPath))
		}
	}

	cfg.Agents.Defaults.Workspace = cfg.Workspace.Path
	fmt.Printf("\n  %s Workspace: %s\n", successStyle.Render("✓"), cfg.Workspace.Path)
	return true
}

// ─── Step 2: Provider selection ──────────────────────────────────────────────

// onboardProvider returns (providerID, keyURL). Empty providerID means cancelled.
func onboardProvider(cfg *config.Config) (string, string) {
	var providerID string

	options := make([]huh.Option[string], 0, len(traditional))
	for _, p := range traditional {
		label := fmt.Sprintf("%-14s  %s", p.name, stepStyle.Render(p.desc))
		options = append(options, huh.NewOption(label, p.id))
	}

	form := huh.NewForm(
		huh.NewGroup(
			huh.NewSelect[string]().
				Title("Which AI provider would you like to use?").
				Description("Not sure? Pick Google Gemini — it has a free tier and works great.").
				Options(options...).
				Value(&providerID),
		),
	)

	if err := form.Run(); err != nil {
		fmt.Println("Setup cancelled.")
		return "", ""
	}

	// Find the key URL for the selected provider.
	var keyURL string
	for _, p := range traditional {
		if p.id == providerID {
			keyURL = p.keyURL
			break
		}
	}

	cfg.Agents.Defaults.Provider = providerID

	// Set default model when we have one; otherwise the next step will ask.
	if models, ok := providerModels[providerID]; ok && len(models) > 0 {
		cfg.Agents.Defaults.Model = models[0]
	} else {
		cfg.Agents.Defaults.Model = ""
	}

	modelSummary := cfg.Agents.Defaults.Model
	if strings.TrimSpace(modelSummary) == "" {
		modelSummary = "choose next"
	}
	fmt.Printf("\n  %s Provider: %s  (model: %s)\n",
		successStyle.Render("✓"), providerID, modelSummary)
	return providerID, keyURL
}

// ─── Step 3: Provider access ─────────────────────────────────────────────────

func onboardAPIKey(cfg *config.Config, providerID, keyURL string) bool {
	// Keyless and special providers have their own flows.
	switch providerID {
	case "vertex":
		return onboardVertexAuth(cfg)
	case "bedrock":
		return onboardBedrockAuth()
	case "azure_openai":
		return onboardAzureAuth(cfg)
	case "ollama":
		return onboardOllamaConfig(cfg)
	case "vllm":
		return onboardVLLMConfig(cfg)
	case "github_copilot":
		return onboardGitHubCopilotConfig(cfg)
	}
	return onboardAPIKeyWithKey(cfg, providerID, keyURL)
}

func onboardModel(cfg *config.Config, providerID string) bool {
	if strings.TrimSpace(cfg.Agents.Defaults.Model) != "" {
		return true
	}

	var model string
	form := huh.NewForm(
		huh.NewGroup(
			huh.NewInput().
				Title(fmt.Sprintf("Enter the model name for %s:", providerID)).
				Description("Use the exact model or deployment name from your provider dashboard.").
				Value(&model),
		),
	)

	if err := form.Run(); err != nil {
		fmt.Println("Setup cancelled.")
		return false
	}

	model = strings.TrimSpace(model)
	if model == "" {
		fmt.Printf("  %s A model name is required for provider %q.\n", errorStyle.Render("✗"), providerID)
		return false
	}

	cfg.Agents.Defaults.Model = model
	fmt.Printf("\n  %s Model: %s\n", successStyle.Render("✓"), cfg.Agents.Defaults.Model)
	return true
}

func providerSupportsLiveValidation(providerID string) bool {
	switch strings.ToLower(strings.TrimSpace(providerID)) {
	case "vertex", "vertex_ai", "vertexai", "bedrock", "aws_bedrock", "aws":
		return false
	default:
		return true
	}
}

// onboardVertexAuth configures Google Vertex AI (gcloud auth, no API key).
func onboardVertexAuth(cfg *config.Config) bool {
	fmt.Printf("\n  %s Google Vertex AI uses your gcloud credentials — no API key needed.\n\n",
		warnStyle.Render("→"))
	fmt.Printf("  If you haven't already, run:  %s\n\n",
		titleStyle.Render("gcloud auth application-default login"))

	var projectID string
	form := huh.NewForm(huh.NewGroup(
		huh.NewInput().
			Title("Enter your Google Cloud Project ID:").
			Placeholder("my-gcp-project").
			Validate(func(s string) error {
				if strings.TrimSpace(s) == "" {
					return fmt.Errorf("project ID cannot be empty")
				}
				return nil
			}).
			Value(&projectID),
	))
	if err := form.Run(); err != nil {
		fmt.Println("Setup cancelled.")
		return false
	}
	cfg.Providers.Vertex.ProjectID = strings.TrimSpace(projectID)
	if cfg.Providers.Vertex.Location == "" {
		cfg.Providers.Vertex.Location = "us-central1"
	}
	fmt.Printf("  %s Vertex AI configured (project: %s, region: %s)\n",
		successStyle.Render("✓"), cfg.Providers.Vertex.ProjectID, cfg.Providers.Vertex.Location)
	return true
}

// onboardBedrockAuth configures AWS Bedrock (uses ~/.aws/credentials, no API key).
func onboardBedrockAuth() bool {
	fmt.Printf("\n  %s AWS Bedrock uses your AWS credentials — no API key needed.\n\n",
		warnStyle.Render("→"))
	fmt.Printf("  Make sure  %s  is configured,\n  or set  %s  environment variables.\n\n",
		titleStyle.Render("~/.aws/credentials"),
		titleStyle.Render("AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY"))
	fmt.Printf("  %s AWS Bedrock configured (reads credentials from environment)\n",
		successStyle.Render("✓"))
	return true
}

// onboardAzureAuth configures Azure OpenAI (endpoint + deployment + api_key).
func onboardAzureAuth(cfg *config.Config) bool {
	fmt.Printf("\n  %s  Get your endpoint and key from: %s\n\n",
		warnStyle.Render("→"), titleStyle.Render("https://portal.azure.com"))

	var endpoint, deployment, apiKey string
	form := huh.NewForm(huh.NewGroup(
		huh.NewInput().
			Title("Azure OpenAI Endpoint:").
			Placeholder("https://mycompany.openai.azure.com").
			Validate(func(s string) error {
				if strings.TrimSpace(s) == "" {
					return fmt.Errorf("endpoint cannot be empty")
				}
				return nil
			}).
			Value(&endpoint),
		huh.NewInput().
			Title("Deployment Name:").
			Placeholder("gpt-4o-prod").
			Validate(func(s string) error {
				if strings.TrimSpace(s) == "" {
					return fmt.Errorf("deployment cannot be empty")
				}
				return nil
			}).
			Value(&deployment),
		huh.NewInput().
			Title("API Key:").
			Password(true).
			Validate(func(s string) error {
				if strings.TrimSpace(s) == "" {
					return fmt.Errorf("API key cannot be empty")
				}
				return nil
			}).
			Value(&apiKey),
	))
	if err := form.Run(); err != nil {
		fmt.Println("Setup cancelled.")
		return false
	}
	cfg.Providers.AzureOpenAI.Endpoint = strings.TrimSpace(endpoint)
	cfg.Providers.AzureOpenAI.Deployment = strings.TrimSpace(deployment)
	cfg.Providers.AzureOpenAI.APIKey = strings.TrimSpace(apiKey)
	// Azure model is the deployment name.
	cfg.Agents.Defaults.Model = strings.TrimSpace(deployment)

	fmt.Printf("  %s Azure OpenAI configured (endpoint: %s, deployment: %s)\n",
		successStyle.Render("✓"), cfg.Providers.AzureOpenAI.Endpoint, cfg.Providers.AzureOpenAI.Deployment)
	return true
}

func onboardOllamaConfig(cfg *config.Config) bool {
	fmt.Printf("\n  %s Ollama runs locally and does not require an API key.\n\n",
		warnStyle.Render("→"))

	apiBase := strings.TrimSpace(cfg.Providers.Ollama.APIBase)
	if apiBase == "" {
		apiBase = defaultProviderAPIBase("ollama")
	}

	form := huh.NewForm(huh.NewGroup(
		huh.NewInput().
			Title("Ollama API Base:").
			Placeholder(defaultProviderAPIBase("ollama")).
			Value(&apiBase),
	))
	if err := form.Run(); err != nil {
		fmt.Println("Setup cancelled.")
		return false
	}

	apiBase = strings.TrimSpace(apiBase)
	if apiBase == "" {
		apiBase = defaultProviderAPIBase("ollama")
	}
	cfg.Providers.Ollama.APIBase = apiBase
	fmt.Printf("  %s Ollama endpoint: %s\n", successStyle.Render("✓"), cfg.Providers.Ollama.APIBase)
	return true
}

func onboardVLLMConfig(cfg *config.Config) bool {
	fmt.Printf("\n  %s vLLM uses an OpenAI-compatible endpoint. API key is optional.\n\n",
		warnStyle.Render("→"))

	apiBase := strings.TrimSpace(cfg.Providers.VLLM.APIBase)
	if apiBase == "" {
		apiBase = defaultProviderAPIBase("vllm")
	}
	apiKey := cfg.Providers.VLLM.APIKey

	form := huh.NewForm(huh.NewGroup(
		huh.NewInput().
			Title("vLLM API Base:").
			Placeholder(defaultProviderAPIBase("vllm")).
			Value(&apiBase),
		huh.NewInput().
			Title("Optional API Key:").
			Password(true).
			Value(&apiKey),
	))
	if err := form.Run(); err != nil {
		fmt.Println("Setup cancelled.")
		return false
	}

	apiBase = strings.TrimSpace(apiBase)
	if apiBase == "" {
		apiBase = defaultProviderAPIBase("vllm")
	}
	cfg.Providers.VLLM.APIBase = apiBase
	cfg.Providers.VLLM.APIKey = strings.TrimSpace(apiKey)
	fmt.Printf("  %s vLLM endpoint: %s\n", successStyle.Render("✓"), cfg.Providers.VLLM.APIBase)
	return true
}

func onboardGitHubCopilotConfig(cfg *config.Config) bool {
	fmt.Printf("\n  %s GitHub Copilot uses your local Copilot auth and does not require an API key.\n\n",
		warnStyle.Render("→"))

	connectMode := strings.TrimSpace(cfg.Providers.GitHubCopilot.ConnectMode)
	if connectMode == "" {
		connectMode = "stdio"
	}
	target := strings.TrimSpace(cfg.Providers.GitHubCopilot.APIBase)
	if target == "" {
		target = defaultGitHubCopilotTarget(connectMode)
	}

	form := huh.NewForm(huh.NewGroup(
		huh.NewSelect[string]().
			Title("Connection Mode:").
			Options(
				huh.NewOption("stdio (run local `copilot` command)", "stdio"),
				huh.NewOption("gRPC bridge", "grpc"),
			).
			Value(&connectMode),
		huh.NewInput().
			Title("CLI command or bridge address:").
			Description("For stdio use a command name/path. For gRPC use host:port or URL.").
			Value(&target),
	))
	if err := form.Run(); err != nil {
		fmt.Println("Setup cancelled.")
		return false
	}

	connectMode = strings.TrimSpace(connectMode)
	if connectMode == "" {
		connectMode = "stdio"
	}
	target = strings.TrimSpace(target)
	if target == "" {
		target = defaultGitHubCopilotTarget(connectMode)
	}
	cfg.Providers.GitHubCopilot.ConnectMode = connectMode
	cfg.Providers.GitHubCopilot.APIBase = target
	fmt.Printf("  %s GitHub Copilot: %s via %s\n", successStyle.Render("✓"), connectMode, target)
	return true
}

// onboardAPIKeyWithKey handles the standard API-key flow.
func onboardAPIKeyWithKey(cfg *config.Config, providerID, keyURL string) bool {
	fmt.Printf("\n  %s  Get your free API key at: %s\n\n",
		warnStyle.Render("→"), titleStyle.Render(keyURL))

	for attempt := 1; attempt <= 3; attempt++ {
		var apiKey string
		form := huh.NewForm(
			huh.NewGroup(
				huh.NewInput().
					Title("Paste your API key:").
					Description("The key will be stored only on this machine.").
					Password(true).
					Validate(func(s string) error {
						if strings.TrimSpace(s) == "" {
							return fmt.Errorf("API key cannot be empty")
						}
						return nil
					}).
					Value(&apiKey),
			),
		)

		if err := form.Run(); err != nil {
			fmt.Println("Setup cancelled.")
			return false
		}

		apiKey = strings.TrimSpace(apiKey)
		setProviderKey(cfg, providerID, apiKey)

		if strings.TrimSpace(cfg.Agents.Defaults.Model) == "" {
			fmt.Printf("  %s API key saved. We'll test it after you choose a model.\n", successStyle.Render("✓"))
			return true
		}

		// Live validation.
		var validationErr error
		withSpinner("Testing connection to "+providerID+"…", func() {
			validationErr = validateProviderKey(cfg)
		})

		if validationErr == nil {
			fmt.Printf("  %s Connected! The API key works.\n", successStyle.Render("✓"))
			return true
		}

		if attempt < 3 {
			fmt.Printf("  %s Could not connect: %s\n", errorStyle.Render("✗"), simplifyProviderErrorFor(providerID, validationErr))
			fmt.Printf("  %s %s\n", warnStyle.Render("→"), providerConnectionHint(cfg, providerID))
			fmt.Printf("  %s (attempt %d/3) — please check the key and try again\n\n", warnStyle.Render("→"), attempt)
		} else {
			fmt.Printf("  %s Could not validate the key after 3 attempts: %s\n",
				errorStyle.Render("✗"), simplifyProviderErrorFor(providerID, validationErr))
			fmt.Printf("  %s %s\n", warnStyle.Render("→"), providerConnectionHint(cfg, providerID))

			var skip string
			skipForm := huh.NewForm(
				huh.NewGroup(
					huh.NewSelect[string]().
						Title("What would you like to do?").
						Options(
							huh.NewOption("Continue anyway  "+stepStyle.Render("(you can fix this later with: vikram configure)"), "continue"),
							huh.NewOption("Quit and fix the key", "quit"),
						).
						Value(&skip),
				),
			)
			_ = skipForm.Run()
			if skip == "quit" {
				fmt.Println("\nRun  vikram onboard  again when you have the correct key.")
				return false
			}
			// User chose to continue despite failure.
			return true
		}
	}
	return true
}

func onboardValidateProvider(cfg *config.Config, providerID string) bool {
	var validationErr error
	withSpinner("Testing connection to "+providerID+"…", func() {
		validationErr = validateProviderKey(cfg)
	})

	if validationErr == nil {
		fmt.Printf("  %s Connected! The provider is working.\n", successStyle.Render("✓"))
		return true
	}

	fmt.Printf("  %s Could not validate %s: %s\n",
		errorStyle.Render("✗"), providerID, simplifyProviderErrorFor(providerID, validationErr))
	fmt.Printf("  %s %s\n", warnStyle.Render("→"), providerConnectionHint(cfg, providerID))

	var choice string
	form := huh.NewForm(
		huh.NewGroup(
			huh.NewSelect[string]().
				Title("What would you like to do?").
				Options(
					huh.NewOption("Continue anyway  "+stepStyle.Render("(you can fix this later with: vikram configure)"), "continue"),
					huh.NewOption("Quit and fix it now", "quit"),
				).
				Value(&choice),
		),
	)
	_ = form.Run()
	return choice != "quit"
}

// validateProviderKey sends a minimal test message to the configured provider.
func validateProviderKey(cfg *config.Config) error {
	provider, err := providers.CreateProvider(cfg)
	if err != nil {
		return err
	}

	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()

	msgs := []providers.Message{
		{Role: "user", Content: "Say \"OK\" and nothing else."},
	}

	opts := map[string]interface{}{
		"max_tokens":  10,
		"temperature": 0.0,
	}

	_, err = provider.Chat(ctx, msgs, nil, cfg.Agents.Defaults.Model, opts)
	return err
}

// simplifyProviderError strips verbose HTTP noise for end-users.
func simplifyProviderError(err error) string {
	if err == nil {
		return ""
	}
	msg := err.Error()
	switch {
	case strings.Contains(msg, "401") || strings.Contains(msg, "403") ||
		strings.Contains(strings.ToLower(msg), "invalid api key") ||
		strings.Contains(strings.ToLower(msg), "api key not valid") ||
		strings.Contains(strings.ToLower(msg), "not valid") ||
		strings.Contains(msg, "Unauthorized"):
		return "Invalid API key — double-check what you pasted."
	case strings.Contains(msg, "429") || strings.Contains(msg, "quota"):
		return "Rate limit or quota exceeded. Wait a moment or check your plan."
	case strings.Contains(msg, "timeout") || strings.Contains(msg, "context deadline"):
		return "Request timed out. Check your internet connection."
	case strings.Contains(msg, "no such host") || strings.Contains(msg, "dial"):
		return "Cannot reach the server. Check your internet connection."
	default:
		if len(msg) > 120 {
			return msg[:120] + "…"
		}
		return msg
	}
}

func simplifyProviderErrorFor(providerID string, err error) string {
	msg := simplifyProviderError(err)
	switch strings.ToLower(strings.TrimSpace(providerID)) {
	case "ollama", "vllm":
		if msg == "Cannot reach the server. Check your internet connection." {
			return "Cannot reach the configured local endpoint."
		}
	case "github_copilot", "copilot":
		if msg == "Cannot reach the server. Check your internet connection." {
			return "Cannot reach the configured Copilot bridge or local CLI worker."
		}
	}
	return msg
}

func providerConnectionHint(cfg *config.Config, providerID string) string {
	switch strings.ToLower(strings.TrimSpace(providerID)) {
	case "ollama":
		apiBase := strings.TrimSpace(cfg.Providers.Ollama.APIBase)
		if apiBase == "" {
			apiBase = defaultProviderAPIBase("ollama")
		}
		return fmt.Sprintf("Start Ollama and make sure %s is reachable.", apiBase)
	case "vllm":
		apiBase := strings.TrimSpace(cfg.Providers.VLLM.APIBase)
		if apiBase == "" {
			apiBase = defaultProviderAPIBase("vllm")
		}
		return fmt.Sprintf("Start your vLLM server and make sure %s is reachable.", apiBase)
	case "github_copilot", "copilot":
		connectMode := strings.TrimSpace(cfg.Providers.GitHubCopilot.ConnectMode)
		if connectMode == "" {
			connectMode = "stdio"
		}
		target := strings.TrimSpace(cfg.Providers.GitHubCopilot.APIBase)
		if target == "" {
			target = defaultGitHubCopilotTarget(connectMode)
		}
		if connectMode == "stdio" {
			return fmt.Sprintf("Install/authenticate the Copilot CLI and make sure %q is runnable.", target)
		}
		return fmt.Sprintf("Start the Copilot bridge and make sure %s is reachable.", target)
	default:
		if providerNeedsAPIKey(providerID) {
			return "Check your API key and internet connection."
		}
		return "Check the provider endpoint and your network connection."
	}
}

// ─── Step 4: Identity ────────────────────────────────────────────────────────

func onboardIdentity(cfg *config.Config) (string, string) {
	aiName := "V1"
	userName := "User"

	form := huh.NewForm(
		huh.NewGroup(
			huh.NewInput().
				Title("What should we call your AI assistant?").
				Description("This is the name it will use to introduce itself.").
				Placeholder("V1").
				Value(&aiName),
			huh.NewInput().
				Title("What should the AI call you?").
				Placeholder("User").
				Value(&userName),
		),
	)

	if err := form.Run(); err != nil {
		return aiName, userName // Keep defaults on cancel.
	}

	if strings.TrimSpace(aiName) == "" {
		aiName = "V1"
	}
	if strings.TrimSpace(userName) == "" {
		userName = "User"
	}

	aiName = sanitizeOnboardingField(aiName, false)
	userName = sanitizeOnboardingField(userName, false)

	fmt.Printf("  %s AI name: %s   Your name: %s\n", successStyle.Render("✓"), aiName, userName)
	// Return both names; memory init happens after workspace templates are seeded.
	return aiName, userName
}

func onboardBootstrap(cfg *config.Config) {
	fmt.Printf("  %s Fresh workspaces now start in bootstrap mode.\n", successStyle.Render("✓"))
	fmt.Printf("  %s On the first private chat, the assistant should ask who it is, what to call you,\n", stepStyle.Render("→"))
	fmt.Printf("    and what tone and role it should keep before it settles into a persona.\n")
}

// ─── Step 5: Tools ───────────────────────────────────────────────────────────

func onboardTools(cfg *config.Config) {
	var enableSearch bool

	form := huh.NewForm(
		huh.NewGroup(
			huh.NewConfirm().
				Title("Enable web search?").
				Description("Uses DuckDuckGo — completely free, no API key required.").
				Affirmative("Yes, enable web search").
				Negative("Skip for now").
				Value(&enableSearch),
		),
	)

	if err := form.Run(); err != nil {
		return
	}

	if enableSearch {
		cfg.Tools.Web.DuckDuckGo.Enabled = true
		fmt.Printf("  %s Web search enabled (DuckDuckGo)\n", successStyle.Render("✓"))
	} else {
		fmt.Printf("  %s Web search skipped  %s\n", warnStyle.Render("○"), stepStyle.Render("(enable later with: vikram configure)"))
	}
}

// ─── Step 6: Save + live test ─────────────────────────────────────────────────

func onboardSaveAndTest(cfg *config.Config, configPath string, aiName string, userName string) bool {
	if err := config.SaveConfig(configPath, cfg); err != nil {
		fmt.Printf("  %s Could not save config: %v\n", errorStyle.Render("✗"), err)
		return false
	}

	// Seed workspace template files first, then write identity memory on top.
	workspacePath := cfg.WorkspacePath()
	createWorkspaceTemplates(workspacePath)
	if workspacePath != "" {
		if workspaceNeedsBootstrap(workspacePath) {
			initMemory(workspacePath, "", "Identity and user profile will be learned during bootstrap", "", "")
		} else {
			const aiRole = "Your personal AI assistant"
			initMemory(workspacePath, aiName, aiRole, userName, "")
		}
	}

	fmt.Printf("  %s Config saved to: %s\n", successStyle.Render("✓"), configPath)
	if cfg.V1API.Enabled {
		fmt.Printf("  %s Remote API: %s  (key: %s)\n", successStyle.Render("✓"), cfg.V1API.Addr, maskKey(cfg.V1API.APIKey))
	}

	// Run live test.
	fmt.Printf("\n  %s\n\n", titleStyle.Render("Sending a live test message to your AI…"))

	var response string
	var testErr error

	withSpinner("Waiting for response…", func() {
		response, testErr = runOnboardTestMessage(cfg, aiName, workspacePath)
	})

	if testErr != nil {
		fmt.Printf("  %s Live test failed: %s\n", warnStyle.Render("⚠"), simplifyProviderErrorFor(cfg.Agents.Defaults.Provider, testErr))
		fmt.Printf("  %s %s\n", stepStyle.Render("→"), providerConnectionHint(cfg, cfg.Agents.Defaults.Provider))
		fmt.Printf("  %s This is usually fine — your provider settings were already saved.\n", stepStyle.Render("→"))
		fmt.Printf("  %s Run  vikram doctor  to check your setup anytime.\n\n", stepStyle.Render("→"))
		return true
	}

	if aiName == "" {
		aiName = "AI"
	}

	demoContent := fmt.Sprintf("  🤖 %s says:\n\n  %s", aiName, wrapText(response, 64, "  "))
	fmt.Println(demoBoxStyle.Render(demoContent))

	return true
}

// runOnboardTestMessage sends a friendly intro message and returns the reply.
// aiName is used in the system prompt so the AI introduces itself by name.
func runOnboardTestMessage(cfg *config.Config, aiName string, workspacePath string) (string, error) {
	provider, err := providers.CreateProvider(cfg)
	if err != nil {
		return "", err
	}

	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()

	var msgs []providers.Message
	if workspaceNeedsBootstrap(workspacePath) {
		msgs = []providers.Message{
			{Role: "system", Content: "You are a newly installed AI assistant in bootstrap mode. Keep your response to 1-2 friendly sentences. Do not claim a fixed identity yet."},
			{Role: "user", Content: "Say hello and explain that you will learn who you should be from the operator on first chat."},
		}
	} else {
		if aiName == "" {
			aiName = "V1"
		}
		msgs = []providers.Message{
			{Role: "system", Content: fmt.Sprintf("You are %s, a helpful personal AI assistant. Keep your response to 1-2 friendly sentences.", aiName)},
			{Role: "user", Content: "Say hello and introduce yourself! Tell me one thing you can help me with."},
		}
	}

	opts := map[string]interface{}{
		"max_tokens":  120,
		"temperature": 0.7,
	}

	resp, err := provider.Chat(ctx, msgs, nil, cfg.Agents.Defaults.Model, opts)
	if err != nil {
		return "", err
	}

	return strings.TrimSpace(resp.Content), nil
}

// ─── Success screen ───────────────────────────────────────────────────────────

func printOnboardSuccess(cfg *config.Config, configPath string, aiName string) {
	bootstrapPending := workspaceNeedsBootstrap(cfg.WorkspacePath())
	if aiName == "" && !bootstrapPending {
		aiName = "V1"
	}

	headline := aiName
	if bootstrapPending {
		headline = "Bootstrap mode"
	}

	successContent := fmt.Sprintf(`
 🎉  %s is ready!

 Start chatting now:

   vikram agent                         ← interactive chat
   vikram agent -m "your question"      ← single question

 Try these demo prompts:

   vikram agent -m "Who are you and what can you do?"
   vikram agent -m "Search the web for today's top AI news"
   vikram agent -m "Create a file called hello.txt saying Hello World"

 Useful commands:

   vikram doctor      ← check that everything is working
   vikram configure   ← change settings, add channels (Telegram, Discord)
   vikram skills list ← see available skills
`,
		headline,
	)

	fmt.Println(boxStyle.
		BorderForeground(lipgloss.Color("10")).
		Render(successStyle.Render(successContent)))

	fmt.Printf("  Config: %s\n", stepStyle.Render(configPath))
	fmt.Printf("  Workspace: %s\n\n", stepStyle.Render(cfg.Workspace.Path))

	if bootstrapPending {
		fmt.Printf("  First chat bootstrap: %s\n", stepStyle.Render("the assistant will ask who it should be before it settles into an identity"))
		fmt.Printf("  Finish bootstrap by answering its discovery questions in a private chat.\n\n")
	}

	if workers := config.DiscoverLocalCLIs(); len(workers) > 0 {
		names := make([]string, 0, len(workers))
		for _, worker := range workers {
			names = append(names, worker.DisplayName)
		}
		fmt.Printf("  Local AI workers detected: %s\n", stepStyle.Render(strings.Join(names, ", ")))
		fmt.Printf("  These are auto-available for delegated/subagent tasks while Vikram is running.\n\n")
	}

	if channels := enabledChannelNames(cfg); len(channels) > 0 {
		fmt.Printf("  Channels configured: %s\n", stepStyle.Render(strings.Join(channels, ", ")))
		fmt.Printf("  Keep them online with: %s\n\n", stepStyle.Render("vikram gateway"))
		if cfg.Channels.Telegram.Enabled && len(cfg.Channels.Telegram.AllowFrom) == 0 {
			fmt.Printf("  Telegram first-use flow: user messages the bot, bot shows an OTP, then approve it with:\n")
			fmt.Printf("    %s\n\n", stepStyle.Render("vikram telegram pairing <otp>"))
		}
	}

	if cfg.V1API.Enabled {
		fmt.Printf("  Multi-device API: %s  (key: %s)\n",
			stepStyle.Render(cfg.V1API.Addr), stepStyle.Render(maskKey(cfg.V1API.APIKey)))
		fmt.Printf("  Example client: %s\n\n",
			stepStyle.Render(fmt.Sprintf("vikram client -s YOUR_GATEWAY_HOST%s -k %s", cfg.V1API.Addr, cfg.V1API.APIKey)))
	}
}

// ─── Helpers ──────────────────────────────────────────────────────────────────

// withSpinner runs fn while showing an animated spinner in the terminal.
func withSpinner(label string, fn func()) {
	frames := []string{"⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"}
	done := make(chan struct{})

	go func() {
		i := 0
		for {
			select {
			case <-done:
				return
			default:
				fmt.Printf("\r  %s  %s  ", stepStyle.Render(frames[i%len(frames)]), label)
				i++
				time.Sleep(80 * time.Millisecond)
			}
		}
	}()

	fn()
	close(done)
	// Clear the spinner line.
	fmt.Printf("\r%s\r", strings.Repeat(" ", len(label)+12))
}

// wrapText soft-wraps s to maxWidth characters, indenting continuation lines with indent.
func workspaceNeedsBootstrap(workspacePath string) bool {
	if strings.TrimSpace(workspacePath) == "" {
		return false
	}
	_, err := os.Stat(filepath.Join(workspacePath, "BOOTSTRAP.md"))
	return err == nil
}

func wrapText(s string, maxWidth int, indent string) string {
	words := strings.Fields(s)
	if len(words) == 0 {
		return s
	}

	var lines []string
	var current strings.Builder
	lineLen := 0

	for _, word := range words {
		if lineLen > 0 && lineLen+1+len(word) > maxWidth {
			lines = append(lines, current.String())
			current.Reset()
			lineLen = 0
		}
		if lineLen > 0 {
			current.WriteByte(' ')
			lineLen++
		}
		current.WriteString(word)
		lineLen += len(word)
	}

	if current.Len() > 0 {
		lines = append(lines, current.String())
	}

	return strings.Join(lines, "\n"+indent)
}

// ─── Auto (non-interactive) mode ─────────────────────────────────────────────

// onboardAuto runs a fully non-interactive setup using flags:
//
//	--provider <id>     Required. e.g. gemini, openai, anthropic, groq, deepseek, openrouter
//	--api-key  <key>    Required for API-key providers.
//	--api-base <url>    Optional. Useful for Ollama, vLLM, and custom endpoints.
//	--connect-mode <m>  Optional. Used by github_copilot (`stdio` or `grpc`).
//	--model    <model> Optional. Defaults to the first model for the provider.
//	--workspace <path> Optional. Defaults to the standard Vikram workspace path.
//	--skip-test        Optional. Skip live provider validation and save config immediately.
func onboardAuto(args []string) {
	providerID := flagValue(args, "--provider")
	apiKey := flagValue(args, "--api-key")
	apiBase := flagValue(args, "--api-base")
	connectMode := flagValue(args, "--connect-mode")
	model := flagValue(args, "--model")
	workspace := flagValue(args, "--workspace")
	skipTest := hasFlag(args, "--skip-test")

	// Validate required flags.
	if providerID == "" {
		fmt.Printf("  %s --provider is required for --auto mode.\n\n", errorStyle.Render("✗"))
		fmt.Printf("  Example:\n")
		fmt.Printf("    vikram onboard --auto --provider gemini --api-key YOUR_KEY\n\n")
		fmt.Printf("  Providers: %s\n", supportedProviderList())
		fmt.Printf("  Keyless/local: vertex, bedrock, ollama, vllm, github_copilot\n\n")
		os.Exit(1)
	}

	if _, ok := lookupProviderInfo(providerID); !ok {
		fmt.Printf("  %s Unknown provider %q.\n\n", errorStyle.Render("✗"), providerID)
		fmt.Printf("  Providers: %s\n\n", supportedProviderList())
		os.Exit(1)
	}

	needsKey := providerNeedsAPIKey(providerID)
	if needsKey && apiKey == "" {
		fmt.Printf("  %s --api-key is required for provider %q.\n\n", errorStyle.Render("✗"), providerID)
		fmt.Printf("  Example:\n")
		fmt.Printf("    vikram onboard --auto --provider %s --api-key YOUR_KEY\n\n", providerID)
		os.Exit(1)
	}

	defaultModel := defaultProviderModel(providerID)
	if strings.TrimSpace(model) == "" && defaultModel == "" {
		fmt.Printf("  %s --model is required for provider %q.\n\n", errorStyle.Render("✗"), providerID)
		fmt.Printf("  This provider does not have a built-in default model.\n")
		fmt.Printf("  Example:\n")
		fmt.Printf("    vikram onboard --auto --provider %s --api-key YOUR_KEY --model YOUR_MODEL\n\n", providerID)
		os.Exit(1)
	}

	fmt.Printf("\n%s Vikram non-interactive setup\n\n", titleStyle.Render("⚡"))

	configPath := getConfigPath()
	cfg := config.DefaultConfig()
	// Load existing config if present — don't wipe existing settings.
	if _, err := os.Stat(configPath); err == nil {
		if loaded, err := loadConfig(); err == nil {
			cfg = loaded
		}
	}

	// Set workspace.
	if workspace != "" {
		cfg.Workspace.Path = expandHome(workspace)
	} else if cfg.Workspace.Path == "" {
		cfg.Workspace.Path = config.DefaultWorkspaceDir()
	}

	// Set provider + model.
	cfg.Agents.Defaults.Provider = providerID
	if model != "" {
		cfg.Agents.Defaults.Model = model
	} else {
		cfg.Agents.Defaults.Model = defaultModel
	}

	// Set API key.
	if apiKey != "" {
		setProviderKey(cfg, providerID, apiKey)
	}
	if connectMode != "" {
		setProviderConnectMode(cfg, providerID, connectMode)
	}
	if apiBase != "" {
		setProviderAPIBase(cfg, providerID, apiBase)
	} else {
		switch providerID {
		case "ollama", "vllm":
			setProviderAPIBase(cfg, providerID, defaultProviderAPIBase(providerID))
		case "github_copilot":
			if strings.TrimSpace(cfg.Providers.GitHubCopilot.ConnectMode) == "" {
				cfg.Providers.GitHubCopilot.ConnectMode = "stdio"
			}
			setProviderAPIBase(cfg, providerID, defaultGitHubCopilotTarget(cfg.Providers.GitHubCopilot.ConnectMode))
		}
	}

	fmt.Printf("  %s Provider:  %s\n", successStyle.Render("✓"), providerID)
	fmt.Printf("  %s Model:     %s\n", successStyle.Render("✓"), cfg.Agents.Defaults.Model)
	fmt.Printf("  %s Workspace: %s\n", successStyle.Render("✓"), cfg.Workspace.Path)
	if providerAPIBase := strings.TrimSpace(apiBaseFromConfig(cfg, providerID)); providerAPIBase != "" {
		fmt.Printf("  %s API Base:  %s\n", successStyle.Render("✓"), providerAPIBase)
	}

	// Validate the provider (live test) when the provider supports it.
	if providerSupportsLiveValidation(providerID) && !skipTest {
		var testErr error
		withSpinner("Testing connection to "+providerID+"…", func() {
			testErr = validateProviderKey(cfg)
		})
		if testErr != nil {
			fmt.Printf("  %s Connection test failed: %s\n", errorStyle.Render("✗"), simplifyProviderErrorFor(providerID, testErr))
			fmt.Printf("  %s %s\n", warnStyle.Render("→"), providerConnectionHint(cfg, providerID))
			fmt.Printf("  %s Config was NOT saved. Check your provider settings and try again.\n\n", warnStyle.Render("→"))
			os.Exit(1)
		}
		fmt.Printf("  %s Provider validated — connection works.\n", successStyle.Render("✓"))
	} else if skipTest {
		fmt.Printf("  %s Skipping live provider validation (--skip-test).\n", warnStyle.Render("→"))
	}

	// Save config + seed workspace.
	if err := config.SaveConfig(configPath, cfg); err != nil {
		fmt.Printf("  %s Could not save config: %v\n", errorStyle.Render("✗"), err)
		os.Exit(1)
	}
	createWorkspaceTemplates(cfg.Workspace.Path)

	fmt.Printf("  %s Config saved to: %s\n\n", successStyle.Render("✓"), configPath)
	fmt.Printf("  %s\n\n", titleStyle.Render("Setup complete. Try it:"))
	fmt.Printf("    vikram agent -m \"Hello! What can you do?\"\n\n")
}

// flagValue returns the value for a --flag <value> pair in args,
// or "" if the flag is not present.
func flagValue(args []string, flag string) string {
	for i, arg := range args {
		if arg == flag && i+1 < len(args) {
			return args[i+1]
		}
		// Support --flag=value syntax too.
		prefix := flag + "="
		if strings.HasPrefix(arg, prefix) {
			return arg[len(prefix):]
		}
	}
	return ""
}

func hasFlag(args []string, flag string) bool {
	for _, arg := range args {
		if arg == flag || arg == flag+"=true" {
			return true
		}
		if arg == flag+"=false" {
			return false
		}
	}
	return false
}

func supportedProviderList() string {
	ids := make([]string, 0, len(traditional))
	for _, p := range traditional {
		ids = append(ids, p.id)
	}
	return strings.Join(ids, ", ")
}

func lookupProviderInfo(providerID string) (providerInfo, bool) {
	for _, p := range traditional {
		if p.id == providerID {
			return p, true
		}
	}
	return providerInfo{}, false
}

func defaultProviderModel(providerID string) string {
	if models, ok := providerModels[providerID]; ok && len(models) > 0 {
		return models[0]
	}
	return ""
}

func apiBaseFromConfig(cfg *config.Config, providerID string) string {
	switch strings.ToLower(strings.TrimSpace(providerID)) {
	case "gemini":
		return cfg.Providers.Gemini.APIBase
	case "openai", "gpt":
		return cfg.Providers.OpenAI.APIBase
	case "anthropic", "claude":
		return cfg.Providers.Anthropic.APIBase
	case "groq":
		return cfg.Providers.Groq.APIBase
	case "deepseek":
		return cfg.Providers.DeepSeek.APIBase
	case "openrouter":
		return cfg.Providers.OpenRouter.APIBase
	case "zhipu", "glm":
		return cfg.Providers.Zhipu.APIBase
	case "moonshot":
		return cfg.Providers.Moonshot.APIBase
	case "nvidia":
		return cfg.Providers.Nvidia.APIBase
	case "vllm":
		return cfg.Providers.VLLM.APIBase
	case "ollama":
		return cfg.Providers.Ollama.APIBase
	case "github_copilot", "copilot":
		return cfg.Providers.GitHubCopilot.APIBase
	default:
		return ""
	}
}

// ─── Refresh mode ────────────────────────────────────────────────────────────

// onboardRefresh upgrades an existing install non-interactively:
//  1. Load existing config (preserving all user values).
//  2. Re-save it — this round-trip adds any new fields with their schema defaults
//     without overwriting anything the user previously set.
//  3. Re-sync workspace templates (skipping files that already exist).
//
// Preserves existing workspace state during upgrade. Run after installing a new version.
// vikram to get new config fields without re-running the full wizard.
func onboardRefresh() {
	configPath := getConfigPath()
	fmt.Printf("\n%s Refreshing Vikram config…\n\n", titleStyle.Render("↻"))

	cfg, err := loadConfig()
	if err != nil {
		if os.IsNotExist(err) {
			fmt.Printf("  %s No config found at %s — run  vikram onboard  first.\n",
				errorStyle.Render("✗"), configPath)
			return
		}
		fmt.Printf("  %s Failed to load config: %v\n", errorStyle.Render("✗"), err)
		return
	}

	// Re-save: JSON marshal → write. New fields in DefaultConfig will be added
	// with their zero/default values; existing values are preserved.
	if err := config.SaveConfig(configPath, cfg); err != nil {
		fmt.Printf("  %s Failed to save config: %v\n", errorStyle.Render("✗"), err)
		return
	}
	fmt.Printf("  %s Config refreshed — new schema fields added with defaults.\n",
		successStyle.Render("✓"))
	fmt.Printf("    %s\n\n", stepStyle.Render(configPath))

	// Re-sync workspace templates (only creates missing files, never overwrites).
	workspace := cfg.WorkspacePath()
	if workspace != "" {
		createWorkspaceTemplates(workspace)
		fmt.Printf("  %s Workspace templates refreshed (existing files untouched).\n",
			successStyle.Render("✓"))
		fmt.Printf("    %s\n\n", stepStyle.Render(workspace))
	}

	fmt.Printf("  %s Run  vikram doctor  to verify everything is configured.\n\n",
		stepStyle.Render("→"))
}
