package console

import (
	"context"
	"crypto/subtle"
	"embed"
	"encoding/json"
	"fmt"
	"html/template"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"github.com/Vatthu/vikram/pkg/config"
	"github.com/Vatthu/vikram/pkg/logger"
	"github.com/Vatthu/vikram/pkg/pairing"
	"github.com/gorilla/websocket"
)

//go:embed templates/*
var templateFS embed.FS

//go:embed web-dist/*
var webDistFS embed.FS

var upgrader = websocket.Upgrader{CheckOrigin: func(r *http.Request) bool {
	origin := r.Header.Get("Origin")
	return origin == "" || origin == "http://"+r.Host || origin == "https://"+r.Host
}}

type wsClient struct {
	conn *websocket.Conn
	send chan []byte
}

type wsHub struct {
	clients    map[*wsClient]bool
	broadcast  chan []byte
	register   chan *wsClient
	unregister chan *wsClient
	mu         sync.RWMutex
}

func newWSHub() *wsHub {
	h := &wsHub{
		clients:    make(map[*wsClient]bool),
		broadcast:  make(chan []byte, 256),
		register:   make(chan *wsClient),
		unregister: make(chan *wsClient),
	}
	go h.run()
	return h
}

func (h *wsHub) run() {
	for {
		select {
		case client := <-h.register:
			h.mu.Lock()
			h.clients[client] = true
			h.mu.Unlock()
		case client := <-h.unregister:
			h.mu.Lock()
			delete(h.clients, client)
			close(client.send)
			h.mu.Unlock()
		case msg := <-h.broadcast:
			h.mu.RLock()
			for client := range h.clients {
				select {
				case client.send <- msg:
				default:
				}
			}
			h.mu.RUnlock()
		}
	}
}

func (h *wsHub) Broadcast(eventType string, data interface{}) {
	msg, _ := json.Marshal(map[string]interface{}{"type": eventType, "data": data, "ts": time.Now().Unix()})
	h.broadcast <- msg
}

// BroadcastChat sends a chat message to all connected WebSocket clients.
func (s *Server) BroadcastChat(role, content string) {
	s.hub.Broadcast("chat_message", map[string]string{"role": role, "content": content})
}

// Config holds console configuration.
type Config struct {
	Enabled bool   `json:"enabled"`
	Addr    string `json:"addr"`
	APIKey  string `json:"api_key"`
}

// AgentChangedFunc is called when an agent is added or removed via the console.
type AgentChangedFunc func(action string, agent config.AgentConfig)

// ChatFunc is called when the user sends a chat message via the Web UI.
// The gateway wires this to the agent loop for LLM processing.
type ChatFunc func(ctx context.Context, message string) (string, error)

// ChannelChangedFunc is called when channel config is saved via the Web UI.
// The gateway wires this to reinitialize the channel at runtime.
type ChannelChangedFunc func(name string)

// Server serves the management console.
type Server struct {
	config          Config
	hub             *wsHub
	progressHub     *ProgressHub
	cfg             *config.Config
	cfgPath         string
	templates       *template.Template
	httpSrv         *http.Server
	onAgentChange   AgentChangedFunc
	onChannelChange ChannelChangedFunc
	chatHandler     ChatFunc
	orchCmd         *exec.Cmd
	orchMu          sync.Mutex
	projectRoot     string
	orchSocket      string
	orchHTTPClient  *http.Client
	orchBaseURL     string
	archive         *Archive
}

// SetOnAgentChange wires a callback for runtime agent registration.
func (s *Server) SetOnAgentChange(fn AgentChangedFunc) {
	s.onAgentChange = fn
}

// SetChatHandler wires the chat callback so the Web UI can talk to Vikram.
func (s *Server) SetChatHandler(fn ChatFunc) {
	s.chatHandler = fn
}

// SetOnChannelChange wires a callback so channel config changes reconnect
// the channel at runtime instead of requiring a gateway restart.
func (s *Server) SetOnChannelChange(fn ChannelChangedFunc) {
	s.onChannelChange = fn
}

func (s *Server) notifyChannelChanged(name string) {
	if s.onChannelChange == nil {
		return
	}
	go s.onChannelChange(name)
}

