# Coding Standards

Read this document before making any changes to the codebase. These standards
are binding and reflect the realities of building on partially documented,
externally controlled interfaces.

## Failure Philosophy: Surface Problems, Don't Absorb Them

This project operates at boundaries between documented protocols and actual
implementations that may diverge from those protocols. In this territory,
silent fallbacks are more dangerous than loud failures. A system that appears
to work but is quietly degraded is harder to fix than one that fails visibly.

### Maturity-Dependent Resilience

Not all code paths deserve the same failure treatment. The policy depends on
how well-understood the interface is:

| Interface maturity | Policy |
|---|---|
| **Unexplored / undocumented** | Fail loudly. No fallbacks. Surface the unexpected behavior immediately so it can be investigated and understood before building further. |
| **Explored but variable** | Fail with clear diagnostics. Retry only for known-transient causes (network, rate limits). Do not retry structural failures. |
| **Well-understood, known-fragile** | Resilient with bounded retries and logging. The failure mode is documented and the recovery path is verified. |

Code paths move through these tiers as understanding deepens. A fallback is
earned by understanding — not assumed by default.

### Never Mask Capability Failures

If a system requires a capability (model selection, file access, terminal
execution) and that capability silently fails, the system is broken — not
resilient. Silent degradation builds a foundation of false assumptions that
become expensive to unwind later.

When a required capability is unavailable:
- Raise or return an error. Do not substitute a default.
- Log what was attempted, what was expected, and what actually happened.
- Let the caller decide whether to proceed, not the callee.

### Distinguish Structural Failures from Transient Failures

Transient failures (network timeouts, rate limits, brief unavailability) are
retried with capped backoff. These are expected and the recovery path is
well-understood.

Structural failures (unexpected response shapes, missing capabilities,
protocol mismatches) are not retried. They indicate a broken assumption
that needs investigation, not repetition.

## Root Cause Resolution

When something fails:
1. Find the true root cause. Do not fix symptoms.
2. If the root cause is in an external system outside our control, document
   the finding and find the actual working path. Do not build a workaround
   that masks the gap.
3. If a workaround is genuinely necessary, it must be explicitly marked
   with a comment explaining what it works around and under what conditions
   it should be revisited.

## Error Handling

- **Fail fast on unexpected response shapes.** When extracting data from
  external responses, do not provide default values that allow silent
  continuation. If the expected structure is not present, raise with a
  message describing what was expected vs. what was received.

- **Log evidence before raising.** Before raising, log at DEBUG level the
  actual response shape so the developer has diagnostic information without
  needing to reproduce the failure.

- **No silent exception swallowing.** Every `except` block must either
  re-raise, log at WARNING or ERROR, or have an explicit comment explaining
  why the exception is expected and safe to ignore.

## Logging

- Use the `logging` module. No `print()` in source files under `src/`.
- Use lazy formatting: `logger.debug("value: %s", val)`, not f-strings.
- Do not log secrets, tokens, or user-identifying information.

| Level | Use for |
|---|---|
| `DEBUG` | Message content, response shapes, protocol-level detail |
| `INFO` | Lifecycle events, configuration changes, startup/shutdown |
| `WARNING` | Capability gaps, unexpected-but-handled conditions |
| `ERROR` | Connection failures, unexpected crashes, protocol violations |

## Testing

- Non-trivial changes must be backed by tests.
- Integration tests should cover real protocol interactions where feasible.
- Tests verify behavior, not implementation details.
- No hardcoded paths, user IDs, or environment-specific values in tests.
- Tests that depend on external binaries not present in all environments
  may use `skipif` with a clear reason explaining the dependency.

## Python Conventions

- Python 3.11+ required.
- Type hints on all function signatures and return types.
- Pydantic for request/response models at HTTP boundaries.
- Dataclasses for internal data structures.
- `async/await` throughout — no blocking I/O in the event loop.
