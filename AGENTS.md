## User

The user's name is Jonathan.

## Coding Standards

**Read [CODING_STANDARDS.md](CODING_STANDARDS.md) before making any code changes.** It
contains the project's binding standards covering failure handling, error
surfacing, resilience policy, and testing philosophy.

## Project Overview

This repo is an ACP-to-OpenAI proxy that bridges OpenCode to GitHub Copilot's
`copilot-language-server` via the Agent Client Protocol. It is architecturally
separate from Meadow.

```
OpenCode  →  ACP Proxy (localhost)  →  copilot-language-server  →  Copilot backend
```

## Module Architecture

| Module | Owns | Does NOT own |
|---|---|---|
| `transport.py` | NDJSON/stdio framing, subprocess lifecycle, JSON-RPC message correlation, bidirectional dispatch | Protocol semantics, session logic |
| `client.py` | ACP initialization, session lifecycle, model selection, prompt execution, agent callback handling (permissions, fs, terminal) | HTTP serving, OpenAI format translation |
| `server.py` | FastAPI app, OpenAI-compatible endpoints, request/response translation, SSE streaming | ACP protocol details, subprocess management |
| `discovery.py` | Binary resolution (single source of truth). Only accepts IntelliJ IDEA 2025.3 Copilot plugin binary. Validates `ps` matches against the known-compatible path. | Protocol, sessions, serving |
| `__main__.py` | CLI entry point, argument parsing, wiring | Binary discovery logic, business logic |

## Tests

- **Unit tests** (`test_transport.py`, `test_client.py`, `test_server.py`, `test_discovery.py`): Fake/mock objects, no real subprocess.
- **Integration tests** (`test_integration.py`): Real copilot-language-server. **Fails** (not skips) if binary not found — a missing binary means the environment is misconfigured.
- **No skips.** Tests must never use `skipif` or `pytest.skip()`. See CODING_STANDARDS.md.
- Run all: `python -m pytest tests/ -v`
- Run unit only: `python -m pytest tests/test_transport.py tests/test_client.py tests/test_server.py tests/test_discovery.py -v`

## Architectural Decisions

**Read the relevant ADRs before making any architectural or design change.**
They document the binding decisions and the empirical evidence behind them —
particularly the failure modes that motivated each decision.

| ADR | Decision |
|---|---|
| [ADR-001](adrs/001-acp-proxy-architecture.md) | Route OpenCode through ACP proxy (why this architecture, what was rejected) |
| [ADR-002](adrs/002-session-per-conversation.md) | Session-per-conversation via first-message hash (why sessions are keyed this way) |
| [ADR-003](adrs/003-system-prompt-injection.md) | System prompt injection as primary control surface (why and how it works) |
| [ADR-004](adrs/004-last-user-message-extraction.md) | Extract only the last user message (why full history replay causes duplication) |
| [ADR-005](adrs/005-fail-loud-testing.md) | Fail-loud testing — no skips (why skips are banned, what they masked) |
| [ADR-006](adrs/006-binary-discovery.md) | Binary discovery — IntelliJ IDEA 2025.3 only (why version specificity, what wrong-binary failure looked like) |
| [ADR-007](adrs/007-tool-ownership.md) | The ACP server owns tools — do not inject or override (protocol constraint, empirical evidence) |
| [ADR-008](adrs/008-proxy-as-substrate.md) | Proxy as substrate — installable command, cwd as workspace |

The ADRs explain the *why* behind the module ownership rules in the table
above. A change that contradicts an accepted ADR requires a new ADR
superseding it, not a silent deviation.

## Journal

**Read [docs/journal.md](docs/journal.md) at the start of every session.** It is the
unfiltered working record of observations, environment differences, and design
decisions accumulated across sessions. It is gitignored — each environment
maintains its own copy.

- If it exists, read it before doing anything else. It contains context that
  is not captured anywhere else (target environment behavior, protocol quirks,
  failure modes observed in practice).
- If it does not exist, create it with a header and start recording.
- Update it throughout the session with observations, discoveries, and decisions.
  Write entries as they happen, not as a batch at the end.
- Entries should be dated and factual. Include what was tried, what happened,
  and what it means. Avoid speculation without evidence.

## Configuration

**`opencode.json`** (repo root) configures OpenCode to use the proxy as its
Copilot provider. It points OpenCode at `http://127.0.0.1:8765/v1` with no
auth, and declares the model IDs the proxy must handle: `gpt-4.1`, `gpt-4o`,
`claude-sonnet-4`, `gemini-2.5-pro`, `auto`. When adding model routing logic,
this file defines what model IDs are valid — they must match what the proxy
advertises on `GET /v1/models`.

## Diagnostic Scripts

Two standalone scripts in `src/` exist for protocol-level debugging — they
are not part of the proxy package and should not be imported by production
code:

- **`acp_probe.py`** — Keeps a language-server subprocess alive and sends a
  sequence of raw JSON-RPC messages. Use to explore the ACP wire protocol
  directly.
- **`acp_validate.py`** — Runs the full init → session → prompt lifecycle and
  prints structured results per step. Use to validate a binary is responsive
  before debugging the proxy layer.

## Git Conventions

- Commit messages describe the "why" not the "what".
- No user IDs or environment-specific paths in committed code.
- `docs/journal.md` is gitignored — unfiltered local working record.

## Target Environment Constraints

- The target is a restricted enterprise environment. The copilot-language-server
  binary bundled with the JetBrains Copilot plugin is the only sanctioned path
  to Copilot.
- Binary path varies by user. Auto-discovery via `ps` or JetBrains plugin
  directory search. Never hardcode user-specific paths.
- OpenCode is the stock prebuilt binary installed via npm. No source builds,
  no custom forks.
- Available models and modes vary between environments. The proxy must handle
  whatever the server advertises but must not silently degrade required
  capabilities.
