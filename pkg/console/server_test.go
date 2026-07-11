package console

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/gorilla/websocket"
)

func TestProgressHubBroadcastsPhaseChangeToClient(t *testing.T) {
	hub := NewProgressHub()
	defer hub.Stop()

	client := &progressClient{send: make(chan ConsoleEvent, 16)}
	hub.register <- client
	// Give the hub goroutine time to register
	time.Sleep(10 * time.Millisecond)

	hub.EmitPhaseChange("task-001", "planning", "implementation", "plan complete")

	select {
	case event := <-client.send:
		if event.Type != EventPhaseChange {
			t.Fatalf("expected phase_change, got %s", event.Type)
		}
		if event.TaskID != "task-001" {
			t.Fatalf("expected task-001, got %s", event.TaskID)
		}
		data, ok := event.Data.(PhaseChangeEvent)
		if !ok {
			t.Fatal("expected PhaseChangeEvent data")
		}
		if data.FromPhase != "planning" || data.ToPhase != "implementation" {
			t.Fatalf("unexpected phase data: %+v", data)
		}
		if data.Reason != "plan complete" {
			t.Fatalf("unexpected reason: %s", data.Reason)
		}
	case <-time.After(500 * time.Millisecond):
		t.Fatal("event not received within 500ms")
	}

	hub.unregister <- client
}

func TestProgressHubBroadcastsAgentCallComplete(t *testing.T) {
	hub := NewProgressHub()
	defer hub.Stop()

	client := &progressClient{send: make(chan ConsoleEvent, 16)}
	hub.register <- client
	time.Sleep(10 * time.Millisecond)

	hub.EmitAgentCallComplete("task-002", AgentCallEvent{
		Role:       "implementer",
		DurationMS: 3500,
		Tokens:     1200,
		CostUSD:    0.0045,
		Summary:    "Implemented login function",
		Model:      "gpt-4",
		Provider:   "openai",
		Success:    true,
	})

	select {
	case event := <-client.send:
		if event.Type != EventAgentCallComplete {
			t.Fatalf("expected agent_call_complete, got %s", event.Type)
		}
		if event.TaskID != "task-002" {
			t.Fatalf("expected task-002, got %s", event.TaskID)
		}
		data, ok := event.Data.(AgentCallEvent)
		if !ok {
			t.Fatal("expected AgentCallEvent data")
		}
		if data.Role != "implementer" {
			t.Fatalf("unexpected role: %s", data.Role)
		}
		if data.DurationMS != 3500 {
			t.Fatalf("unexpected duration: %d", data.DurationMS)
		}
		if data.Tokens != 1200 {
			t.Fatalf("unexpected tokens: %d", data.Tokens)
		}
		if data.CostUSD != 0.0045 {
			t.Fatalf("unexpected cost: %f", data.CostUSD)
		}
		if data.Summary != "Implemented login function" {
			t.Fatalf("unexpected summary: %s", data.Summary)
		}
	case <-time.After(500 * time.Millisecond):
		t.Fatal("event not received within 500ms")
	}

	hub.unregister <- client
}

func TestProgressHubBroadcastsTaskStatusChange(t *testing.T) {
	hub := NewProgressHub()
	defer hub.Stop()

	client := &progressClient{send: make(chan ConsoleEvent, 16)}
	hub.register <- client
	time.Sleep(10 * time.Millisecond)

	hub.EmitTaskStatusChange("task-003", "running", "paused", "budget_exceeded")

	select {
	case event := <-client.send:
		if event.Type != EventTaskStatusChange {
			t.Fatalf("expected task_status_change, got %s", event.Type)
		}
		data, ok := event.Data.(TaskStatusEvent)
		if !ok {
			t.Fatal("expected TaskStatusEvent data")
		}
		if data.OldStatus != "running" || data.NewStatus != "paused" {
			t.Fatalf("unexpected status: %+v", data)
		}
		if data.Reason != "budget_exceeded" {
			t.Fatalf("unexpected reason: %s", data.Reason)
		}
	case <-time.After(500 * time.Millisecond):
		t.Fatal("event not received within 500ms")
	}

	hub.unregister <- client
}

