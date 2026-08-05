# API and SSE Contract Audit

## REST Boundaries

The new-novel API accepts generation preferences and maps them to the domain
aggregate. The direct next-act endpoint reaches
`ContinuousPlanningService.create_next_act_auto()`, so capacity, parent, and
number checks are enforced at the shared service boundary rather than by a
new HTTP contract. The repair set preserves endpoint names and request DTO
shapes while returning existing validation/domain failure paths for unsafe
operations.

The FastAPI plus deterministic-Mock-LLM acceptance test verifies the following
against a temporary SQLite database: novel creation, unique setting save and
refresh, macro planning, safe macro confirmation, act/chapter structure
confirmation, Beat Sheet persistence, and reload. It deliberately mocks only
the external LLM boundary; project routing, services, repositories, and
SQLite remain real.

## SSE-001 Resolution

The audit found that only terminal logs had an application cursor, while
general autopilot/DAG events lacked standard SSE IDs, resume handling, and
client-side deduplication. The bounded repair now provides stable event IDs,
cursor/replay behavior, and frontend store deduplication/state calibration.
The relevant backend and store contract tests cover event framing, duplicate
suppression, replay ordering, and restart-state projection.

This is intentionally a narrow protocol repair. It does not replace the
existing EventSource transport, introduce a second event system, or change
unrelated REST shapes. The final frontend validation is type check plus
production build because the package currently provides no independent
frontend lint or test script.

## Contract Limits

No live browser session was used to claim a full visual UI acceptance flow.
The acceptance evidence is classified accurately in
`20-e2e-acceptance.md`; API and SSE behavior are covered by project tests,
while a real-browser UI plus Mock-LLM autopilot run remains a non-blocking
future acceptance expansion.
