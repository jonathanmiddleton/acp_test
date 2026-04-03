# ACP Proxy — Open Work Items

## High Priority

- [ ] **Analyze OpenCode REST API ↔ ACP protocol alignment** — Meadow talks to OpenCode via its own REST API (`POST /session`, `POST /session/{id}/message` with agent, model, system prompt, structured output schema). This is NOT OpenAI-compatible. Evaluate whether the proxy should expose the OpenCode API on its inbound side and translate to ACP, which would let Meadow connect directly to the proxy as a drop-in OpenCode replacement in the constrained environment. Key mappings to analyze:
  - OpenCode `POST /session` → ACP `session/new` (both create sessions)
  - OpenCode `model.providerID/modelID` → ACP `session/set_model` (both select models)
  - OpenCode `system` field → ACP first-turn injection (system prompt)
  - OpenCode `format.schema` → structured output (does ACP support this?)
  - OpenCode `agent` field → no ACP equivalent (the ACP server IS the agent)
  - OpenCode streaming events → ACP `session/update` notifications
  - OpenCode `parts` → ACP `ContentBlock[]`

- [ ] **End-to-end test with OpenCode** — Test the refactored proxy (last-user-message extraction, per-conversation sessions, system prompt injection) with OpenCode connected. Verify: no context duplication, no session leakage, no "Operation cancelled by user" errors, title generator isolated.

- [ ] **Design system prompt for Meadow integration** — What instructions to inject per session. Must cover: agent mode (build vs plan), workspace context (how to reference AGENTS.md content), tool behavior shaping, coding standards. The system prompt is the primary control surface now that we own the first turn.

- [ ] **Mode control via system prompt** — OpenCode communicates mode (build/plan) via `<system-reminder>` tags in message content. Since we strip everything except the last user message, these never reach the ACP session. Mode must be set via the injected system prompt. Open question: do we need dynamic mode switching mid-session, or is one mode per session sufficient?

## Medium Priority

- [ ] **Session cleanup strategy** — Old ACP sessions accumulate. The ACP spec has no `session/close` or `session/delete` method. The ACP server may have internal TTL (it uses SQLite). Test: does the server clean up idle sessions? If not, we need to track session age and periodically restart the ACP process, or accept the leak.

- [ ] **Session creation overhead** — Each new conversation creates a new ACP session (~2s). Acceptable for interactive use but may be a bottleneck for Meadow's multi-agent orchestration. Measure and consider session pooling if needed.

- [ ] **Synthetic tools (future)** — The ACP server owns tool execution. If we need custom tools (beyond what the ACP server provides), options are: (a) MCP servers via `session/new` (spec supports it, firm policy currently blocks MCP in Copilot plugin — revisit when policy evolves), (b) Synthetic tool patterns where the model emits a structured command in its text response and the proxy intercepts it. Brittle but possible.

- [ ] **Test on target environment** — Pull latest, run `npm install`, start proxy with `--system-prompt`, verify behavior matches dev.

## Low Priority

- [ ] **Session health checking** — Detect when ACP server becomes unresponsive and recover. Less critical now that sessions are per-conversation (a stuck session only affects one conversation, not all).

- [ ] **AGENTS.md context injection** — The ACP server scans the workspace from `cwd` and knows file names, but does NOT read file contents without using a tool. If we want AGENTS.md content available to the model without a tool call, inject it (or a summary) in the system prompt.

## Rejected / Deferred

- ~~Investigate disabling LSP tool injection via ACP initialization capabilities~~ — Rejected. The ACP server owns tools by design. We shape behavior via system prompt injection instead.

- ~~Native Copilot OAuth path~~ — Not viable in target environment (policy).

- ~~Forward OpenAI tool definitions through ACP~~ — ACP `session/prompt` has no `tools` parameter. Tools are agent-owned, not client-provided. Confirmed by spec and two independent open-source implementations.

## Notes

- **ACP sessions are stateful** — context accumulates across turns. We send only the last user message to avoid duplication.
- **Sessions keyed by `(model, hash(first_user_message))`** — stable across turns because OpenCode replays full history.
- **System prompt injection confirmed effective** — first-turn instructions override ACP server's default tool reporting.
- **ACP server has built-in GitHub MCP server** — provides github-mcp-server-* tools by default.
- **`cwd` anchors the workspace** — part of the ACP spec, used by the server for file system boundary.
- **Meadow uses OpenCode's REST API, not OpenAI-compatible** — `POST /session/{id}/message` with first-class `system`, `agent`, `model`, `format` (structured output schema), `parts`. This is richer than `/v1/chat/completions`.
- **Current flow (OpenCode in the middle) is awkward but functional** — OpenCode's tools are stripped, system prompt is stripped, only the last user message reaches ACP. The value of OpenCode in this path is minimal (UI shell only). Keep until the OpenCode API → ACP proxy analysis is complete.
- **Two integration paths for Meadow:**
  1. Normal environments: Meadow → OpenCode → native provider (existing, works)
  2. Constrained environment: Meadow → proxy → ACP server (needs OpenCode API on inbound side OR Meadow learns OpenAI-compatible)
