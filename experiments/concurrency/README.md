# Concurrency Behavior of copilot-language-server (ACP Mode)

Empirical investigation of how `copilot-language-server --acp --stdio` handles
concurrent workloads. Tests two scaling dimensions:

1. **Intra-process**: parallel sessions within a single language server process
2. **Inter-process**: multiple language server processes running simultaneously

## Key Findings

### 1. Parallel sessions on one process: works, scales sub-linearly

A single copilot-language-server process handles multiple concurrent sessions
without errors. All prompts complete successfully with correct, deterministic
output (2752 chars / 688 tokens every time).

**Token throughput scaling curve (3-run averages, gpt-4.1, 688-token response):**

| Sessions | Per-prompt tok/s | Aggregate tok/s | Mean latency (s) |
|---------:|:----------------:|:---------------:|:-----------------:|
| 1        | 170              | 170             | 4.2               |
| 2        | 169              | 307             | 4.1               |
| 4        | 152              | 509             | 4.7               |
| 8        | 118              | 580             | 6.2               |
| 12       | 119              | 1272            | 5.8               |
| 16       | 113              | 1212            | 6.2               |

1 token ~ 4 characters. Per-prompt tok/s is the per-agent experience (how
fast each agent sees its response generated). Aggregate tok/s is total token
production across all sessions divided by wall time.

**Per-prompt generation rate degrades ~30% then stabilizes.** Each agent sees
~115 tok/s at 8-16 sessions, down from ~170 tok/s solo. The backend throttles
per-request generation under concurrent load. Aggregate throughput peaks at
~1270 tok/s (12 sessions) but has high variance at 8+ sessions due to tail
latency outliers.

**Prompts-per-second (trivial 1-char response, isolating round-trip overhead):**

| Sessions | Wall time (s) | Throughput (p/s) | Efficiency |
|---------:|:-------------:|:----------------:|:----------:|
| 1 (seq)  | 1.49          | 0.67             | baseline   |
| 1        | 0.85          | 1.18             | —          |
| 2        | 1.03          | 1.95             | 145%       |
| 4        | 1.36          | 3.01             | 112%       |
| 8        | 2.37          | 3.41             | 64%        |
| 12       | 2.83          | 4.26             | 53%        |
| 16       | 3.82          | 4.35             | 41%        |

The bottleneck is the Copilot backend, not the local process. The language
server is a thin async relay — all sessions multiplex over a single upstream
connection, and the backend applies rate limiting or request serialization.

### 2. Parallel prompts on the SAME session: cancellation

Sending two concurrent `session/prompt` calls to the same session causes the
server to cancel the earlier prompt:

- The second prompt returns `"Info: Operation cancelled by user"` in ~0.12s
- The first prompt completes but may receive the second prompt's response
  (the update queue is shared and overwritten)

With 3 concurrent prompts on one session, only the last-submitted prompt
produces any content; all others are cancelled or lose their update stream.

**Conclusion**: one prompt at a time per session is a hard constraint, enforced
server-side via cancellation. This is not an error — it's the designed behavior.

### 3. Multiple processes: works, scales similarly to intra-process

Multiple copilot-language-server processes can run simultaneously with
independent auth tokens (auto-discovered from `~/.config/github-copilot/`).
Each manages its own sessions. No interference observed.

| Configuration        | Wall time (s) | Throughput (prompts/s) |
|---------------------:|:-------------:|:----------------------:|
| 2 proc x 1 session   | ~0.95         | ~2.1                   |
| 4 proc x 1 session   | ~1.1-1.6      | ~2.5-3.7               |
| 2 proc x 2 sessions  | ~1.7          | ~2.3                   |
| 4 proc x 2 sessions  | ~2.0-2.5      | ~3.2-4.1               |

Inter-process scaling is comparable to intra-process scaling. This makes sense:
the bottleneck is the Copilot backend API, not the local process. Spinning up
extra processes adds startup overhead (~3-5s per process for init + session
creation + warmup) but does not improve throughput beyond what parallel sessions
on one process already achieve.

### 4. Session creation is slow, prompting is fast

| Operation          | Latency     |
|-------------------:|:-----------:|
| Process start + init | ~2-3s     |
| Session creation   | ~1.5-2.5s   |
| Warmup prompt      | ~1.7-2.7s   |
| Subsequent prompt  | ~0.9-2.0s   |

