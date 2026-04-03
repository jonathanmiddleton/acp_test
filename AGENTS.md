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
| `__main__.py` | CLI entry point, binary discovery, argument parsing | Business logic |

## Tests

- **Unit tests** (`test_transport.py`, `test_server.py`): Fake/mock objects, no real subprocess.
- **Integration tests** (`test_integration.py`): Real copilot-language-server. Skipped if binary not found.
- Run all: `python -m pytest tests/ -v`
- Run unit only: `python -m pytest tests/test_transport.py tests/test_server.py -v`

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
