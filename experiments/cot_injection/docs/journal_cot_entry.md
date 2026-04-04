## 2026-04-04 — CoT Injection via Packed Prompt: Empirical Results

### Motivation

ACP sessions are stateful and the proxy extracts only the last user message
(ADR-004). This creates an opportunity: the "last user message" can contain
a synthetic conversation transcript with injected role labels. The model
receives it as a single text block but interprets the role labels as
conversational structure.

The hypothesis: if we pack a synthetic partial assistant response (truncated
mid-sentence) followed by a user nudge ("you got cut off, please continue"),
the model will seamlessly continue the interrupted thought. This could be
used to manufacture chain-of-thought reflection — seeding the model with a
direction to explore before it produces its final answer.

### Experiment evolution

Three phases of experimentation, each building on the prior:

1. **Multi-turn injection** — multiple HTTP requests, each injecting one turn.
   Finding: every `session/prompt` is a user turn in ACP. No way to inject an
   assistant turn via protocol. The model always responded *to* the injection
   rather than *continuing* it. Socratic pushing ("What did you overlook?")
   worked for depth but was honest user-to-assistant interaction, not synthetic CoT.

2. **Single-blob transcript** — pack the entire synthetic conversation into one
   user message with role labels as plain text. Several framings tested. The
   `system_framed` variant (meta-instruction + transcript) achieved true
   mid-sentence continuation but was dismissed as too close to a normal prompt.

3. **Two-request packed injection** (final approach) — R1 is a normal question.
   R2 packs a closing remark, a truncated synthetic assistant turn, and a
   synthetic user nudge into the last user message. The proxy sends only this
   packed content to the ACP session which already has R1's Q&A in memory. The
   key insight: this is prompt injection (like SQL injection) — the role labels
   break out of the user role and manufacture a synthetic conversation history.

### The packed injection format

R2's last user message is a single text block with three sections separated
by blank lines:

1. A closing remark that ends the prior exchange (e.g., "Thanks, that covers
   the basics.")
2. A line starting with the assistant role label, followed by a partial sentence
   that is deliberately truncated mid-clause — the model's "interrupted thought"
3. A line starting with the user role label, with a nudge like "You got cut off.
   Please continue where you left off."

The exact format is preserved in the experiment config files under
`experiments/cot_injection/configs/`. See `first_run.json` and `reversal.json`
for the qualitative variants, and `code_eval_expr_statistical.json` for the
quantitative experiment.

### Qualitative results: depth steering (probe_cot_v3)

**Question:** "What are the tradeoffs between using a stateful session model
versus a stateless replay model for proxying LLM conversations?"

Three injection variants tested, each seeding a different direction:

| Variant | Injection seed | R2 behavior |
|---|---|---|
| `failure_modes` | "...didn't fully address the failure modes. When session state is lost" | Picked up the topic, added substantive failure analysis |
| `edge_cases` | "...edge cases that matter in production. Specifically, when concurrent requests hit the same session" | Continued the exact thread, addressed race conditions |
| `deeper_issue` | "...a deeper issue I glossed over. The real question isn't performance — it's what happens to context integrity when" | Started with an ellipsis and continued MID-SENTENCE. True seamless continuation. |

The `deeper_issue` variant produced the most compelling result — the model
literally continued from the truncation point with no preamble. The more
specific the truncation point (ending mid-clause rather than at a sentence
boundary), the more seamless the continuation.

### Qualitative results: reversal resistance

Tested whether the injection could reverse the model's position (e.g., making
it argue stateful is more scalable than stateless — the opposite of its actual
view).

| Variant | R2 behavior |
|---|---|
| `full_reversal` | **Refused.** "If I implied the opposite, let me clarify" — restated correct position |
| `reversal_with_seed` | **Continued syntax, refused content.** Ellipsis continuation, but pivoted back to truth mid-sentence |
| `reversal_strong` | **Explicitly corrected.** "However, to clarify: in most distributed systems, stateless replay is generally considered more scalable..." |

