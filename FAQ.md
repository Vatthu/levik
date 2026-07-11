<div align="center">

# ❓ Frequently Asked Questions

Everything you need to know before using Vikram.

</div>

---

## 🚀 The Basics

<details>
<summary><strong>What is Vikram?</strong></summary>

> Your AI coding team. You say what to build, it builds it. You review, you merge.

</details>

<details>
<summary><strong>Is it free?</strong></summary>

> Vikram is free and open source. You pay your AI provider (OpenAI, Anthropic, etc.) for the API calls it makes. A typical task costs **$0.50–$5**.

</details>

<details>
<summary><strong>Who is this for?</strong></summary>

> Developers who can build and test their own projects but want to multiply output. You should be comfortable with terminal, git, and your project's toolchain. Vikram does the coding — you steer.

</details>

---

## 🔑 API Keys & Models

<details>
<summary><strong>How many API keys do I need?</strong></summary>

> **Minimum: 1.** One OpenAI or Anthropic key and you're running.
>
> **Recommended: 2–3** from different providers. One for coding (Claude or DeepSeek), one for independent review (GPT-4o), one as backup. Vikram auto-switches if one goes down.

</details>

<details>
<summary><strong>Can all roles use the same key?</strong></summary>

> Yes. One key works for everything. Multiple keys give you independent review (a different "brain" checking the code) and automatic fallback.

</details>

<details>
<summary><strong>Can I use local models (Ollama, LM Studio)?</strong></summary>

> Technically yes, but be realistic:
>
> - Small models (7B–13B) **will fail** at autonomous coding tasks
> - You need **30B+ parameters** with good tool-use support
> - That means serious hardware (Apple M-series 32GB+ RAM, or dedicated GPU)
>
> **👉 Start with cloud APIs.** They're cheap ($0.50–$5/task) and reliably capable. Try local models once you understand what Vikram expects.

</details>

<details>
<summary><strong>What providers are supported?</strong></summary>

> OpenAI, Anthropic (Claude), Google Gemini, DeepSeek, Mistral, OpenRouter, Groq, Ollama, NVIDIA, Azure OpenAI, AWS Bedrock, and 10+ more. Anything with an OpenAI-compatible API works.

</details>

---

## ⚙️ Setup & Requirements

<details>
<summary><strong>What do I need to get started?</strong></summary>

> 1. A Mac, Linux machine, or VPS
> 2. At least one LLM API key
> 3. A code repository you want Vikram to work on
> 4. **Your project's toolchain already installed**
>
> ⚠️ That last point matters: if your project uses `npm test`, you need Node installed. If it uses `go build`, you need Go. Vikram runs your project's commands directly — it doesn't bundle runtimes.

</details>

<details>
<summary><strong>What languages does it handle?</strong></summary>

> All of them. Go, Python, JS, TS, Rust, Java, Ruby — if your repo has it, Vikram works on it.

</details>

<details>
<summary><strong>What hardware do I need?</strong></summary>

> Any laptop or a $5/month VPS. Vikram uses ~250MB RAM total. The heavy compute happens at your AI provider's servers.

</details>

<details>
<summary><strong>Do I need Telegram?</strong></summary>

> No. CLI, web console, and REST API all work. Telegram is just convenient for approvals from your phone.

</details>

---

## 🛡️ Safety & Security

<details>
<summary><strong>Will it break my code?</strong></summary>

> No. Vikram works on a **separate, temporary copy** of your project (branched from your repo). Your main branch is never touched until you explicitly approve and merge. If anything is wrong, the temporary copy is deleted.

</details>

<details>
<summary><strong>Where does this "temporary copy" live?</strong></summary>

> Under `~/.vikram/worktrees/`. Each task gets its own folder. Once a task completes or is cancelled, the folder is cleaned up. You can inspect it at any time.

</details>

<details>
<summary><strong>Does it send my code somewhere?</strong></summary>

