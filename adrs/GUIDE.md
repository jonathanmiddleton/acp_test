# Architectural Decision Records — Guide

## Brief Motivation

Our use of ADRs diverges from the textbook in several ways worth calling out
up front.

- **AI agents are the primary audience.** Traditional ADRs target human
  engineers joining a team months later. Ours are consumed by AI agents at
  the start of every session — agents that have no persistent memory and
  need to reconstruct architectural context from scratch each time. ADRs
  serve as durable institutional memory that compensates for the agent's
  lack of it.

- **They feed directly into enforceable rules.** In most projects, ADRs are
  advisory — a developer reads them and exercises judgment. Here, ADRs are
  extracted into `CODING_STANDARDS.md` which is injected into agent system
  prompts. The ADR-to-standard pipeline makes decisions mechanically
  enforceable, not just documented.

- **The write cadence is higher.** A conventional team might produce a
  handful of ADRs per quarter. This is partly because AI agents need
  explicit written decisions for things a human team would absorb through
  osmosis (pair programming, hallway conversations, code review norms). If
  it is not written down, it does not exist for the agent.

- **They encode negative results and operational constraints, not just
  architecture.** [ADR-005](005-fail-loud-testing.md) (fail-loud testing)
  documents an observed failure mode — silent test skips masking a wrong-
  binary bug on the target machine — and the policy that prevents it.
  These are closer to runbook entries than classical architecture decisions,
  but they need the same "context + decision + rejected alternatives"
  structure to prevent agents from re-introducing the problems.

- **Rejected alternatives matter more.** For human teams, rejected
  alternatives prevent re-litigation in meetings. For AI agents, they
  prevent the agent from confidently proposing the exact approach that was
  already evaluated and dismissed. Without that section, the agent has no
  way to know that "just bypass the language server and call the Copilot
  backend directly" was already considered and rejected for specific reasons
  (see [ADR-001](001-acp-proxy-architecture.md)).

- **They are versioned in-place more often.** Traditional ADRs are
  immutable — you supersede, not edit. We sometimes append revision history
  tables when the core decision holds but implementation details shift.
  This is pragmatic: creating a new ADR for every tactical adjustment to an
  existing decision would flood the index without adding clarity.

- **They record empirical data, not just opinions.**
  [ADR-009](009-intra-process-session-scaling.md) records throughput
  measurements, fitted scaling model parameters, and derived latency
  planning tables. This is unusual for ADRs but essential when the
  "architecture decision" is a capacity planning policy grounded in data
  rather than opinion.

## What is an ADR?

An Architectural Decision Record (ADR) is a short document that captures a
single significant design decision along with its context and consequences.
The idea comes from Michael Nygard's original proposal: decisions are the
things that are hardest to change later, so they deserve the same
version-controlled rigor as the code they govern.

An ADR is not a design doc, RFC, or spec. It records a **decision that was
made** — not a proposal under debate. If a decision is later reversed, you
write a new ADR that supersedes the old one; you do not edit the original.

## Why bother?

1. **Onboarding.** A new contributor (human or AI) can read the ADR index
   and understand *why* the system is shaped the way it is, not just *what*
   it looks like today.
2. **Preventing re-litigation.** When someone asks "why don't we just use
   X?", the ADR explains the tradeoffs that were already evaluated. This
   saves time and preserves institutional knowledge.
3. **Accountability.** Decisions have dates and context. When circumstances
   change (a library matures, a constraint is lifted), you can revisit the
   ADR with fresh information rather than guessing whether the original
   reasoning still holds.
4. **Binding standards.** In this project, ADRs feed directly into
   `CODING_STANDARDS.md`. A standard like "never truncate LLM responses"
   traces back to the ADR that explains the reasoning. The standard is the
   rule; the ADR is the rationale.

## When to write an ADR

Write one when:

- You are choosing between multiple viable approaches and the choice has
  lasting consequences (proxy architecture, protocol bridging strategy,
  session management model).
- You are establishing a convention that all future code must follow
  (logging policy, error handling pattern, test structure).
