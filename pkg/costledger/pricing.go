package costledger

import "fmt"

// ModelPricing holds per-token pricing for a specific model.
type ModelPricing struct {
	Provider       string  `json:"provider"`
	Model          string  `json:"model"`
	InputPerToken  float64 `json:"input_per_token"`  // USD per input token
	OutputPerToken float64 `json:"output_per_token"` // USD per output token
}

// PricingTable maps provider/model combinations to their token pricing.
// The key format is "provider/model" (e.g., "anthropic/claude-sonnet-4-20250514").
type PricingTable map[string]ModelPricing

// pricingKey returns the lookup key for a provider/model combination.
func pricingKey(provider, model string) string {
	return provider + "/" + model
}

// DefaultPricingTable returns the built-in pricing table with published rates
// for major providers. Prices are in USD per token.
func DefaultPricingTable() PricingTable {
	return PricingTable{
		// Anthropic models
		pricingKey("anthropic", "claude-sonnet-4-20250514"): {
			Provider:       "anthropic",
			Model:          "claude-sonnet-4-20250514",
			InputPerToken:  0.000003, // $3.00 per 1M input tokens
			OutputPerToken: 0.000015, // $15.00 per 1M output tokens
		},
		pricingKey("anthropic", "claude-opus-4-20250514"): {
			Provider:       "anthropic",
			Model:          "claude-opus-4-20250514",
			InputPerToken:  0.000015, // $15.00 per 1M input tokens
			OutputPerToken: 0.000075, // $75.00 per 1M output tokens
		},
		pricingKey("anthropic", "claude-haiku-3-5-20241022"): {
			Provider:       "anthropic",
			Model:          "claude-haiku-3-5-20241022",
			InputPerToken:  0.0000008, // $0.80 per 1M input tokens
			OutputPerToken: 0.000004,  // $4.00 per 1M output tokens
		},

		// OpenAI models
		pricingKey("openai", "gpt-4o"): {
			Provider:       "openai",
			Model:          "gpt-4o",
			InputPerToken:  0.0000025, // $2.50 per 1M input tokens
			OutputPerToken: 0.00001,   // $10.00 per 1M output tokens
		},
		pricingKey("openai", "gpt-4o-mini"): {
			Provider:       "openai",
			Model:          "gpt-4o-mini",
			InputPerToken:  0.00000015, // $0.15 per 1M input tokens
			OutputPerToken: 0.0000006,  // $0.60 per 1M output tokens
		},
		pricingKey("openai", "o3"): {
			Provider:       "openai",
			Model:          "o3",
			InputPerToken:  0.00001, // $10.00 per 1M input tokens
			OutputPerToken: 0.00004, // $40.00 per 1M output tokens
		},
		pricingKey("openai", "o3-mini"): {
			Provider:       "openai",
			Model:          "o3-mini",
			InputPerToken:  0.0000011, // $1.10 per 1M input tokens
			OutputPerToken: 0.0000044, // $4.40 per 1M output tokens
		},

		// Google models
		pricingKey("google", "gemini-2.5-pro"): {
			Provider:       "google",
			Model:          "gemini-2.5-pro",
			InputPerToken:  0.00000125, // $1.25 per 1M input tokens
			OutputPerToken: 0.00001,    // $10.00 per 1M output tokens
		},
		pricingKey("google", "gemini-2.5-flash"): {
			Provider:       "google",
			Model:          "gemini-2.5-flash",
			InputPerToken:  0.00000015, // $0.15 per 1M input tokens
			OutputPerToken: 0.0000006,  // $0.60 per 1M output tokens
		},
		pricingKey("google", "gemini-2.0-flash"): {
			Provider:       "google",
			Model:          "gemini-2.0-flash",
			InputPerToken:  0.0000001, // $0.10 per 1M input tokens
			OutputPerToken: 0.0000004, // $0.40 per 1M output tokens
		},
	}
}

// LookupPricing returns the pricing for a given provider/model combination.
// Returns an error if the model is not found in the table.
func (pt PricingTable) LookupPricing(provider, model string) (ModelPricing, error) {
	key := pricingKey(provider, model)
	pricing, ok := pt[key]
	if !ok {
		return ModelPricing{}, fmt.Errorf("pricing not found for %s/%s", provider, model)
	}
	return pricing, nil
}

// ComputeCost calculates the total USD cost for a given token usage.
// Returns the cost and an error if the model is not in the pricing table.
func (pt PricingTable) ComputeCost(provider, model string, inputTokens, outputTokens int) (float64, error) {
	pricing, err := pt.LookupPricing(provider, model)
	if err != nil {
		return 0, err
	}
	cost := float64(inputTokens)*pricing.InputPerToken + float64(outputTokens)*pricing.OutputPerToken
	return cost, nil
}

// ComputeCostWithPricing calculates cost directly from a ModelPricing value
// without a table lookup. Useful when the pricing is already resolved.
func ComputeCostWithPricing(pricing ModelPricing, inputTokens, outputTokens int) float64 {
	return float64(inputTokens)*pricing.InputPerToken + float64(outputTokens)*pricing.OutputPerToken
}
