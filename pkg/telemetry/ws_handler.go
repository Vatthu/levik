package telemetry

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"net/http"
	"time"

	"github.com/gorilla/websocket"
)

const (
	// writeWait is the time allowed to write a message to the peer.
	writeWait = 10 * time.Second

	// pongWait is the time allowed to read the next pong message from the peer.
	pongWait = 60 * time.Second

	// pingPeriod sends pings to peer at this interval. Must be less than pongWait.
	pingPeriod = 30 * time.Second

	// subscriberBufferSize is the channel buffer size for each subscriber.
	subscriberBufferSize = 256
)

// wsUpgrader handles HTTP → WebSocket upgrades for the telemetry stream.
var wsUpgrader = websocket.Upgrader{
	ReadBufferSize:  1024,
	WriteBufferSize: 1024,
	CheckOrigin: func(r *http.Request) bool {
		// Allow all origins for the telemetry stream endpoint.
		// In production, this should be tightened to specific origins.
		return true
	},
}

// StreamHandler handles WebSocket connections for real-time telemetry streaming.
// It upgrades HTTP connections, subscribes them to telemetry events via the
// SubscriberManager, and delivers matching events as JSON over the WebSocket.
type StreamHandler struct {
	manager *SubscriberManager
}

// NewStreamHandler creates a new StreamHandler backed by the given SubscriberManager.
func NewStreamHandler(manager *SubscriberManager) *StreamHandler {
	return &StreamHandler{manager: manager}
}

// ServeHTTP upgrades the HTTP connection to WebSocket and streams telemetry events.
// Supports an optional query parameter `filter` to receive only events of a specific type.
// Example: /v1/telemetry/stream?filter=agent_call_start
func (h *StreamHandler) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	// Parse optional event type filter from query parameters.
	filterParam := r.URL.Query().Get("filter")
	filter := EventType(filterParam)

	// Upgrade HTTP connection to WebSocket.
	conn, err := wsUpgrader.Upgrade(w, r, nil)
	if err != nil {
		// Upgrade writes the HTTP error response internally.
		return
	}
	defer conn.Close()

	// Generate a unique subscriber ID.
	subID, err := generateSubscriberID()
	if err != nil {
		conn.WriteMessage(websocket.CloseMessage,
			websocket.FormatCloseMessage(websocket.CloseInternalServerErr, "failed to generate subscriber ID"))
		return
	}

	// Create subscriber and register with manager.
	ctx, cancel := context.WithCancel(r.Context())
	defer cancel()

	sub := NewSubscriber(subID, filter, subscriberBufferSize)
	// Override the subscriber's context with the request context so it
	// respects server shutdown.
	sub.ctx = ctx
	sub.cancel = cancel

	h.manager.Add(sub)
	defer h.manager.Remove(subID)

	// Start read pump (handles pongs and detects client disconnect).
	go h.readPump(conn, cancel)

	// Write pump: deliver events from subscriber channel to WebSocket.
	h.writePump(conn, sub)
}

// readPump reads from the WebSocket to detect disconnects and handle pong frames.
// It closes the cancel function when the client disconnects.
func (h *StreamHandler) readPump(conn *websocket.Conn, cancel context.CancelFunc) {
	defer cancel()

	conn.SetReadLimit(512) // We don't expect large messages from clients.
	conn.SetReadDeadline(time.Now().Add(pongWait))
	conn.SetPongHandler(func(string) error {
		conn.SetReadDeadline(time.Now().Add(pongWait))
		return nil
	})

	for {
		_, _, err := conn.ReadMessage()
		if err != nil {
			return
		}
	}
}

// writePump reads events from the subscriber channel and writes them as JSON
// to the WebSocket connection. It also sends periodic ping frames.
func (h *StreamHandler) writePump(conn *websocket.Conn, sub *Subscriber) {
	pingTicker := time.NewTicker(pingPeriod)
	defer pingTicker.Stop()

	for {
		select {
		case <-sub.Done():
			// Subscriber context cancelled (client disconnected or server shutdown).
			conn.WriteMessage(websocket.CloseMessage,
				websocket.FormatCloseMessage(websocket.CloseNormalClosure, ""))
			return

		case event, ok := <-sub.Events:
			if !ok {
				// Channel closed.
				conn.WriteMessage(websocket.CloseMessage,
					websocket.FormatCloseMessage(websocket.CloseNormalClosure, ""))
				return
			}

			conn.SetWriteDeadline(time.Now().Add(writeWait))
			data, err := json.Marshal(event)
			if err != nil {
				continue
			}
			if err := conn.WriteMessage(websocket.TextMessage, data); err != nil {
				return
			}

		case <-pingTicker.C:
			conn.SetWriteDeadline(time.Now().Add(writeWait))
			if err := conn.WriteMessage(websocket.PingMessage, nil); err != nil {
				return
			}
		}
	}
}

// Subscribe creates a subscription for telemetry events using the given
// SubscriberManager. It returns a channel that delivers events matching the
// specified filter. If filter is empty, all events are delivered.
// The channel is closed when the context is cancelled.
func Subscribe(ctx context.Context, manager *SubscriberManager, filter EventType) <-chan TelemetryEvent {
	subID, err := generateSubscriberID()
	if err != nil {
		// Fallback to timestamp-based ID if crypto/rand fails.
		subID = "sub-fallback-" + time.Now().Format("20060102150405.000000")
	}

	sub := NewSubscriber(subID, filter, subscriberBufferSize)

	// Wire the provided context into the subscriber lifecycle.
	go func() {
		<-ctx.Done()
		manager.Remove(subID)
	}()

	manager.Add(sub)
	return sub.Events
}

// generateSubscriberID creates a cryptographically random subscriber identifier.
func generateSubscriberID() (string, error) {
	buf := make([]byte, 8)
	if _, err := rand.Read(buf); err != nil {
		return "", err
	}
	return "telsub-" + hex.EncodeToString(buf), nil
}
