# Canonical Aftermath Auto-Recovery Design

## Goal

Keep autopilot blocked until canonical chapter memory is committed, while recovering once from transient provider failures without presenting stale chapter data or retrying forever.

## Durable Source Of Truth

The latest completed chapter and its current `chapter_narrative_commits` row define the canonical gate. A status response must not let older shared-memory fields override a newer matching SQLite row. When SQLite is unavailable, the existing shared-memory fallback remains available.

The reconciler returns one of three states:

- `failed`: the latest prose version has a terminal failed claim;
- `ready`: the matching canonical and required memory state is committed;
- `unknown`: SQLite could not be read, so callers preserve the existing fallback.

## Shared-State Publication

When StoryPipeline pauses on `canonical_aftermath_not_ready`, it publishes the actual chapter number and persisted failure reason. When the matching canonical commit succeeds, it clears the pause reason, chapter number, failure reason, and stale review gate fields.

## Bounded Automatic Recovery

The existing canonical attempt loop remains capped at three attempts. If all three attempts end in a failure classified by `is_retryable_llm_error`, automatic mode runs one additional recovery cycle for that exact chapter hash and revision. The cycle reuses the existing CAS reclaim and ChapterAftermathPipeline.

The attempted recovery identity is persisted in `novels.autopilot_recovery_reason`. A failed recovery is marked exhausted for that chapter version, preventing daemon ticks or restarts from creating an unbounded loop. A new chapter hash/revision gets a new identity. Non-transient extraction, validation, version, and SQLite failures never receive the automatic extra cycle.

On success, the novel remains subject to the existing canonical advance CAS. The next writing pass recovers and applies the pending advance before generating another chapter. On failure, autopilot remains paused and the existing explicit retry action stays available.

## Verification

- A live status built with stale chapter 8 shared state and a durable chapter 63 failure reports chapter 63.
- A ready latest commit clears stale canonical failure fields.
- A transient terminal failure gets exactly one automatic recovery cycle.
- A hard failure and an already-exhausted chapter version remain paused.
- Recovery success never generates prose and never bypasses the canonical advance gate.

