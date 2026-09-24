# Agent development guidelines

Read README.md and docs/{architecture,migration,contracts,TODO,deployment}.md first.
Historical documents in docs/source-snapshot are evidence, not the current implementation plan.

- Python owns reasoning and tools orchestration. Keep EdgeOne-specific APIs in adapters/edgeone.
- Never import Xiaomi protocol/session-decryption code into Python. Never share XIAOMI_SESSION_SECRET with it.
- Model messages must not contain session bindings, internal secrets, principal/home IDs, real scene IDs, or DIDs.
- principal/home/scopes come only from authenticated server context, never model arguments.
- All model access uses the configured Makers AI Gateway. No user-supplied key or direct-provider fallback.
- Preserve server-side explicit-action checks, strict tool schemas, no-store responses and sanitized errors.
- A timeout after a write is an unknown physical outcome. Never retry automatically.
- EdgeOne KV is a soft quota store, not a distributed lock. In-memory locks are only a local optimization.
- Do not enable remote device execution until the durable executor claim task in docs/TODO.md is complete.
- Preview must not invoke a model or control physical devices.
- Reminders and learned habits are future work. Habits suggest; they do not silently execute.
- Keep `.env*`, real account data, logs, cookies, tokens and generated build directories out of git.
- Update requirements locks with dependency changes; retain fake-only test credentials.
- Validate Python with pytest and ruff; validate the Makers adapter with its Node tests.
- For companion changes, follow the web console's own AGENTS.md and quality gates.
- When changing `docs/mijia-general-assistant-design.md`, update `docs/mijia-general-assistant-design.zh-CN.md` in the same change, and vice versa; keep both versions aligned.
- Do not mark mocked integration tests as live EdgeOne, Gateway, KV, or Xiaomi validation.

Do not create commits, push, or deploy unless the user's request authorizes that action.