func TestProgressHubTaskFilter(t *testing.T) {
	hub := NewProgressHub()
	defer hub.Stop()

	// Client filtered to task-A only
	client := &progressClient{
		send:       make(chan ConsoleEvent, 16),
		taskFilter: "task-A",
	}
	hub.register <- client
	time.Sleep(10 * time.Millisecond)

	// Emit event for task-B (should be filtered out)
	hub.EmitPhaseChange("task-B", "planning", "implementation", "")
	// Emit event for task-A (should be received)
	hub.EmitPhaseChange("task-A", "verification", "review", "tests pass")

	select {
	case event := <-client.send:
		if event.TaskID != "task-A" {
			t.Fatalf("expected task-A, got %s", event.TaskID)
		}
	case <-time.After(500 * time.Millisecond):
		t.Fatal("filtered event not received within 500ms")
	}

	// Ensure no extra events from task-B
	select {
	case event := <-client.send:
		t.Fatalf("unexpected event received: %+v", event)
	case <-time.After(50 * time.Millisecond):
		// Good — no extra events
	}

	hub.unregister <- client
}

func TestProgressHubMultipleClients(t *testing.T) {
	hub := NewProgressHub()
	defer hub.Stop()

	const numClients = 5
	clients := make([]*progressClient, numClients)
	for i := range clients {
		clients[i] = &progressClient{send: make(chan ConsoleEvent, 16)}
		hub.register <- clients[i]
	}
	time.Sleep(10 * time.Millisecond)

	hub.EmitPhaseChange("task-multi", "planning", "implementation", "")

	for i, client := range clients {
		select {
		case event := <-client.send:
			if event.TaskID != "task-multi" {
				t.Fatalf("client %d: expected task-multi, got %s", i, event.TaskID)
			}
		case <-time.After(500 * time.Millisecond):
			t.Fatalf("client %d: event not received within 500ms", i)
		}
	}

	for _, client := range clients {
		hub.unregister <- client
	}
}

func TestProgressHubClientCount(t *testing.T) {
	hub := NewProgressHub()
	defer hub.Stop()

	if hub.ClientCount() != 0 {
		t.Fatalf("expected 0 clients, got %d", hub.ClientCount())
	}

	client := &progressClient{send: make(chan ConsoleEvent, 16)}
	hub.register <- client
	time.Sleep(10 * time.Millisecond)

	if hub.ClientCount() != 1 {
		t.Fatalf("expected 1 client, got %d", hub.ClientCount())
	}

	hub.unregister <- client
	time.Sleep(10 * time.Millisecond)

	if hub.ClientCount() != 0 {
		t.Fatalf("expected 0 clients after unregister, got %d", hub.ClientCount())
	}
}

func TestPhaseChangeDeliveredWithin500ms(t *testing.T) {
	hub := NewProgressHub()
	defer hub.Stop()

	client := &progressClient{send: make(chan ConsoleEvent, 16)}
	hub.register <- client
	time.Sleep(10 * time.Millisecond)

	start := time.Now()
	hub.EmitPhaseChange("task-timing", "implementation", "verification", "code done")

	select {
	case <-client.send:
		elapsed := time.Since(start)
		if elapsed > 500*time.Millisecond {
			t.Fatalf("event delivery took %v, exceeds 500ms requirement", elapsed)
		}
	case <-time.After(500 * time.Millisecond):
		t.Fatal("event not received within 500ms deadline")
	}

	hub.unregister <- client
}