// NewServer creates a new console server.
func NewServer(cfg Config, appCfg *config.Config, cfgPath string) *Server {
	if cfg.Addr == "" {
		cfg.Addr = "127.0.0.1:18793"
	}
	tmpl := template.Must(template.ParseFS(templateFS, "templates/*.html"))
	// Find project root by walking up from the binary
	projectRoot := findProjectRoot()
	return &Server{
		config:      cfg,
		hub:         newWSHub(),
		progressHub: NewProgressHub(),
		cfg:         appCfg,
		cfgPath:     cfgPath,
		templates:   tmpl,
		projectRoot: projectRoot,
		orchSocket:  defaultOrchestratorSocket(),
		archive:     NewArchive(),
	}
}

func defaultOrchestratorSocket() string {
	if socket := strings.TrimSpace(os.Getenv("VIKRAM_ORCHESTRATOR_SOCKET")); socket != "" {
		return socket
	}
	return "/tmp/vikram-orchestrator.sock"
}

func findProjectRoot() string {
	// Check common locations for the orchestrator directory
	candidates := []string{
		".",
		os.Getenv("VIKRAM_PROJECT_ROOT"),
		filepath.Join(os.Getenv("HOME"), "projects", "vikram"),
	}
	for _, c := range candidates {
		if c != "" {
			orchDir := filepath.Join(c, "services", "orchestrator")
			if _, err := os.Stat(filepath.Join(orchDir, "pyproject.toml")); err == nil {
				if abs, absErr := filepath.Abs(c); absErr == nil {
					return abs
				}
				return c
			}
		}
	}
	// Fallback: try to find it relative to the current working directory
	if wd, err := os.Getwd(); err == nil {
		for depth := 0; depth < 5; depth++ {
			orchDir := filepath.Join(wd, "services", "orchestrator")
			if _, err := os.Stat(filepath.Join(orchDir, "pyproject.toml")); err == nil {
				if abs, absErr := filepath.Abs(wd); absErr == nil {
					return abs
				}
				return wd
			}
			wd = filepath.Dir(wd)
		}
	}
	if abs, err := filepath.Abs("."); err == nil {
		return abs
	}
	return "."
}

// spaHandler serves the React SPA from embedded files.
func (s *Server) spaHandler(w http.ResponseWriter, r *http.Request) {
	// Try to serve the exact file
	path := "web-dist" + r.URL.Path
	if r.URL.Path == "/" {
		path = "web-dist/index.html"
	}
	data, err := webDistFS.ReadFile(path)
	if err != nil {
		// SPA fallback: serve index.html for all unmatched routes
		data, err = webDistFS.ReadFile("web-dist/index.html")
		if err != nil {
			http.Error(w, "Not found", http.StatusNotFound)
			return
		}
	}
	// Set correct content type
	if strings.HasSuffix(path, ".js") {
		w.Header().Set("Content-Type", "application/javascript")
	} else if strings.HasSuffix(path, ".css") {
		w.Header().Set("Content-Type", "text/css")
	} else if strings.HasSuffix(path, ".svg") {
		w.Header().Set("Content-Type", "image/svg+xml")
	}
	w.Write(data)
}

