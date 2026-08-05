# Frontend Function and UI Audit

## Confirmed wiring

The normal creation path is coherent:

```text
frontend/src/views/Home.vue
  -> frontend/src/api/novel.ts
    -> interfaces/api/v1/core/novels.py
      -> NovelService.create_novel()
        -> generation_prefs_json
```

The initial UI/HTTP model is not the reason locked settings disappear from the
final prose prompt. The break is downstream in backfill, context assembly,
and composition. This prevents duplicate or cosmetic frontend-only repairs.

The embedding configuration feedback requested in prior work already exists:
`GlobalLLMEntryButton.vue` emits the success message `嵌入配置已保存` after a
successful save. It is deliberately excluded from this repair scope.

## Functional UI risks and plan

The UI must still be exercised during final E2E for form refresh persistence,
error feedback, duplicate clicks, route changes, and autopilot live state.
The audit found no evidence that a broad visual rewrite would fix the P0/P1
issues. Therefore the required UI work is bounded to functional state and
feedback only:

- surface a durable restart-interruption reason in existing autopilot status
  projection when the backend adds it;
- retain a clear blocking error for failed safe structure confirmation;
- preserve existing embedding-save confirmation;
- add tests/build evidence instead of a speculative redesign.

No card/layout/theme overhaul is scheduled. Such work would change a large
surface without improving data safety or setting propagation.
