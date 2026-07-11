package orchestratorhost

import (
	"context"
	"encoding/json"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"sync"
	"testing"
	"time"

	"github.com/stretchr/testify/require"
)

func TestConfigWatcher_DetectsFileChange(t *testing.T) {
	// Create a temp directory with a config file
	dir := t.TempDir()
	configPath := filepath.Join(dir, "approval-matrix.yaml")
	err := os.WriteFile(configPath, []byte("version: 1\nrules: []\n"), 0o644)
	require.NoError(t, err)

	// Use a short path for Unix socket to avoid macOS 104-char limit
	sockDir, err := os.MkdirTemp("/tmp", "cw-test-")
	require.NoError(t, err)
	defer os.RemoveAll(sockDir)
	socketPath := filepath.Join(sockDir, "orch.sock")

	var reloadCalled sync.WaitGroup
	reloadCalled.Add(1)

	var receivedPath string
	var mu sync.Mutex

	listener, err := net.Listen("unix", socketPath)
	require.NoError(t, err)
	defer listener.Close()

	mux := http.NewServeMux()
	mux.HandleFunc("/v1/config/reload", func(w http.ResponseWriter, r *http.Request) {
		var body map[string]string
		_ = json.NewDecoder(r.Body).Decode(&body)
		mu.Lock()
		receivedPath = body["config_path"]
		mu.Unlock()
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"success": true}`))
		reloadCalled.Done()
	})

	srv := &http.Server{Handler: mux}
	go func() { _ = srv.Serve(listener) }()
	defer srv.Close()

	// Create and start the config watcher
	cw := NewConfigWatcher(ConfigWatcherOpts{
		ConfigPath:         configPath,
		OrchestratorSocket: socketPath,
		Debounce:           100 * time.Millisecond,
	})

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	err = cw.Start(ctx)
	require.NoError(t, err)
	defer cw.Stop()

	// Give the watcher time to start
	time.Sleep(200 * time.Millisecond)

	// Modify the config file
	err = os.WriteFile(configPath, []byte("version: 2\nrules: []\n"), 0o644)
	require.NoError(t, err)

	// Wait for the reload to be called
	done := make(chan struct{})
	go func() {
		reloadCalled.Wait()
		close(done)
	}()

	select {
	case <-done:
		// success
	case <-time.After(3 * time.Second):
		t.Fatal("timeout waiting for reload callback")
	}

	mu.Lock()
	require.Equal(t, configPath, receivedPath)
	mu.Unlock()
}

func TestConfigWatcher_DebouncesRapidChanges(t *testing.T) {
	dir := t.TempDir()
	configPath := filepath.Join(dir, "approval-matrix.yaml")
	err := os.WriteFile(configPath, []byte("version: 1\n"), 0o644)
	require.NoError(t, err)

	sockDir, err := os.MkdirTemp("/tmp", "cw-test-")
	require.NoError(t, err)
	defer os.RemoveAll(sockDir)
	socketPath := filepath.Join(sockDir, "orch.sock")

	var callCount int
	var mu sync.Mutex

	listener, err := net.Listen("unix", socketPath)
	require.NoError(t, err)
	defer listener.Close()

	mux := http.NewServeMux()
	mux.HandleFunc("/v1/config/reload", func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		callCount++
		mu.Unlock()
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"success": true}`))
	})

	srv := &http.Server{Handler: mux}
	go func() { _ = srv.Serve(listener) }()
	defer srv.Close()

	cw := NewConfigWatcher(ConfigWatcherOpts{
		ConfigPath:         configPath,
		OrchestratorSocket: socketPath,
		Debounce:           300 * time.Millisecond,
	})

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	err = cw.Start(ctx)
	require.NoError(t, err)
	defer cw.Stop()

	time.Sleep(200 * time.Millisecond)

	// Write multiple rapid changes — these should be debounced into one reload
	for i := 0; i < 5; i++ {
		err = os.WriteFile(configPath, []byte("version: "+string(rune('0'+i))+"\n"), 0o644)
		require.NoError(t, err)
		time.Sleep(50 * time.Millisecond)
	}

	// Wait for debounce + execution
	time.Sleep(800 * time.Millisecond)

	mu.Lock()
	count := callCount
	mu.Unlock()

	// Should have been debounced to just 1 call (or at most 2 if timing is tight)
	require.LessOrEqual(t, count, 2, "expected at most 2 reload calls due to debouncing, got %d", count)
	require.GreaterOrEqual(t, count, 1, "expected at least 1 reload call")
}

