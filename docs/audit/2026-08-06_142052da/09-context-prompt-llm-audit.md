# 09. Context, Prompt and LLM Audit

## Context hierarchy retained

- T0: Bible/locked facts and hard constraints.
- T1: valid summaries, worldbuilding and bounded reusable context such as the
  new location catalog.
- T2: recent chapter handoff/continuity material.
- T3: filtered retrieval evidence; it cannot override T0/T1 facts.

ContextBudgetAllocator remains the sole allocation ledger. It uses existing
token estimation and slot max-token enforcement; the new location catalog is a
bounded slot rather than an untracked prompt append.

## Prompt integrity repair

StoryPipeline supplies a complete prepared context_text to prose composition.
The AI Invocation review router now preserves persisted explicit variable-plan
values across GET, draft preview, draft save, resume and retry. It still
refreshes Hub-owned values and gives a Variables API edit precedence for its
explicit alias.

## Provider-boundary evidence

The regression test captures the actual Prompt passed to the streaming Mock LLM
after resume. It verifies the full prepared marker is present and the stale Hub
marker is absent. This proves the repaired value reaches the model boundary
rather than only a template or intermediate object.
