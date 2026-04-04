# ADR-002: Session-per-Conversation via First-Message Hash

**Status:** Accepted  
**Date:** 2026-04-03  

## Context

ACP sessions are stateful — the copilot-language-server maintains full
conversation history within a session. Each `session/prompt` adds a turn;
context accumulates. New sessions are isolated.

OpenCode follows the standard OpenAI pattern: it sends the **full message
history** with every request (messages 1,2,3... then 1,2,3,4...). It provides
no session identifier, thread ID, or conversation key in the request body.
This was confirmed by logging all extra fields in incoming HTTP requests —
only standard OpenAI fields are present.

The proxy must map OpenCode's stateless full-replay requests to ACP's stateful
sessions. Two problems had to be solved:

1. **Which ACP session does a request belong to?** Multiple conversations can
   be active simultaneously (e.g., a coding conversation and a title
   generator).
2. **What content to send?** If the full replayed history is sent to a session
   that already has prior turns, the model sees everything twice.

### Empirical evidence

**Context duplication confirmed.** Turn 1: "My name is Alice." Turn 2: full
history replayed into the same session. Model responded: "you've told me your
name **twice**." Every request after the first doubled the context.

**Concurrent session collision confirmed.** OpenCode sends parallel requests
for the conversation and a title generator. When both hit the same ACP session,
the server cancelled one — producing "Operation cancelled by user" errors.

**Session state isolation confirmed.** BANANA7742 test: told a session a secret
code, retrieved it from the same session (success), failed to retrieve it from
a new session (correctly isolated).

## Decision

Sessions are keyed by `(model_id, sha256(first_user_message)[:16])`.

- OpenCode replays the full message history with every request, so the first
  user message is **stable** across all turns of a conversation.
- Different conversations have different first messages, so they get different
  sessions.
- The title generator sends a different first message (a summarization
  request), so it gets its own session — no collision with the main
  conversation.
- Model ID is included in the key because different models require separate
  ACP sessions (model is set at session creation via `session/set_model`).

Only the **last user message** is sent to the ACP session (see ADR-004),
avoiding context duplication.

## Rationale

- **No session identifier available.** OpenCode sends no thread/session ID.
  The first user message is the only stable conversation anchor in the
  request payload.
- **Hash is collision-resistant enough.** 16 hex chars (64 bits) from SHA-256
  is sufficient for the expected number of concurrent conversations (single
  digits).
- **Concurrent safety.** Title generator and conversation are naturally
  separated because their first messages differ. This eliminated the
  "Operation cancelled" errors without any special-case logic.

## Consequences

- **Session accumulation.** Old sessions are never explicitly closed (the ACP
  spec has no `session/close` method). They accumulate in the language server's
  internal state. The server may have internal TTL — this needs empirical
  testing. If not, periodic process restart is the cleanup strategy.
- **First-message sensitivity.** If two conversations happen to start with the
  same first user message, they'll share a session. In practice this is
  unlikely — OpenCode's first messages include context-specific content.
- **Session creation latency.** Each new conversation incurs ~2s for ACP
  session creation. This is a one-time cost per conversation, not per turn.
