# Frequently Asked Questions

## Getting Started

### What is Vikram in one sentence?

Vikram is an AI engineering team that runs on your computer. You tell it what code to write, and it plans, implements, tests, and delivers a merge-ready branch — autonomously.

### Who is Vikram for?

Solo founders, indie hackers, small teams, and any developer who wants to multiply their output without hiring. You describe what you want built, and Vikram's team of AI agents does the engineering work while you review and approve.

### How much does it cost to run Vikram?

Vikram itself is free and open source. You pay only for the LLM API calls it makes (OpenAI, Anthropic, Google, DeepSeek, etc.). A typical task costs $0.50–$5.00 depending on complexity. Vikram has built-in budget controls so you can set a maximum spend per task and it will never exceed it.

### What do I need to get started?

Three things:
1. A computer (Mac, Linux, or Windows with WSL)
2. At least one LLM API key (see next question)
3. A code repository you want Vikram to work on

### How many API keys do I need? Can I use just one?

**Minimum: 1 key.** You can run Vikram with a single API key (e.g., one OpenAI key or one Anthropic key). Every role (planner, coder, reviewer) will use that same key and model.

**Recommended: 2–3 keys from different providers.** This gives you:
- A primary model for implementation (e.g., Claude for coding)
- A different model for review (independent verification from a different "brain")
- A fallback in case one provider goes down or rate-limits you

**Optimal for production: 3+ keys across providers.** For example:
- Anthropic Claude for planning and implementation (strong reasoning)
- DeepSeek for bulk coding tasks (cost-effective)
- OpenAI GPT-4o for independent code review (different perspective)
- Google Gemini as a fallback

Vikram automatically falls back to your secondary provider if your primary one errors out.

### Do all agents need the same AI provider?

No. Each role (lead planner, engineer, reviewer, QA) can use a different provider and model. You can assign cheap models to simple tasks and expensive models to complex ones. Vikram does this automatically based on task complexity if you configure multiple models.

### What LLM providers does Vikram support?

OpenAI, Anthropic (Claude), Google Gemini, Google Vertex AI, DeepSeek, Mistral, OpenRouter, Groq, Ollama (local models), NVIDIA, Cerebras, SambaNova, Azure OpenAI, AWS Bedrock, GitHub Models, xAI, Moonshot, Zhipu, and more. Basically anything with an OpenAI-compatible API works.

### Can I use free/local models like Ollama?

Yes. Vikram works with Ollama and any local model. Be aware that local models are generally less capable than cloud models — Vikram works best with models that support function/tool calling well (Claude, GPT-4o, Gemini).

---

## How It Works

### What happens when I give Vikram a task?

1. **Planning** — An AI agent analyzes your codebase and creates an implementation plan
2. **Adversarial review** — A different AI agent attacks the plan looking for flaws (like a code review before code is written)
3. **Implementation** — An engineer agent writes the code in an isolated git branch
4. **Verification** — Runs linting, tests, and property-based verification
5. **Independent review** — A reviewer agent (different model from the coder) evaluates the changes
6. **Approval** — Either auto-approves (if low-risk) or asks you to review via Telegram/Console
7. **Merge-ready** — The branch is ready. You click merge when satisfied.

### Does Vikram modify my code directly?

Never on your main branch. Vikram creates an isolated git worktree (a separate working copy of your repo) and makes all changes there. Your main branch is untouched until you explicitly approve and merge. If anything goes wrong, the worktree is deleted — zero impact on your real code.

### Can I use Vikram without Telegram?

Yes. Telegram is optional. You can interact entirely through:
- The command line (`vikram agent -m "your task"`)
- The web console (`http://localhost:8080/console`)
- The REST API (for programmatic integration)

Telegram is just convenient for approving things from your phone.

### Can Vikram work on any programming language?

Yes. Vikram works through standard tools (git, shell commands, file editing). It handles any language that your repository uses — Go, Python, JavaScript, TypeScript, Rust, Java, Ruby, etc. It discovers your build/test commands automatically.

### How does Vikram know how to build and test my project?

Vikram inspects your repository for common configuration files (package.json, go.mod, Makefile, pyproject.toml, Cargo.toml, etc.) and discovers the correct build, test, and lint commands automatically. Over time, it remembers what works for your specific repo.

