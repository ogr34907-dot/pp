# Onboarding and Setting Inventory

## Authoritative persistence model

The new-novel UI submits to `interfaces/api/v1/core/novels.py`, which delegates
to `application/core/services/novel_service.py`. The service constructs and
persists a `Novel` aggregate whose locked creative settings live in
`Novel.generation_prefs`, serialized as `novels.generation_prefs_json`.

This is the authoritative model for the following onboarding values:

| User-facing value | Authoritative field | Variable Hub canonical key |
| --- | --- | --- |
| Title | `Novel.title` | `novel.title` |
| Premise | `Novel.premise` | `novel.premise` |
| Target chapters | `Novel.target_chapters` | `novel.target_chapters` |
| Target words | `Novel.target_words_per_chapter` | `novel.target_words_per_chapter` |
| Genre | `generation_prefs.locked_genre` | `novel.genre_label` |
| World preset | `generation_prefs.locked_world_preset` | `novel.world_preset` |
| Story structure | `generation_prefs.locked_story_structure` | `novel.story_structure` |
| Pacing | `generation_prefs.locked_pacing_control` | `novel.pacing_control` |
| Writing style | `generation_prefs.locked_writing_style` | `novel.writing_style` |
| Special requirements / taboo | `generation_prefs.locked_special_requirements` | `novel.special_requirements` |

The Variable Hub also exposes `novel.setup.*` aliases. They are compatibility
keys, not a separate source of truth.

## Confirmed breaks

Several consumers still use top-level `Novel` attributes such as
`novel.locked_genre` that do not exist on the normal aggregate. The affected
paths include `application/ai_invocation/variable_backfill.py`,
`interfaces/api/v1/world/bible.py`,
`application/blueprint/services/setup_context_builder.py`, and
`engine/runtime/writing_delegate.py`.

`NovelService._sync_variable_hub_from_novel()` reads the correct
`generation_prefs` fields, but it skips blank values. Therefore clearing a
setting does not overwrite an old alias or canonical value. This turns a
historical setting into hidden prompt state for later requests.

The frontend embedding configuration save feedback is already implemented in
`frontend/src/components/global/GlobalLLMEntryButton.vue` through
`message.success('嵌入配置已保存')`; it is not an open issue in this audit.
