package telemetry

import (
	"context"
	"fmt"
	"path/filepath"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

func setupTestStore(t *testing.T) *SQLiteStore {
	t.Helper()
	dir := t.TempDir()
	dbPath := filepath.Join(dir, "telemetry_test.db")
	db, err := OpenDB(dbPath)
	require.NoError(t, err)
	t.Cleanup(func() { db.Close() })

	store := NewSQLiteStore(db)
	return store
}

func TestSQLiteStore_Emit(t *testing.T) {
	store := setupTestStore(t)
	defer store.Close()
	ctx := context.Background()

	event := TelemetryEvent{
		EventID:   "evt-100",
		EventType: EventAgentCallEnd,
		TaskID:    "task-abc",
		Timestamp: time.Now().UTC(),
		Attributes: map[string]interface{}{
			"role":          "planner",
			"model":         "claude-sonnet-4-20250514",
			"input_tokens":  float64(500),
			"output_tokens": float64(200),
			"computed_cost": 0.0035,
			"latency_ms":    float64(1200),
			"success":       true,
		},
	}

	err := store.Emit(ctx, event)
	require.NoError(t, err)

	// Verify event was persisted.
	events, total, err := store.Events(ctx, map[string]string{"task_id": "task-abc"}, 1, 10)
	require.NoError(t, err)
	assert.Equal(t, 1, total)
	assert.Len(t, events, 1)
	assert.Equal(t, "evt-100", events[0].EventID)
	assert.Equal(t, EventAgentCallEnd, events[0].EventType)
	assert.Equal(t, "task-abc", events[0].TaskID)
}

func TestSQLiteStore_Emit_GeneratesIDAndTimestamp(t *testing.T) {
	store := setupTestStore(t)
	defer store.Close()
	ctx := context.Background()

	event := TelemetryEvent{
		EventType:  EventPhaseTransition,
		TaskID:     "task-xyz",
		Attributes: map[string]interface{}{"from_phase": "planning", "to_phase": "implementation"},
	}

	err := store.Emit(ctx, event)
	require.NoError(t, err)

	events, total, err := store.Events(ctx, map[string]string{"task_id": "task-xyz"}, 1, 10)
	require.NoError(t, err)
	assert.Equal(t, 1, total)
	assert.NotEmpty(t, events[0].EventID)
	assert.False(t, events[0].Timestamp.IsZero())
}

func TestSQLiteStore_Emit_BroadcastsToSubscribers(t *testing.T) {
	store := setupTestStore(t)
	defer store.Close()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	ch := store.Subscribe(ctx, EventAgentCallEnd)

	event := TelemetryEvent{
		EventID:    "evt-broadcast",
		EventType:  EventAgentCallEnd,
		TaskID:     "task-1",
		Timestamp:  time.Now().UTC(),
		Attributes: map[string]interface{}{},
	}

	err := store.Emit(context.Background(), event)
	require.NoError(t, err)

	select {
	case received := <-ch:
		assert.Equal(t, "evt-broadcast", received.EventID)
	case <-time.After(2 * time.Second):
		t.Fatal("timed out waiting for broadcast event")
	}
}

func TestSQLiteStore_Query_Flat(t *testing.T) {
	store := setupTestStore(t)
	defer store.Close()
	ctx := context.Background()

	now := time.Now().UTC()

	// Insert some agent_call_end events with cost/token/latency attributes.
	events := []TelemetryEvent{
		{
			EventID: "q-1", EventType: EventAgentCallEnd, TaskID: "task-1",
			Timestamp: now.Add(-1 * time.Hour),
			Attributes: map[string]interface{}{
				"computed_cost": 0.01, "input_tokens": float64(100), "output_tokens": float64(50),
				"latency_ms": float64(500), "success": true, "role": "planner", "model": "gpt-4",
			},
		},
		{
			EventID: "q-2", EventType: EventAgentCallEnd, TaskID: "task-1",
			Timestamp: now.Add(-30 * time.Minute),
			Attributes: map[string]interface{}{
				"computed_cost": 0.02, "input_tokens": float64(200), "output_tokens": float64(100),
				"latency_ms": float64(1000), "success": true, "role": "coder", "model": "claude-sonnet-4-20250514",
			},
		},
		{
			EventID: "q-3", EventType: EventAgentCallEnd, TaskID: "task-2",
			Timestamp: now.Add(-10 * time.Minute),
			Attributes: map[string]interface{}{
				"computed_cost": 0.005, "input_tokens": float64(50), "output_tokens": float64(25),
				"latency_ms": float64(300), "success": false, "role": "planner", "model": "gpt-4",
			},
		},
	}

	for _, ev := range events {
		require.NoError(t, store.Emit(ctx, ev))
	}

	result, err := store.Query(ctx, SummaryQuery{
		StartTime: now.Add(-2 * time.Hour),
		EndTime:   now,
	})
	require.NoError(t, err)

	assert.Equal(t, int64(3), result.CallCount)
	assert.InDelta(t, 0.035, result.TotalCost, 0.001)
	assert.Equal(t, int64(525), result.TotalTokens)
	assert.InDelta(t, 600.0, result.AvgLatencyMS, 1.0)
	// 1 out of 3 failed.
	assert.InDelta(t, 1.0/3.0, result.ErrorRate, 0.01)
}

func TestSQLiteStore_Query_GroupByRole(t *testing.T) {
	store := setupTestStore(t)
	defer store.Close()
	ctx := context.Background()

	now := time.Now().UTC()

	events := []TelemetryEvent{
		{
			EventID: "g-1", EventType: EventAgentCallEnd, TaskID: "task-1",
			Timestamp: now.Add(-1 * time.Hour),
			Attributes: map[string]interface{}{
				"computed_cost": 0.01, "input_tokens": float64(100), "output_tokens": float64(50),
				"latency_ms": float64(500), "success": true, "role": "planner", "model": "gpt-4",
			},
		},
		{
			EventID: "g-2", EventType: EventAgentCallEnd, TaskID: "task-1",
			Timestamp: now.Add(-30 * time.Minute),
			Attributes: map[string]interface{}{
				"computed_cost": 0.02, "input_tokens": float64(200), "output_tokens": float64(100),
				"latency_ms": float64(1000), "success": true, "role": "coder", "model": "claude-sonnet-4-20250514",
			},
		},
		{
			EventID: "g-3", EventType: EventAgentCallEnd, TaskID: "task-2",
			Timestamp: now.Add(-10 * time.Minute),
			Attributes: map[string]interface{}{
				"computed_cost": 0.005, "input_tokens": float64(50), "output_tokens": float64(25),
				"latency_ms": float64(300), "success": true, "role": "planner", "model": "gpt-4",
			},
		},
	}

	for _, ev := range events {
		require.NoError(t, store.Emit(ctx, ev))
	}

	result, err := store.Query(ctx, SummaryQuery{
		StartTime: now.Add(-2 * time.Hour),
		EndTime:   now,
		GroupBy:   []string{"role"},
	})
	require.NoError(t, err)

	assert.Equal(t, int64(3), result.CallCount)
	require.NotNil(t, result.Groups)
	assert.Len(t, result.Groups, 2)

	plannerGroup, ok := result.Groups["planner"]
	require.True(t, ok)
	assert.Equal(t, int64(2), plannerGroup.CallCount)

	coderGroup, ok := result.Groups["coder"]
	require.True(t, ok)
	assert.Equal(t, int64(1), coderGroup.CallCount)
}

func TestSQLiteStore_Query_GroupByModel(t *testing.T) {
	store := setupTestStore(t)
	defer store.Close()
	ctx := context.Background()

	now := time.Now().UTC()

	events := []TelemetryEvent{
		{
			EventID: "m-1", EventType: EventAgentCallEnd, TaskID: "task-1",
			Timestamp: now.Add(-1 * time.Hour),
			Attributes: map[string]interface{}{
				"computed_cost": 0.01, "input_tokens": float64(100), "output_tokens": float64(50),
				"latency_ms": float64(500), "success": true, "role": "planner", "model": "gpt-4",
			},
		},
		{
			EventID: "m-2", EventType: EventAgentCallEnd, TaskID: "task-1",
			Timestamp: now.Add(-30 * time.Minute),
			Attributes: map[string]interface{}{
				"computed_cost": 0.02, "input_tokens": float64(200), "output_tokens": float64(100),
				"latency_ms": float64(1000), "success": true, "role": "coder", "model": "claude-sonnet-4-20250514",
			},
		},
	}

	for _, ev := range events {
		require.NoError(t, store.Emit(ctx, ev))
	}

	result, err := store.Query(ctx, SummaryQuery{
		StartTime: now.Add(-2 * time.Hour),
		EndTime:   now,
		GroupBy:   []string{"model"},
	})
	require.NoError(t, err)

	require.NotNil(t, result.Groups)
	assert.Len(t, result.Groups, 2)
	_, hasGPT4 := result.Groups["gpt-4"]
	assert.True(t, hasGPT4)
	_, hasClaude := result.Groups["claude-sonnet-4-20250514"]
	assert.True(t, hasClaude)
}

func TestSQLiteStore_Query_GroupByTaskID(t *testing.T) {
	store := setupTestStore(t)
	defer store.Close()
	ctx := context.Background()

	now := time.Now().UTC()

	events := []TelemetryEvent{
		{
			EventID: "t-1", EventType: EventAgentCallEnd, TaskID: "task-A",
			Timestamp: now.Add(-1 * time.Hour),
			Attributes: map[string]interface{}{
				"computed_cost": 0.01, "input_tokens": float64(100), "output_tokens": float64(50),
				"latency_ms": float64(500), "success": true, "role": "planner", "model": "gpt-4",
			},
		},
		{
			EventID: "t-2", EventType: EventAgentCallEnd, TaskID: "task-B",
			Timestamp: now.Add(-30 * time.Minute),
			Attributes: map[string]interface{}{
				"computed_cost": 0.02, "input_tokens": float64(200), "output_tokens": float64(100),
				"latency_ms": float64(1000), "success": true, "role": "coder", "model": "claude-sonnet-4-20250514",
			},
		},
	}

	for _, ev := range events {
		require.NoError(t, store.Emit(ctx, ev))
	}

	result, err := store.Query(ctx, SummaryQuery{
		StartTime: now.Add(-2 * time.Hour),
		EndTime:   now,
		GroupBy:   []string{"task_id"},
	})
	require.NoError(t, err)

	require.NotNil(t, result.Groups)
	assert.Len(t, result.Groups, 2)
	_, hasTaskA := result.Groups["task-A"]
	assert.True(t, hasTaskA)
	_, hasTaskB := result.Groups["task-B"]
	assert.True(t, hasTaskB)
}

func TestSQLiteStore_Events_Pagination(t *testing.T) {
	store := setupTestStore(t)
	defer store.Close()
	ctx := context.Background()

	now := time.Now().UTC()

	// Insert 15 events.
	for i := 0; i < 15; i++ {
		ev := TelemetryEvent{
			EventID:    fmt.Sprintf("page-evt-%d", i),
			EventType:  EventAgentCallStart,
			TaskID:     "task-page",
			Timestamp:  now.Add(time.Duration(i) * time.Minute),
			Attributes: map[string]interface{}{},
		}
		require.NoError(t, store.Emit(ctx, ev))
	}

	// Page 1 with size 5.
	events, total, err := store.Events(ctx, map[string]string{}, 1, 5)
	require.NoError(t, err)
	assert.Equal(t, 15, total)
	assert.Len(t, events, 5)

	// Page 3 with size 5.
	events, total, err = store.Events(ctx, map[string]string{}, 3, 5)
	require.NoError(t, err)
	assert.Equal(t, 15, total)
	assert.Len(t, events, 5)

	// Page 4 with size 5 (should be empty).
	events, total, err = store.Events(ctx, map[string]string{}, 4, 5)
	require.NoError(t, err)
	assert.Equal(t, 15, total)
	assert.Len(t, events, 0)
}

func TestSQLiteStore_Events_FilterByEventType(t *testing.T) {
	store := setupTestStore(t)
	defer store.Close()
	ctx := context.Background()

	now := time.Now().UTC()

	events := []TelemetryEvent{
		{EventID: "f-1", EventType: EventAgentCallStart, TaskID: "t1", Timestamp: now, Attributes: map[string]interface{}{}},
		{EventID: "f-2", EventType: EventAgentCallEnd, TaskID: "t1", Timestamp: now, Attributes: map[string]interface{}{}},
		{EventID: "f-3", EventType: EventPhaseTransition, TaskID: "t1", Timestamp: now, Attributes: map[string]interface{}{}},
	}
	for _, ev := range events {
		require.NoError(t, store.Emit(ctx, ev))
	}

	result, total, err := store.Events(ctx, map[string]string{"event_type": "agent_call_start"}, 1, 50)
	require.NoError(t, err)
	assert.Equal(t, 1, total)
	assert.Len(t, result, 1)
	assert.Equal(t, "f-1", result[0].EventID)
}

func TestSQLiteStore_Events_FilterByRole(t *testing.T) {
	store := setupTestStore(t)
	defer store.Close()
	ctx := context.Background()

	now := time.Now().UTC()

	events := []TelemetryEvent{
		{EventID: "r-1", EventType: EventAgentCallEnd, TaskID: "t1", Timestamp: now, Attributes: map[string]interface{}{"role": "planner"}},
		{EventID: "r-2", EventType: EventAgentCallEnd, TaskID: "t1", Timestamp: now, Attributes: map[string]interface{}{"role": "coder"}},
	}
	for _, ev := range events {
		require.NoError(t, store.Emit(ctx, ev))
	}

	result, total, err := store.Events(ctx, map[string]string{"role": "planner"}, 1, 50)
	require.NoError(t, err)
	assert.Equal(t, 1, total)
	assert.Len(t, result, 1)
	assert.Equal(t, "r-1", result[0].EventID)
}

func TestSQLiteStore_Events_FilterByModel(t *testing.T) {
	store := setupTestStore(t)
	defer store.Close()
	ctx := context.Background()

	now := time.Now().UTC()

	events := []TelemetryEvent{
		{EventID: "m-1", EventType: EventAgentCallEnd, TaskID: "t1", Timestamp: now, Attributes: map[string]interface{}{"model": "gpt-4"}},
		{EventID: "m-2", EventType: EventAgentCallEnd, TaskID: "t1", Timestamp: now, Attributes: map[string]interface{}{"model": "claude-sonnet-4-20250514"}},
	}
	for _, ev := range events {
		require.NoError(t, store.Emit(ctx, ev))
	}

	result, total, err := store.Events(ctx, map[string]string{"model": "gpt-4"}, 1, 50)
	require.NoError(t, err)
	assert.Equal(t, 1, total)
	assert.Len(t, result, 1)
	assert.Equal(t, "m-1", result[0].EventID)
}

func TestSQLiteStore_Subscribe_FilteredByEventType(t *testing.T) {
	store := setupTestStore(t)
	defer store.Close()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	ch := store.Subscribe(ctx, EventPhaseTransition)

	// Emit an event that does NOT match the filter.
	err := store.Emit(context.Background(), TelemetryEvent{
		EventID: "no-match", EventType: EventAgentCallStart, TaskID: "t1",
		Timestamp: time.Now().UTC(), Attributes: map[string]interface{}{},
	})
	require.NoError(t, err)

	// Emit an event that DOES match the filter.
	err = store.Emit(context.Background(), TelemetryEvent{
		EventID: "match", EventType: EventPhaseTransition, TaskID: "t1",
		Timestamp: time.Now().UTC(), Attributes: map[string]interface{}{},
	})
	require.NoError(t, err)

	select {
	case received := <-ch:
		assert.Equal(t, "match", received.EventID)
	case <-time.After(2 * time.Second):
		t.Fatal("timed out waiting for filtered event")
	}
}

func TestSQLiteStore_Subscribe_ContextCancellation(t *testing.T) {
	store := setupTestStore(t)
	defer store.Close()
	ctx, cancel := context.WithCancel(context.Background())

	_ = store.Subscribe(ctx, "")

	// Verify subscriber was added.
	assert.Equal(t, 1, store.subscribers.Count())

	// Cancel context.
	cancel()

	// Give goroutine time to clean up.
	time.Sleep(100 * time.Millisecond)

	assert.Equal(t, 0, store.subscribers.Count())
}

func TestSQLiteStore_RetentionConfig(t *testing.T) {
	store := setupTestStore(t)
	defer store.Close()

	assert.Equal(t, DefaultRetentionDays, store.retentionDays)

	// Create store with custom retention.
	dir := t.TempDir()
	dbPath := filepath.Join(dir, "retention_test.db")
	db, err := OpenDB(dbPath)
	require.NoError(t, err)
	defer db.Close()

	customStore := NewSQLiteStore(db, WithRetentionDays(30))
	defer customStore.Close()
	assert.Equal(t, 30, customStore.retentionDays)
}

func TestSQLiteStore_PruneExpired(t *testing.T) {
	store := setupTestStore(t)
	defer store.Close()
	ctx := context.Background()

	// Set retention to 1 day for test purposes.
	store.retentionDays = 1

	// Insert an event from 2 days ago.
	oldEvent := TelemetryEvent{
		EventID:    "old-evt",
		EventType:  EventAgentCallStart,
		TaskID:     "old-task",
		Timestamp:  time.Now().UTC().AddDate(0, 0, -2),
		Attributes: map[string]interface{}{},
	}
	require.NoError(t, store.Emit(ctx, oldEvent))

	// Insert a recent event.
	recentEvent := TelemetryEvent{
		EventID:    "recent-evt",
		EventType:  EventAgentCallStart,
		TaskID:     "recent-task",
		Timestamp:  time.Now().UTC(),
		Attributes: map[string]interface{}{},
	}
	require.NoError(t, store.Emit(ctx, recentEvent))

	// Run pruning.
	store.pruneExpired()

	// The old event should be gone.
	events, total, err := store.Events(ctx, map[string]string{}, 1, 50)
	require.NoError(t, err)
	assert.Equal(t, 1, total)
	assert.Len(t, events, 1)
	assert.Equal(t, "recent-evt", events[0].EventID)
}

func TestSQLiteStore_Query_EmptyTimeRange(t *testing.T) {
	store := setupTestStore(t)
	defer store.Close()
	ctx := context.Background()

	// Query a time range with no events.
	result, err := store.Query(ctx, SummaryQuery{
		StartTime: time.Now().UTC().Add(-2 * time.Hour),
		EndTime:   time.Now().UTC().Add(-1 * time.Hour),
	})
	require.NoError(t, err)
	assert.Equal(t, int64(0), result.CallCount)
	assert.Equal(t, float64(0), result.TotalCost)
	assert.Equal(t, int64(0), result.TotalTokens)
}

func TestSQLiteStore_Query_UnsupportedGroupBy(t *testing.T) {
	store := setupTestStore(t)
	defer store.Close()
	ctx := context.Background()

	_, err := store.Query(ctx, SummaryQuery{
		StartTime: time.Now().UTC().Add(-1 * time.Hour),
		EndTime:   time.Now().UTC(),
		GroupBy:   []string{"invalid_field"},
	})
	assert.Error(t, err)
	assert.Contains(t, err.Error(), "unsupported group_by field")
}
