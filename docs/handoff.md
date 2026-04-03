# Meadow ↔ GitHub Copilot ACP Integration — Context Handoff

## Objective

Wire up **Meadow** (Jonathan's multi-agent software engineering orchestrator) to use **GitHub Copilot instances as agents** via the **Agent Client Protocol (ACP)**, communicating directly with the `copilot-language-server` binary. This is targeting a restricted environment where the Copilot JetBrains plugin is the only authorized path to Copilot — no Copilot CLI is available, and direct API access is blocked.

## What We've Established So Far

### The copilot-language-server binary supports ACP

- The JetBrains Copilot plugin bundles a `copilot-language-server` binary that runs as a subprocess with `--stdio`.
- Jonathan confirmed it's running on the target machine — one instance per open IDE. Identified as version **1.442.0** from `github/copilot-language-server-release`.
- The binary also accepts `--acp` mode, which switches from LSP wire format (`Content-Length` framing) to **ACP wire format (NDJSON over stdio)**.
- Jonathan launched `copilot-language-server --acp --stdio` and the process came up successfully.

### ACP is the right protocol for this use case

- ACP (Agent Client Protocol) is a JSON-RPC 2.0 protocol over NDJSON (newline-delimited JSON) on stdin/stdout.
- It supports: session creation, prompting, streaming responses (thought chunks, message chunks, tool calls), cancellation, and session lifecycle management.
- It was explicitly designed for multi-agent coordination — Meadow can act as an ACP client, launching and managing multiple copilot-language-server processes as agents.

### Authentication is the next unsolved step

- An OAuth token exists at `~/.config/github-copilot/oauth.json` on the target machine (written by the JetBrains Copilot plugin's auth flow).
- It is **unknown** whether the language server in ACP mode auto-discovers this token or requires explicit auth.
- The `initialize` response's `authenticationMethods` field will indicate what's needed.

## Where to Pick Up

The immediate next steps are hands-on terminal work:

### 1. Find the binary path

```bash
ps -eo pid,command | grep copilot-language-server
```

Note the full path to the binary.

### 2. Inspect the OAuth token structure

```bash
cat ~/.config/github-copilot/oauth.json
```

Understand the JSON shape — likely contains an `oauth_token` field, possibly keyed by hostname.

### 3. Test ACP initialization

Start the server and send the initialize handshake:

```bash
mkfifo /tmp/acp_in
cat /tmp/acp_in | /path/to/copilot-language-server --acp --stdio &

echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":1,"clientInfo":{"name":"meadow","version":"0.1.0"},"capabilities":{}}}' > /tmp/acp_in
```

**Key things to check in the response:**
- `authenticationMethods` — if non-empty, explicit auth is required before session creation.
- `agentCapabilities.sessions.new` — must be `true` for the agentic flow to work.
- Any error messages about auth or unsupported features.

### 4. If auth is not automatic, try environment variable

```bash
export GITHUB_TOKEN=$(python3 -c "import json; print(json.load(open('$HOME/.config/github-copilot/oauth.json')).get('oauth_token',''))")
```

Adjust the key path based on the actual structure of `oauth.json`. Then relaunch the server.

### 5. Create a session and send a test prompt

```bash
echo '{"jsonrpc":"2.0","id":2,"method":"session/new","params":{"workingDirectory":"/path/to/a/repo"}}' > /tmp/acp_in
```

If successful, capture the `sessionId` from the response, then:

```bash
echo '{"jsonrpc":"2.0","id":3,"method":"session/prompt","params":{"sessionId":"SESSION_ID","content":[{"type":"text","text":"What files are in this directory?"}]}}' > /tmp/acp_in
```

The server should stream back `session/update` NDJSON lines with agent thoughts, messages, and tool call results.

### 6. If ACP sessions are not supported on this binary

Fall back to checking whether the LSP mode exposes chat/agent methods beyond inline completion. The known LSP custom methods are:
- `textDocument/inlineCompletion` — code completions at cursor
- `textDocument/copilotPanelCompletion` — panel completions
- `textDocument/copilotInlineEdit` — next-edit suggestions

These are completion-oriented, not conversational/agentic. If ACP doesn't work, the options narrow to:
- Extracting the OAuth token and finding a way to call Copilot's backend directly.
- Building a JetBrains IDE plugin as a bridge (more complex — see architecture notes below).

## Architecture Context

### Meadow

Meadow is Jonathan's concurrent multi-agent software engineering system with seven LLM-backed roles (PM, BA, Reviewer, Architect, Researcher, Developer, Tester) using DAG-enforced message passing and cross-run memory via FAISS and SQLite. In this integration, Meadow acts as the **orchestrator/client** and Copilot instances act as **worker agents**.

### Why not the JetBrains plugin bridge?

We explored building a JetBrains IDE plugin to bridge Meadow to Copilot. This would require:
- Declaring a `<depends>` on the Copilot plugin for classloader visibility
- Reflecting into Copilot's internal services (undocumented, unstable)
- Exposing a local HTTP API for Meadow to call

The ACP path via the language server binary is dramatically simpler and uses a documented protocol. The plugin bridge is the fallback if ACP doesn't pan out.

## Key References

- **copilot-language-server-release**: https://github.com/github/copilot-language-server-release
- **ACP specification**: https://agentclientprotocol.com/protocol/overview
- **ACP TypeScript SDK (reference for message shapes)**: https://agentclientprotocol.github.io/typescript-sdk/classes/ClientSideConnection.html
- **Copilot CLI ACP docs**: https://docs.github.com/en/copilot/reference/acp-server
- **ACP GitHub repo**: https://github.com/agentclientprotocol/agent-client-protocol

## ACP Protocol Quick Reference

**Transport:** NDJSON over stdio — each message is one line of JSON, no Content-Length headers.

**Message flow:**
1. `initialize` → response with capabilities and auth requirements
2. (optional) `authenticate` if `authenticationMethods` was non-empty
3. `session/new` → response with `sessionId`
4. `session/prompt` → streams `session/update` notifications back
5. `session/cancel` → stops ongoing operations

**Session update types (streamed back from agent):**
- `agent_message_chunk` — the agent's response text
- `agent_thought_chunk` — internal reasoning
- `tool_call` / `tool_call_update` — tool execution
- `plan` — agent's execution plan

**Permission requests:** The agent may send `requestPermission` for file writes, terminal commands, etc. The client must respond with `approved` or `cancelled`.