func (s *Server) Start(ctx context.Context) error {
	mux := http.NewServeMux()
	mux.HandleFunc("/", s.auth(s.handleIndex))
	mux.HandleFunc("/ws", s.auth(s.handleWS))
	// --- Dashboard ---
	mux.HandleFunc("/api/dashboard", s.auth(s.handleAPIDashboard))
	mux.HandleFunc("/api/health", s.auth(s.handleAPIHealth))
	// --- Agents ---
	mux.HandleFunc("/api/agents", s.auth(s.handleAPIAgents))
	mux.HandleFunc("/api/agents/{id}", s.auth(s.handleAPIAgentByID))
	mux.HandleFunc("/api/agents/add", s.auth(s.handleAPIAddAgent))
	mux.HandleFunc("/api/agents/remove/{id}", s.auth(s.handleAPIRemoveAgent))
	// --- Providers ---
	mux.HandleFunc("/api/providers", s.auth(s.handleAPIProviders))
	mux.HandleFunc("/api/providers/save", s.auth(s.handleAPISaveProvider))
	mux.HandleFunc("/api/providers/{name}/test", s.auth(s.handleAPIProviderTest))
	// --- Channels ---
	mux.HandleFunc("/api/channels/", s.auth(s.handleAPIChannels))
	// --- Configuration ---
	mux.HandleFunc("/api/config/gateway", s.auth(s.handleAPIGateway))
	mux.HandleFunc("/api/config/workspace", s.auth(s.handleAPIWorkspace))
	mux.HandleFunc("/api/config/tools", s.auth(s.handleAPITools))
	mux.HandleFunc("/api/config/voice", s.auth(s.handleAPIVoice))
	mux.HandleFunc("/api/config/heartbeat", s.auth(s.handleAPIHeartbeat))
	mux.HandleFunc("/api/config/full", s.auth(s.handleAPIFullConfig))
	// --- MCP ---
	mux.HandleFunc("/api/config/mcp", s.auth(s.handleAPIMCP))
	mux.HandleFunc("/api/config/mcp/{name}", s.auth(s.handleAPIMCPDelete))
	// --- Tasks ---
	mux.HandleFunc("/api/tasks", s.auth(s.handleAPITasks))
	mux.HandleFunc("/api/tasks/batch", s.auth(s.handleBatchTasks))
	mux.HandleFunc("/api/tasks/{task_id}/review", s.auth(s.handleAPITaskReview))
	mux.HandleFunc("/api/tasks/{task_id}/resume", s.auth(s.handleAPITaskResume))
	mux.HandleFunc("/api/tasks/{task_id}/diff", s.auth(s.handleAPITaskDiff))
	mux.HandleFunc("/api/tasks/{task_id}/files", s.auth(s.handleAPITaskFiles))
	mux.HandleFunc("/api/tasks/{task_id}/merge", s.auth(s.handleAPITaskMerge))
	mux.HandleFunc("/api/orchestrator", s.auth(s.handleOrchStatus))
	mux.HandleFunc("/api/orchestrator/start", s.auth(s.handleOrchStart))
	mux.HandleFunc("/api/orchestrator/stop", s.auth(s.handleOrchStop))
	mux.HandleFunc("/api/chat/send", s.auth(s.handleChatSend))
	mux.HandleFunc("/api/telegram/pair", s.auth(s.handleTelegramPair))
	// --- Archive ---
	mux.HandleFunc("/api/archive", s.auth(s.handleArchiveSearch))
	mux.HandleFunc("/api/archive/export", s.auth(s.handleArchiveExport))
	// --- Cost Dashboard ---
	mux.HandleFunc("/api/cost/overview", s.auth(s.handleAPICostOverview))
	mux.HandleFunc("/api/cost/breakdown", s.auth(s.handleAPICostBreakdown))
	// --- Formations ---
	mux.HandleFunc("/api/formations", s.auth(s.handleAPIFormations))
	mux.HandleFunc("/api/formations/ab-test", s.auth(s.handleAPIFormationABTest))
	mux.HandleFunc("/api/formations/ab-test/promote", s.auth(s.handleAPIFormationABPromote))
	mux.HandleFunc("/api/formations/{id}/clone", s.auth(s.handleAPIFormationClone))
	mux.HandleFunc("/api/formations/{id}", s.auth(s.handleAPIFormationByID))
	// --- Team Health ---
	mux.HandleFunc("/api/team/health", s.auth(s.handleAPITeamHealth))
	// --- Skills ---
	mux.HandleFunc("/api/skills", s.auth(s.handleAPISkills))
	// --- Console Progress (real-time) ---
	s.RegisterConsoleRoutes(mux)
	// Legacy HTML fragment routes
	mux.HandleFunc("/agents", s.auth(s.handleAgents))
	mux.HandleFunc("/agents/add", s.auth(s.handleAddAgent))
	mux.HandleFunc("/agents/remove", s.auth(s.handleRemoveAgent))
	mux.HandleFunc("/providers", s.auth(s.handleProviders))
	mux.HandleFunc("/providers/save", s.auth(s.handleSaveProvider))
	mux.HandleFunc("/tasks", s.auth(s.handleTasks))
	mux.HandleFunc("/overview", s.auth(s.handleOverview))
	mux.HandleFunc("/events", s.auth(s.handleEvents))

	s.httpSrv = &http.Server{Addr: s.config.Addr, Handler: mux,
		ReadTimeout: 15 * time.Second, WriteTimeout: 30 * time.Second, IdleTimeout: 60 * time.Second}

	logger.InfoC("console", "Management console starting on "+s.config.Addr)
	go func() { <-ctx.Done(); s.Stop() }()
	err := s.httpSrv.ListenAndServe()
	if err == http.ErrServerClosed {
		return nil
	}
	return err
}

func (s *Server) Stop() {
	s.orchMu.Lock()
	if s.orchCmd != nil && s.orchCmd.Process != nil {
		s.orchCmd.Process.Kill()
	}
	s.orchMu.Unlock()
	if s.progressHub != nil {
		s.progressHub.Stop()
	}
	if s.httpSrv != nil {
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		s.httpSrv.Shutdown(ctx)
	}
}

