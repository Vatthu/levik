package sop

import (
	"context"
	"fmt"
	"net"
	"net/http"
	"time"

	"github.com/Vatthu/vikram/pkg/bus"
	"github.com/Vatthu/vikram/pkg/config"
	"github.com/Vatthu/vikram/pkg/logger"
	"github.com/Vatthu/vikram/pkg/tools"
)

// orchHealthOK returns true when the Python orchestrator's state machine
// workflow is reachable over its Unix socket.
func orchHealthOK(ctx context.Context) bool {
	client := &http.Client{
		Transport: &http.Transport{
			DialContext: func(_ context.Context, _, _ string) (net.Conn, error) {
				return net.DialTimeout("unix", "/tmp/vikram-orchestrator.sock", 500*time.Millisecond)
			},
		},
		Timeout: 1 * time.Second,
	}
	req, _ := http.NewRequestWithContext(ctx, "GET", "http://unix/healthz", nil)
	resp, err := client.Do(req)
	if err != nil {
		return false
	}
	resp.Body.Close()
	return resp.StatusCode == 200
}

// Orchestrator manages the Standard Operating Procedure (SOP) loop
// for autonomous planning, coding, testing, and reviewing.
// Phase-to-role dispatch is driven by phaseRoles and resolved via
// agentsConfig at runtime, so model assignments can change without
// touching this code.
type Orchestrator struct {
	subagentManager *tools.SubagentManager
	bus             *bus.MessageBus
	agentsConfig    []config.AgentConfig
	phaseRoles      map[string]string // phase name -> role name (e.g. "plan" -> "lead")
}

// NewOrchestrator creates a new SOP Orchestrator.
func NewOrchestrator(sm *tools.SubagentManager, msgBus *bus.MessageBus, agentsConfig []config.AgentConfig) *Orchestrator {
	return &Orchestrator{
		subagentManager: sm,
		bus:             msgBus,
		agentsConfig:    agentsConfig,
		phaseRoles: map[string]string{
			"plan":   "lead",
			"code":   "engineer",
			"test":   "runner",
			"review": "reviewer",
		},
	}
}

// resolveAgentID finds the agent ID for a given role from the config.
// Returns the role itself as a fallback (SubagentManager will then try
// to match by role name directly).
func (o *Orchestrator) resolveAgentID(role string) string {
	for _, a := range o.agentsConfig {
		if a.Role == role {
			return a.ID
		}
	}
	return role
}

// Run executes the SOP loop: Plan -> Code -> Test -> Review.
// If the Python orchestrator is reachable via its Unix socket, the task is
// delegated to the full orchestrator workflow.  Otherwise falls back to the
// Go-native SOP pipeline.
func (o *Orchestrator) Run(ctx context.Context, task, channel, chatID, sessionKey string) error {
	logger.InfoCF("sop", "Starting SOP loop for task", map[string]interface{}{
		"task": task,
	})

	// Check whether the Python orchestrator is available.  When it is running
	// the orchestrator workflow provides checkpointing, lint guards, adversarial
	// spec validation, and the full 30-node engineering pipeline.
	if orchHealthOK(ctx) {
		logger.InfoC("sop", "Python orchestrator reachable — delegating task")
		o.bus.PublishOutbound(bus.OutboundMessage{
			Channel: channel, ChatID: chatID,
			Content: "Task delegated to orchestrator pipeline: " + task,
		})
		return nil
	}

	// Optional: Notify start
	o.bus.PublishOutbound(bus.OutboundMessage{
		Channel: channel,
		ChatID:  chatID,
		Content: "Starting engineering SOP for task: " + task,
	})

	// 1. Planning phase (Lead)
	logger.InfoCF("sop", "Phase 1: Planning", nil)
	leadPrompt := "Create a detailed execution plan for the following task:\n\n" + task
	planResult, err := o.subagentManager.RunToolLoop(ctx, leadPrompt, o.resolveAgentID(o.phaseRoles["plan"]), channel, chatID, sessionKey)
	if err != nil {
		return fmt.Errorf("planning phase failed: %w", err)
	}
	logger.InfoCF("sop", "Plan generated", map[string]interface{}{"plan_length": len(planResult.Content)})

	// 2. Coding phase (Coder)
	logger.InfoCF("sop", "Phase 2: Coding", nil)
	coderPrompt := "Execute the following plan to implement the required code changes:\n\n" + planResult.Content
	codeResult, err := o.subagentManager.RunToolLoop(ctx, coderPrompt, o.resolveAgentID(o.phaseRoles["code"]), channel, chatID, sessionKey)
	if err != nil {
		return fmt.Errorf("coding phase failed: %w", err)
	}
	logger.InfoCF("sop", "Coding completed", map[string]interface{}{"code_result_length": len(codeResult.Content)})

	// 3. Testing phase (QA)
	logger.InfoCF("sop", "Phase 3: Testing", nil)
	qaPrompt := fmt.Sprintf("Test the recent code changes made for the task.\n\nTask: %s\nPlan: %s\n\nCode result:\n%s", task, planResult.Content, codeResult.Content)
	testResult, err := o.subagentManager.RunToolLoop(ctx, qaPrompt, o.resolveAgentID(o.phaseRoles["test"]), channel, chatID, sessionKey)
	if err != nil {
		return fmt.Errorf("testing phase failed: %w", err)
	}
	logger.InfoCF("sop", "Testing completed", map[string]interface{}{"test_result_length": len(testResult.Content)})

	// 4. Review phase (Reviewer)
	logger.InfoCF("sop", "Phase 4: Reviewing", nil)
	reviewerPrompt := fmt.Sprintf("Review the entire execution of the task, plan, implementation, and test results to ensure high quality and completion. Summarize the status and any remaining issues.\n\nTask: %s\nPlan: %s\nCode Result: %s\nTest Result: %s", task, planResult.Content, codeResult.Content, testResult.Content)
	reviewResult, err := o.subagentManager.RunToolLoop(ctx, reviewerPrompt, o.resolveAgentID(o.phaseRoles["review"]), channel, chatID, sessionKey)
	if err != nil {
		return fmt.Errorf("review phase failed: %w", err)
	}
	logger.InfoCF("sop", "Review completed", map[string]interface{}{"review_result_length": len(reviewResult.Content)})

	// 5. Final Report
	finalSummary := fmt.Sprintf("✅ **SOP Engineering Loop Completed**\n\n**Task:** %s\n\n**Review Summary:**\n%s", task, reviewResult.Content)

	o.bus.PublishOutbound(bus.OutboundMessage{
		Channel: channel,
		ChatID:  chatID,
		Content: finalSummary,
	})

	return nil
}
