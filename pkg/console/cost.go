package console

import (
	"context"
	"net/http"
	"time"
)

// --- Cost Dashboard Models (Requirement 45) ---

// CostOverview holds aggregated cost data for the dashboard header.
// Requirement 45.1: today, week, month, projected monthly.
type CostOverview struct {
	TodayUSD          float64 `json:"today_usd"`
	WeekUSD           float64 `json:"week_usd"`
	MonthUSD          float64 `json:"month_usd"`
	ProjectedMonthUSD float64 `json:"projected_month_usd"`
	DailyCeilingUSD   float64 `json:"daily_ceiling_usd"`
	DailyUsedUSD      float64 `json:"daily_used_usd"`
}

// CostBreakdownEntry represents a single entry in a cost breakdown chart.
type CostBreakdownEntry struct {
	Label   string  `json:"label"`
	CostUSD float64 `json:"cost_usd"`
	Calls   int     `json:"calls"`
	Tokens  int     `json:"tokens"`
}

// CostBreakdownResponse contains cost breakdowns grouped by dimension.
// Requirement 45.2: by role, model, task, phase, provider.
type CostBreakdownResponse struct {
	Dimension string               `json:"dimension"`
	TimeRange string               `json:"time_range"`
	Entries   []CostBreakdownEntry `json:"entries"`
}

// BudgetUtilization represents budget usage for a single scope.
// Requirement 45.3: system-wide, per-role, per-task with 80% warning.
type BudgetUtilization struct {
	Scope      string  `json:"scope"`
	Label      string  `json:"label"`
	UsedUSD    float64 `json:"used_usd"`
	LimitUSD   float64 `json:"limit_usd"`
	Percentage float64 `json:"percentage"`
	Warning    bool    `json:"warning"`
}

// CostDashboardResponse is the full response for the cost overview endpoint.
type CostDashboardResponse struct {
	Overview     CostOverview        `json:"overview"`
	Utilizations []BudgetUtilization `json:"utilizations"`
}

// --- Cost API Handlers ---

// handleAPICostOverview serves GET /api/cost/overview.
// Returns cost summary (today, week, month, projected) and budget utilization bars.
// Proxies to the orchestrator's /v1/telemetry/cost endpoint.
func (s *Server) handleAPICostOverview(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		s.writeError(w, http.StatusMethodNotAllowed, "GET only")
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), 10*time.Second)
	defer cancel()

	var result CostDashboardResponse
	if err := s.orchestratorJSON(ctx, http.MethodGet, "/v1/telemetry/cost?view=overview", nil, &result); err != nil {
		// If orchestrator is unreachable, return empty data with zeros
		if !isOrchestratorHTTPError(err) {
			s.writeOK(w, CostDashboardResponse{
				Overview:     CostOverview{},
				Utilizations: []BudgetUtilization{},
			})
			return
		}
		s.writeError(w, http.StatusBadGateway, err.Error())
		return
	}

	// Apply 80% warning threshold to utilizations
	for i := range result.Utilizations {
		if result.Utilizations[i].LimitUSD > 0 {
			result.Utilizations[i].Percentage = (result.Utilizations[i].UsedUSD / result.Utilizations[i].LimitUSD) * 100
			result.Utilizations[i].Warning = result.Utilizations[i].Percentage >= 80
		}
	}

	s.writeOK(w, result)
}

// handleAPICostBreakdown serves GET /api/cost/breakdown?dimension=role&range=today.
// Returns cost breakdown by the specified dimension.
// Proxies to the orchestrator's /v1/telemetry/cost endpoint with breakdown params.
func (s *Server) handleAPICostBreakdown(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		s.writeError(w, http.StatusMethodNotAllowed, "GET only")
		return
	}

	dimension := r.URL.Query().Get("dimension")
	if dimension == "" {
		dimension = "role"
	}
	// Validate dimension
	validDimensions := map[string]bool{
		"role": true, "model": true, "task": true, "phase": true, "provider": true,
	}
	if !validDimensions[dimension] {
		s.writeError(w, http.StatusBadRequest, "dimension must be one of: role, model, task, phase, provider")
		return
	}

	timeRange := r.URL.Query().Get("range")
	if timeRange == "" {
		timeRange = "today"
	}
	validRanges := map[string]bool{
		"today": true, "week": true, "month": true, "30d": true,
	}
	if !validRanges[timeRange] {
		s.writeError(w, http.StatusBadRequest, "range must be one of: today, week, month, 30d")
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), 10*time.Second)
	defer cancel()

	var result CostBreakdownResponse
	path := "/v1/telemetry/cost?view=breakdown&dimension=" + dimension + "&range=" + timeRange
	if err := s.orchestratorJSON(ctx, http.MethodGet, path, nil, &result); err != nil {
		if !isOrchestratorHTTPError(err) {
			s.writeOK(w, CostBreakdownResponse{
				Dimension: dimension,
				TimeRange: timeRange,
				Entries:   []CostBreakdownEntry{},
			})
			return
		}
		s.writeError(w, http.StatusBadGateway, err.Error())
		return
	}

	s.writeOK(w, result)
}
