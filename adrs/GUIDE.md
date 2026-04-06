# Architectural Decision Records — Guide

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
   `CODING_STANDARDS.md`. A standard like "no print statements" traces back
   to ADR-010, which explains the reasoning. The standard is the rule; the
   ADR is the rationale.

## When to write an ADR

Write one when:

- You are choosing between multiple viable approaches and the choice has
  lasting consequences (runtime architecture, persistence strategy, protocol
  design).
- You are establishing a convention that all future code must follow
  (logging policy, error handling pattern, test structure).
- You are deliberately *not* doing something that a reasonable person might
  expect (e.g., not using an ORM, not supporting mailbox replay on resume).
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
**Relates to:** MG-ticket (if applicable)
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
  acceptance (see ADR-036 for an example).
- **Non-goals** section when it is important to explicitly fence scope.
- **File-path impact inventory** when the decision touches many modules
  (see ADR-027).

## Conventions in this project

### Numbering

ADRs are numbered sequentially: `NNN-short-kebab-title.md`. The next
number is one higher than the highest existing file. Check the `adrs/`
directory before creating a new one.

### Index

`000-index.md` is the table of contents, organized by domain (core runtime,
memory, validation, etc.). Every new ADR must be added to the index under the
appropriate section.

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

If an ADR relates to a Jira ticket (e.g., it was written as part of
implementing MG-87), include the ticket key in the **Relates to** field.
This creates a bidirectional trace: the ticket links to the code change,
and the ADR links to the ticket that motivated it.

## How to write a good ADR

1. **Be specific, not abstract.** Name the modules, the failure modes, the
   measurements. "This was slow" is weak. "ValidationActor subprocess
   spawned unbounded recursion because `os.environ` propagates
   `APP_DATA`" is strong.

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
