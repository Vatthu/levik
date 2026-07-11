package orchestratorhost

import (
	"context"
	"os/exec"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// mockSandbox is a test implementation of NetworkSandbox that tracks calls.
type mockSandbox struct {
	available  bool
	wrapCalled bool
	lastOpts   SandboxOpts
}

func (s *mockSandbox) WrapCommand(_ context.Context, cmd *exec.Cmd, opts SandboxOpts) (*exec.Cmd, error) {
	s.wrapCalled = true
	s.lastOpts = opts
	return cmd, nil
}

func (s *mockSandbox) Available() bool {
	return s.available
}

func (s *mockSandbox) Name() string {
	return "mock-sandbox"
}

func TestSandboxManager_AllowNetworkSkipsWrap(t *testing.T) {
	mock := &mockSandbox{available: true}
	sm := NewSandboxManagerWith(mock)

	cmd := exec.Command("echo", "hello")
	wrapped, err := sm.WrapCommand(context.Background(), cmd, SandboxOpts{
		AllowNetwork: true,
		TaskID:       "task-1",
	})
	require.NoError(t, err)

	// When AllowNetwork=true, the sandbox should NOT be invoked.
	assert.False(t, mock.wrapCalled)
	assert.Equal(t, cmd, wrapped)
}

func TestSandboxManager_DenyNetworkCallsWrap(t *testing.T) {
	mock := &mockSandbox{available: true}
	sm := NewSandboxManagerWith(mock)

	cmd := exec.Command("echo", "hello")
	_, err := sm.WrapCommand(context.Background(), cmd, SandboxOpts{
		AllowNetwork: false,
		TaskID:       "task-2",
		WorktreePath: "/tmp/worktree",
	})
	require.NoError(t, err)

	// When AllowNetwork=false, the sandbox SHOULD be invoked.
	assert.True(t, mock.wrapCalled)
	assert.Equal(t, "task-2", mock.lastOpts.TaskID)
	assert.Equal(t, "/tmp/worktree", mock.lastOpts.WorktreePath)
}

func TestSandboxManager_Available(t *testing.T) {
	mock := &mockSandbox{available: true}
	sm := NewSandboxManagerWith(mock)
	assert.True(t, sm.Available())

	mock2 := &mockSandbox{available: false}
	sm2 := NewSandboxManagerWith(mock2)
	assert.False(t, sm2.Available())
}

func TestSandboxManager_Name(t *testing.T) {
	mock := &mockSandbox{available: true}
	sm := NewSandboxManagerWith(mock)
	assert.Equal(t, "mock-sandbox", sm.SandboxName())
}

func TestNoopSandbox(t *testing.T) {
	noop := newNoopSandbox("test reason")

	assert.False(t, noop.Available())
	assert.Contains(t, noop.Name(), "test reason")

	cmd := exec.Command("echo", "test")
	wrapped, err := noop.WrapCommand(context.Background(), cmd, SandboxOpts{
		TaskID: "task-noop",
	})
	require.NoError(t, err)
	assert.Equal(t, cmd, wrapped) // should pass through unchanged
}

func TestShouldAllowNetwork(t *testing.T) {
	tests := []struct {
		name        string
		constraints map[string]interface{}
		expected    bool
	}{
		{
			name:        "nil constraints defaults to deny",
			constraints: nil,
			expected:    false,
		},
		{
			name:        "empty constraints defaults to deny",
			constraints: map[string]interface{}{},
			expected:    false,
		},
		{
			name:        "allow_network=true allows",
			constraints: map[string]interface{}{"allow_network": true},
			expected:    true,
		},
		{
			name:        "allow_network=false denies",
			constraints: map[string]interface{}{"allow_network": false},
			expected:    false,
		},
		{
			name:        "allow_network string true allows",
			constraints: map[string]interface{}{"allow_network": "true"},
			expected:    true,
		},
		{
			name:        "allow_network string True allows",
			constraints: map[string]interface{}{"allow_network": "True"},
			expected:    true,
		},
		{
			name:        "allow_network string false denies",
			constraints: map[string]interface{}{"allow_network": "false"},
			expected:    false,
		},
		{
			name:        "unrelated constraints defaults to deny",
			constraints: map[string]interface{}{"max_cost": 10.0},
			expected:    false,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			result := ShouldAllowNetwork(tt.constraints)
			assert.Equal(t, tt.expected, result)
		})
	}
}

func TestNewSandboxManager_DefaultSelection(t *testing.T) {
	// This tests that NewSandboxManager doesn't panic and picks a sandbox.
	sm := NewSandboxManager()
	require.NotNil(t, sm)

	// The name should be non-empty.
	assert.NotEmpty(t, sm.SandboxName())
}