// --- Orchestrator lifecycle ---

func (s *Server) handleOrchStatus(w http.ResponseWriter, r *http.Request) {
	s.orchMu.Lock()
	running := s.orchCmd != nil && s.orchCmd.Process != nil
	s.orchMu.Unlock()
	// Also check if socket is reachable
	reachable := s.orchSocketAlive()
	s.writeOK(w, map[string]interface{}{
		"running":   running,
		"reachable": reachable,
		"socket":    s.orchSocket,
	})
}

func (s *Server) handleOrchStart(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		s.writeError(w, http.StatusMethodNotAllowed, "POST only")
		return
	}
	s.orchMu.Lock()
	defer s.orchMu.Unlock()

	if s.orchCmd != nil && s.orchCmd.Process != nil {
		s.writeOK(w, map[string]string{"status": "already running"})
		return
	}

	orchDir := filepath.Join(s.projectRoot, "services", "orchestrator")
	venvPython := filepath.Join(orchDir, ".venv", "bin", "python")
	if _, err := os.Stat(venvPython); err != nil {
		s.writeError(w, http.StatusInternalServerError, "orchestrator venv not found at "+venvPython)
		return
	}

	s.orchCmd = exec.Command(venvPython, "-m", "vikram_orchestrator.main")
	s.orchCmd.Dir = orchDir
	s.orchCmd.Stdout = os.Stdout
	s.orchCmd.Stderr = os.Stderr
	if err := s.orchCmd.Start(); err != nil {
		s.writeError(w, http.StatusInternalServerError, "failed to start: "+err.Error())
		return
	}
	logger.InfoC("console", "Orchestrator started")
	s.hub.Broadcast("orchestrator_started", nil)
	s.writeOK(w, map[string]string{"status": "started"})
}

func (s *Server) handleOrchStop(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		s.writeError(w, http.StatusMethodNotAllowed, "POST only")
		return
	}
	s.orchMu.Lock()
	defer s.orchMu.Unlock()

	if s.orchCmd == nil || s.orchCmd.Process == nil {
		s.writeOK(w, map[string]string{"status": "not running"})
		return
	}
	if err := s.orchCmd.Process.Kill(); err != nil {
		s.writeError(w, http.StatusInternalServerError, "failed to stop: "+err.Error())
		return
	}
	s.orchCmd = nil
	logger.InfoC("console", "Orchestrator stopped")
	s.hub.Broadcast("orchestrator_stopped", nil)
	s.writeOK(w, map[string]string{"status": "stopped"})
}

func (s *Server) handleChatSend(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		s.writeError(w, http.StatusMethodNotAllowed, "POST only")
		return
	}
	if s.chatHandler == nil {
		s.writeError(w, http.StatusServiceUnavailable, "chat handler not configured")
		return
	}
	var body struct{ Message string }
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil || body.Message == "" {
		s.writeError(w, http.StatusBadRequest, "message required")
		return
	}
	// Broadcast that the user sent a message
	s.hub.Broadcast("chat_user", map[string]string{"content": body.Message})
	// Process via the agent loop in a goroutine
	go func() {
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Minute)
		defer cancel()
		s.hub.Broadcast("chat_status", map[string]string{"status": "thinking"})
		resp, err := s.chatHandler(ctx, body.Message)
		if err != nil {
			s.hub.Broadcast("chat_response", map[string]string{"content": "Error: " + err.Error(), "error": "true"})
			return
		}
		s.hub.Broadcast("chat_response", map[string]string{"content": resp})
	}()
	s.writeOK(w, map[string]string{"status": "processing"})
}

func (s *Server) orchSocketAlive() bool {
	return orchSocketAlive(s.orchSocket)
}