The first prompt on a session (warmup) is consistently ~0.5s slower than
subsequent prompts. Session creation is ~2s regardless of how many sessions
already exist on the process.

## Implications for Meadow Integration

### Recommended architecture: single process, session pool

1. **One copilot-language-server process** is sufficient. Multiple processes
   do not improve throughput because the backend API is the bottleneck. Extra
   processes only add startup latency and memory.

2. **Pre-create a pool of sessions** at startup. Session creation takes ~2s
   each, so a pool of N sessions adds ~2N seconds to startup but allows
   immediate agent dispatch without per-agent session creation latency.

3. **One prompt at a time per session** — enforce this in the proxy with a
   per-session lock or semaphore. Concurrent prompts cause cancellation.

4. **Session affinity per agent** — each Meadow agent should be bound to a
   dedicated session for the duration of its conversation. Sessions accumulate
   context, so switching sessions mid-conversation loses history.

5. **8-12 sessions is the operating sweet spot.** Beyond 12, throughput
   plateaus while latency and tail variance increase. 8 sessions give ~3.4 p/s
   at 64% efficiency; 12 give ~4.3 p/s at 53%. For Meadow's typical 4-6
   concurrent agents plus subagents, 8-12 sessions provides headroom without
   hitting the diminishing-returns zone.

6. **Process-level redundancy** — if the language server crashes, a second
   standby process could be kept warm for failover. But do not use multiple
   processes for load distribution.

### Throughput ceiling

Two ceilings, depending on what you measure:

- **Round-trip ceiling**: ~4.5 prompts/s (trivial response). This is the
  maximum request rate the backend will service.
- **Token generation ceiling**: ~1270 aggregate tok/s (688-token response,
  12 sessions). Each individual agent sees ~115 tok/s.

Both are backend-imposed limits. 12 and 16 sessions produce nearly identical
throughput despite doubling local parallelism.

For Meadow's multi-agent workloads, **per-agent latency is the binding
constraint**, not aggregate throughput. At 8-16 concurrent agents, each agent
waits ~6s for a 688-token response vs ~4s solo — a 50% latency penalty.

See [ADR-009](../../adrs/009-intra-process-session-scaling.md) for the
architectural decision based on these findings.

## Running the Experiment

The probe talks directly to copilot-language-server over stdio — no proxy
needed.

```bash
# Full suite
python probe_concurrency.py --config configs/default.json

# Subset of tests
python probe_concurrency.py --config configs/default.json \
    --tests baseline_sequential intra_parallel_sessions_4

# Longer prompt (more realistic response length)
python probe_concurrency.py --config configs/longer_prompt.json

# Custom binary
python probe_concurrency.py --config configs/default.json \
    --binary /path/to/copilot-language-server

# Debug logging (verbose JSON-RPC traffic)
python probe_concurrency.py --config configs/default.json -v
```

## Directory Structure

```
acp_harness.py        Lightweight async ACP transport + client for experiments
probe_concurrency.py  Main experiment script
configs/
  default.json        Short prompt, 9 test configurations (intra + inter + same-session)
  longer_prompt.json  Longer prompt, 9 test configurations
  scaling_curve.json         Intra-process p/s scaling: 1-16 sessions, trivial prompt
  scaling_curve_tokens.json  Intra-process tok/s scaling: 1-16 sessions, 688-token response
logs/                 Per-run output (gitignored)
```

## Test Configurations

Each config defines a `prompt`, `model`, and list of `tests`. Each test
specifies:

- `mode`: `intra_process` or `inter_process`
- `num_processes`: how many language server processes to spawn
- `sessions_per_process`: how many sessions per process
- `parallel_prompts_per_session`: how many concurrent prompts per session
- `total_prompts`: for sequential baseline tests, how many prompts to run

The `default.json` config includes:

| Test | Mode | Procs | Sessions | Parallel | Purpose |
|------|------|------:|---------:|---------:|---------|
| baseline_sequential | intra | 1 | 1 | 1 | Per-prompt latency baseline (3 sequential) |
| intra_parallel_sessions_{2,4,8} | intra | 1 | 2/4/8 | 1 | Intra-process session scaling |
| intra_same_session_2 | intra | 1 | 1 | 2 | Concurrent prompts on same session (expect cancellation) |
| inter_process_{2,4} | inter | 2/4 | 1 | 1 | Inter-process scaling |
| inter_process_{2x2,4x2} | inter | 2/4 | 2 | 1 | Combined inter-process + intra-process scaling |