func TestConsoleWSEndToEnd(t *testing.T) {
	// Create a test server with the console WebSocket handler
	server := &Server{
		hub:         newWSHub(),
		progressHub: NewProgressHub(),
	}
	defer server.progressHub.Stop()

	mux := http.NewServeMux()
	mux.HandleFunc("/console/ws", server.handleConsoleWS)

	ts := httptest.NewServer(mux)
	defer ts.Close()

	// Connect WebSocket client
	wsURL := "ws" + strings.TrimPrefix(ts.URL, "http") + "/console/ws"
	ws, _, err := websocket.DefaultDialer.Dial(wsURL, nil)
	if err != nil {
		t.Fatalf("WebSocket dial failed: %v", err)
	}
	defer ws.Close()

	// Give the client time to register
	time.Sleep(50 * time.Millisecond)

	// Emit a phase change event
	server.progressHub.EmitPhaseChange("task-ws-001", "planning", "implementation", "plan approved")

	// Read the event from WebSocket
	ws.SetReadDeadline(time.Now().Add(2 * time.Second))
	_, message, err := ws.ReadMessage()
	if err != nil {
		t.Fatalf("WebSocket read failed: %v", err)
	}

	var event ConsoleEvent
	if err := json.Unmarshal(message, &event); err != nil {
		t.Fatalf("JSON unmarshal failed: %v", err)
	}

	if event.Type != EventPhaseChange {
		t.Fatalf("expected phase_change, got %s", event.Type)
	}
	if event.TaskID != "task-ws-001" {
		t.Fatalf("expected task-ws-001, got %s", event.TaskID)
	}

	// Verify the data field is a map (JSON unmarshals interface{} to map)
	dataMap, ok := event.Data.(map[string]interface{})
	if !ok {
		t.Fatalf("expected map data, got %T", event.Data)
	}
	if dataMap["from_phase"] != "planning" {
		t.Fatalf("unexpected from_phase: %v", dataMap["from_phase"])
	}
	if dataMap["to_phase"] != "implementation" {
		t.Fatalf("unexpected to_phase: %v", dataMap["to_phase"])
	}
}

func TestConsoleWSAgentCallEvent(t *testing.T) {
	server := &Server{
		hub:         newWSHub(),
		progressHub: NewProgressHub(),
	}
	defer server.progressHub.Stop()

	mux := http.NewServeMux()
	mux.HandleFunc("/console/ws", server.handleConsoleWS)

	ts := httptest.NewServer(mux)
	defer ts.Close()

	wsURL := "ws" + strings.TrimPrefix(ts.URL, "http") + "/console/ws"
	ws, _, err := websocket.DefaultDialer.Dial(wsURL, nil)
	if err != nil {
		t.Fatalf("WebSocket dial failed: %v", err)
	}
	defer ws.Close()

	time.Sleep(50 * time.Millisecond)

	// Emit an agent call complete event
	server.progressHub.EmitAgentCallComplete("task-ws-002", AgentCallEvent{
		Role:       "reviewer",
		DurationMS: 5000,
		Tokens:     2500,
		CostUSD:    0.0125,
		Summary:    "Code review completed",
		Model:      "claude-3",
		Provider:   "anthropic",
		Success:    true,
	})

	ws.SetReadDeadline(time.Now().Add(2 * time.Second))
	_, message, err := ws.ReadMessage()
	if err != nil {
		t.Fatalf("WebSocket read failed: %v", err)
	}

	var event ConsoleEvent
	if err := json.Unmarshal(message, &event); err != nil {
		t.Fatalf("JSON unmarshal failed: %v", err)
	}

	if event.Type != EventAgentCallComplete {
		t.Fatalf("expected agent_call_complete, got %s", event.Type)
	}
	if event.TaskID != "task-ws-002" {
		t.Fatalf("expected task-ws-002, got %s", event.TaskID)
	}

	dataMap, ok := event.Data.(map[string]interface{})
	if !ok {
		t.Fatalf("expected map data, got %T", event.Data)
	}
	if dataMap["role"] != "reviewer" {
		t.Fatalf("unexpected role: %v", dataMap["role"])
	}
	if dataMap["summary"] != "Code review completed" {
		t.Fatalf("unexpected summary: %v", dataMap["summary"])
	}
	if dataMap["tokens"].(float64) != 2500 {
		t.Fatalf("unexpected tokens: %v", dataMap["tokens"])
	}
}

