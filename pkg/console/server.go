package console

import (
	"net/http"
	"sync"
	"time"
)

// ConsoleEventType represents the type of real-time console event.
type ConsoleEventType string

const (
	// EventPhaseChange is emitted when a task transitions between Work_Phases.
	EventPhaseChange ConsoleEventType = "phase_change"
	// EventAgentCallComplete is emitted when an agent call finishes.
	EventAgentCallComplete ConsoleEventType = "agent_call_complete"
	// EventTaskStatusChange is emitted when a task's overall status changes.
	EventTaskStatusChange ConsoleEventType = "task_status_change"
)

// ConsoleEvent is the envelope for all real-time console events.
type ConsoleEvent struct {
	Type      ConsoleEventType `json:"type"`
	Timestamp int64            `json:"timestamp"`
	TaskID    string           `json:"task_id"`
	Data      interface{}      `json:"data"`
}

// PhaseChangeEvent contains details about a Work_Phase transition.
type PhaseChangeEvent struct {
	FromPhase string `json:"from_phase"`
	ToPhase   string `json:"to_phase"`
	Reason    string `json:"reason,omitempty"`
}

// AgentCallEvent contains details about a completed agent call.
type AgentCallEvent struct {
	Role       string  `json:"role"`
	DurationMS int64   `json:"duration_ms"`
	Tokens     int     `json:"tokens"`
	CostUSD    float64 `json:"cost_usd"`
	Summary    string  `json:"summary"`
	Model      string  `json:"model,omitempty"`
	Provider   string  `json:"provider,omitempty"`
	Success    bool    `json:"success"`
}

// TaskStatusEvent contains details about a task status change.
type TaskStatusEvent struct {
	OldStatus string `json:"old_status"`
	NewStatus string `json:"new_status"`
	Reason    string `json:"reason,omitempty"`
}

// ProgressHub manages WebSocket clients for real-time progress streaming.
// It maintains connected clients and broadcasts typed console events.
type ProgressHub struct {
	clients    map[*progressClient]bool
	broadcast  chan ConsoleEvent
	register   chan *progressClient
	unregister chan *progressClient
	mu         sync.RWMutex
	done       chan struct{}
}

// progressClient is a connected WebSocket client receiving progress events.
type progressClient struct {
	send       chan ConsoleEvent
	mu         sync.RWMutex
	taskFilter string
}

// setFilter updates the client's task filter (thread-safe).
func (c *progressClient) setFilter(taskID string) {
	c.mu.Lock()
	c.taskFilter = taskID
	c.mu.Unlock()
}

// getFilter retrieves the client's task filter (thread-safe).
func (c *progressClient) getFilter() string {
	c.mu.RLock()
	defer c.mu.RUnlock()
	return c.taskFilter
}

// NewProgressHub creates and starts a new ProgressHub for real-time event delivery.
func NewProgressHub() *ProgressHub {
	h := &ProgressHub{
		clients:    make(map[*progressClient]bool),
		broadcast:  make(chan ConsoleEvent, 256),
		register:   make(chan *progressClient),
		unregister: make(chan *progressClient),
		done:       make(chan struct{}),
	}
	go h.run()
	return h
}

func (h *ProgressHub) run() {
	for {
		select {
		case <-h.done:
			return
		case client := <-h.register:
			h.mu.Lock()
			h.clients[client] = true
			h.mu.Unlock()
		case client := <-h.unregister:
			h.mu.Lock()
			if _, ok := h.clients[client]; ok {
				delete(h.clients, client)
				close(client.send)
			}
			h.mu.Unlock()
		case event := <-h.broadcast:
			h.mu.RLock()
			for client := range h.clients {
				// Apply task filter if set
				if f := client.getFilter(); f != "" && f != event.TaskID {
					continue
				}
				select {
				case client.send <- event:
				default:
					// Client too slow, skip this event
				}
			}
			h.mu.RUnlock()
		}
	}
}

// Stop shuts down the ProgressHub.
func (h *ProgressHub) Stop() {
	close(h.done)
}

// ClientCount returns the number of connected progress clients.
func (h *ProgressHub) ClientCount() int {
	h.mu.RLock()
	defer h.mu.RUnlock()
	return len(h.clients)
}

// EmitPhaseChange broadcasts a phase change event to all connected clients.
// The event is delivered within 500ms as per requirement 43.2.
func (h *ProgressHub) EmitPhaseChange(taskID, fromPhase, toPhase, reason string) {
	event := ConsoleEvent{
		Type:      EventPhaseChange,
		Timestamp: time.Now().UnixMilli(),
		TaskID:    taskID,
		Data: PhaseChangeEvent{
			FromPhase: fromPhase,
			ToPhase:   toPhase,
			Reason:    reason,
		},
	}
	// Non-blocking send to broadcast channel for low-latency delivery.
	select {
	case h.broadcast <- event:
	default:
		// If broadcast buffer is full, we still need to deliver within 500ms.
		// Force delivery in a goroutine.
		go func() { h.broadcast <- event }()
	}
}

// EmitAgentCallComplete broadcasts an agent call completion event.
func (h *ProgressHub) EmitAgentCallComplete(taskID string, call AgentCallEvent) {
	event := ConsoleEvent{
		Type:      EventAgentCallComplete,
		Timestamp: time.Now().UnixMilli(),
		TaskID:    taskID,
		Data:      call,
	}
	select {
	case h.broadcast <- event:
	default:
		go func() { h.broadcast <- event }()
	}
}

