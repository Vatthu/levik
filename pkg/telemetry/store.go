// Package telemetry provides structured event collection, time-series storage,
// query APIs, WebSocket streaming, and health alerting for the Vikram autonomous
// engineering platform.
package telemetry

import (
	"context"
	"time"
)

// EventType identifies the category of a telemetry event.
type EventType string

const (
	EventAgentCallStart  EventType = "agent_call_start"
	EventAgentCallEnd    EventType = "agent_call_end"
	EventPhaseTransition EventType = "phase_transition"
	EventHostAction      EventType = "host_action"
	EventCircuitBreaker  EventType = "circuit_breaker"
	EventShutdown        EventType = "shutdown"
	EventRecovery        EventType = "recovery"
)

// TelemetryEvent represents a single structured event emitted by the platform.
type TelemetryEvent struct {
	EventID    string                 `json:"event_id"`
	EventType  EventType              `json:"event_type"`
	TaskID     string                 `json:"task_id"`
	Timestamp  time.Time              `json:"timestamp"`
	Attributes map[string]interface{} `json:"attributes"`
}

// SummaryQuery defines the parameters for aggregated telemetry queries.
type SummaryQuery struct {
	StartTime time.Time `json:"start_time"`
	EndTime   time.Time `json:"end_time"`
	GroupBy   []string  `json:"group_by"` // supported: "role", "model", "task_id"
}

// SummaryResult holds aggregated telemetry metrics for a time window.
type SummaryResult struct {
	TotalCost    float64                  `json:"total_cost"`
	TotalTokens  int64                    `json:"total_tokens"`
	CallCount    int64                    `json:"call_count"`
	AvgLatencyMS float64                  `json:"avg_latency_ms"`
	ErrorRate    float64                  `json:"error_rate"`
	Groups       map[string]SummaryResult `json:"groups,omitempty"`
}

// Store defines the interface for telemetry event persistence and querying.
type Store interface {
	// Emit persists a telemetry event and notifies any active subscribers.
	Emit(ctx context.Context, event TelemetryEvent) error

	// Query returns aggregated metrics for events within the specified time window,
	// optionally grouped by the fields specified in SummaryQuery.GroupBy.
	Query(ctx context.Context, q SummaryQuery) (SummaryResult, error)

	// Events returns paginated raw telemetry events matching the given filters.
	// Supported filter keys: "task_id", "event_type", "role", "model".
	// Returns the matching events and total count for pagination.
	Events(ctx context.Context, filters map[string]string, page, pageSize int) ([]TelemetryEvent, int, error)

	// Subscribe returns a channel that receives events matching the given filter.
	// If filter is empty, all events are delivered. The channel is closed when
	// the context is cancelled.
	Subscribe(ctx context.Context, filter EventType) <-chan TelemetryEvent
}
