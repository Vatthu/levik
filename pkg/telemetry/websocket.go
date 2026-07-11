package telemetry

import (
	"context"
	"sync"
)

// Subscriber represents a connected WebSocket client subscribed to telemetry events.
type Subscriber struct {
	// ID is a unique identifier for this subscriber.
	ID string

	// Filter restricts which event types are delivered to this subscriber.
	// An empty filter means all events are delivered.
	Filter EventType

	// Events is the channel on which matching events are sent.
	Events chan TelemetryEvent

	// ctx tracks the subscriber's lifecycle.
	ctx    context.Context
	cancel context.CancelFunc
}

// NewSubscriber creates a new subscriber with the given filter and buffer size.
func NewSubscriber(id string, filter EventType, bufferSize int) *Subscriber {
	ctx, cancel := context.WithCancel(context.Background())
	return &Subscriber{
		ID:     id,
		Filter: filter,
		Events: make(chan TelemetryEvent, bufferSize),
		ctx:    ctx,
		cancel: cancel,
	}
}

// Close terminates the subscriber and closes its event channel.
func (s *Subscriber) Close() {
	s.cancel()
	close(s.Events)
}

// Done returns a channel that is closed when the subscriber is terminated.
func (s *Subscriber) Done() <-chan struct{} {
	return s.ctx.Done()
}

// SubscriberManager manages active WebSocket subscribers for real-time
// telemetry streaming.
type SubscriberManager struct {
	mu          sync.RWMutex
	subscribers map[string]*Subscriber
}

// NewSubscriberManager creates a new SubscriberManager.
func NewSubscriberManager() *SubscriberManager {
	return &SubscriberManager{
		subscribers: make(map[string]*Subscriber),
	}
}

// Add registers a new subscriber. If a subscriber with the same ID already exists,
// it is replaced (the old one is closed).
func (m *SubscriberManager) Add(sub *Subscriber) {
	m.mu.Lock()
	defer m.mu.Unlock()

	if existing, ok := m.subscribers[sub.ID]; ok {
		existing.Close()
	}
	m.subscribers[sub.ID] = sub
}

// Remove unregisters and closes a subscriber by ID.
func (m *SubscriberManager) Remove(id string) {
	m.mu.Lock()
	defer m.mu.Unlock()

	if sub, ok := m.subscribers[id]; ok {
		sub.Close()
		delete(m.subscribers, id)
	}
}

// Broadcast sends an event to all subscribers whose filters match.
// Events are delivered non-blocking; if a subscriber's buffer is full,
// the event is dropped for that subscriber.
func (m *SubscriberManager) Broadcast(event TelemetryEvent) {
	m.mu.RLock()
	defer m.mu.RUnlock()

	for _, sub := range m.subscribers {
		if sub.Filter != "" && sub.Filter != event.EventType {
			continue
		}
		// Non-blocking send to avoid slow subscribers blocking the pipeline.
		select {
		case sub.Events <- event:
		default:
			// Subscriber buffer full — drop event.
		}
	}
}

// Count returns the number of active subscribers.
func (m *SubscriberManager) Count() int {
	m.mu.RLock()
	defer m.mu.RUnlock()
	return len(m.subscribers)
}

// CloseAll terminates and removes all subscribers.
func (m *SubscriberManager) CloseAll() {
	m.mu.Lock()
	defer m.mu.Unlock()

	for id, sub := range m.subscribers {
		sub.Close()
		delete(m.subscribers, id)
	}
}
