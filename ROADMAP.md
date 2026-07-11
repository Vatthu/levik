# Vikram Roadmap

## Completed

### Foundation (Phase 1–5)
- Go host daemon with Unix socket API
- Python orchestrator with state machine workflow (31 nodes)
- Typed Go-Python contract over Unix domain socket
- Git worktree isolation per task with atomic rollback
- SQLite checkpointing for state persistence
- 20+ LLM provider support with council fallback
- Multi-channel communication (CLI, Telegram, WhatsApp, REST, WebSocket)
- OS-level command sandboxing (allowlist + deny patterns + path containment)

### Multi-Agent Team (Phase 6)
- Config-driven role assignment (lead, engineer, reviewer, runner, qa)
- Per-role provider and model selection
- Resilient team coordination with retry and fallback
- Scored capability matching (never silently fails)
- AgentUnavailableError with available alternatives
- Corrective-prompt retry on malformed agent output
- is_valid_plan() quality gate halts on broken plans
- Subagent spawning with independent tool loops

### Verification Pipeline (Phase 7)
- Adversarial spec validation (Devil's Advocate attacks plan → lead revises)
- Pre/post-edit lint guard with new-error diffing
- Automated test execution with exit code capture
- Independent LLM review (different model from implementer)
- Formal property generation from execution plans
- Property-based verification execution with shrunk counterexamples
- Strategy selection: MINIMAL → STANDARD → PROPERTY → COMPREHENSIVE
- Feedback loop: max 3 fix iterations before founder escalation

### Cost Management (Phase 8)
- Per-call token tracking with full provenance chain
- Per-task budget enforcement with 80% warning + 100% circuit breaker
- Budget strategy allocation (phase-based percentage splits)
- Cost forecasting from historical data (min/expected/max estimates)
- System-wide daily ceiling with global circuit breaker and auto-reset
- Budget-responsive model downgrade with capability floor

### Observability (Phase 9)
- Structured telemetry events (agent_call, phase_transition, host_action)
- SQLite time-series store with WAL mode and configurable retention
- Summary aggregation API (group by role, model, task, phase)
- WebSocket real-time streaming for Console updates
- Health alerting: error rate, latency, provider-down detection
- Tamper-evident execution trace with SHA-256 hash chain
- Replay verification guarantee (same inputs → same decision)
- Periodic integrity validation

### Intelligent Routing (Phase 10)
- Complexity classification (routine / moderate / complex / critical)
- Model selection based on complexity tier and budget position
- Rolling success rate per model per complexity tier
- Named formations with role-to-model mappings
- Formation effectiveness scoring (success × cost × time × first-pass)
- Automatic formation recommendation for new tasks
- Underperformance detection and founder notification

### Governance (Phase 11)
- Declarative approval matrix (YAML config, priority-ordered rules)
- Composable conditions: risk_level, file_patterns, scope, confidence, cost
- Confidence scoring: asymmetric (+1 success, -3 failure) with tier ceilings
- Promotion thresholds (earned autonomy through track record)
- Risk classification engine with repository-specific patterns
- Hot-reload via fsnotify (no restart required)
- Full approval audit trail with export capability

### Conflict Prevention (Phase 12)
- Predictive conflict detection at planning time
- Conflict probability scoring (base + function overlap + proximity + history)
- Cross-task semantic dependency graph (imports, types, function calls)
- Conflict-aware task reordering proposals
- File-level lock registry for runtime contention management
- Incremental dependency graph updates as tasks progress

### Knowledge & Learning (Phase 13)
- Repository knowledge extraction (build commands, conventions, pitfalls)
- Codebase familiarity scoring (coverage × experience × recency)
- Approach fingerprinting and effectiveness scoring
- Failure pattern recognition with taxonomy classification
- Successful alternative suggestions for known failure patterns
- Context compression for large repositories (module graph + relevance ranking)
- Formation evolution based on measured outcomes

### Multi-Repository (Phase 14)
- Multi-repo task definition (1-8 repos per task)
- Per-repository state tracking within single task session
- Cross-repo interface contract analysis (detect breaking changes)
- Coordinated verification (all repos must independently pass)
- Atomic merge gate (ALL repos pass or entire task blocks)
- Repo detachment for isolating blocked repos
- Repo dependency graph for independent parallel execution

### Scheduling & Concurrency (Phase 15)
- Priority queue (critical > high > normal > low, FIFO within level)
- Configurable concurrent execution (default: 3, max: 10)
- Preemption for critical tasks (checkpoint lowest priority)
- Dependency-aware scheduling with cascade failure propagation
- Task execution timeout with checkpoint and notification
- Per-provider rate limit tracking with call queuing

### Platform Operations (Phase 16)
- Founder console with real-time task progress streaming
- Syntax-highlighted diff viewer (unified and side-by-side)
- Cost dashboard with interactive breakdown charts
- One-click merge with strategy selection
- Team health overview with per-agent metrics
- Batch operations (approve-all, reject-all, reprioritize, cancel)
- Task archival with full-text search (365-day retention)
- Formation editor with A/B testing support
- Graceful shutdown with 30-second checkpoint window
- Crash recovery from checkpoints (validates worktree integrity)
- Credential isolation (Go host exclusive holder, never in env vars)
- Network egress restrictions on task worktrees
- Configuration hot-reload without service restart

## In Progress

### MCP Integration Expansion
- Extended MCP tool server support with per-tool timeout
- Allowlist filtering for external tool access
- MCP server health monitoring

## Planned

### Performance & Scale
- PostgreSQL checkpointer option for multi-instance deployments
- Redis pub/sub for distributed task notification
- Horizontal scaling: multiple orchestrator instances with leader election
- Performance benchmarking under sustained concurrent load

### Extended Integrations
- GitHub/GitLab/Bitbucket webhook integration (auto-create tasks from issues)
- Jira/Linear ticket synchronization
- Slack channel adapter
- Custom webhook receivers for CI/CD feedback

### Advanced Verification
- Visual regression testing (screenshot diffing via Playwright)
- Coverage-guided property generation
- Mutation testing integration for verification quality assessment
- Cross-language property translation

### Open Ecosystem
- Plugin system for custom workflow phases
- Formation marketplace (share team configs)
- Custom governance rule functions (beyond declarative YAML)
- Embeddable SDK for custom integration