func (s *Server) handleTelegramPair(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		s.writeError(w, http.StatusMethodNotAllowed, "POST only")
		return
	}

	var body struct {
		OTP string `json:"otp"`
	}
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		s.writeError(w, http.StatusBadRequest, "invalid JSON")
		return
	}
	body.OTP = strings.TrimSpace(body.OTP)
	if body.OTP == "" {
		s.writeError(w, http.StatusBadRequest, "otp required")
		return
	}

	homeDir := filepath.Dir(s.cfgPath)
	store := pairing.NewTelegramStoreAt(homeDir)
	req, err := store.Approve(body.OTP)
	if err != nil {
		s.writeError(w, http.StatusBadRequest, err.Error())
		return
	}

	found := false
	for _, id := range s.cfg.Channels.Telegram.AllowFrom {
		if id == req.SenderID {
			found = true
			break
		}
	}
	if !found {
		s.cfg.Channels.Telegram.AllowFrom = append(s.cfg.Channels.Telegram.AllowFrom, req.SenderID)
		if err := config.SaveConfig(s.cfgPath, s.cfg); err != nil {
			s.writeError(w, http.StatusInternalServerError, "save config: "+err.Error())
			return
		}
		s.notifyChannelChanged("telegram")
	}

	s.writeOK(w, map[string]interface{}{
		"status":    "paired",
		"chat_id":   req.ChatID,
		"username":  req.Username,
		"sender_id": req.SenderID,
	})
}

func orchSocketAlive(socketPath string) bool {
	conn, err := net.DialTimeout("unix", socketPath, 200*time.Millisecond)
	if err != nil {
		return false
	}
	conn.Close()
	return true
}

func (s *Server) auth(next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if s.config.APIKey == "" {
			next(w, r)
			return
		}
		token := strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer ")
		if token == "" {
			token = r.URL.Query().Get("token")
		}
		if subtle.ConstantTimeCompare([]byte(token), []byte(s.config.APIKey)) != 1 {
			w.Header().Set("WWW-Authenticate", "Bearer")
			http.Error(w, "Unauthorized", http.StatusUnauthorized)
			return
		}
		next(w, r)
	}
}

func (s *Server) handleIndex(w http.ResponseWriter, r *http.Request) {
	s.spaHandler(w, r)
}

func (s *Server) handleWS(w http.ResponseWriter, r *http.Request) {
	conn, err := upgrader.Upgrade(w, r, nil)
	if err != nil {
		return
	}
	client := &wsClient{conn: conn, send: make(chan []byte, 256)}
	s.hub.register <- client
	defer func() { s.hub.unregister <- client; conn.Close() }()

	go func() {
		for msg := range client.send {
			conn.WriteMessage(websocket.TextMessage, msg)
		}
	}()

	for {
		_, _, err := conn.ReadMessage()
		if err != nil {
			break
		}
	}
}

func (s *Server) handleAgents(w http.ResponseWriter, r *http.Request) {
	s.templates.ExecuteTemplate(w, "agents.html", map[string]interface{}{
		"Agents": s.cfg.Agents.List,
	})
}

func (s *Server) handleEvents(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("Connection", "keep-alive")
	flusher, _ := w.(http.Flusher)
	for {
		fmt.Fprintf(w, "data: {\"ts\":%d}\n\n", time.Now().Unix())
		flusher.Flush()
		time.Sleep(5 * time.Second)
	}
}

func (s *Server) handleOverview(w http.ResponseWriter, r *http.Request) {
	providerCount := 0
	for _, p := range []*config.ProviderConfig{
		&s.cfg.Providers.DeepSeek, &s.cfg.Providers.Mistral, &s.cfg.Providers.Nvidia,
		&s.cfg.Providers.Zhipu, &s.cfg.Providers.OpenAI, &s.cfg.Providers.Anthropic,
		&s.cfg.Providers.OpenRouter,
	} {
		if p.APIKey != "" {
			providerCount++
		}
	}
	err := s.templates.ExecuteTemplate(w, "overview.html", map[string]interface{}{
		"AgentCount":    len(s.cfg.Agents.List),
		"ProviderCount": providerCount,
		"GatewayPort":   s.cfg.Gateway.Port,
		"Uptime":        "running",
	})
	if err != nil {
		logger.ErrorCF("console", "overview template failed", map[string]interface{}{"error": err.Error()})
		http.Error(w, err.Error(), http.StatusInternalServerError)
	}
}

func (s *Server) handleAddAgent(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}
	r.ParseForm()
	id := strings.TrimSpace(r.FormValue("id"))
	role := strings.TrimSpace(r.FormValue("role"))
	provider := strings.TrimSpace(r.FormValue("provider"))
	model := strings.TrimSpace(r.FormValue("model"))

	if id == "" || role == "" || provider == "" || model == "" {
		http.Error(w, "All fields required", http.StatusBadRequest)
		return
	}

	agent := config.AgentConfig{ID: id, Role: role, Provider: provider, Model: model}
	s.cfg.Agents.List = append(s.cfg.Agents.List, agent)
	config.SaveConfig(s.cfgPath, s.cfg)
	if s.onAgentChange != nil {
		s.onAgentChange("add", agent)
	}
	s.hub.Broadcast("agent_added", map[string]string{"id": id, "role": role})
	s.handleAgents(w, r)
}

