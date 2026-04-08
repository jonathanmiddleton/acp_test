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

- [x] **Prompt-level timeout + structured errors** — `client.prompt()` enforces a 120s default deadline (`DEFAULT_PROMPT_TIMEOUT_S`). On expiry: cancels the prompt task, raises `PromptTimeout` with session ID, timeout, and partial text. Server returns HTTP 504 (non-streaming) or SSE error event (streaming). Session creation failures return HTTP 502. 8 new tests. *(Done 2026-04-08)*

- [x] **Default log level to DEBUG** — Console default changed to DEBUG during development phase. File logger was already DEBUG. Revert to INFO when proxy stabilizes. *(Done 2026-04-08)*

## Medium Priority

- [ ] **Probe `session/load` and `session/list`** — The ACP spec defines `session/load` (replay conversation history into a session by ID) and `session/list` (discover existing sessions). The copilot-language-server advertises `loadSession: true` but we've never exercised either method. Write a probe script or test to determine: (a) does `session/list` return sessions we've created? (b) does `session/load` successfully reconnect to a prior session and replay history? (c) can we `session/load` after a timeout/failure? Results inform the session health checking design. See [ACP session setup spec](https://agentclientprotocol.com/protocol/session-setup.md).

- [ ] **Session cleanup strategy** — Old ACP sessions accumulate. The ACP spec has no stable `session/close` method (it's an [RFD](https://agentclientprotocol.com/rfds/session-close.md)). The ACP server may have internal TTL (it uses SQLite). Test: does the server clean up idle sessions? If not, we need to track session age and periodically restart the ACP process, or accept the leak.

- [ ] **Synthetic tools (future)** — The ACP server owns tool execution. If we need custom tools (beyond what the ACP server provides), options are: (a) MCP servers via `session/new` (spec supports it, firm policy currently blocks MCP in Copilot plugin — revisit when policy evolves), (b) Synthetic tool patterns where the model emits a structured command in its text response and the proxy intercepts it. Brittle but possible.

- [ ] **Test on target environment** — Pull latest, run `npm install`, start proxy with `--system-prompt`, verify behavior matches dev.

- [ ] **Stress test for failure case collection** — Design and run a stress test that deliberately provokes failure modes: concurrent requests to the same session, rapid session creation/teardown, long-running prompts, idle sessions left open for extended periods, large payloads. Catalog what actually breaks, how it fails (timeout, error response, hang, crash), and whether failures are transient or permanent. Results feed into the session health checking design.

## Low Priority

- [ ] **Session health checking and retry strategy** — Confirmed real failure mode: the copilot-language-server stops responding and the proxy hangs indefinitely. Important but not actionable until we have: (1) concrete failure cases from stress testing and production use, (2) `session/load` and `session/list` probe results, (3) clarity on which integration path is active (OpenCode in the middle vs direct consumer↔proxy). The retry design differs significantly between paths — OpenCode replays full history (fresh session is less costly) while direct communication loses all ACP-side context on session replacement. Potential recovery chain: timeout → `session/list` (session exists?) → `session/load` (reconnect with history replay) → `session/new` (fresh start, accept context loss).

- [ ] **AGENTS.md context injection** — The ACP server scans the workspace from `cwd` and knows file names, but does NOT read file contents without using a tool. If we want AGENTS.md content available to the model without a tool call, inject it (or a summary) in the system prompt.

## Rejected / Deferred

- ~~Investigate disabling LSP tool injection via ACP initialization capabilities~~ — Rejected. The ACP server owns tools by design. We shape behavior via system prompt injection instead.

- ~~Native Copilot OAuth path~~ — Not viable in target environment (policy).

- ~~Forward OpenAI tool definitions through ACP~~ — ACP `session/prompt` has no `tools` parameter. Tools are agent-owned, not client-provided. Confirmed by spec and two independent open-source implementations.

- ~~Session pooling for creation overhead~~ — Deferred. ~2s per session creation is fixed protocol overhead. Pooling is not feasible given the stateful session model.

## Recently Completed (2026-04-08)

- [x] **ACP spec reference documented** — Official spec at https://agentclientprotocol.com. Full index at `llms.txt`. Added comprehensive reference tables to AGENTS.md (stable spec pages + RFDs) and shorter reference to README.md.

- [x] **Prompt-level timeout + structured errors** — `PromptTimeout` exception in `client.py`, 120s default deadline, structured OpenAI-format error responses at the HTTP layer. 8 new tests (166 total, was 158).

- [x] **Default log level to DEBUG** — Console logging now defaults to DEBUG during development.

- [x] **TODO.md restructured** — Removed session pooling (deferred), added stress test item, added `session/load` probe, rewrote session health checking with spec findings. Priority vs importance distinction applied.

### Key spec discovery: `session/load` and `session/list`

The ACP spec defines methods we haven't exercised that are directly relevant to session recovery:

- **`session/load`** (stable) — reconnect to an existing session by ID. The server replays the entire conversation history via `session/update` notifications. The copilot-language-server advertises `loadSession: true`.
- **`session/list`** (stable) — discover all known sessions, optionally filtered by `cwd`. Returns session IDs, titles, timestamps.
- **`session/close`** (RFD, not stable) — explicit session cleanup. Not yet in the spec.
- **`session/resume`** (RFD) — like load but without history replay. The RFD explicitly mentions the proxy use case.

These change the recovery strategy: instead of "session died → create new (lose context)", we may be able to do "session timed out → `session/list` (exists?) → `session/load` (reconnect) → `session/new` (fallback)". Needs empirical validation — the `session/load` probe item above.

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
- **ACP spec reference:** https://agentclientprotocol.com — full index at https://agentclientprotocol.com/llms.txt
- **Other ACP-to-OpenAI proxies exist** — [iot2020/rest-acp](https://github.com/iot2020/rest-acp) (TypeScript, uses `npx @github/copilot --acp --yolo`), [OpenSource03/harnss](https://github.com/OpenSource03/harnss) (desktop client for multiple ACP agents). Worth monitoring for ideas but neither has our session management or system prompt injection.
