package telemetry

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/gorilla/websocket"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

func TestStreamHandler_BasicConnection(t *testing.T) {
	manager := NewSubscriberManager()
	handler := NewStreamHandler(manager)

	server := httptest.NewServer(handler)
	defer server.Close()

	// Connect via WebSocket.
	wsURL := "ws" + strings.TrimPrefix(server.URL, "http")
	conn, resp, err := websocket.DefaultDialer.Dial(wsURL, nil)
	require.NoError(t, err)
	defer conn.Close()

	assert.Equal(t, http.StatusSwitchingProtocols, resp.StatusCode)

	// Verify a subscriber was registered.
	assert.Equal(t, 1, manager.Count())

	// Close and verify cleanup.
	conn.WriteMessage(websocket.CloseMessage,
		websocket.FormatCloseMessage(websocket.CloseNormalClosure, ""))

	// Allow time for cleanup.
	assert.Eventually(t, func() bool {
		return manager.Count() == 0
	}, 2*time.Second, 50*time.Millisecond)
}

func TestStreamHandler_ReceivesEvents(t *testing.T) {
	manager := NewSubscriberManager()
	handler := NewStreamHandler(manager)

	server := httptest.NewServer(handler)
	defer server.Close()

	wsURL := "ws" + strings.TrimPrefix(server.URL, "http")
	conn, _, err := websocket.DefaultDialer.Dial(wsURL, nil)
	require.NoError(t, err)
	defer conn.Close()

	// Wait for subscriber registration.
	require.Eventually(t, func() bool {
		return manager.Count() == 1
	}, 2*time.Second, 10*time.Millisecond)

	// Broadcast an event.
	event := TelemetryEvent{
		EventID:   "evt-test-001",
		EventType: EventAgentCallStart,
		TaskID:    "task-42",
		Timestamp: time.Now().UTC().Truncate(time.Millisecond),
		Attributes: map[string]interface{}{
			"role":  "planner",
			"model": "claude-sonnet-4-20250514",
		},
	}
	manager.Broadcast(event)

	// Read the event from WebSocket.
	conn.SetReadDeadline(time.Now().Add(5 * time.Second))
	_, message, err := conn.ReadMessage()
	require.NoError(t, err)

	var received TelemetryEvent
	err = json.Unmarshal(message, &received)
	require.NoError(t, err)

	assert.Equal(t, event.EventID, received.EventID)
	assert.Equal(t, event.EventType, received.EventType)
	assert.Equal(t, event.TaskID, received.TaskID)
}

func TestStreamHandler_FilterByEventType(t *testing.T) {
	manager := NewSubscriberManager()
	handler := NewStreamHandler(manager)

	server := httptest.NewServer(handler)
	defer server.Close()

	// Connect with filter for phase_transition events only.
	wsURL := "ws" + strings.TrimPrefix(server.URL, "http") + "?filter=phase_transition"
	conn, _, err := websocket.DefaultDialer.Dial(wsURL, nil)
	require.NoError(t, err)
	defer conn.Close()

	// Wait for subscriber registration.
	require.Eventually(t, func() bool {
		return manager.Count() == 1
	}, 2*time.Second, 10*time.Millisecond)

	// Broadcast an agent_call_start event (should be filtered out).
	filteredEvent := TelemetryEvent{
		EventID:   "evt-filtered",
		EventType: EventAgentCallStart,
		TaskID:    "task-1",
		Timestamp: time.Now().UTC(),
	}
	manager.Broadcast(filteredEvent)

	// Broadcast a phase_transition event (should be delivered).
	matchingEvent := TelemetryEvent{
		EventID:   "evt-matching",
		EventType: EventPhaseTransition,
		TaskID:    "task-1",
		Timestamp: time.Now().UTC(),
	}
	manager.Broadcast(matchingEvent)

	// Read message — should get the matching event only.
	conn.SetReadDeadline(time.Now().Add(2 * time.Second))
	_, message, err := conn.ReadMessage()
	require.NoError(t, err)

	var received TelemetryEvent
	err = json.Unmarshal(message, &received)
	require.NoError(t, err)

	assert.Equal(t, "evt-matching", received.EventID)
	assert.Equal(t, EventPhaseTransition, received.EventType)
}

func TestStreamHandler_NoFilter_ReceivesAll(t *testing.T) {
	manager := NewSubscriberManager()
	handler := NewStreamHandler(manager)

	server := httptest.NewServer(handler)
	defer server.Close()

	// Connect without any filter.
	wsURL := "ws" + strings.TrimPrefix(server.URL, "http")
	conn, _, err := websocket.DefaultDialer.Dial(wsURL, nil)
	require.NoError(t, err)
	defer conn.Close()

	require.Eventually(t, func() bool {
		return manager.Count() == 1
	}, 2*time.Second, 10*time.Millisecond)

	// Broadcast multiple event types.
	events := []TelemetryEvent{
		{EventID: "evt-1", EventType: EventAgentCallStart, Timestamp: time.Now().UTC()},
		{EventID: "evt-2", EventType: EventPhaseTransition, Timestamp: time.Now().UTC()},
		{EventID: "evt-3", EventType: EventHostAction, Timestamp: time.Now().UTC()},
	}
	for _, e := range events {
		manager.Broadcast(e)
	}

	// Should receive all 3 events.
	for i := 0; i < 3; i++ {
		conn.SetReadDeadline(time.Now().Add(2 * time.Second))
		_, message, err := conn.ReadMessage()
		require.NoError(t, err)

		var received TelemetryEvent
		err = json.Unmarshal(message, &received)
		require.NoError(t, err)
		assert.Equal(t, events[i].EventID, received.EventID)
	}
}