func (s *Server) handleRemoveAgent(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}
	r.ParseForm()
	id := strings.TrimSpace(r.FormValue("id"))

	var removed config.AgentConfig
	var filtered []config.AgentConfig
	for _, a := range s.cfg.Agents.List {
		if a.ID != id {
			filtered = append(filtered, a)
		} else {
			removed = a
		}
	}
	s.cfg.Agents.List = filtered
	config.SaveConfig(s.cfgPath, s.cfg)
	if s.onAgentChange != nil && removed.ID != "" {
		s.onAgentChange("remove", removed)
	}
	s.hub.Broadcast("agent_removed", map[string]string{"id": id})
	s.handleAgents(w, r)
}

func (s *Server) handleProviders(w http.ResponseWriter, r *http.Request) {
	type provInfo struct {
		Name, Key, Base, Status string
	}
	providers := []provInfo{
		{"deepseek", maskKey(s.cfg.Providers.DeepSeek.APIKey), s.cfg.Providers.DeepSeek.APIBase, status(s.cfg.Providers.DeepSeek.APIKey != "")},
		{"mistral", maskKey(s.cfg.Providers.Mistral.APIKey), s.cfg.Providers.Mistral.APIBase, status(s.cfg.Providers.Mistral.APIKey != "")},
		{"nvidia", maskKey(s.cfg.Providers.Nvidia.APIKey), s.cfg.Providers.Nvidia.APIBase, status(s.cfg.Providers.Nvidia.APIKey != "")},
		{"zhipu", maskKey(s.cfg.Providers.Zhipu.APIKey), s.cfg.Providers.Zhipu.APIBase, status(s.cfg.Providers.Zhipu.APIKey != "")},
		{"openai", maskKey(s.cfg.Providers.OpenAI.APIKey), s.cfg.Providers.OpenAI.APIBase, status(s.cfg.Providers.OpenAI.APIKey != "")},
		{"anthropic", maskKey(s.cfg.Providers.Anthropic.APIKey), s.cfg.Providers.Anthropic.APIBase, status(s.cfg.Providers.Anthropic.APIKey != "")},
		{"openrouter", maskKey(s.cfg.Providers.OpenRouter.APIKey), s.cfg.Providers.OpenRouter.APIBase, status(s.cfg.Providers.OpenRouter.APIKey != "")},
	}
	s.templates.ExecuteTemplate(w, "providers.html", map[string]interface{}{"Providers": providers})
}

func (s *Server) handleSaveProvider(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}
	r.ParseForm()
	name := strings.TrimSpace(r.FormValue("name"))
	key := strings.TrimSpace(r.FormValue("key"))
	base := strings.TrimSpace(r.FormValue("base"))

	switch name {
	case "deepseek":
		s.cfg.Providers.DeepSeek.APIKey = key
		if base != "" {
			s.cfg.Providers.DeepSeek.APIBase = base
		}
	case "mistral":
		s.cfg.Providers.Mistral.APIKey = key
		if base != "" {
			s.cfg.Providers.Mistral.APIBase = base
		}
	case "nvidia":
		s.cfg.Providers.Nvidia.APIKey = key
		if base != "" {
			s.cfg.Providers.Nvidia.APIBase = base
		}
	case "zhipu":
		s.cfg.Providers.Zhipu.APIKey = key
		if base != "" {
			s.cfg.Providers.Zhipu.APIBase = base
		}
	case "openai":
		s.cfg.Providers.OpenAI.APIKey = key
	case "anthropic":
		s.cfg.Providers.Anthropic.APIKey = key
	case "openrouter":
		s.cfg.Providers.OpenRouter.APIKey = key
	}
	config.SaveConfig(s.cfgPath, s.cfg)
	s.hub.Broadcast("provider_updated", map[string]string{"name": name})
	s.handleProviders(w, r)
}

func (s *Server) handleTasks(w http.ResponseWriter, r *http.Request) {
	s.templates.ExecuteTemplate(w, "tasks.html", map[string]interface{}{
		"Tasks": []map[string]string{},
		"Note":  "Orchestrator tasks appear here when the orchestrator is running.",
	})
}
