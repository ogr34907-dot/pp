# 10. Memory and Continuity Audit

## Canonical commit contract

chapter-narrative-sync remains the only canonical chapter extraction contract.
A successful canonical result requires matching content identity, valid
structured output and confirmed critical writes. A no-op extraction is valid;
a write failure is propagated. Vector indexing occurs after canonical commit
and is retryable without pretending that an uncommitted extraction succeeded.

## Repairs verified

- Mock memory-extraction dispatch now returns only completed_beats,
  revealed_clues and fact_violations, conforming to the existing strict
  MemoryEngine model.
- State cache tests cover reuse, LRU eviction, expiry and disabled caching.
- Completed beats and revealed clues render recent valid entries within the
  configured 1000/800 token context limits; storage soft limits retain the
  most recent 500/800 records.
- Required memory persistence failure does not cache an uncommitted mutated
  state or allow a false advance.
- Rewriting old prose pauses the mainline and guards asynchronous aftermath
  writes by content hash/revision; a stale job is discarded.
- retain_prose replay selects the last nonempty retained chapter rather than
  the last planned structural row.

## Long-flow evidence

The explicit 30 and 100 chapter tests prove continuity state survives restart,
resists vector/indexing transient failure, remains paused on extraction
failure, and resumes only through the correct replay barrier.