> Only to the AI providers you configure (OpenAI, Anthropic, etc.) — same as any AI coding tool. Vikram itself has **zero telemetry**, makes **zero network calls** except to your configured providers.

</details>

<details>
<summary><strong>Is it sandboxed?</strong></summary>

> Let's be precise: Vikram runs commands **directly on your machine** (not in Docker or a VM). It uses a command blocklist that prevents known-dangerous operations and restricts file access to your project directory.
>
> It is **not** a container. If you need full isolation, run Vikram on a dedicated VPS or inside Docker yourself.

</details>

<details>
<summary><strong>Can I set spending limits?</strong></summary>

> Yes. Three levels:
>
> | Level | What it does |
> |-------|-------------|
> | Per task | "max $3 for this task" — stops and asks if reached |
> | Per day | "max $20/day total" — pauses all tasks if reached |
> | Warning | Notifies you at 80% of any limit |

</details>

---

## 🔄 How It Works

<details>
<summary><strong>What happens when I give it a task?</strong></summary>

> 1. **Plans** the approach (analyzes your codebase)
> 2. **Reviews the plan** adversarially (a different AI pokes holes)
> 3. **Implements** the code on an isolated branch
> 4. **Verifies** — runs linting, tests, and property checks
> 5. **Reviews the code** — independent model evaluates changes
> 6. **Asks you** — approves automatically or requests your sign-off
> 7. **Ready to merge** — you click merge when satisfied

</details>

<details>
<summary><strong>How long does a task take?</strong></summary>

> | Complexity | Time |
> |-----------|------|
> | Simple (docs, config) | 2–5 min |
> | Normal features | 10–20 min |
> | Big refactors | 30–60 min |

</details>

<details>
<summary><strong>What if a task gets stuck?</strong></summary>

> Vikram has automatic protection:
> - Tasks timeout after **2 hours** (configurable)
> - Loops on the same error **5+ times** → auto-halt
> - Budget limits stop runaway spending
> - You can **kill any task manually** from console or CLI

</details>

<details>
<summary><strong>Can it do multiple tasks at once?</strong></summary>

> Yes. Default: 3 parallel tasks. Configurable up to 10.

</details>

<details>
<summary><strong>Can it work across multiple repos?</strong></summary>

> Yes. Define tasks that span multiple repositories — e.g., API changes in backend + client updates in frontend. Vikram coordinates them as one atomic unit.

</details>

---

## 📋 Daily Usage

<details>
<summary><strong>How do I control what gets auto-approved?</strong></summary>

> Add `.vikram/approval-matrix.yaml` to your repo:
>
> ```yaml
> rules:
>   - name: docs-auto-approve
>     conditions:
>       file_patterns: ["**/*.md", "docs/**"]
>     routing: auto_approve
>
>   - name: everything-else
>     conditions: {}
>     routing: founder_review  # requires your approval
> ```
>
> `vikram onboard` generates a sensible default for you.

</details>

<details>
<summary><strong>What if I disagree with what it built?</strong></summary>

> - **Reject** — one click, nothing changes
> - **"Change X instead"** — it revises based on your feedback
> - **Clarify** — ask a question before deciding
>
> You're always in control.

</details>

<details>
<summary><strong>Can I run it 24/7 on a server?</strong></summary>

> Yes. `vikram gateway` runs as a background service. Handles crashes, restarts, and resumes interrupted tasks automatically.

</details>

---

## 🆚 Comparisons

<details>
<summary><strong>How is this different from GitHub Copilot?</strong></summary>

> Copilot helps you write code **line by line**. Vikram writes **entire features** while you do something else. Different category.

</details>

<details>
<summary><strong>How is this different from Devin?</strong></summary>

> Devin is cloud-hosted, closed source, and sends your code to their servers. Vikram runs **on your machine**, is **open source**, and you pay API costs directly.

</details>

<details>
<summary><strong>What's the catch?</strong></summary>

> It's as good as the AI models you give it. Complex architecture decisions still need a human. For implementation work — writing code, tests, and plumbing — that's where Vikram saves you hours.

</details>