---

## Safety & Control

### Can Vikram run dangerous commands on my machine?

Vikram has a built-in command allowlist and deny list. It blocks destructive commands (rm -rf /, format disk, etc.) by default. Shell execution is sandboxed within the task's worktree — it cannot modify files outside the project directory unless you explicitly allow it.

### What if Vikram makes a mistake?

Every change is in an isolated git worktree. If anything is wrong:
- You reject the change in the approval step — nothing happens to your code
- If auto-approved, you can revert with one command (Vikram keeps rollback snapshots)
- The execution trace shows exactly what decisions were made and why

### Can I set spending limits?

Yes. Three levels:
- **Per task** — "This task can spend max $3.00" (stops and asks if exceeded)
- **Per day** — "Don't spend more than $20/day total across all tasks"
- **Warning at 80%** — You get a Telegram/console notification before hitting the limit

### Does Vikram send my code to the cloud?

Only to the LLM providers you configure (OpenAI, Anthropic, etc.) — the same way any AI coding tool works. Vikram itself has zero telemetry, zero phone-home, zero data collection. It runs entirely on your machine. The code in your repository goes only to the AI provider APIs you explicitly set up.

### Can I control what gets auto-approved vs. what needs my review?

Yes. Vikram has a declarative approval matrix:
- Documentation-only changes → auto-approve after tests pass
- Code changes → require your review
- Security-sensitive files → always require your review
- As Vikram proves reliable, it earns more autonomy over time (configurable trust scoring)

---

## Daily Usage

### What's a typical workflow?

Morning routine:
1. Open Telegram or the console
2. Send tasks: "/task Add rate limiting to the API endpoints"
3. Go about your day
4. Vikram notifies you when review is needed (usually 5–15 minutes later)
5. Review the diff, approve or request changes
6. Merge when satisfied

You can queue multiple tasks — Vikram handles them in priority order, even in parallel if configured.

### Can Vikram handle multiple tasks at the same time?

Yes. By default it runs up to 3 tasks concurrently (configurable up to 10). When tasks target different files, they run in full parallel. When they might conflict, Vikram automatically serializes them to prevent merge issues.

### How long does a typical task take?

- Simple (documentation, config changes): 2–5 minutes
- Moderate (single-file logic, adding tests): 5–15 minutes
- Complex (multi-file feature, refactoring): 15–45 minutes
- Critical (architecture changes): 30–90 minutes

### Can I give Vikram tasks that span multiple repositories?

Yes. You can define tasks that coordinate changes across up to 8 repositories (e.g., update an API in the backend repo + update the client in the frontend repo). Vikram manages them as one atomic unit.

### What if I disagree with Vikram's approach?

When Vikram asks for approval, you can:
- **Approve** — merge as-is
- **Reject** — discard the work entirely
- **Edit and approve** — tell it what to change, it implements your feedback
- **Clarify** — ask a question before deciding

---

## Technical Details

### What's the recommended hardware?

Vikram is lightweight. The Go binary uses ~50MB RAM. The Python orchestrator uses ~200MB. Any modern laptop or a $5/month VPS is fine. The heavy lifting is done by the cloud LLM providers, not your machine.

### Can I run Vikram on a server 24/7?

Yes. Run `vikram gateway` as a systemd service (Linux) or launchd daemon (macOS). It handles graceful shutdown, crash recovery, and auto-resumes interrupted tasks on restart.

### Is Vikram an alternative to GitHub Copilot?

No. Copilot autocompletes code as you type. Vikram is a different category — it takes entire tasks, plans them, implements them, tests them, and delivers complete branches. You don't use them together; you use Vikram instead of writing the code yourself.

### How is Vikram different from Devin, Cursor, or Windsurf?

- **Devin**: Cloud-hosted, closed source, sends your code to their servers. Vikram is self-hosted, open source, your code stays on your machine.
- **Cursor/Windsurf**: IDE-based copilots — you still write the code, they assist. Vikram works autonomously — you review the output, not write it.
- **Vikram**: Self-hosted autonomous engineering team with governance, cost controls, and formal verification. No vendor lock-in.
