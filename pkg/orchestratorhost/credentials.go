// credentials.go implements secure credential isolation for the Go Host.
// The Host is the exclusive holder of provider API credentials — they are
// never passed to the Python Orchestrator, agent execution contexts, or
// any subprocess spawned for task execution.
//
// Requirements: 55.1, 55.2, 55.3
package orchestratorhost

import (
	"context"
	"crypto/aes"
	"crypto/cipher"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"strings"
	"sync"

	"github.com/Vatthu/vikram/pkg/logger"
)

// CredentialValidator validates a provider credential by making a minimal
// authenticated API call. Implementations are provider-specific.
type CredentialValidator interface {
	// Validate performs a minimal API call to verify the credential is valid.
	// Returns nil if the credential is accepted by the provider.
	Validate(ctx context.Context, provider, credential string) error
}

// CredentialStore manages encrypted provider credentials in Go Host process memory.
// Credentials are loaded from the encrypted store at startup and held only in
// memory — never written to environment variables, log files, or temporary files.
type CredentialStore struct {
	mu          sync.RWMutex
	credentials map[string]string // provider -> decrypted credential (in-memory only)
	storePath   string            // path to the encrypted credential file on disk
	masterKey   []byte            // derived encryption key (in-memory only)
	validator   CredentialValidator
}

// CredentialStoreConfig holds configuration for creating a CredentialStore.
type CredentialStoreConfig struct {
	// StorePath is the file path to the encrypted credentials file.
	StorePath string
	// MasterKey is the master encryption key (or passphrase) used to derive the AES key.
	MasterKey string
	// Validator is an optional provider credential validator.
	Validator CredentialValidator
}

// encryptedStore is the on-disk format for the credential store.
type encryptedStore struct {
	Version     int               `json:"version"`
	Credentials map[string]string `json:"credentials"` // provider -> encrypted token
	Salt        string            `json:"salt"`
}

// NewCredentialStore creates a CredentialStore and loads credentials from the
// encrypted store file into process memory. The credentials are decrypted at
// load time and held in memory only.
func NewCredentialStore(cfg CredentialStoreConfig) (*CredentialStore, error) {
	if cfg.StorePath == "" {
		return nil, errors.New("credentials: store path is required")
	}
	if cfg.MasterKey == "" {
		return nil, errors.New("credentials: master key is required")
	}

	// Derive a 32-byte AES key from the master key using SHA-256.
	keyHash := sha256.Sum256([]byte(cfg.MasterKey))

	cs := &CredentialStore{
		credentials: make(map[string]string),
		storePath:   cfg.StorePath,
		masterKey:   keyHash[:],
		validator:   cfg.Validator,
	}

	// Load existing credentials from disk if the file exists.
	if err := cs.loadFromDisk(); err != nil {
		// If the file doesn't exist, start with an empty store.
		if !os.IsNotExist(err) {
			return nil, fmt.Errorf("credentials: failed to load store: %w", err)
		}
	}

	return cs, nil
}

// Get retrieves a decrypted credential for the given provider.
// Returns ("", false) if the provider is not configured.
// Credentials are NEVER exposed outside the Host process boundary.
func (cs *CredentialStore) Get(provider string) (string, bool) {
	cs.mu.RLock()
	defer cs.mu.RUnlock()
	cred, ok := cs.credentials[provider]
	return cred, ok
}

// Set validates and persists a new credential for the given provider.
// If a validator is configured, it performs a minimal API call to verify
// the credential before persisting. (Requirement 55.3)
func (cs *CredentialStore) Set(ctx context.Context, provider, credential string) error {
	if provider == "" {
		return errors.New("credentials: provider name is required")
	}
	if credential == "" {
		return errors.New("credentials: credential value is required")
	}

	// Validate with minimal API call before persisting (Req 55.3).
	if cs.validator != nil {
		if err := cs.validator.Validate(ctx, provider, credential); err != nil {
			return fmt.Errorf("credentials: validation failed for provider %q: %w", provider, err)
		}
	}

	cs.mu.Lock()
	cs.credentials[provider] = credential
	cs.mu.Unlock()

	// Persist the updated store to disk.
	if err := cs.saveToDisk(); err != nil {
		return fmt.Errorf("credentials: failed to persist store: %w", err)
	}

	logger.Info(fmt.Sprintf("Credential stored for provider %q", provider))
	return nil
}

// Delete removes a credential for the given provider.
func (cs *CredentialStore) Delete(provider string) error {
	cs.mu.Lock()
	delete(cs.credentials, provider)
	cs.mu.Unlock()

	if err := cs.saveToDisk(); err != nil {
		return fmt.Errorf("credentials: failed to persist store after delete: %w", err)
	}
	return nil
}

