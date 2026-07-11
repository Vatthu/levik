package telemetry

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"strings"
	"sync"
	"time"

	"github.com/google/uuid"
)

// Compile-time interface compliance check.
var _ Store = (*SQLiteStore)(nil)

// SQLiteStore is a concrete implementation of the Store interface backed by SQLite.
type SQLiteStore struct {
	db            *sql.DB
	subscribers   *SubscriberManager
	retentionDays int
	stopPruning   chan struct{}
	pruneOnce     sync.Once
}

// SQLiteStoreOption configures the SQLiteStore.
type SQLiteStoreOption func(*SQLiteStore)

// WithRetentionDays sets the retention period for telemetry events.
func WithRetentionDays(days int) SQLiteStoreOption {
	return func(s *SQLiteStore) {
		if days > 0 {
			s.retentionDays = days
		}
	}
}

// WithSubscriberManager sets the subscriber manager for broadcasting events.
func WithSubscriberManager(sm *SubscriberManager) SQLiteStoreOption {
	return func(s *SQLiteStore) {
		s.subscribers = sm
	}
}

// NewSQLiteStore creates a new SQLiteStore with the given database and options.
func NewSQLiteStore(db *sql.DB, opts ...SQLiteStoreOption) *SQLiteStore {
	s := &SQLiteStore{
		db:            db,
		subscribers:   NewSubscriberManager(),
		retentionDays: DefaultRetentionDays,
		stopPruning:   make(chan struct{}),
	}
	for _, opt := range opts {
		opt(s)
	}
	return s
}

// Start begins the background pruning goroutine.
func (s *SQLiteStore) Start() {
	go s.pruneLoop()
}

// Close stops the background pruning goroutine and cleans up subscribers.
func (s *SQLiteStore) Close() {
	s.pruneOnce.Do(func() {
		close(s.stopPruning)
	})
	s.subscribers.CloseAll()
}

// Emit persists a telemetry event to SQLite and broadcasts to subscribers.
func (s *SQLiteStore) Emit(ctx context.Context, event TelemetryEvent) error {
	if event.EventID == "" {
		event.EventID = uuid.New().String()
	}
	if event.Timestamp.IsZero() {
		event.Timestamp = time.Now().UTC()
	}

	attrs, err := json.Marshal(event.Attributes)
	if err != nil {
		return fmt.Errorf("telemetry: failed to marshal attributes: %w", err)
	}

	_, err = s.db.ExecContext(ctx, `
		INSERT INTO telemetry_events (event_id, event_type, task_id, timestamp, attributes)
		VALUES (?, ?, ?, ?, ?)`,
		event.EventID,
		string(event.EventType),
		event.TaskID,
		event.Timestamp.UTC().Format(time.RFC3339Nano),
		string(attrs),
	)
	if err != nil {
		return fmt.Errorf("telemetry: failed to insert event: %w", err)
	}

	// Broadcast to subscribers.
	s.subscribers.Broadcast(event)

	return nil
}

// Query returns aggregated metrics for events within the specified time window.
func (s *SQLiteStore) Query(ctx context.Context, q SummaryQuery) (SummaryResult, error) {
	if len(q.GroupBy) > 0 {
		return s.queryGrouped(ctx, q)
	}
	return s.queryFlat(ctx, q)
}

// queryFlat returns a single aggregated SummaryResult with no grouping.
func (s *SQLiteStore) queryFlat(ctx context.Context, q SummaryQuery) (SummaryResult, error) {
	row := s.db.QueryRowContext(ctx, `
		SELECT
			COALESCE(SUM(json_extract(attributes, '$.computed_cost')), 0) AS total_cost,
			COALESCE(SUM(
				COALESCE(json_extract(attributes, '$.input_tokens'), 0) +
				COALESCE(json_extract(attributes, '$.output_tokens'), 0)
			), 0) AS total_tokens,
			COUNT(*) AS call_count,
			COALESCE(AVG(json_extract(attributes, '$.latency_ms')), 0) AS avg_latency_ms,
			COALESCE(
				CAST(SUM(CASE WHEN json_extract(attributes, '$.success') = 0 THEN 1 ELSE 0 END) AS REAL) /
				NULLIF(COUNT(*), 0),
			0) AS error_rate
		FROM telemetry_events
		WHERE timestamp >= ? AND timestamp <= ?
		  AND event_type IN ('agent_call_end', 'host_action')`,
		q.StartTime.UTC().Format(time.RFC3339Nano),
		q.EndTime.UTC().Format(time.RFC3339Nano),
	)

	var result SummaryResult
	err := row.Scan(
		&result.TotalCost,
		&result.TotalTokens,
		&result.CallCount,
		&result.AvgLatencyMS,
		&result.ErrorRate,
	)
	if err != nil {
		return SummaryResult{}, fmt.Errorf("telemetry: query failed: %w", err)
	}
	return result, nil
}

