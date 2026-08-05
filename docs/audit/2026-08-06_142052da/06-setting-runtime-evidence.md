# 06. Setting Runtime Evidence

## Isolated macro-planning evidence

The audit used unique synthetic setting markers and a deterministic provider.
The real AI Invocation/CPMS path resolved all 16 worldbuilding text leaves and
all 3 saved locations into the macro planning request. This establishes that the
repaired worldbuilding.content and locations.list bindings are not
template-only declarations.

## Isolated writer-context evidence

For a chapter whose outline did not name a saved location, the context
allocator emitted exactly one T1 LOCATION_CATALOG carrier. It is bounded to
eight ordered items and 600 tokens; an outline-selected location stays in the
existing scene-specific hint rather than being duplicated in the catalog.

## Review/resume evidence

A review-gated prose session was prepared with a full explicit context value
and then supplied a deliberately stale chapter-scoped Hub replacement. The
GET/review render and actual Mock LLM request after resume retained the
prepared value and excluded the stale value. Hub-backed and explicitly edited
aliases still refresh according to their existing precedence rules.

## Evidence safeguards

- Stored test markers and aggregate counts are recorded, not raw user prompt
  text, private settings or API credentials.
- The pre-existing review sessions identified during earlier reproduction were
  not accepted, resumed, retried or committed during this completion audit.
- Dynamic tests used only the isolated 8015 backend runtime and temporary
  SQLite files under the ignored audit directory.