- You are deliberately *not* doing something that a reasonable person might
  expect (e.g., not calling the Copilot backend API directly, not spawning
  multiple server processes for concurrency).
- You are reversing or significantly amending a prior decision.

Do **not** write an ADR for:

- Routine implementation choices that are easy to change (variable naming in
  a single module, choice of assertion helper).
- Bug fixes.
- Incremental feature work that follows an already-decided architecture.

## Structure

Every ADR in this project follows the same skeleton:

```markdown
# ADR-NNN: Title — Short Descriptive Subtitle

**Status:** Accepted | Superseded by ADR-XXX
**Date:** YYYY-MM-DD
**Relates to:** PROJ-ticket (if applicable)
**Related ADRs:** [ADR-XXX](xxx-filename.md), ...

## Context

What is the situation? What forces are at play? What problem needs solving?
Be specific — name modules, describe failure modes, cite observed behavior.

## Decision

What are we doing? Describe the approach in enough detail that someone
reading the code can connect the implementation back to this document.
Use subsections, tables, and diagrams where they aid clarity.

## Consequences

What follows from this decision? Split into:
- **Positive**: What improves.
- **Tradeoffs**: What gets harder or what we give up.

## Rejected Alternatives

What else was considered and why it was not chosen. This is the section
that prevents re-litigation. Be honest about tradeoffs, not dismissive.
```

Optional additions:

- **Revision History** table at the end, for decisions that evolve after
  acceptance.
- **Non-goals** section when it is important to explicitly fence scope.
- **File-path impact inventory** when the decision touches many modules.

## Conventions in this project

### Numbering

ADRs are numbered sequentially: `NNN-short-kebab-title.md`. The next
number is one higher than the highest existing file. Check the `adrs/`
directory before creating a new one.

### Index

`000-index.md` is the table of contents, organized by domain (proxy
architecture, session management, testing, etc.). Every new ADR must be
added to the index under the appropriate section.

### Status lifecycle

| Status                    | Meaning                                                                       |
|---------------------------|-------------------------------------------------------------------------------|
| **Accepted**              | Active and binding. Code must conform.                                        |
| **Superseded by ADR-XXX** | Replaced. The new ADR explains what changed. The old ADR is kept for history. |

We do not use Draft or Proposed — if a decision is not yet made, it does not
get an ADR. Discuss in a Jira ticket or conversation first; write the ADR
when the decision lands.

### Relationship to CODING_STANDARDS.md

`CODING_STANDARDS.md` extracts the enforceable rules from ADRs into a single
reference. When an ADR establishes a new coding convention, add a
corresponding entry to `CODING_STANDARDS.md` with a back-link to the ADR.
The standard is the quick-reference rule; the ADR is the full reasoning.

### Relationship to Jira

If an ADR relates to a Jira ticket, include the ticket key in the
**Relates to** field. This creates a bidirectional trace: the ticket links
to the code change, and the ADR links to the ticket that motivated it.

## How to write a good ADR

1. **Be specific, not abstract.** Name the modules, the failure modes, the
   measurements. "This was slow" is weak. "`find_binary()` grabbed an
   incompatible `copilot-language-server` from `ps` output because multiple
   IDE instances were running" is strong.

2. **Record what you rejected and why.** The rejected-alternatives section
   is often the most valuable part. Future readers will have the same ideas
   you had; save them the investigation.

3. **Keep it short.** An ADR is not a design doc. If you need more than ~2
   pages of prose, you are probably bundling multiple decisions. Split them.

4. **Write it after the decision, not before.** An ADR records what was
   decided, not what might be decided. If you are still evaluating options,
   that is a conversation or a Jira ticket, not an ADR.

5. **Date it.** Decisions age. Context that was true in March may not hold
   in June. The date helps future readers calibrate.

## Further reading

- Michael Nygard, [Documenting Architecture Decisions](https://cognitect.com/blog/2011/11/15/documenting-architecture-decisions) (2011) — the original proposal.
- Joel Parker Henderson, [adr-tools](https://github.com/joelparkerhenderson/architecture-decision-record) — a collection of templates and tooling.
