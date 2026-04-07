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

**Token throughput scaling curve (local, multi-run averages, gpt-4.1, 688-token response):**

| Sessions | Per-prompt tok/s | Aggregate tok/s | Mean latency (s) |
|---------:|:----------------:|:---------------:|:-----------------:|
| 1        | 170              | 170             | 4.2               |
| 2        | 169              | 307             | 4.1               |
| 4        | 152              | 509             | 4.7               |
| 8        | 118              | 580             | 6.2               |
| 12       | 119              | 1272            | 5.8               |
| 16       | 113              | 1320            | 6.2               |
| 20       | 110              | 1655            | 6.4               |
| 24       | 97               | 1988            | 7.1               |
| 32       | 81               | 2041            | 8.6               |

**Target environment scaling (gpt-4.1 + gpt-4o mixed, single runs):**

| Sessions | Wall time (s) | Throughput (p/s) | Est. agg tok/s |
|---------:|:-------------:|:----------------:|:--------------:|
| 1        | ~10.0         | 0.10             | 69             |
| 8        | ~11.6         | 0.69             | 475            |
| 16       | ~12.0         | 1.33             | 915            |
| 24       | ~13.3         | 1.80             | 1238           |
| 32       | 13.54         | 2.36             | 1624           |
| 40       | 13.95         | 2.87             | 1975           |
| 48       | 13.98         | 3.43             | 2360           |
| 64       | 14.36         | 4.46             | 3069           |

1 token ~ 4 characters. Per-prompt tok/s is the per-agent experience (how
fast each agent sees its response generated). Aggregate tok/s is total token
production across all sessions divided by wall time.

**No hard throughput ceiling exists.** Earlier results at 12-16 sessions
appeared to show a plateau, but extending to 32 (local) and 64 (target)
revealed continued scaling. The backend spreads compute across concurrent
requests rather than imposing a fixed rate limit. The tradeoff is aggregate
throughput vs per-agent latency — adding sessions increases total token
production but degrades each individual agent's generation rate.

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

### 2. Mixed models: rate limit is per-account, not per-model

Tested gpt-4.1 + claude-haiku-4.5 sessions running concurrently on the same
process. At each total session count, gpt-4.1 per-prompt tok/s is unchanged
regardless of whether the other sessions use the same model or a different one:

| Total sessions | gpt-4.1 solo (tok/s) | gpt-4.1 mixed (tok/s) | Delta |
|---------------:|:--------------------:|:---------------------:|:-----:|
| 2              | 169                  | 165                   | -3%   |
| 4              | 152                  | 161                   | +6%   |
| 8              | 118                  | 122                   | +4%   |
| 12             | 119                  | 118                   | -1%   |
| 16             | 113                  | 114                   | +1%   |

All deltas are within noise. Mixing models does not unlock additional
throughput — all models share the same backend rate limit budget. This rules
out model-diverse session pools as a scaling strategy.

Side finding: claude-haiku-4.5 leaks chain-of-thought reasoning into its
response text (visible as "The user is asking..." preamble). This is CoT
exposed via ACP with no separate reasoning token stream.

### 3. Parallel prompts on the SAME session: cancellation

Sending two concurrent `session/prompt` calls to the same session causes the
server to cancel the earlier prompt:

- The second prompt returns `"Info: Operation cancelled by user"` in ~0.12s
- The first prompt completes but may receive the second prompt's response
  (the update queue is shared and overwritten)

With 3 concurrent prompts on one session, only the last-submitted prompt
produces any content; all others are cancelled or lose their update stream.

**Conclusion**: one prompt at a time per session is a hard constraint, enforced
server-side via cancellation. This is not an error — it's the designed behavior.

### 4. Multiple processes: works, scales similarly to intra-process

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

### 5. Session creation is slow, prompting is fast

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

5. **Size for latency, not throughput.** There is no hard throughput
   ceiling — scaling continues to at least 64 sessions. Use the scaling
   model to estimate per-agent latency at a given concurrency:

   `mean_latency_s = T * (1 + k * N) / p0`

   For Meadow's typical 4-8 concurrent agents, 8-16 sessions keeps
   per-agent latency under ~12s on the target environment. For workflows
   with more subagents, 32-64 sessions are viable.

6. **Process-level redundancy** — if the language server crashes, a second
   standby process could be kept warm for failover. But do not use multiple
   processes for load distribution.

### Scaling model

The empirical data fits two parameterized functions (R² ≥ 0.94 local,
≥ 0.99 target):

```
aggregate_tok_s(N)   = a * N^b
per_prompt_tok_s(N)  = p0 / (1 + k * N)
mean_latency_s(N, T) = T * (1 + k * N) / p0
```

where N = concurrent sessions and T = tokens per response.

| Parameter | Local | Target | Meaning |
|-----------|------:|-------:|---------|
| a         | 173   | 76     | Base aggregate throughput (tok/s) |
| b         | 0.737 | 0.895  | Scaling exponent (closer to 1 = more linear) |
| p0        | 170   | 68     | Solo per-prompt generation rate (tok/s) |
| k         | 0.033 | 0.008  | Per-session degradation coefficient |

The target environment has a slower baseline (p0 is 40% of local) but
scales more efficiently (b = 0.895 vs 0.737, k is 4x smaller).

See [ADR-009](../../adrs/009-intra-process-session-scaling.md) for the
full architectural decision, scaling curves from both environments, and
latency planning tables.

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

# Tag with host identity (for baseline tracking across machines)
python probe_concurrency.py --config configs/mixed_model_gpt4o_extended.json \
    --host target-mbp-na
```

### Baseline tracking

The probe computes a baseline summary from each run and includes it in the
JSON output under the `baseline` key. The primary scalar is the **scaling
factor at N=8** — the ratio of mean latency at 8 concurrent sessions to
solo latency. This normalizes out absolute environment speed and isolates
the concurrency degradation coefficient.

```
--- Baseline (bigmac.local) ---
Solo latency: 4.20s  (170.0 tok/s)
N=8 latency:  6.20s
Scaling factor (N=8): 1.48
```

Use `--host` to tag runs with a stable host identifier (defaults to
`socket.gethostname()`). The host and baseline are recorded in the JSON
output for cross-run comparison. Baselines are per-host — do not compare
absolute values across different machines or network paths.

The canonical baseline config is `mixed_model_gpt4o_extended.json` (the
most exercised config with target environment data).

## Directory Structure

```
acp_harness.py        Lightweight async ACP transport + client for experiments
probe_concurrency.py  Main experiment script
configs/
  default.json                      Short prompt, 9 tests (intra + inter + same-session)
  longer_prompt.json                Longer prompt, 9 tests
  scaling_curve.json                Intra-process p/s scaling: 1-16 sessions, trivial prompt
  scaling_curve_tokens.json         Intra-process tok/s scaling: 1-16 sessions, 688-token response
  scaling_curve_tokens_extended.json  Extended tok/s scaling: 1-32 sessions
  mixed_model_tokens.json           Mixed-model: gpt-4.1 + haiku, 1-16 sessions
  mixed_model_gpt4o.json            Mixed-model: gpt-4.1 + gpt-4o, 1-16 sessions (target env)
  mixed_model_gpt4o_extended.json   Extended mixed-model: 1-32 sessions (target env)
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
