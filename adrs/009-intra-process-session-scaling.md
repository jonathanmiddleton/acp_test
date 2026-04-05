# ADR-009: Intra-Process Session Scaling

**Status:** Accepted  
**Date:** 2026-04-05  

## Context

Meadow orchestrates multiple concurrent agents (PM, developer, tester,
reviewer, and potentially subagents). Each agent needs its own ACP session
to maintain isolated conversation context. The proxy currently runs a single
copilot-language-server process with sessions multiplexed over one stdio
connection.

Two open questions needed empirical answers before committing to a workload
management policy:

1. Can a single language server process handle N parallel sessions?
2. Should Meadow scale by adding processes or by adding sessions?

### Empirical evidence

Two experiments were run: a prompts-per-second measurement with a trivial
prompt (1-char response), and a token throughput measurement with a 688-token
response (code review of a Python script). Both used `gpt-4.1` with 3
independent runs each at 1, 2, 4, 8, 12, 16 parallel sessions on one process.

**Token throughput scaling curve (3-run averages, 688-token response):**

| Sessions | Per-prompt tok/s | Aggregate tok/s | Mean latency (s) |
|---------:|:----------------:|:---------------:|:-----------------:|
| 1        | 170              | 170             | 4.2               |
| 2        | 169              | 307             | 4.1               |
| 4        | 152              | 509             | 4.7               |
| 8        | 118              | 580             | 6.2               |
| 12       | 119              | 1272            | 5.8               |
| 16       | 113              | 1212            | 6.2               |

**Per-prompt generation rate** is the stable metric. It degrades ~30% from
1 to 8 sessions (170 → 118 tok/s) then holds steady through 16. This is
the per-agent experience: each agent sees its responses generated at ~115
tok/s regardless of how many other agents are active.

**Aggregate throughput** measures total token production across all sessions
per unit of wall time. It scales well to 12 sessions (~1270 tok/s, 7.5x
baseline) but has high variance at 8+ sessions due to tail latency outliers
(one slow response dominates wall time via `max(latencies)`).

**Prompts-per-second scaling curve (3-run averages, 1-char response):**

| Sessions | Wall time (s) | Throughput (p/s) | Efficiency |
|---------:|:-------------:|:----------------:|:----------:|
| 1 (seq)  | 1.49          | 0.67             | baseline   |
| 1        | 0.85          | 1.18             | —          |
| 2        | 1.03          | 1.95             | 145%       |
| 4        | 1.36          | 3.01             | 112%       |
| 8        | 2.37          | 3.41             | 64%        |
| 12       | 2.83          | 4.26             | 53%        |
| 16       | 3.82          | 4.35             | 41%        |

Efficiency = actual throughput / ideal linear throughput. The trivial-prompt
experiment isolates round-trip overhead from generation time.

**Key observations:**

- **Per-prompt generation rate degrades ~30% then stabilizes.** Individual
  agents see ~115 tok/s at any parallelism from 8-16 sessions, down from
  ~170 tok/s solo. The backend appears to throttle per-request generation
  when concurrent load increases.
- **Aggregate throughput plateaus between 12 and 16 sessions.** Adding
  sessions beyond 12 provides negligible gain while increasing tail latency
  variance.
- **The bottleneck is the Copilot backend, not the local process.** Wall time
  grows because the backend serializes or rate-limits upstream API calls.
  The language server itself is a thin async relay.
- **All 16 sessions complete successfully** with correct content. No errors,
  no cancellations, no timeouts. The language server is structurally capable
  of multiplexing 16+ sessions over one stdio connection.
- **Inter-process scaling provides no throughput advantage.** Experiments with
  2-4 separate processes yielded comparable throughput to the same number of
  sessions on one process. Extra processes add ~3-5s startup overhead per
  process without increasing the backend throughput ceiling.
- **Same-session concurrent prompts cause server-side cancellation.** Two
  prompts to the same session result in `"Operation cancelled by user"` on
  the earlier prompt. One prompt at a time per session is a hard constraint.
- **Tail latency increases with parallelism.** At 16 sessions with a
  688-token response, p50 is ~6.0s vs ~4.2s baseline, with occasional
  outliers to ~15s.

## Decision

Scale by adding sessions within a single process. Do not use multiple
processes for throughput.

Specifically:

1. **Single copilot-language-server process** — the proxy spawns and manages
   exactly one language server. No process pool.
2. **Pre-created session pool** — at startup, create N sessions (where N is
   the expected agent concurrency, likely 4-8 for Meadow's standard workflow).
   Session creation takes ~2s each, so this adds ~8-16s to startup but
   eliminates per-agent session creation latency.
3. **Per-session concurrency lock** — enforce one-prompt-at-a-time per session
   with an asyncio semaphore. Concurrent prompts cause cancellation, so this
   must be enforced client-side.
4. **Session affinity per agent** — bind each agent to a dedicated session for
   the duration of its conversation. Sessions accumulate context, so switching
   mid-conversation loses history.
5. **8-12 sessions as the operating range** — beyond 12 sessions, throughput
   plateaus while latency and variance increase. 8 sessions provide ~3.4 p/s
   at 64% efficiency; 12 provide ~4.3 p/s at 53%. For Meadow's typical
   4-6 concurrent agents, 8 sessions provides headroom without hitting the
   diminishing-returns zone.

## Rationale

- **Empirical, not theoretical.** The scaling curve was measured, not inferred
  from architecture diagrams. Three independent runs confirm the plateau.
- **Startup cost matters.** Each additional process adds ~3-5s to startup.
  For a system that orchestrates agent workflows, fast startup enables
  iteration.
- **Simplicity.** One process, one transport, one read loop. No process pool
  management, no inter-process message routing, no split-brain failure modes.
- **The ceiling is external.** No local architectural change will move the
  throughput ceiling (~4.5 p/s trivial, ~1270 aggregate tok/s with 688-token
  responses). It is set by the Copilot backend.

## Consequences

- **Session accumulation** remains an unsolved problem (see ADR-002). With
  8-12 pre-created sessions plus ad-hoc sessions (title generator, etc.),
  the language server will hold 10-15+ sessions in memory. Periodic process
  restart may be needed as a cleanup strategy.
- **If Meadow requires >12 concurrent agents**, throughput will not scale
  further. The options at that point are: (a) queue prompts and accept higher
  latency, (b) investigate whether multiple GitHub accounts or API keys
  provide independent rate limits, or (c) use a non-Copilot model provider
  for overflow.
- **Per-session locks add complexity** but are necessary. The proxy currently
  has no concurrency control (relies on OpenCode sending one request at a
  time). Meadow's multi-agent workload will require explicit serialization.
