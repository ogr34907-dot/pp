# Do-Not-Change List

The following components are stable or lack evidence for a safe broad change.
They are excluded from the minimal repair batches.

1. Do not replace DDD, StoryPipeline, CPMS, EngineDaemon, SQLite, Write
   Dispatch, ChromaDB/FAISS, MemoryEngine, Evolution, or UnifiedCheckpoint.
2. Do not rewrite the existing canonical aftermath contract, its three-attempt
   retry policy, hash/revision CAS, `safe_snapshot`/`retain_prose` modes, or
   30/100 chapter fixtures merely because adjacent structure fixes are needed.
3. Do not introduce a new queue, database, memory store, prompt framework, or
   frontend state framework.
4. Do not add a global unique index that can fail on existing duplicate
   `story_nodes`; use a compatibility-safe new-write guard instead.
5. Do not rename published migration files or silently treat migration failure
   as success.
6. Do not solve destructive replanning by pretending the product already has
   prose branch isolation. The safe supported behavior is to block and pause.
7. Do not perform a broad frontend visual redesign, bulk API renaming, or
   formatting campaign during functional repair.
8. Do not modify, commit, delete, or regenerate `requirements-local.txt`,
   `.venv`, local embedding models, production SQLite files, logs, secrets,
   or the formal workspace.
9. Do not weaken validation or tests to make an old behavior appear to pass.
10. Defer global SSE protocol redesign and broad raw-cursor repository
    conversion until the P0/P1 data-integrity work has proof and a narrower
    implementation boundary.