func TestConsoleWSTaskFilter(t *testing.T) {
	server := &Server{
		hub:         newWSHub(),
		progressHub: NewProgressHub(),
	}
	defer server.progressHub.Stop()

	mux := http.NewServeMux()
	mux.HandleFunc("/console/ws", server.handleConsoleWS)

	ts := httptest.NewServer(mux)
	defer ts.Close()

	wsURL := "ws" + strings.TrimPrefix(ts.URL, "http") + "/console/ws"
	ws, _, err := websocket.DefaultDialer.Dial(wsURL, nil)
	if err != nil {
		t.Fatalf("WebSocket dial failed: %v", err)
	}
	defer ws.Close()

	time.Sleep(50 * time.Millisecond)

	// Send filter subscription
	filterMsg := `{"filter_task":"task-filtered"}`
	if err := ws.WriteMessage(websocket.TextMessage, []byte(filterMsg)); err != nil {
		t.Fatalf("write filter failed: %v", err)
	}
	time.Sleep(50 * time.Millisecond)

	// Emit event for different task (should be filtered)
	server.progressHub.EmitPhaseChange("task-other", "planning", "implementation", "")
	// Emit event for filtered task (should be received)
	server.progressHub.EmitPhaseChange("task-filtered", "implementation", "verification", "done")

	ws.SetReadDeadline(time.Now().Add(2 * time.Second))
	_, message, err := ws.ReadMessage()
	if err != nil {
		t.Fatalf("WebSocket read failed: %v", err)
	}

	var event ConsoleEvent
	if err := json.Unmarshal(message, &event); err != nil {
		t.Fatalf("JSON unmarshal failed: %v", err)
	}

	if event.TaskID != "task-filtered" {
		t.Fatalf("expected task-filtered, got %s (filter not applied)", event.TaskID)
	}
}

func TestConsoleUIHandler(t *testing.T) {
	server := &Server{
		hub:         newWSHub(),
		progressHub: NewProgressHub(),
	}
	defer server.progressHub.Stop()

	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodGet, "/console", nil)
	server.handleConsoleUI(recorder, request)

	if recorder.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", recorder.Code)
	}
	contentType := recorder.Header().Get("Content-Type")
	if !strings.Contains(contentType, "text/html") {
		t.Fatalf("expected text/html content type, got %s", contentType)
	}
	body := recorder.Body.String()
	if !strings.Contains(body, "Vikram Console") {
		t.Fatal("expected console HTML to contain 'Vikram Console'")
	}
	if !strings.Contains(body, "/console/ws") {
		t.Fatal("expected console HTML to reference /console/ws WebSocket endpoint")
	}
}

func TestConsoleUIRejectsNonGET(t *testing.T) {
	server := &Server{
		hub:         newWSHub(),
		progressHub: NewProgressHub(),
	}
	defer server.progressHub.Stop()

	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodPost, "/console", nil)
	server.handleConsoleUI(recorder, request)

	if recorder.Code != http.StatusMethodNotAllowed {
		t.Fatalf("expected 405, got %d", recorder.Code)
	}
}

func TestConcurrentEmitAndReceive(t *testing.T) {
	hub := NewProgressHub()
	defer hub.Stop()

	const numClients = 3
	const numEvents = 50

	clients := make([]*progressClient, numClients)
	for i := range clients {
		clients[i] = &progressClient{send: make(chan ConsoleEvent, numEvents*3)}
		hub.register <- clients[i]
	}
	time.Sleep(20 * time.Millisecond)

	// Emit events concurrently
	var wg sync.WaitGroup
	for i := 0; i < numEvents; i++ {
		wg.Add(1)
		go func(n int) {
			defer wg.Done()
			switch n % 3 {
			case 0:
				hub.EmitPhaseChange("task-concurrent", "phase-a", "phase-b", "")
			case 1:
				hub.EmitAgentCallComplete("task-concurrent", AgentCallEvent{
					Role: "worker", DurationMS: 100, Tokens: 50, CostUSD: 0.001, Summary: "test",
				})
			case 2:
				hub.EmitTaskStatusChange("task-concurrent", "running", "paused", "")
			}
		}(i)
	}
	wg.Wait()

	// Wait for delivery
	time.Sleep(100 * time.Millisecond)

	// Each client should have received events
	for i, client := range clients {
		count := len(client.send)
		if count == 0 {
			t.Fatalf("client %d received 0 events, expected > 0", i)
		}
	}

	for _, client := range clients {
		hub.unregister <- client
	}
}