func TestStreamHandler_MultipleClients(t *testing.T) {
	manager := NewSubscriberManager()
	handler := NewStreamHandler(manager)

	server := httptest.NewServer(handler)
	defer server.Close()

	wsURL := "ws" + strings.TrimPrefix(server.URL, "http")

	// Connect two clients.
	conn1, _, err := websocket.DefaultDialer.Dial(wsURL, nil)
	require.NoError(t, err)
	defer conn1.Close()

	conn2, _, err := websocket.DefaultDialer.Dial(wsURL+"?filter=host_action", nil)
	require.NoError(t, err)
	defer conn2.Close()

	require.Eventually(t, func() bool {
		return manager.Count() == 2
	}, 2*time.Second, 10*time.Millisecond)

	// Broadcast a host_action event.
	event := TelemetryEvent{
		EventID:   "evt-multi",
		EventType: EventHostAction,
		TaskID:    "task-99",
		Timestamp: time.Now().UTC(),
	}
	manager.Broadcast(event)

	// Both clients should receive it.
	for _, conn := range []*websocket.Conn{conn1, conn2} {
		conn.SetReadDeadline(time.Now().Add(2 * time.Second))
		_, message, err := conn.ReadMessage()
		require.NoError(t, err)

		var received TelemetryEvent
		err = json.Unmarshal(message, &received)
		require.NoError(t, err)
		assert.Equal(t, "evt-multi", received.EventID)
	}
}

func TestStreamHandler_ClientDisconnectCleansUp(t *testing.T) {
	manager := NewSubscriberManager()
	handler := NewStreamHandler(manager)

	server := httptest.NewServer(handler)
	defer server.Close()

	wsURL := "ws" + strings.TrimPrefix(server.URL, "http")
	conn, _, err := websocket.DefaultDialer.Dial(wsURL, nil)
	require.NoError(t, err)

	require.Eventually(t, func() bool {
		return manager.Count() == 1
	}, 2*time.Second, 10*time.Millisecond)

	// Close the connection abruptly.
	conn.Close()

	// Subscriber should be cleaned up.
	assert.Eventually(t, func() bool {
		return manager.Count() == 0
	}, 2*time.Second, 50*time.Millisecond)
}

func TestSubscribe_ReceivesMatchingEvents(t *testing.T) {
	manager := NewSubscriberManager()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	ch := Subscribe(ctx, manager, EventPhaseTransition)

	// Broadcast matching event.
	event := TelemetryEvent{
		EventID:   "evt-sub-1",
		EventType: EventPhaseTransition,
		TaskID:    "task-sub",
		Timestamp: time.Now().UTC(),
	}
	manager.Broadcast(event)

	select {
	case received := <-ch:
		assert.Equal(t, "evt-sub-1", received.EventID)
	case <-time.After(2 * time.Second):
		t.Fatal("timed out waiting for event")
	}
}

func TestSubscribe_FiltersNonMatchingEvents(t *testing.T) {
	manager := NewSubscriberManager()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	ch := Subscribe(ctx, manager, EventShutdown)

	// Broadcast a non-matching event.
	manager.Broadcast(TelemetryEvent{
		EventID:   "evt-wrong-type",
		EventType: EventAgentCallStart,
		Timestamp: time.Now().UTC(),
	})

	// Should not receive anything.
	select {
	case <-ch:
		t.Fatal("should not receive non-matching event")
	case <-time.After(100 * time.Millisecond):
		// Expected — no event received.
	}
}

func TestSubscribe_EmptyFilterReceivesAll(t *testing.T) {
	manager := NewSubscriberManager()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	ch := Subscribe(ctx, manager, "") // Empty filter = all events.

	manager.Broadcast(TelemetryEvent{
		EventID:   "evt-all-1",
		EventType: EventAgentCallStart,
		Timestamp: time.Now().UTC(),
	})
	manager.Broadcast(TelemetryEvent{
		EventID:   "evt-all-2",
		EventType: EventShutdown,
		Timestamp: time.Now().UTC(),
	})

	for i := 1; i <= 2; i++ {
		select {
		case received := <-ch:
			assert.Contains(t, received.EventID, "evt-all-")
		case <-time.After(2 * time.Second):
			t.Fatalf("timed out waiting for event %d", i)
		}
	}
}

func TestSubscribe_ContextCancellationCleansUp(t *testing.T) {
	manager := NewSubscriberManager()
	ctx, cancel := context.WithCancel(context.Background())

	Subscribe(ctx, manager, "")

	// Wait for registration.
	require.Eventually(t, func() bool {
		return manager.Count() == 1
	}, 2*time.Second, 10*time.Millisecond)

	// Cancel context.
	cancel()

	// Subscriber should be removed.
	assert.Eventually(t, func() bool {
		return manager.Count() == 0
	}, 2*time.Second, 50*time.Millisecond)
}

func TestGenerateSubscriberID_Unique(t *testing.T) {
	ids := make(map[string]bool)
	for i := 0; i < 100; i++ {
		id, err := generateSubscriberID()
		require.NoError(t, err)
		assert.True(t, strings.HasPrefix(id, "telsub-"))
		assert.False(t, ids[id], "duplicate ID generated: %s", id)
		ids[id] = true
	}
}
