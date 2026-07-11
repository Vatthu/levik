# FAQ

**Q: What is Vikram?**
Your AI coding team. You say what to build, it builds it. You review, you merge.

**Q: Is it free?**
Vikram is free and open source. You pay your AI provider (OpenAI, Anthropic, etc.) for the API calls it makes. A typical task costs $0.50–$5.

**Q: Who is this for?**
Developers who can build and test their own projects but want to multiply output. You should be comfortable with terminal, git, and your project's toolchain. Vikram does the coding work — you still steer the direction.

**Q: What do I need to get started?**
- A Mac, Linux machine, or VPS
- At least one LLM API key (OpenAI, Anthropic, DeepSeek, etc.)
- A code repository you want Vikram to work on
- Your project's toolchain already installed (Node, Python, Go, Rust — whatever your project needs to build and test)

That last point matters: if your project uses `npm test`, you need Node installed. If it uses `go build`, you need Go. Vikram runs your project's commands on your machine — it doesn't bundle runtimes.

**Q: How many API keys do I need?**
Minimum 1. One OpenAI or Anthropic key and you're running.

Recommended: 2–3 from different providers. One for coding (Claude or DeepSeek), one for independent review (GPT-4o), one as a backup. Vikram auto-switches if one goes down.

**Q: Can all roles use the same key?**
Yes. One key works for everything. Multiple keys give you independent review (a different "brain" checking the code) and automatic fallback.

**Q: Can I use local models (Ollama, LM Studio)?**
Technically yes, but be realistic: autonomous coding tasks require strong reasoning and tool-calling ability. Small local models (7B–13B) will fail at planning and implementation. You need at minimum a 30B+ parameter model with good tool-use support, which means serious hardware (Apple M-series with 32GB+ RAM, or a dedicated GPU).

**Start with cloud APIs.** They're cheap ($0.50–$5 per task) and reliably capable. Try local models once you understand what Vikram expects from a model.

**Q: What languages does it handle?**
All of them. If your repo has it, Vikram works on it. Go, Python, JS, TS, Rust, Java, Ruby, whatever.

**Q: Will it break my code?**
No. It works on a separate copy of your project (a temporary folder branched from your repo). Your main branch is never touched until you explicitly approve and merge. If anything is wrong, the temporary copy is deleted — nothing reaches your real code.

**Q: Where does this "separate copy" live on my disk?**
Under `~/.vikram/worktrees/`. Each task gets its own folder there. Once a task completes or is cancelled, the folder is cleaned up. You can look at it at any time to see what Vikram is working on.

**Q: Does it send my code somewhere?**
Only to the AI providers you configure (OpenAI, Anthropic, etc.) — same as any AI coding tool. Vikram itself collects zero data, has zero telemetry, and makes zero network calls except to your configured providers.

**Q: Is it sandboxed? What about dangerous commands?**
Let's be precise: Vikram runs commands directly on your machine (not in Docker or a VM). It has a command blocklist that prevents known-dangerous operations (rm -rf /, disk formatting, etc.) and restricts file access to your project directory. But it's not a container — if a model somehow crafts a novel dangerous command not on the blocklist, it would execute on your host.

For most users this is fine (it's your machine, your project, your toolchain). If you need full isolation, run Vikram on a dedicated VPS or inside a Docker container yourself.

**Q: Can I set a spending limit?**
Yes. Three levels:
- Per task: "max $3 for this task" — stops and asks if reached
- Per day: "max $20/day total" — pauses all tasks if reached
- Warning at 80% — you get notified before hitting any limit

**Q: What if a task gets stuck?**
Vikram has automatic circuit breakers:
- Tasks timeout after 2 hours by default (configurable)
- If an agent loops on the same error 5+ times, it halts automatically
- Budget limits stop runaway spending
- You can kill any task manually from the console or CLI at any time

**Q: How long does a task take?**
Simple stuff (docs, config): 2–5 min. Normal features: 10–20 min. Big refactors: 30–60 min.

**Q: Do I need Telegram?**
No. CLI, web console, and REST API all work without it. Telegram is just convenient for approving tasks from your phone while you're away from your desk.

**Q: Can it do multiple tasks at once?**
Yes. Default is 3 parallel tasks. Configurable up to 10.

**Q: What hardware do I need?**
Any laptop or a $5/month VPS. Vikram uses about 250MB RAM total. The heavy compute happens at the AI provider's servers, not yours.

**Q: Can I run it 24/7 on a server?**
Yes. `vikram gateway` runs as a background service. It handles crashes, restarts, and resumes interrupted tasks automatically.

**Q: How do I control what gets auto-approved?**
Add a file called `.vikram/approval-matrix.yaml` to your repo. It's a simple rules file:

```yaml
rules:
  - name: docs-auto-approve
    conditions:
      file_patterns: ["**/*.md", "docs/**"]
    routing: auto_approve
  - name: everything-else
    conditions: {}
    routing: founder_review  # requires your approval
```

This says: auto-approve documentation changes, ask me for everything else. You can make it as detailed as you want — by file type, risk level, number of files changed, etc. Running `vikram onboard` generates a sensible default for you.

**Q: How is this different from Copilot?**
Copilot helps you write code line by line. Vikram writes entire features while you do something else. Different category.

**Q: How is this different from Devin?**
Devin is cloud-hosted, closed source, and sends your code to their servers. Vikram runs on your machine, is open source, and you only pay API costs directly to providers.

**Q: What's the catch?**
It's as good as the AI models you give it. For complex architecture decisions, you still need to think. For implementation — writing the actual code, tests, and plumbing — that's where Vikram saves you hours every day.

**Q: Can it work across multiple repos?**
Yes. You can define tasks that span multiple repositories — for example, updating an API in your backend while adjusting the client in your frontend. Vikram coordinates the changes as one atomic unit.

**Q: What if I disagree with what it built?**
Reject it (one click, nothing changes), or say "change X instead" and it revises. You're always in control.
