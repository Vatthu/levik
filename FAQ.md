# FAQ

**Q: What is Vikram?**
Your AI coding team. You say what to build, it builds it. You review, you merge.

**Q: Is it free?**
Vikram is free. You pay your AI provider (OpenAI, Anthropic, etc.) for API calls. A typical task costs $0.50–$5.

**Q: How many API keys do I need?**
Minimum 1. That's it. One OpenAI key or one Anthropic key and you're running.

**Q: What's the recommended setup?**
2–3 keys from different providers. One for coding (Claude or DeepSeek), one for review (GPT-4o), one as backup. Vikram auto-switches if one goes down.

**Q: Can all roles use the same key?**
Yes. One key works for everything. Multiple keys give you independent review and fallback.

**Q: Does it work with free/local models?**
Yes. Ollama, LM Studio, anything with an OpenAI-compatible endpoint. Results vary with model quality though.

**Q: What languages does it handle?**
All of them. If your repo has it, Vikram works on it. Go, Python, JS, TS, Rust, Java, whatever.

**Q: Will it break my code?**
No. It works on a separate git branch. Your main branch is untouched until you click merge.

**Q: Does it send my code somewhere?**
Only to the AI providers you configure. Same as Copilot or Cursor. Vikram itself collects zero data.

**Q: Can I set a spending limit?**
Yes. Per task ("max $3"), per day ("max $20/day"), with warnings at 80%. It stops and asks before going over.

**Q: How long does a task take?**
Simple stuff: 2–5 min. Normal features: 10–20 min. Big refactors: 30–60 min.

**Q: Do I need Telegram?**
No. CLI, web console, and REST API all work. Telegram is just convenient for approvals from your phone.

**Q: Can it do multiple tasks at once?**
Yes. Default is 3 parallel tasks. Configurable up to 10.

**Q: What hardware do I need?**
Any laptop or a $5 VPS. Vikram uses ~250MB RAM. The AI providers do the heavy compute.

**Q: How is this different from Copilot?**
Copilot helps you write code. Vikram writes it for you. Different category entirely.

**Q: How is this different from Devin?**
Devin is cloud-hosted, closed source, and expensive. Vikram runs on your machine, is open source, and you only pay API costs.

**Q: Can I run it 24/7 on a server?**
Yes. `vikram gateway` as a systemd service. It handles crashes, restarts, and resumes interrupted tasks.

**Q: What if I don't like what it built?**
Reject it. One click. Nothing changes. Or say "change X" and it revises.

**Q: Does it auto-merge without asking?**
Only if you configure it to. By default, it asks for your approval. Over time, as it proves reliable, you can let it auto-merge low-risk changes.

**Q: Can it work across multiple repos?**
Yes. Up to 8 repos in one task. It coordinates changes so they're consistent.

**Q: What's the catch?**
It's as good as the AI models behind it. Complex architecture decisions still need a human. Use it for implementation work, not for deciding what to build.