// queryGrouped returns aggregated metrics grouped by the specified fields.
func (s *SQLiteStore) queryGrouped(ctx context.Context, q SummaryQuery) (SummaryResult, error) {
	// First compute the top-level aggregate.
	topLevel, err := s.queryFlat(ctx, q)
	if err != nil {
		return SummaryResult{}, err
	}

	// Build the GROUP BY expression from valid fields.
	var groupExprs []string
	var selectExprs []string
	for _, field := range q.GroupBy {
		switch field {
		case "role":
			groupExprs = append(groupExprs, "json_extract(attributes, '$.role')")
			selectExprs = append(selectExprs, "json_extract(attributes, '$.role') AS group_key")
		case "model":
			groupExprs = append(groupExprs, "json_extract(attributes, '$.model')")
			selectExprs = append(selectExprs, "json_extract(attributes, '$.model') AS group_key")
		case "task_id":
			groupExprs = append(groupExprs, "task_id")
			selectExprs = append(selectExprs, "task_id AS group_key")
		default:
			return SummaryResult{}, fmt.Errorf("telemetry: unsupported group_by field: %s", field)
		}
	}

	// For simplicity, use first group_by field for grouping.
	// Multi-level grouping would require recursive queries.
	query := fmt.Sprintf(`
		SELECT
			%s,
			COALESCE(SUM(json_extract(attributes, '$.computed_cost')), 0) AS total_cost,
			COALESCE(SUM(
				COALESCE(json_extract(attributes, '$.input_tokens'), 0) +
				COALESCE(json_extract(attributes, '$.output_tokens'), 0)
			), 0) AS total_tokens,
			COUNT(*) AS call_count,
			COALESCE(AVG(json_extract(attributes, '$.latency_ms')), 0) AS avg_latency_ms,
			COALESCE(
				CAST(SUM(CASE WHEN json_extract(attributes, '$.success') = 0 THEN 1 ELSE 0 END) AS REAL) /
				NULLIF(COUNT(*), 0),
			0) AS error_rate
		FROM telemetry_events
		WHERE timestamp >= ? AND timestamp <= ?
		  AND event_type IN ('agent_call_end', 'host_action')
		GROUP BY %s`,
		selectExprs[0],
		strings.Join(groupExprs[:1], ", "),
	)

	rows, err := s.db.QueryContext(ctx, query,
		q.StartTime.UTC().Format(time.RFC3339Nano),
		q.EndTime.UTC().Format(time.RFC3339Nano),
	)
	if err != nil {
		return SummaryResult{}, fmt.Errorf("telemetry: grouped query failed: %w", err)
	}
	defer rows.Close()

	groups := make(map[string]SummaryResult)
	for rows.Next() {
		var groupKey sql.NullString
		var gr SummaryResult
		if err := rows.Scan(&groupKey, &gr.TotalCost, &gr.TotalTokens, &gr.CallCount, &gr.AvgLatencyMS, &gr.ErrorRate); err != nil {
			return SummaryResult{}, fmt.Errorf("telemetry: failed to scan grouped row: %w", err)
		}
		key := groupKey.String
		if key == "" {
			key = "(unknown)"
		}
		groups[key] = gr
	}
	if err := rows.Err(); err != nil {
		return SummaryResult{}, fmt.Errorf("telemetry: rows iteration error: %w", err)
	}

	topLevel.Groups = groups
	return topLevel, nil
}

