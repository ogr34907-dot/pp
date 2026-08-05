# 15. Dynamic E2E Evidence

## Real API + SQLite + deterministic Mock LLM

The acceptance test uses a real FastAPI application, real application services,
repositories and temporary SQLite database. Only the external model transport
is replaced by a deterministic implementation. It verifies:

1. Create a new test novel with synthetic setting values.
2. Reload and verify persisted title/genre/world/style/requirements fields.
3. Generate and confirm macro structure.
4. Generate and confirm three act chapters.
5. Generate, persist and reload a three-scene Beat Sheet.

This reached the same router/service/store handoffs used by the application;
it did not mock an upstream application service merely to assert local output.

## Long chapter-flow evidence

The 30 chapter regression exercises a real BaseStoryPipeline and
ChapterAftermathPipeline with persistent evidence, a controlled vector failure,
an extraction retry failure/recovery, restart, rewrite and ordered
auxiliary-stage drain. The 100 chapter variant performs one hundred actual
iterations and checks chapter-100 context for chapter-99 memory evidence.

## UI/API evidence

The isolated web preview retrieved data from the isolated backend, opened the
AI control surface, loaded embedding settings and submitted the current test
configuration. The DOM exposed one exact success message. No generation,
review acceptance, prose resume/retry or commit endpoint was called for the
protected review sessions.