// Providers returns the list of providers that have credentials stored.
func (cs *CredentialStore) Providers() []string {
	cs.mu.RLock()
	defer cs.mu.RUnlock()
	providers := make([]string, 0, len(cs.credentials))
	for p := range cs.credentials {
		providers = append(providers, p)
	}
	return providers
}

// SanitizedEnvForSubprocess returns a copy of the process environment with
// all credential-related variables removed. This MUST be used when spawning
// any subprocess to ensure credential isolation. (Requirement 55.1)
func SanitizedEnvForSubprocess() []string {
	sensitiveVars := []string{
		"VIKRAM_AUTH_MASTER_KEY",
		"OPENAI_API_KEY",
		"ANTHROPIC_API_KEY",
		"GOOGLE_API_KEY",
		"AZURE_OPENAI_API_KEY",
		"VIKRAM_CREDENTIALS_KEY",
	}

	var clean []string
	for _, e := range os.Environ() {
		sensitive := false
		for _, sv := range sensitiveVars {
			if strings.HasPrefix(e, sv+"=") {
				sensitive = true
				break
			}
		}
		if !sensitive {
			clean = append(clean, e)
		}
	}
	return clean
}

// loadFromDisk reads and decrypts the credential store from disk.
func (cs *CredentialStore) loadFromDisk() error {
	data, err := os.ReadFile(cs.storePath)
	if err != nil {
		return err
	}

	var store encryptedStore
	if err := json.Unmarshal(data, &store); err != nil {
		return fmt.Errorf("credentials: invalid store format: %w", err)
	}

	cs.mu.Lock()
	defer cs.mu.Unlock()

	for provider, encCred := range store.Credentials {
		decrypted, err := credDecrypt(encCred, cs.masterKey)
		if err != nil {
			logger.Warn(fmt.Sprintf("Failed to decrypt credential for provider %q: %v", provider, err))
			continue
		}
		cs.credentials[provider] = decrypted
	}

	return nil
}

// saveToDisk encrypts and writes all credentials to the store file.
func (cs *CredentialStore) saveToDisk() error {
	cs.mu.RLock()
	encCreds := make(map[string]string, len(cs.credentials))
	for provider, cred := range cs.credentials {
		encrypted, err := credEncrypt(cred, cs.masterKey)
		if err != nil {
			cs.mu.RUnlock()
			return fmt.Errorf("credentials: failed to encrypt credential for %q: %w", provider, err)
		}
		encCreds[provider] = encrypted
	}
	cs.mu.RUnlock()

	store := encryptedStore{
		Version:     1,
		Credentials: encCreds,
	}

	data, err := json.MarshalIndent(store, "", "  ")
	if err != nil {
		return err
	}

	// Write atomically using temp file + rename.
	tmpPath := cs.storePath + ".tmp"
	if err := os.WriteFile(tmpPath, data, 0600); err != nil {
		return err
	}
	return os.Rename(tmpPath, cs.storePath)
}

// credEncrypt encrypts plaintext using AES-256-GCM.
func credEncrypt(plaintext string, key []byte) (string, error) {
	block, err := aes.NewCipher(key)
	if err != nil {
		return "", err
	}

	gcm, err := cipher.NewGCM(block)
	if err != nil {
		return "", err
	}

	nonce := make([]byte, gcm.NonceSize())
	if _, err = io.ReadFull(rand.Reader, nonce); err != nil {
		return "", err
	}

	ciphertext := gcm.Seal(nonce, nonce, []byte(plaintext), nil)
	return base64.StdEncoding.EncodeToString(ciphertext), nil
}

// credDecrypt decrypts ciphertext using AES-256-GCM.
func credDecrypt(ciphertext string, key []byte) (string, error) {
	data, err := base64.StdEncoding.DecodeString(ciphertext)
	if err != nil {
		return "", err
	}

	block, err := aes.NewCipher(key)
	if err != nil {
		return "", err
	}

	gcm, err := cipher.NewGCM(block)
	if err != nil {
		return "", err
	}

	nonceSize := gcm.NonceSize()
	if len(data) < nonceSize {
		return "", fmt.Errorf("credentials: ciphertext too short")
	}

	nonce, ciphertextBytes := data[:nonceSize], data[nonceSize:]
	plaintextBytes, err := gcm.Open(nil, nonce, ciphertextBytes, nil)
	if err != nil {
		return "", err
	}
	return string(plaintextBytes), nil
}
