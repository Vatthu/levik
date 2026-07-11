package orchestratorhost

import (
	"context"
	"os"
	"path/filepath"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// mockValidator is a test validator that can be configured to pass or fail.
type mockValidator struct {
	shouldFail bool
	lastCalled string
}

func (v *mockValidator) Validate(_ context.Context, provider, credential string) error {
	v.lastCalled = provider
	if v.shouldFail {
		return assert.AnError
	}
	return nil
}

func TestCredentialStore_NewEmptyStore(t *testing.T) {
	dir := t.TempDir()
	storePath := filepath.Join(dir, "creds.json")

	cs, err := NewCredentialStore(CredentialStoreConfig{
		StorePath: storePath,
		MasterKey: "test-master-key",
	})
	require.NoError(t, err)
	require.NotNil(t, cs)

	// No providers initially.
	assert.Empty(t, cs.Providers())
}

func TestCredentialStore_SetAndGet(t *testing.T) {
	dir := t.TempDir()
	storePath := filepath.Join(dir, "creds.json")

	cs, err := NewCredentialStore(CredentialStoreConfig{
		StorePath: storePath,
		MasterKey: "test-master-key",
	})
	require.NoError(t, err)

	ctx := context.Background()

	// Set a credential.
	err = cs.Set(ctx, "openai", "sk-test-key-12345")
	require.NoError(t, err)

	// Get it back.
	cred, ok := cs.Get("openai")
	assert.True(t, ok)
	assert.Equal(t, "sk-test-key-12345", cred)

	// Non-existent provider.
	_, ok = cs.Get("nonexistent")
	assert.False(t, ok)
}

func TestCredentialStore_Persistence(t *testing.T) {
	dir := t.TempDir()
	storePath := filepath.Join(dir, "creds.json")
	masterKey := "test-master-key"
	ctx := context.Background()

	// Create and populate store.
	cs1, err := NewCredentialStore(CredentialStoreConfig{
		StorePath: storePath,
		MasterKey: masterKey,
	})
	require.NoError(t, err)

	err = cs1.Set(ctx, "anthropic", "sk-ant-test")
	require.NoError(t, err)
	err = cs1.Set(ctx, "openai", "sk-openai-test")
	require.NoError(t, err)

	// Verify file exists and is encrypted (not plaintext).
	data, err := os.ReadFile(storePath)
	require.NoError(t, err)
	assert.NotContains(t, string(data), "sk-ant-test")
	assert.NotContains(t, string(data), "sk-openai-test")

	// Load a new store from the same file.
	cs2, err := NewCredentialStore(CredentialStoreConfig{
		StorePath: storePath,
		MasterKey: masterKey,
	})
	require.NoError(t, err)

	cred, ok := cs2.Get("anthropic")
	assert.True(t, ok)
	assert.Equal(t, "sk-ant-test", cred)

	cred, ok = cs2.Get("openai")
	assert.True(t, ok)
	assert.Equal(t, "sk-openai-test", cred)
}

func TestCredentialStore_WrongMasterKey(t *testing.T) {
	dir := t.TempDir()
	storePath := filepath.Join(dir, "creds.json")
	ctx := context.Background()

	// Create store with one key.
	cs1, err := NewCredentialStore(CredentialStoreConfig{
		StorePath: storePath,
		MasterKey: "key-one",
	})
	require.NoError(t, err)
	err = cs1.Set(ctx, "openai", "sk-secret")
	require.NoError(t, err)

	// Try to load with a different key — decryption should fail gracefully.
	cs2, err := NewCredentialStore(CredentialStoreConfig{
		StorePath: storePath,
		MasterKey: "key-two-wrong",
	})
	require.NoError(t, err)

	// Credential should not be loadable with wrong key.
	_, ok := cs2.Get("openai")
	assert.False(t, ok)
}

func TestCredentialStore_ValidationBeforePersist(t *testing.T) {
	dir := t.TempDir()
	storePath := filepath.Join(dir, "creds.json")
	ctx := context.Background()

	validator := &mockValidator{shouldFail: true}

	cs, err := NewCredentialStore(CredentialStoreConfig{
		StorePath: storePath,
		MasterKey: "test-key",
		Validator: validator,
	})
	require.NoError(t, err)

	// Set should fail because validator rejects.
	err = cs.Set(ctx, "openai", "sk-invalid")
	assert.Error(t, err)
	assert.Contains(t, err.Error(), "validation failed")

	// Credential should NOT be stored.
	_, ok := cs.Get("openai")
	assert.False(t, ok)

	// Now allow validation.
	validator.shouldFail = false
	err = cs.Set(ctx, "openai", "sk-valid")
	require.NoError(t, err)

	cred, ok := cs.Get("openai")
	assert.True(t, ok)
	assert.Equal(t, "sk-valid", cred)
}

func TestCredentialStore_Delete(t *testing.T) {
	dir := t.TempDir()
	storePath := filepath.Join(dir, "creds.json")
	ctx := context.Background()

	cs, err := NewCredentialStore(CredentialStoreConfig{
		StorePath: storePath,
		MasterKey: "test-key",
	})
	require.NoError(t, err)

	err = cs.Set(ctx, "openai", "sk-test")
	require.NoError(t, err)

	err = cs.Delete("openai")
	require.NoError(t, err)

	_, ok := cs.Get("openai")
	assert.False(t, ok)
}

func TestCredentialStore_EmptyProviderOrCredential(t *testing.T) {
	dir := t.TempDir()
	storePath := filepath.Join(dir, "creds.json")
	ctx := context.Background()

	cs, err := NewCredentialStore(CredentialStoreConfig{
		StorePath: storePath,
		MasterKey: "test-key",
	})
	require.NoError(t, err)

	err = cs.Set(ctx, "", "sk-test")
	assert.Error(t, err)

	err = cs.Set(ctx, "openai", "")
	assert.Error(t, err)
}

func TestCredentialStore_RequiresConfig(t *testing.T) {
	_, err := NewCredentialStore(CredentialStoreConfig{})
	assert.Error(t, err)

	_, err = NewCredentialStore(CredentialStoreConfig{StorePath: "/tmp/test"})
	assert.Error(t, err)
}

func TestSanitizedEnvForSubprocess(t *testing.T) {
	// Set a sensitive variable.
	t.Setenv("OPENAI_API_KEY", "sk-test-secret")
	t.Setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
	t.Setenv("SAFE_VAR", "safe-value")

	env := SanitizedEnvForSubprocess()

	// Sensitive vars should be removed.
	for _, e := range env {
		assert.NotContains(t, e, "OPENAI_API_KEY=")
		assert.NotContains(t, e, "ANTHROPIC_API_KEY=")
	}

	// Safe vars should remain.
	found := false
	for _, e := range env {
		if e == "SAFE_VAR=safe-value" {
			found = true
			break
		}
	}
	assert.True(t, found, "SAFE_VAR should be preserved")
}

func TestCredentialStore_FilePermissions(t *testing.T) {
	dir := t.TempDir()
	storePath := filepath.Join(dir, "creds.json")
	ctx := context.Background()

	cs, err := NewCredentialStore(CredentialStoreConfig{
		StorePath: storePath,
		MasterKey: "test-key",
	})
	require.NoError(t, err)

	err = cs.Set(ctx, "openai", "sk-test")
	require.NoError(t, err)

	// Verify file has restrictive permissions (0600).
	info, err := os.Stat(storePath)
	require.NoError(t, err)
	perm := info.Mode().Perm()
	assert.Equal(t, os.FileMode(0600), perm, "credential store should have 0600 permissions")
}
