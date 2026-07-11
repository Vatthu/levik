package console

import (
	"encoding/json"
	"net/http"
	"time"

	"github.com/Vatthu/vikram/pkg/logger"
	"github.com/gorilla/websocket"
)

// consoleWSUpgrader is the WebSocket upgrader for the console progress endpoint.
// It allows same-origin connections and checks the Origin header.
var consoleWSUpgrader = websocket.Upgrader{
	CheckOrigin: func(r *http.Request) bool {
		origin := r.Header.Get("Origin")
		return origin == "" || origin == "http://"+r.Host || origin == "https://"+r.Host
	},
	ReadBufferSize:  1024,
	WriteBufferSize: 1024,
}

// handleConsoleWS upgrades the HTTP connection to WebSocket and streams
// real-time progress events to the client. Clients may optionally send
// a subscription message with a task_id filter.
//
// Protocol:
//   - Client connects via GET /console/ws
//   - Server streams ConsoleEvent JSON messages
//   - Client may send {"filter_task": "task-id"} to filter by task
//   - Server closes connection on hub shutdown or client disconnect
func (s *Server) handleConsoleWS(w http.ResponseWriter, r *http.Request) {
	conn, err := consoleWSUpgrader.Upgrade(w, r, nil)
	if err != nil {
		logger.ErrorCF("console", "WebSocket upgrade failed", map[string]interface{}{"error": err.Error()})
		return
	}

	client := &progressClient{
		send: make(chan ConsoleEvent, 64),
	}

	s.progressHub.register <- client

	// Set read deadline for ping/pong and client messages
	conn.SetReadDeadline(time.Now().Add(60 * time.Second))
	conn.SetPongHandler(func(string) error {
		conn.SetReadDeadline(time.Now().Add(60 * time.Second))
		return nil
	})

	// Writer goroutine: reads from client.send and writes to WebSocket
	done := make(chan struct{})
	go func() {
		defer close(done)
		ticker := time.NewTicker(30 * time.Second)
		defer ticker.Stop()

		for {
			select {
			case event, ok := <-client.send:
				if !ok {
					conn.WriteMessage(websocket.CloseMessage, []byte{})
					return
				}
				// Set write deadline to ensure 500ms delivery for phase changes
				conn.SetWriteDeadline(time.Now().Add(500 * time.Millisecond))
				data, err := json.Marshal(event)
				if err != nil {
					logger.ErrorCF("console", "JSON marshal failed", map[string]interface{}{"error": err.Error()})
					continue
				}
				if err := conn.WriteMessage(websocket.TextMessage, data); err != nil {
					return
				}
			case <-ticker.C:
				conn.SetWriteDeadline(time.Now().Add(10 * time.Second))
				if err := conn.WriteMessage(websocket.PingMessage, nil); err != nil {
					return
				}
			}
		}
	}()

	// Reader goroutine: reads client messages (filter subscriptions)
	go func() {
		defer func() {
			s.progressHub.unregister <- client
			conn.Close()
		}()

		for {
			_, message, err := conn.ReadMessage()
			if err != nil {
				if websocket.IsUnexpectedCloseError(err, websocket.CloseGoingAway, websocket.CloseNormalClosure) {
					logger.ErrorCF("console", "WebSocket read error", map[string]interface{}{"error": err.Error()})
				}
				return
			}
			// Parse filter subscription messages
			var sub struct {
				FilterTask string `json:"filter_task"`
			}
			if json.Unmarshal(message, &sub) == nil && sub.FilterTask != "" {
				client.taskFilter = sub.FilterTask
			}
		}
	}()

	<-done
}

// GetProgressHub returns the server's ProgressHub for emitting events.
// External packages (e.g., orchestratorhost) use this to emit progress events.
func (s *Server) GetProgressHub() *ProgressHub {
	return s.progressHub
}
