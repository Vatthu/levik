// sandbox.go implements network egress restrictions for task worktree subprocesses.
// When AllowNetwork=false (the default), subprocesses spawned for verification
// or implementation commands are restricted from making outbound network
// connections. This prevents agent-generated code from exfiltrating data or
// reaching external services during task execution.
//
// The implementation uses a pluggable interface (NetworkSandbox) so that:
// - On Linux with root: real network namespace isolation or iptables rules
// - On macOS or non-root: a no-op sandbox that logs a warning
// - In tests: a mock implementation for verification
//
// Requirements: 55.5
package orchestratorhost

import (
	"context"
	"fmt"
	"os/exec"
	"runtime"
	"strings"

	"github.com/Vatthu/vikram/pkg/logger"
)

// NetworkSandbox defines the interface for subprocess network restriction.
// Implementations apply OS-level network egress restrictions to a command
// before execution.
type NetworkSandbox interface {
	// WrapCommand modifies the given exec.Cmd to apply network restrictions.
	// If AllowNetwork is true, this is a no-op.
	// Returns the (potentially modified) command and any setup error.
	WrapCommand(ctx context.Context, cmd *exec.Cmd, opts SandboxOpts) (*exec.Cmd, error)

	// Available returns true if this sandbox implementation can actually
	// enforce network restrictions on the current platform/privileges.
	Available() bool

	// Name returns a human-readable name for this sandbox implementation.
	Name() string
}

// SandboxOpts holds per-invocation sandbox configuration.
type SandboxOpts struct {
	// AllowNetwork controls whether the subprocess is allowed outbound network.
	// Default false means network is blocked.
	AllowNetwork bool
	// WorktreePath is the task worktree path (for logging/scoping).
	WorktreePath string
	// TaskID is the task identifier (for logging/audit).
	TaskID string
}

// SandboxManager selects and applies the appropriate sandbox implementation
// for the current platform and privilege level.
type SandboxManager struct {
	sandbox NetworkSandbox
}

// NewSandboxManager creates a SandboxManager that picks the best available
// network sandbox for the current environment.
func NewSandboxManager() *SandboxManager {
	sandbox := selectSandbox()
	return &SandboxManager{sandbox: sandbox}
}

// NewSandboxManagerWith creates a SandboxManager with a specific sandbox
// implementation (useful for testing).
func NewSandboxManagerWith(sandbox NetworkSandbox) *SandboxManager {
	return &SandboxManager{sandbox: sandbox}
}

// WrapCommand applies network sandboxing to the given command based on opts.
// If AllowNetwork is true, the command passes through unchanged.
func (sm *SandboxManager) WrapCommand(ctx context.Context, cmd *exec.Cmd, opts SandboxOpts) (*exec.Cmd, error) {
	if opts.AllowNetwork {
		logger.Info(fmt.Sprintf("Sandbox: network allowed for task %s", opts.TaskID))
		return cmd, nil
	}
	return sm.sandbox.WrapCommand(ctx, cmd, opts)
}

// Available returns whether real network restriction is available.
func (sm *SandboxManager) Available() bool {
	return sm.sandbox.Available()
}

// SandboxName returns the name of the active sandbox implementation.
func (sm *SandboxManager) SandboxName() string {
	return sm.sandbox.Name()
}

// selectSandbox picks the best sandbox implementation for the platform.
func selectSandbox() NetworkSandbox {
	switch runtime.GOOS {
	case "linux":
		// On Linux, try unshare-based network namespace isolation.
		if unshareSandbox := newUnshareNetworkSandbox(); unshareSandbox.Available() {
			return unshareSandbox
		}
		// Fall through to noop if unshare is not available.
		return newNoopSandbox("linux (insufficient privileges for unshare)")
	case "darwin":
		// macOS sandbox-exec is deprecated but can still restrict network.
		if sbSandbox := newDarwinSandbox(); sbSandbox.Available() {
			return sbSandbox
		}
		return newNoopSandbox("darwin (sandbox-exec not available)")
	default:
		return newNoopSandbox(runtime.GOOS + " (unsupported)")
	}
}

// --- Noop Sandbox (fallback) ---

// noopSandbox is a no-op implementation that logs a warning but does not
// actually restrict network access. Used when the platform doesn't support
// sandboxing or insufficient privileges are available.
type noopSandbox struct {
	reason string
}

func newNoopSandbox(reason string) *noopSandbox {
	return &noopSandbox{reason: reason}
}

func (s *noopSandbox) WrapCommand(_ context.Context, cmd *exec.Cmd, opts SandboxOpts) (*exec.Cmd, error) {
	logger.Warn(fmt.Sprintf("Sandbox: network restriction NOT enforced for task %s (reason: %s)", opts.TaskID, s.reason))
	return cmd, nil
}

