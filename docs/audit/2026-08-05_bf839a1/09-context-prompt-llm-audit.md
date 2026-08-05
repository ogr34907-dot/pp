# Context, Prompt, and LLM Audit

## Existing long-form continuity controls

The baseline already implements the scoped controls required by the previous
memory-stability work:

- `engine/pipeline/prose_composer.py` uses the full context request and a
  separate continuity block.
- `ContextBudgetAllocator` owns T0-T3 slot selection and re-counting.
- `ChapterProseInvocationComposer` routes through CPMS.
- Chapter prose has an explicit AI invocation contract.
- Provider-spy observability tests cover the actual generated request.
- The 30 chapter regression exercises a real StoryPipeline and aftermath path.

The passed focused regression was:

```powershell
.\.venv\Scripts\python.exe -m pytest `
  tests\unit\engine\test_memory_stability_observability.py::test_thirty_chapter_regression_recovers_injected_vector_failure -q
```

Result: `1 passed in 7.23s` on the baseline.

## Remaining prompt-boundary failure

The settings probe shows that a structurally sound T0-T3 system does not help
when onboarding values never enter the relevant slots. The prose contract
currently binds only `novel_title`, `target_words`, `chapter_outline`, and
`continuity_context`; it cannot recover omitted locked genre, world, style,
and special requirements by itself.

`engine/runtime/writing_delegate.py` also reads `getattr(novel, "genre", "")`
instead of `novel.generation_prefs.locked_genre`. The repair for
`SETTING-001` must use authoritative preferences consistently and place the
result in a separately budgeted settings/narrative-promise block. It must not
replace CPMS or add an unbounded prompt bypass.

## Acceptance evidence required after repair

The same recording-provider probe must assert all six markers in both final
provider calls, while the final allocator token count remains within the
requested budget. Database storage or an intermediate metadata dictionary is
not sufficient evidence.
