package orchestratorhost

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"path/filepath"
	"sync"
	"time"

	"github.com/fsnotify/fsnotify"

	"github.com/Vatthu/vikram/pkg/logger"
)

// ConfigWatcher watches a configuration file for changes and triggers a
// reload callback when the file is modified. It uses fsnotify for filesystem
// events and debounces rapid changes (e.g., editor save operations that
// create multiple events).
//
// Validates: Requirements 31.1, 31.2
type ConfigWatcher struct {
	configPath     string
	reloadEndpoint string // Python endpoint to call, e.g. "/v1/config/reload"
	socketPath     string // Unix socket for the Python orchestrator
	debounce       time.Duration
	onReload       func(success bool, errMsg string) // optional callback for notifications

	watcher *fsnotify.Watcher
	cancel  context.CancelFunc
	done    chan struct{}
	mu      sync.Mutex
}

// ConfigWatcherOpts holds configuration for the ConfigWatcher.
type ConfigWatcherOpts struct {
	// ConfigPath is the absolute path to the config file to watch.
	ConfigPath string

	// OrchestratorSocket is the Unix socket path for the Python orchestrator.
	// Used to call the reload endpoint when a change is detected.
	OrchestratorSocket string

	// ReloadEndpoint is the HTTP endpoint path on the orchestrator to call.
	// Defaults to "/v1/config/reload" if empty.
	ReloadEndpoint string

	// Debounce is the duration to wait after the last file event before
	// triggering a reload. This handles editors that perform multiple writes.
	// Defaults to 500ms if zero.
	Debounce time.Duration

	// OnReload is an optional callback invoked after each reload attempt
	// with the success status and any error message.
	OnReload func(success bool, errMsg string)
}

// NewConfigWatcher creates a new ConfigWatcher. Call Start() to begin watching.
func NewConfigWatcher(opts ConfigWatcherOpts) *ConfigWatcher {
	endpoint := opts.ReloadEndpoint
	if endpoint == "" {
		endpoint = "/v1/config/reload"
	}
	debounce := opts.Debounce
	if debounce == 0 {
		debounce = 500 * time.Millisecond
	}

	return &ConfigWatcher{
		configPath:     opts.ConfigPath,
		reloadEndpoint: endpoint,
		socketPath:     opts.OrchestratorSocket,
		debounce:       debounce,
		onReload:       opts.OnReload,
		done:           make(chan struct{}),
	}
}

// Start begins watching the config file. It returns an error if the watcher
// cannot be created. The watcher runs in a background goroutine until Stop()
// is called or the context is cancelled.
func (cw *ConfigWatcher) Start(ctx context.Context) error {
	watcher, err := fsnotify.NewWatcher()
	if err != nil {
		return fmt.Errorf("create fsnotify watcher: %w", err)
	}

	// Watch the directory containing the config file, since some editors
	// do atomic writes (create temp + rename) which remove the file from
	// the inotify watch. Watching the parent directory catches renames.
	dir := filepath.Dir(cw.configPath)
	if err := watcher.Add(dir); err != nil {
		watcher.Close()
		return fmt.Errorf("watch directory %s: %w", dir, err)
	}

	cw.mu.Lock()
	cw.watcher = watcher
	childCtx, cancel := context.WithCancel(ctx)
	cw.cancel = cancel
	cw.mu.Unlock()

	go cw.loop(childCtx)

	logger.InfoCF("config-watcher", "Config file watcher started", map[string]interface{}{
		"config_path": cw.configPath,
		"watch_dir":   dir,
		"debounce_ms": cw.debounce.Milliseconds(),
	})

	return nil
}

// Stop halts the config watcher and releases resources.
func (cw *ConfigWatcher) Stop() {
	cw.mu.Lock()
	defer cw.mu.Unlock()

	if cw.cancel != nil {
		cw.cancel()
		cw.cancel = nil
	}
	if cw.watcher != nil {
		cw.watcher.Close()
		cw.watcher = nil
	}
	<-cw.done
}

// loop is the main event loop that processes filesystem events.
func (cw *ConfigWatcher) loop(ctx context.Context) {
	defer close(cw.done)

	var debounceTimer *time.Timer
	targetFile := filepath.Base(cw.configPath)

	for {
		select {
		case <-ctx.Done():
			if debounceTimer != nil {
				debounceTimer.Stop()
			}
			return

		case event, ok := <-cw.watcher.Events:
			if !ok {
				return
			}

			// Only react to events on our target file
			if filepath.Base(event.Name) != targetFile {
				continue
			}

			// We care about writes, creates (atomic rename), and chmod
			if event.Has(fsnotify.Write) || event.Has(fsnotify.Create) {
				// Debounce: reset timer on each event
				if debounceTimer != nil {
					debounceTimer.Stop()
				}
				debounceTimer = time.AfterFunc(cw.debounce, func() {
					cw.triggerReload(ctx)
				})
			}

		case err, ok := <-cw.watcher.Errors:
			if !ok {
				return
			}
			logger.WarnCF("config-watcher", "Filesystem watcher error", map[string]interface{}{
				"error": err.Error(),
			})
		}
	}
}

// triggerReload calls the Python orchestrator's reload endpoint over the Unix socket.
func (cw *ConfigWatcher) triggerReload(ctx context.Context) {
	logger.InfoCF("config-watcher", "Config file change detected, triggering reload", map[string]interface{}{
		"config_path": cw.configPath,
	})

	reqBody, _ := json.Marshal(map[string]string{
		"config_path": cw.configPath,
	})

	client := &http.Client{
		Transport: &http.Transport{
			DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
				return net.Dial("unix", cw.socketPath)
			},
		},
		Timeout: 10 * time.Second,
	}

	url := "http://orchestrator" + cw.reloadEndpoint
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(reqBody))
	if err != nil {
		cw.reportReload(false, fmt.Sprintf("failed to create reload request: %v", err))
		return
	}
	req.Header.Set("Content-Type", "application/json")

	resp, err := client.Do(req)
	if err != nil {
		cw.reportReload(false, fmt.Sprintf("failed to call reload endpoint: %v", err))
		return
	}
	defer resp.Body.Close()

	if resp.StatusCode == http.StatusOK {
		cw.reportReload(true, "")
	} else {
		var body map[string]string
		_ = json.NewDecoder(resp.Body).Decode(&body)
		errMsg := body["error"]
		if errMsg == "" {
			errMsg = fmt.Sprintf("reload endpoint returned status %d", resp.StatusCode)
		}
		cw.reportReload(false, errMsg)
	}
}

// reportReload logs the reload result and calls the optional callback.
func (cw *ConfigWatcher) reportReload(success bool, errMsg string) {
	if success {
		logger.InfoCF("config-watcher", "Config reload succeeded", map[string]interface{}{
			"config_path": cw.configPath,
		})
	} else {
		logger.WarnCF("config-watcher", "Config reload failed", map[string]interface{}{
			"config_path": cw.configPath,
			"error":       errMsg,
		})
	}

	if cw.onReload != nil {
		cw.onReload(success, errMsg)
	}
}