func (s *noopSandbox) Available() bool {
	return false
}

func (s *noopSandbox) Name() string {
	return fmt.Sprintf("noop (%s)", s.reason)
}

// --- Linux unshare-based network namespace sandbox ---

// unshareNetworkSandbox uses `unshare --net` to create an isolated network
// namespace for the subprocess. This completely isolates the subprocess from
// all network interfaces (it only sees loopback).
type unshareNetworkSandbox struct {
	unsharePath string
}

func newUnshareNetworkSandbox() *unshareNetworkSandbox {
	path, err := exec.LookPath("unshare")
	if err != nil {
		return &unshareNetworkSandbox{}
	}
	return &unshareNetworkSandbox{unsharePath: path}
}

func (s *unshareNetworkSandbox) WrapCommand(ctx context.Context, cmd *exec.Cmd, opts SandboxOpts) (*exec.Cmd, error) {
	if s.unsharePath == "" {
		return cmd, fmt.Errorf("sandbox: unshare binary not found")
	}

	// Wrap the original command with `unshare --net --`
	// This creates a new network namespace with only loopback.
	originalArgs := append([]string{cmd.Path}, cmd.Args[1:]...)
	newArgs := append([]string{"--net", "--"}, originalArgs...)

	wrapped := exec.CommandContext(ctx, s.unsharePath, newArgs...)
	wrapped.Dir = cmd.Dir
	wrapped.Env = cmd.Env
	wrapped.Stdin = cmd.Stdin
	wrapped.Stdout = cmd.Stdout
	wrapped.Stderr = cmd.Stderr

	logger.Info(fmt.Sprintf("Sandbox: wrapping command with unshare --net for task %s", opts.TaskID))
	return wrapped, nil
}

func (s *unshareNetworkSandbox) Available() bool {
	if s.unsharePath == "" {
		return false
	}
	// Test if we can actually use unshare (requires CAP_SYS_ADMIN or root).
	testCmd := exec.Command(s.unsharePath, "--net", "true")
	err := testCmd.Run()
	return err == nil
}

func (s *unshareNetworkSandbox) Name() string {
	return "linux-unshare-net"
}

// --- macOS sandbox-exec based sandbox ---

// darwinSandbox uses macOS sandbox-exec with a profile that denies network access.
type darwinSandbox struct {
	sandboxExecPath string
}

func newDarwinSandbox() *darwinSandbox {
	path, err := exec.LookPath("sandbox-exec")
	if err != nil {
		return &darwinSandbox{}
	}
	return &darwinSandbox{sandboxExecPath: path}
}

// darwinDenyNetworkProfile is a minimal sandbox profile that denies all network access.
const darwinDenyNetworkProfile = `(version 1)
(allow default)
(deny network*)
`

func (s *darwinSandbox) WrapCommand(ctx context.Context, cmd *exec.Cmd, opts SandboxOpts) (*exec.Cmd, error) {
	if s.sandboxExecPath == "" {
		return cmd, fmt.Errorf("sandbox: sandbox-exec not found")
	}

	// Use sandbox-exec -p <profile> to deny network access.
	originalArgs := cmd.Args
	newArgs := []string{"-p", darwinDenyNetworkProfile}
	newArgs = append(newArgs, originalArgs...)

	wrapped := exec.CommandContext(ctx, s.sandboxExecPath, newArgs...)
	wrapped.Dir = cmd.Dir
	wrapped.Env = cmd.Env
	wrapped.Stdin = cmd.Stdin
	wrapped.Stdout = cmd.Stdout
	wrapped.Stderr = cmd.Stderr

	logger.Info(fmt.Sprintf("Sandbox: wrapping command with sandbox-exec (deny network) for task %s", opts.TaskID))
	return wrapped, nil
}

func (s *darwinSandbox) Available() bool {
	if s.sandboxExecPath == "" {
		return false
	}
	// Verify sandbox-exec exists and is executable.
	testCmd := exec.Command(s.sandboxExecPath, "-p", "(version 1)(allow default)", "/usr/bin/true")
	err := testCmd.Run()
	return err == nil
}

func (s *darwinSandbox) Name() string {
	return "darwin-sandbox-exec"
}

// --- Utility functions ---

// ShouldAllowNetwork determines whether a task should have network access
// based on its constraints. The default is to DENY network access unless
// explicitly allowed.
func ShouldAllowNetwork(constraints map[string]interface{}) bool {
	if constraints == nil {
		return false
	}

	// Check for AllowNetwork field in constraints.
	if allow, ok := constraints["allow_network"]; ok {
		switch v := allow.(type) {
		case bool:
			return v
		case string:
			return strings.EqualFold(v, "true")
		}
	}

	return false
}
