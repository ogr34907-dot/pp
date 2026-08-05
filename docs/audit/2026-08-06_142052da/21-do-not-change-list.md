# 21. Do Not Change List

The following were intentionally retained because current evidence does not
justify broader modification:

- DDD layers, FastAPI, Vue, SQLite, Write Dispatch, CPMS and the existing
  StoryPipeline/EngineDaemon architecture.
- Strict MemoryDeltaPayload validation and three-attempt canonical failure
  behavior. Making invalid Mock output acceptable would hide a real contract
  fault.
- The existing chapter-narrative-sync canonical contract and vector
  post-commit retry semantics.
- Graph entity extraction in ContextBudgetAllocator: it has a live graph
  subnetwork caller and is not unused code.
- Legacy emergency writing mode and compatibility import surfaces; default
  production behavior remains StoryPipeline.
- Broad SQLite repository migration, global transaction rewrite, queue
  replacement, or a new memory/vector service.
- Full UI visual redesign or API contract renaming. This batch makes only
  functional/information-feedback changes.
- Tauri browser-preview probe warning. It is P3/no-change pending a
  desktop-shell-specific reproduction.
- requirements-local.txt, local models, .venv, databases, runtime data, logs,
  screenshots, build output and raw prompts. They must remain local.
