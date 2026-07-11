# Architecture Rationale

## Why Two Languages?

Vikram uses Go for the host daemon and Python for the orchestrator. This is a deliberate choice, not accidental complexity.

### Go Host (vikramd)

The Go binary owns everything that touches the operating system:
- Filesystem operations (read, write, atomic rename)
- Git operations (worktree create/remove, branch, merge)
- Shell execution (sandboxed, with allowlists and deny patterns)
- Credential storage (encrypted, never exposed to subprocesses)
- Provider API calls (token tracking, cost recording)
- Network (Unix socket server, Telegram adapter, WebSocket)

Go is correct here because:
- **Single compiled binary** — no runtime dependencies, trivial deployment
- **Goroutines** — handle 10+ concurrent tasks with real parallelism, not async/await
- **OS-level control** — process isolation, signal handling, file permissions
- **Performance** — sub-millisecond response on the Unix socket API; no GC pauses during filesystem operations

### Python Orchestrator

The Python service owns everything that involves *deciding what to do*:
- Workflow state machine (what phase to enter next)
- Agent coordination (which model to call, with what prompt)
- Approval evaluation (should this auto-approve or need founder review?)
- Knowledge queries (what did we learn from prior tasks?)
- Conflict prediction (will this task collide with another?)

Python is correct here because:
- **LangGraph** — the state machine library with checkpointing and interrupt/resume. No equivalent exists in Go.
- **Pydantic** — typed models with validation. The orchestrator manipulates complex nested state that benefits from Python's expressiveness.
- **Rapid iteration** — workflow logic changes frequently as the platform evolves. Python's edit-run cycle is seconds; Go's is compile-link-run.
- **ML ecosystem** — if we need embeddings, tokenizers, or local model inference for knowledge features, Python has them.

### The Boundary Contract

The two processes communicate over a Unix domain socket using HTTP+JSON. The contract is documented in `go-python-contract.md` with:
- Explicit ownership: Go performs actions, Python makes decisions
- Versioned endpoints under `/v1/`
- Typed request/response models on both sides (Go structs, Pydantic models)
- No shared memory, no shared files, no implicit coupling

This means:
- Either side can be restarted independently
- The contract can be tested in isolation (mock the other side)
- A bug in Python cannot corrupt filesystem state (Go validates everything)
- A slow LLM call in Go doesn't block Python's scheduling decisions

### Why Not All-Go or All-Python?

**All-Go** would mean: writing LLM orchestration logic in a language with no state machine library, no checkpointing, verbose error handling for what is essentially scripting logic, and recompiling the binary every time you adjust a workflow prompt.

**All-Python** would mean: managing filesystem sandboxing, Unix socket servers, credential encryption, concurrent git operations, and process lifecycle from an interpreted language with the GIL. Every "simple" operation (atomic file write, signal handling, worktree cleanup) would require subprocess calls to system utilities.

The split puts each language where it excels. The cost is a documented contract between them. That cost is paid once and amortized across every feature built on top.

## Why SQLite?

Vikram uses SQLite for all persistent state (checkpoints, cost ledger, telemetry, knowledge store, execution trace). Not PostgreSQL, not Redis.

- **Zero-config** — no database server to install, configure, or maintain
- **Single-file backup** — copy one file to back up all state
- **Transactional** — ACID guarantees without network round-trips
- **WAL mode** — concurrent readers don't block writers
- **Appropriate scale** — a single Vikram instance handles 3-10 concurrent tasks. SQLite handles this trivially.

If Vikram ever needs multi-instance horizontal scaling, PostgreSQL is the migration path (the schemas are designed for it). But single-instance is the 99% use case for self-hosted tools, and SQLite is the correct choice there.

## Why Not a Unified Messaging Gateway?

Vikram has core channels (CLI, Telegram, REST, WebSocket) and community channels (Discord, Slack, etc. in `contrib/`). The question: why not use Twilio/Matrix/a gateway?

- **Telegram is the primary channel** — the founder needs a mobile-native approval workflow. Telegram's bot API is simple, well-documented, and free. One adapter, maintained in core, is manageable.
- **Community channels are opt-in** — `contrib/channels/` exists for contributors who want their platform supported. These are not core-team maintained and carry no reliability guarantee.
- **No external dependency** — Vikram runs on your machine with your keys. Adding a messaging gateway (Twilio, Matrix homeserver) adds external infrastructure, accounts, and monthly costs. That violates the "your machine, your keys, your terms" principle.

## Why a "Fat" CLI?

`vikram onboard`, `vikram doctor`, `vikram agent`, `vikram gateway` — these are subcommands of a single binary. This is standard Go CLI design (see: Docker, kubectl, Terraform, Hugo).

The alternative — separate binaries per function — would mean:
- Users install and manage multiple executables
- Version skew between components
- No shared configuration loading
- Worse discoverability (users don't know what commands exist)

A single binary with subcommands gives you: one install, one upgrade, one `--help`, and guaranteed version consistency.