// EmitTaskStatusChange broadcasts a task status change event.
func (h *ProgressHub) EmitTaskStatusChange(taskID, oldStatus, newStatus, reason string) {
	event := ConsoleEvent{
		Type:      EventTaskStatusChange,
		Timestamp: time.Now().UnixMilli(),
		TaskID:    taskID,
		Data: TaskStatusEvent{
			OldStatus: oldStatus,
			NewStatus: newStatus,
			Reason:    reason,
		},
	}
	select {
	case h.broadcast <- event:
	default:
		go func() { h.broadcast <- event }()
	}
}

// RegisterConsoleRoutes mounts the console web UI and WebSocket progress endpoint
// on the given ServeMux. Call this during server initialization.
//
// Routes mounted:
//   - GET /console — serves the console web UI
//   - GET /console/ws — WebSocket endpoint for real-time progress events
func (s *Server) RegisterConsoleRoutes(mux *http.ServeMux) {
	mux.HandleFunc("/console", s.auth(s.handleConsoleUI))
	mux.HandleFunc("/console/ws", s.auth(s.handleConsoleWS))
}

// handleConsoleUI serves the console web interface.
func (s *Server) handleConsoleUI(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.Write(consoleIndexHTML)
}

// consoleIndexHTML is the minimal console web UI HTML page.
var consoleIndexHTML = []byte(`<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Vikram Console - Real-Time Progress</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #0f1419; color: #e7e9ea; }
        .header { padding: 1rem 2rem; border-bottom: 1px solid #2f3336; display: flex; align-items: center; gap: 1rem; }
        .header h1 { font-size: 1.25rem; font-weight: 600; }
        .status { display: inline-flex; align-items: center; gap: 0.5rem; font-size: 0.875rem; color: #71767b; }
        .status .dot { width: 8px; height: 8px; border-radius: 50%; background: #f44; }
        .status.connected .dot { background: #0f0; }
        .container { max-width: 1200px; margin: 0 auto; padding: 2rem; }
        .feed { display: flex; flex-direction: column; gap: 0.5rem; max-height: 80vh; overflow-y: auto; }
        .event { padding: 0.75rem 1rem; border-radius: 8px; background: #1a1f25; border-left: 3px solid #536471; font-size: 0.875rem; }
        .event.phase_change { border-left-color: #1d9bf0; }
        .event.agent_call_complete { border-left-color: #00ba7c; }
        .event.task_status_change { border-left-color: #f9a825; }
        .event .meta { color: #71767b; font-size: 0.75rem; margin-top: 0.25rem; }
        .event .label { font-weight: 600; text-transform: uppercase; font-size: 0.7rem; letter-spacing: 0.05em; }
        .empty { text-align: center; padding: 4rem; color: #71767b; }
    </style>
</head>
<body>
    <div class="header">
        <h1>Vikram Console</h1>
        <div class="status" id="ws-status">
            <span class="dot"></span>
            <span class="text">Disconnected</span>
        </div>
        <span style="margin-left:auto;font-size:0.75rem;color:#71767b" id="client-count"></span>
    </div>
    <div class="container">
        <div class="feed" id="feed">
            <div class="empty">Waiting for events...</div>
        </div>
    </div>
    <script>
        const feed = document.getElementById('feed');
        const status = document.getElementById('ws-status');
        const MAX_EVENTS = 100;
        let ws;

        function connect() {
            const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
            const token = new URLSearchParams(location.search).get('token') || '';
            const url = proto + '//' + location.host + '/console/ws' + (token ? '?token=' + token : '');
            ws = new WebSocket(url);
            ws.onopen = () => {
                status.classList.add('connected');
                status.querySelector('.text').textContent = 'Connected';
            };
            ws.onclose = () => {
                status.classList.remove('connected');
                status.querySelector('.text').textContent = 'Disconnected';
                setTimeout(connect, 2000);
            };
            ws.onmessage = (e) => {
                const event = JSON.parse(e.data);
                addEvent(event);
            };
        }

        function addEvent(event) {
            const empty = feed.querySelector('.empty');
            if (empty) empty.remove();

            const el = document.createElement('div');
            el.className = 'event ' + event.type;
            el.innerHTML = renderEvent(event);
            feed.prepend(el);

            while (feed.children.length > MAX_EVENTS) {
                feed.removeChild(feed.lastChild);
            }
        }

        function renderEvent(event) {
            const time = new Date(event.timestamp).toLocaleTimeString();
            const d = event.data;
            switch (event.type) {
                case 'phase_change':
                    return '<span class="label">Phase Change</span> ' +
                        d.from_phase + ' → ' + d.to_phase +
                        '<div class="meta">Task: ' + event.task_id + ' | ' + time + (d.reason ? ' | ' + d.reason : '') + '</div>';
                case 'agent_call_complete':
                    return '<span class="label">Agent Call</span> ' +
                        d.role + ' — ' + d.summary +
                        '<div class="meta">Task: ' + event.task_id + ' | ' + d.duration_ms + 'ms | ' + d.tokens + ' tokens | $' + d.cost_usd.toFixed(4) + ' | ' + time + '</div>';
                case 'task_status_change':
                    return '<span class="label">Status</span> ' +
                        d.old_status + ' → ' + d.new_status +
                        '<div class="meta">Task: ' + event.task_id + ' | ' + time + (d.reason ? ' | ' + d.reason : '') + '</div>';
                default:
                    return '<span class="label">' + event.type + '</span> ' + JSON.stringify(d) +
                        '<div class="meta">' + time + '</div>';
            }
        }

        connect();
    </script>
</body>
</html>`)