func TestConfigWatcher_OnReloadCallback(t *testing.T) {
	dir := t.TempDir()
	configPath := filepath.Join(dir, "approval-matrix.yaml")
	err := os.WriteFile(configPath, []byte("version: 1\n"), 0o644)
	require.NoError(t, err)

	// Use a non-existent socket path — reload will fail, which is what we test
	sockDir, err := os.MkdirTemp("/tmp", "cw-test-")
	require.NoError(t, err)
	defer os.RemoveAll(sockDir)
	socketPath := filepath.Join(sockDir, "orch.sock")

	// Don't start any listener — the reload will fail
	var callbackSuccess bool
	var callbackErr string
	var callbackCalled sync.WaitGroup
	callbackCalled.Add(1)

	cw := NewConfigWatcher(ConfigWatcherOpts{
		ConfigPath:         configPath,
		OrchestratorSocket: socketPath,
		Debounce:           100 * time.Millisecond,
		OnReload: func(success bool, errMsg string) {
			callbackSuccess = success
			callbackErr = errMsg
			callbackCalled.Done()
		},
	})

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	err = cw.Start(ctx)
	require.NoError(t, err)
	defer cw.Stop()

	time.Sleep(200 * time.Millisecond)

	// Modify config — reload will fail because no listener
	err = os.WriteFile(configPath, []byte("version: 2\n"), 0o644)
	require.NoError(t, err)

	done := make(chan struct{})
	go func() {
		callbackCalled.Wait()
		close(done)
	}()

	select {
	case <-done:
		// success
	case <-time.After(3 * time.Second):
		t.Fatal("timeout waiting for onReload callback")
	}

	require.False(t, callbackSuccess)
	require.NotEmpty(t, callbackErr)
}

func TestConfigWatcher_IgnoresUnrelatedFiles(t *testing.T) {
	dir := t.TempDir()
	configPath := filepath.Join(dir, "approval-matrix.yaml")
	err := os.WriteFile(configPath, []byte("version: 1\n"), 0o644)
	require.NoError(t, err)

	sockDir, err := os.MkdirTemp("/tmp", "cw-test-")
	require.NoError(t, err)
	defer os.RemoveAll(sockDir)
	socketPath := filepath.Join(sockDir, "orch.sock")

	var callCount int
	var mu sync.Mutex

	listener, err := net.Listen("unix", socketPath)
	require.NoError(t, err)
	defer listener.Close()

	mux := http.NewServeMux()
	mux.HandleFunc("/v1/config/reload", func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		callCount++
		mu.Unlock()
		w.WriteHeader(http.StatusOK)
	})

	srv := &http.Server{Handler: mux}
	go func() { _ = srv.Serve(listener) }()
	defer srv.Close()

	cw := NewConfigWatcher(ConfigWatcherOpts{
		ConfigPath:         configPath,
		OrchestratorSocket: socketPath,
		Debounce:           100 * time.Millisecond,
	})

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	err = cw.Start(ctx)
	require.NoError(t, err)
	defer cw.Stop()

	time.Sleep(200 * time.Millisecond)

	// Write an unrelated file in the same directory
	otherPath := filepath.Join(dir, "other-config.yaml")
	err = os.WriteFile(otherPath, []byte("unrelated: true\n"), 0o644)
	require.NoError(t, err)

	time.Sleep(500 * time.Millisecond)

	mu.Lock()
	count := callCount
	mu.Unlock()

	require.Equal(t, 0, count, "expected no reload calls for unrelated file changes")
}
