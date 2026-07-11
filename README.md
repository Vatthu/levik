<div align="center">

# Vikram

**The autonomous engineering team that runs on your infrastructure.**

[![CI](https://github.com/Vatthu/vikram/actions/workflows/ci.yml/badge.svg)](https://github.com/Vatthu/vikram/actions/workflows/ci.yml)
[![Go Report Card](https://goreportcard.com/badge/github.com/Vatthu/vikram)](https://goreportcard.com/report/github.com/Vatthu/vikram)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Go Version](https://img.shields.io/github/go-mod-go-version/Vatthu/vikram)](go.mod)

[Install](#install) · [What is Vikram?](#what-is-vikram) · [How It Works](#how-it-works) · [Quick Start](#quick-start) · [FAQ](FAQ.md) · [Architecture](#architecture) · [Contributing](CONTRIBUTING.md)

</div>

---

## What is Vikram?

Vikram is a self-hosted platform that operates as your autonomous engineering team. You give it an objective — "implement OAuth callback hardening" or "add pagination to the /users API" — and Vikram plans the work, writes the code, verifies correctness through property-based testing, reviews its own output with an independent model, and delivers a merge-ready branch. All on your machine, with your API keys, under your governance policies.

It is not a copilot. It is not an autocomplete engine. It is a team.

**What makes Vikram different from every other AI coding tool:**

| Capability | What it means |
|-----------|--------------|
| **Tamper-evident execution trace** | Every decision is recorded in a SHA-256 hash chain. Ask "why was this approved?" and get the exact state, policy, and reasoning. No other tool does this. |
| **Formal verification protocol** | Generates correctness properties from the plan and proves the implementation satisfies them through property-based testing. Not just "tests pass" — formally verified. |
| **Predictive conflict prevention** | Detects merge conflicts at planning time by analyzing target files across all active tasks. Reorders execution to avoid wasted work. |
| **Budget strategy (not just limits)** | Allocates spend as a strategy: 60% implementation, 20% review, 10% planning, 10% QA. Model selection follows the strategy dynamically. |
| **Declarative governance** | Approval policies defined as code. Composable rules: risk × scope × trust × cost. Version-controlled, hot-reloadable, audit-ready. |
| **Self-improving formations** | Learns which team configurations (model + role combinations) work for which task types. Auto-reconfigures based on measured outcomes. |
| **Multi-repository coordination** | Handles tasks spanning multiple repos as atomic units. Cross-repo interface analysis catches breaking changes before they happen. |
| **Full sovereignty** | Your keys, your machine, your rules. Zero telemetry. Zero cloud dependency. Compiled Go binary + Python orchestrator. |

## How It Works

```
You: "Add rate limiting to the /api/v1/* endpoints"
     ↓
Vikram:
  1. Queues task with priority, produces cost forecast
  2. Detects potential conflicts with other active tasks
  3. Plans the approach, injecting repository knowledge from prior tasks
  4. Selects optimal models per work phase based on complexity and budget
  5. Implements in an isolated git worktree (atomic, rollbackable)
  6. Generates formal correctness properties and verifies them
  7. Runs lint guard + test suite + independent LLM review
  8. Evaluates governance policy → auto-approves or requests your sign-off
  9. Passes merge gate → ready to merge
     ↓
You: review the diff, click merge (or let Vikram auto-merge if trusted)
```

Every step is recorded in the execution trace. Every cost is attributed. Every decision is auditable.

## Install

### One-Command Install (macOS / Linux)

```bash
curl -sSL https://raw.githubusercontent.com/Vatthu/vikram/main/install.sh | sh
```

### Build from Source

**Requirements:**
- Go 1.22+ 
- Python 3.12+ (for the orchestrator)
- Git

```bash
git clone https://github.com/Vatthu/vikram.git
cd vikram

# Build the Go host binary
make build

# Set up the Python orchestrator
cd services/orchestrator
python -m venv .venv
source .venv/bin/activate
pip install -e .
cd ../..
```

### Verify Installation

```bash
# Check the Go binary
vikram doctor

# First-time setup (configures providers, workspace, channels)
vikram onboard
```

## Quick Start

### 1. Configure Your Providers

```bash
vikram configure
```

This opens an interactive setup where you add your API keys for any combination of:
- OpenAI, Anthropic, Google Gemini, DeepSeek, Mistral, NVIDIA, OpenRouter, Groq, Ollama, and 10+ more.

### 2. Start the Platform

```bash
# Start the full platform (Go host + Python orchestrator)
vikram gateway
```

### 3. Submit Your First Task

Via Telegram (if configured):
```
/task Add input validation to the user registration endpoint
```

Via CLI:
```bash
vikram agent -m "Add input validation to the user registration endpoint"
```

Via REST API:
```bash
curl -X POST http://localhost:8080/v1/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "task_id": "val-001",
    "objective": "Add input validation to the user registration endpoint",
    "repo": {"path": "/path/to/your/repo", "default_branch": "main"},
    "priority": "normal"
  }'
```

### 4. Monitor Progress

Open the founder console at `http://localhost:8080/console` to see:
- Real-time task progress
- Cost breakdown per task and role
- Team health and agent status
- Diffs for review and one-click merge

## Architecture

Vikram is split into two processes with a strict ownership boundary:

```
┌─────────────────────────────────────────────────────────────────┐
│                     Go Host (vikramd)                            │
│                                                                 │
│  Owns: filesystem, git, exec, credentials, provider calls,     │
│        cost recording, telemetry storage, notifications,        │
│        lock registry, console serving, merge operations         │
│                                                                 │
│  Packages:                                                      │
│    pkg/orchestratorhost  — Unix socket API (40+ endpoints)      │
│    pkg/costledger        — per-call cost tracking + budgets     │
│    pkg/telemetry         — event store + alerting + streaming   │
│    pkg/locks             — file-level contention management     │
│    pkg/console           — web UI + diff viewer + merge ops     │
│    pkg/providers         — 20+ LLM provider adapters            │
│    pkg/mcp              — MCP client for external tool servers  │
│    pkg/tools            — sandboxed shell, filesystem, git      │
└───────────────────────────────┬─────────────────────────────────┘
                                │ HTTP+JSON over Unix domain socket
┌───────────────────────────────┴─────────────────────────────────┐
│                  Python Orchestrator                             │
│                                                                 │
│  Owns: workflow decisions, agent coordination, state            │
│        transitions, approval evaluation, knowledge queries      │
│                                                                 │
│  Modules:                                                       │
│    workflow.py             — state machine (planning → merge)    │
│    model_router.py        — complexity classification + routing  │
│    approval_matrix.py     — declarative governance policies     │
│    execution_trace.py     — tamper-evident decision audit       │
│    conflict_detector.py   — predictive conflict scoring         │
│    knowledge_store.py     — repository learning + patterns      │
│    verification_protocol.py — property generation + execution   │
│    scheduler.py           — priority queue + concurrency        │
│    multi_repo.py          — cross-repo coordination             │
│    formations.py          — team topology management            │
│    cost_client.py         — budget-aware decision queries       │
│    telemetry_client.py    — phase transition event emission     │
└─────────────────────────────────────────────────────────────────┘
```

**Go owns the machine.** Every system interaction (shell, filesystem, git, network, credentials) goes through the Go host. The orchestrator never touches the filesystem directly.

**Python owns the decisions.** What to do next, which model to use, whether to approve, when to escalate — all determined by the orchestrator. The Go host executes; Python decides.

**Communication:** HTTP+JSON over a Unix domain socket. No public TCP ports. No shared memory. Clean contract documented in `docs/architecture/go-python-contract.md`.

## Key Capabilities

### Cost Management
- Per-call token tracking with full provenance (task → role → phase → invocation)
- Per-task budget caps with 80% warning + 100% circuit breaker
- Budget strategy allocation across work phases
- Cost forecasting before task execution begins
- System-wide daily ceiling with automatic reset

### Observability
- Structured telemetry for every agent call, phase transition, and host action
- Real-time WebSocket streaming to the founder console
- Health alerting (error rate, latency, provider-down detection)
- Execution trace with tamper-evident hash chain (every decision auditable)

### Intelligent Routing
- Complexity-based model selection (cheap models for docs, capable models for architecture)
- Budget-responsive downgrade with capability floor enforcement
- Rolling success rate tracking per model per complexity tier
- Named formations (team configs) with effectiveness scoring and auto-recommendation

### Governance
- Declarative approval matrix loaded from `.vikram/approval-matrix.yaml`
- Confidence scoring: asymmetric trust building (+1 success, -3 failure)
- Risk classification engine with repository-specific sensitivity patterns
- Hot-reloadable policies (change governance without restarting)
- Full audit trail exportable for compliance

### Verification
- Three-layer verification: lint guard → test execution → independent LLM review
- Formal property generation from execution plans
- Property-based testing execution with shrunk counterexamples on failure
- Feedback loop: failed properties feed back to implementation agent (max 3 iterations)
- Strategy selection adapts rigor to change type (minimal for docs, comprehensive for architecture)

### Multi-Repository
- Tasks spanning 1-8 repositories handled as atomic units
- Per-repository state tracking within a single task session
- Cross-repo interface contract analysis (detects breaking changes)
- Coordinated merge gate (all repos pass or entire task blocks)
- Repo isolation: failure in one repo doesn't block independent repos

### Scheduling & Concurrency
- Priority queue (critical > high > normal > low)
- Configurable concurrent execution (default: 3 parallel tasks)
- Preemption for critical-priority tasks
- Dependency-aware scheduling with cascade failure propagation
- File-level lock registry prevents concurrent write conflicts

### Learning
- Repository knowledge extraction (build commands, conventions, pitfalls)
- Approach effectiveness tracking (which strategies work for which task types)
- Failure pattern recognition with alternative suggestions
- Formation evolution based on measured outcomes
- Codebase context compression for large repositories

## Configuration

### Team Formation Example

```json
{
  "agents": {
    "list": [
      {"id": "lead", "role": "lead", "provider": "anthropic", "model": "claude-sonnet-4-20250514"},
      {"id": "engineer", "role": "engineer", "provider": "deepseek", "model": "deepseek-chat"},
      {"id": "reviewer", "role": "reviewer", "provider": "openai", "model": "gpt-4o"},
      {"id": "qa", "role": "qa", "provider": "anthropic", "model": "claude-sonnet-4-20250514"}
    ]
  }
}
```

### Approval Matrix Example

Create `.vikram/approval-matrix.yaml` in your repository:

```yaml
version: 1
rules:
  - name: security-always-review
    priority: 1
    conditions:
      file_patterns: ["**/auth/**", "**/security/**"]
    routing: founder_review

  - name: docs-auto-approve
    priority: 10
    conditions:
      risk_level: [low]
      file_patterns: ["**/*.md", "docs/**"]
      min_confidence_score: 5
    routing: auto_approve

  - name: default
    priority: 999
    conditions: {}
    routing: founder_review
```

### Budget Strategy Example

```json
{
  "budget_strategy": {
    "planning": 10,
    "implementation": 60,
    "verification": 10,
    "review": 20
  }
}
```

## Multi-Channel Access

**Core channels** (maintained, tested, supported):

| Channel | Use Case |
|---------|----------|
| **CLI** | Interactive terminal or single-shot commands |
| **Telegram** | Submit tasks and receive approvals from your phone |
| **REST API** | Integrate with CI/CD, issue trackers, or custom tooling |
| **WebSocket** | Real-time streaming for custom UIs |
| **Founder Console** | Web dashboard for full platform management |

**Community channels** (`contrib/channels/` — community-maintained, no SLA):

Discord, Slack, DingTalk, Feishu, Line, QQ, OneBot, MaixCam. These adapters are contributed by the community and work but are not part of the core test suite. PRs welcome.

## Project Structure

```
cmd/vikram/              CLI and gateway binary entry point
pkg/                     Go packages
  orchestratorhost/      Unix socket API server (40+ endpoints)
  costledger/            Cost tracking and budget enforcement
  telemetry/             Event store, alerting, WebSocket streaming
  locks/                 File-level contention management
  console/               Web UI server (diff, cost, merge, health)
  providers/             20+ LLM provider adapters
  mcp/                   Model Context Protocol client
  tools/                 Sandboxed execution (shell, fs, git)
  channels/              Telegram, WhatsApp adapters
  auth/                  Credential store and OAuth
services/orchestrator/   Python orchestrator
  src/vikram_orchestrator/
    workflow.py           State machine (31 nodes, planning → merge)
    model_router.py       Complexity classification + routing
    approval_matrix.py    Declarative governance engine
    execution_trace.py    Tamper-evident decision audit
    conflict_detector.py  Predictive conflict scoring
    knowledge_store.py    Repository learning
    verification_protocol.py  Property generation + execution
    scheduler.py          Priority queue + concurrency
    multi_repo.py         Cross-repo coordination
    formations.py         Team topology management
docs/architecture/       Technical design documents
web/                     Console frontend assets
```

## Privacy & Security

- **Zero telemetry.** Vikram sends no data anywhere except the LLM providers you configure.
- **Your keys, your control.** API credentials are stored in an encrypted local credential store, never exposed to task execution contexts.
- **Sandboxed execution.** Shell commands run within path containment with configurable network egress restrictions.
- **Audit-ready.** Every decision in the execution trace, every cost in the ledger, every approval in the audit log.

See [PRIVACY.md](PRIVACY.md) for the full privacy commitment and [SECURITY.md](SECURITY.md) for responsible disclosure.

## Contributing

We welcome contributions. See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup, architecture overview, and PR workflow.

## License

[MIT](LICENSE)