// Events returns paginated raw telemetry events matching the given filters.
func (s *SQLiteStore) Events(ctx context.Context, filters map[string]string, page, pageSize int) ([]TelemetryEvent, int, error) {
	if page < 1 {
		page = 1
	}
	if pageSize < 1 {
		pageSize = 50
	}

	whereClauses := []string{"1=1"}
	args := []interface{}{}

	for key, val := range filters {
		switch key {
		case "task_id":
			whereClauses = append(whereClauses, "task_id = ?")
			args = append(args, val)
		case "event_type":
			whereClauses = append(whereClauses, "event_type = ?")
			args = append(args, val)
		case "role":
			whereClauses = append(whereClauses, "json_extract(attributes, '$.role') = ?")
			args = append(args, val)
		case "model":
			whereClauses = append(whereClauses, "json_extract(attributes, '$.model') = ?")
			args = append(args, val)
		}
	}

	whereSQL := strings.Join(whereClauses, " AND ")

	// Get total count.
	countQuery := fmt.Sprintf("SELECT COUNT(*) FROM telemetry_events WHERE %s", whereSQL)
	var total int
	err := s.db.QueryRowContext(ctx, countQuery, args...).Scan(&total)
	if err != nil {
		return nil, 0, fmt.Errorf("telemetry: count query failed: %w", err)
	}

	// Fetch paginated events.
	offset := (page - 1) * pageSize
	dataQuery := fmt.Sprintf(`
		SELECT event_id, event_type, task_id, timestamp, attributes
		FROM telemetry_events
		WHERE %s
		ORDER BY timestamp DESC
		LIMIT ? OFFSET ?`, whereSQL)

	dataArgs := append(args, pageSize, offset)
	rows, err := s.db.QueryContext(ctx, dataQuery, dataArgs...)
	if err != nil {
		return nil, 0, fmt.Errorf("telemetry: events query failed: %w", err)
	}
	defer rows.Close()

	var events []TelemetryEvent
	for rows.Next() {
		var ev TelemetryEvent
		var tsStr string
		var attrsStr string
		if err := rows.Scan(&ev.EventID, &ev.EventType, &ev.TaskID, &tsStr, &attrsStr); err != nil {
			return nil, 0, fmt.Errorf("telemetry: failed to scan event row: %w", err)
		}
		ev.Timestamp, err = time.Parse(time.RFC3339Nano, tsStr)
		if err != nil {
			// Try alternative format from SQLite datetime().
			ev.Timestamp, err = time.Parse("2006-01-02 15:04:05", tsStr)
			if err != nil {
				return nil, 0, fmt.Errorf("telemetry: failed to parse timestamp %q: %w", tsStr, err)
			}
		}
		if attrsStr != "" {
			ev.Attributes = make(map[string]interface{})
			if jsonErr := json.Unmarshal([]byte(attrsStr), &ev.Attributes); jsonErr != nil {
				return nil, 0, fmt.Errorf("telemetry: failed to unmarshal attributes: %w", jsonErr)
			}
		}
		events = append(events, ev)
	}
	if err := rows.Err(); err != nil {
		return nil, 0, fmt.Errorf("telemetry: rows iteration error: %w", err)
	}

	return events, total, nil
}

// Subscribe returns a channel that receives events matching the given filter.
func (s *SQLiteStore) Subscribe(ctx context.Context, filter EventType) <-chan TelemetryEvent {
	id := uuid.New().String()
	sub := NewSubscriber(id, filter, 100)
	s.subscribers.Add(sub)

	// Clean up when context is cancelled.
	go func() {
		select {
		case <-ctx.Done():
			s.subscribers.Remove(id)
		case <-sub.Done():
		}
	}()

	return sub.Events
}

// pruneLoop runs a daily background routine that deletes events older than the retention period.
func (s *SQLiteStore) pruneLoop() {
	ticker := time.NewTicker(24 * time.Hour)
	defer ticker.Stop()

	// Run an initial prune on start.
	s.pruneExpired()

	for {
		select {
		case <-ticker.C:
			s.pruneExpired()
		case <-s.stopPruning:
			return
		}
	}
}

// pruneExpired deletes telemetry events older than the configured retention period.
func (s *SQLiteStore) pruneExpired() {
	cutoff := time.Now().UTC().AddDate(0, 0, -s.retentionDays).Format(time.RFC3339Nano)
	_, _ = s.db.Exec("DELETE FROM telemetry_events WHERE timestamp < ?", cutoff)
}
