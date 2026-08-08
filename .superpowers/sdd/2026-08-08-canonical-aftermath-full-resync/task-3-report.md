# Task 3 Report: Frontend Full-Resync Control And Progress

## Implementation

- Added `apiRoutes.autopilot.canonicalAftermathResyncAll()` for `POST /api/v1/autopilot/{novel_id}/canonical-aftermath/resync-all`.
- Added `autopilotApi.consumeCanonicalAftermathFullResync()` with typed lifecycle handlers, AbortSignal support, CRLF/LF and multiline `data:` SSE parsing, and `HttpError` propagation for non-2xx responses.
- Added the canonical gate's full-resync state model and panel control. The panel shows an icon-plus-text `全章重同步` action only for a canonical gate in stopped/paused state, a stable Naive UI progress bar, processed/total, current chapter, synced/skipped/vector-failed counters, and the first failure reason.
- Resume/start controls are disabled while the stream is active. Completion refreshes `/status` and explicitly does not call resume; cancellation leaves the paused state visible.

## TDD Evidence

RED command:

```powershell
Set-Location frontend
npm run test:unit -- src/api/autopilotFullResync.spec.ts src/components/autopilot/canonicalAftermathFullResync.spec.ts
```

RED output: `4 failed` as expected (`consumeCanonicalAftermathFullResync` and `createCanonicalAftermathFullResyncState` were not yet defined).

Focused GREEN/regression command:

```powershell
npm run test:unit -- src/api/autopilotFullResync.spec.ts src/components/autopilot/canonicalAftermathFullResync.spec.ts src/api/autopilotCanonicalAftermath.spec.ts src/components/autopilot/canonicalAftermathGate.spec.ts
```

GREEN output: `4 passed`, `6 passed`.

Additional verification:

- `npx vue-tsc -b`: passed.
- `npm run lint -- --no-warn-ignored`: passed.
- `git diff --check`: passed.

## Commit

Implementation commit SHA: `aa61ae3c`.

## Concerns

- The browser stream consumer stops dispatching after the first terminal (`failed`, `completed`, or `cancelled`) event; the backend contract treats those as terminal frames.
- The panel keeps the first failure chapter/reason for display while later failure frames cannot overwrite that first-failure diagnostic.

## Reviewer Fix Round 1

- Made full-resync progress visibility depend on local active-run state or retained first-failure diagnostics, so a transient `/status` pause-marker change cannot hide an active stream.
- Added `failure_reason` to the typed SSE event and made it the first-choice failure diagnostic; the first failure chapter is retained as well.
- Added focused coverage for local visibility and `failure_reason` precedence.

Verification:

```powershell
npm run test:unit -- src/api/autopilotFullResync.spec.ts src/components/autopilot/canonicalAftermathFullResync.spec.ts src/api/autopilotCanonicalAftermath.spec.ts src/components/autopilot/canonicalAftermathGate.spec.ts
```

Output: `4 passed`, `7 passed`.

```powershell
npx vue-tsc -b
npm run lint -- --no-warn-ignored
git diff --check
```

All passed.
