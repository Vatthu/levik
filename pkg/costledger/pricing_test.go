package costledger

import (
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

func TestPricingKey(t *testing.T) {
	assert.Equal(t, "anthropic/claude-sonnet-4-20250514", pricingKey("anthropic", "claude-sonnet-4-20250514"))
	assert.Equal(t, "openai/gpt-4o", pricingKey("openai", "gpt-4o"))
}

func TestDefaultPricingTable_ContainsExpectedModels(t *testing.T) {
	pt := DefaultPricingTable()

	expectedModels := []struct {
		provider string
		model    string
	}{
		{"anthropic", "claude-sonnet-4-20250514"},
		{"anthropic", "claude-opus-4-20250514"},
		{"anthropic", "claude-haiku-3-5-20241022"},
		{"openai", "gpt-4o"},
		{"openai", "gpt-4o-mini"},
		{"openai", "o3"},
		{"openai", "o3-mini"},
		{"google", "gemini-2.5-pro"},
		{"google", "gemini-2.5-flash"},
		{"google", "gemini-2.0-flash"},
	}

	for _, em := range expectedModels {
		pricing, err := pt.LookupPricing(em.provider, em.model)
		require.NoError(t, err, "expected pricing for %s/%s", em.provider, em.model)
		assert.Equal(t, em.provider, pricing.Provider)
		assert.Equal(t, em.model, pricing.Model)
		assert.Greater(t, pricing.InputPerToken, 0.0)
		assert.Greater(t, pricing.OutputPerToken, 0.0)
	}
}

func TestLookupPricing_NotFound(t *testing.T) {
	pt := DefaultPricingTable()
	_, err := pt.LookupPricing("unknown", "nonexistent-model")
	assert.Error(t, err)
	assert.Contains(t, err.Error(), "pricing not found")
}

func TestComputeCost_Anthropic(t *testing.T) {
	pt := DefaultPricingTable()

	// claude-sonnet-4: $3/M input, $15/M output
	cost, err := pt.ComputeCost("anthropic", "claude-sonnet-4-20250514", 1000, 500)
	require.NoError(t, err)

	// 1000 * 0.000003 + 500 * 0.000015 = 0.003 + 0.0075 = 0.0105
	assert.InDelta(t, 0.0105, cost, 1e-10)
}

func TestComputeCost_OpenAI(t *testing.T) {
	pt := DefaultPricingTable()

	// gpt-4o: $2.50/M input, $10/M output
	cost, err := pt.ComputeCost("openai", "gpt-4o", 2000, 1000)
	require.NoError(t, err)

	// 2000 * 0.0000025 + 1000 * 0.00001 = 0.005 + 0.01 = 0.015
	assert.InDelta(t, 0.015, cost, 1e-10)
}

func TestComputeCost_Google(t *testing.T) {
	pt := DefaultPricingTable()

	// gemini-2.5-pro: $1.25/M input, $10/M output
	cost, err := pt.ComputeCost("google", "gemini-2.5-pro", 10000, 5000)
	require.NoError(t, err)

	// 10000 * 0.00000125 + 5000 * 0.00001 = 0.0125 + 0.05 = 0.0625
	assert.InDelta(t, 0.0625, cost, 1e-10)
}

func TestComputeCost_UnknownModel(t *testing.T) {
	pt := DefaultPricingTable()
	_, err := pt.ComputeCost("openai", "gpt-99", 1000, 500)
	assert.Error(t, err)
}

func TestComputeCost_ZeroTokens(t *testing.T) {
	pt := DefaultPricingTable()
	cost, err := pt.ComputeCost("openai", "gpt-4o", 0, 0)
	require.NoError(t, err)
	assert.Equal(t, 0.0, cost)
}

func TestComputeCostWithPricing(t *testing.T) {
	pricing := ModelPricing{
		Provider:       "test",
		Model:          "test-model",
		InputPerToken:  0.000001,
		OutputPerToken: 0.000002,
	}

	cost := ComputeCostWithPricing(pricing, 5000, 3000)
	// 5000 * 0.000001 + 3000 * 0.000002 = 0.005 + 0.006 = 0.011
	assert.InDelta(t, 0.011, cost, 1e-10)
}