Critical finding: GPT-4.1 resists factual reversal even when the injection
achieves syntactic continuation. The technique controls *form* but not
*substance* when the substance contradicts the model's understanding. This is
a necessary result — if the model were susceptible to reversal, the technique
would be invalid for the purpose of improving accuracy through self-reflection.

### Quantitative results: code correctness (eval_expr)

**Task:** Implement `eval_expr(expr: str) -> float` — a mathematical expression
evaluator with operator precedence, parentheses, unary minus. No imports, no
eval(). 23 test cases including edge cases (empty input, division by zero,
nested parens, unary minus after operator).

**The local minimum:** GPT-4.1 consistently produces a recursive descent parser
using nested functions with a shared position variable `i`. It consistently
fails to add `nonlocal i` declarations to inner functions that modify `i`,
causing `UnboundLocalError` on every test. This failure is 100% reproducible
(0/23 across all 5 iterations of the statistical run).

**Statistical results (5 iterations, gpt-4.1):**

| Variant | R1 mean | R2 mean | Improved | Same | Regressed |
|---|---|---|---|---|---|
| baseline (control) | 0.0/23 | n/a | n/a | n/a | n/a |
| no_injection (control) | 0.0/23 | n/a | n/a | n/a | n/a |
| generic_quality | 0.0/23 | 0.0/23 | 0 | 5 | 0 |
| exact_bug | 0.0/23 | 23.0/23 | 5 | 0 | 0 |
| category_scoping | 0.0/23 | 23.0/23 | 5 | 0 | 0 |
| category_scoping_mutation_edges | 0.0/23 | 14.2/23 | 5 | 0 | 0 |

**Key findings:**

1. **The model hits the nonlocal bug 100% of the time.** Every baseline, control,
   and R1 scored 0/23 across all iterations. This is a deterministic local minimum.

2. **`exact_bug` and `category_scoping` both achieve 100% recovery (5/5).**
   Naming the exact bug works, but so does the category-level injection. You don't
   need to know the specific bug — mentioning the failure category is sufficient.

3. **`generic_quality` does nothing (0% recovery).** "Mentally trace through test
   cases" had zero effect. The model cannot self-diagnose the nonlocal bug through
   introspection alone.

4. **Adding too many concerns dilutes the signal.** `category_scoping_mutation_edges`
   (scoping + mutation + boundary conditions) only achieved 62% mean recovery.
   The model tried to address all three concerns simultaneously and sometimes got
   the scoping fix wrong while adding edge case handling.

5. **The specificity gradient:**
   - Generic -> 0% recovery
   - Category-level ("variable scoping in closures") -> 100% recovery
   - Category + extras -> ~62% recovery (diluted)
   - Exact bug -> 100% recovery

   The sweet spot is category-level: specific enough to steer the model out of
   the local minimum, general enough to not require knowing the exact bug.

### Operationalization

The category-level injection is the operationalizable form. For code generation
tasks where models tend to hit systematic failure modes, injecting a reflection
that draws attention to the *class of failure* (scoping, off-by-one, null
handling, concurrency) can rescue the output without requiring knowledge of the
specific bug.

The injection format is generic and can be templated. The three components are:

1. A brief closing remark
2. A synthetic assistant partial turn naming the failure category, truncated
   mid-sentence
3. A synthetic user nudge asking to continue

This is cheap (one additional LLM call per task), measurable (test suite
before/after), and composable (different failure categories can be injected
for different task types).

### Observation: role labels in content may trigger EOS

During documentation of this experiment, writing the raw injection format into
markdown caused repeated truncation in the authoring LLM's output. The
assistant role label followed by a truncated sentence appears to trigger
early termination in some models — likely because it resembles a turn boundary
or end-of-sequence pattern in training data. The injection format should be
stored in config files, not inlined in prose, to avoid this issue.

### Files

All experiment artifacts are under `experiments/cot_injection/`:
- `configs/` — JSON configs for each experiment variant
- `probe_cot_v3.py` — qualitative depth/reversal probe
- `probe_code_correctness.py` — quantitative code correctness probe with N-iteration support
- `logs/` — detailed per-run logs with verbatim request/response content
