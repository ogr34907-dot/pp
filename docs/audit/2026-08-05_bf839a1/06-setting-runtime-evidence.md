# Setting Runtime Evidence

## Dynamic probe method

`setting_prompt_probe.py` constructed a real StoryPipeline-facing composition
path with a deterministic recording LLM provider. It used six non-production
markers: title, genre, theme, world rule, style, and taboo. The recording
provider observed two actual final prompts, including the real redispatch
path.

## Observed output

```json
{
  "provider_call_count": 2,
  "provider_prompt_marker_presence": [
    {"genre": false, "style": false, "taboo": false, "theme": true,
     "title": false, "world": false},
    {"genre": false, "style": false, "taboo": false, "theme": true,
     "title": false, "world": false}
  ],
  "context_builder_allocator_has_novel_repository": false,
  "legacy_backfill_written_keys": ["novel.setup.premise",
    "novel.setup.target_chapters", "novel.setup.target_words_per_chapter",
    "novel.setup.title"]
}
```

The probe also showed that the allocator narrative-promise block contained no
trace marker, and Bible setup backfill wrote only title, premise, target
chapters, and target words.

`setting_clear_probe.py` then set a genre marker, cleared it using the normal
update service, and read both Variable Hub forms:

```json
{
  "alias_value_after_clear": "TRACE_OLD_GENRE_SHOULD_BE_CLEARED",
  "canonical_value_after_clear": "TRACE_OLD_GENRE_SHOULD_BE_CLEARED",
  "setting_writes_after_clear": [],
  "stale_value_survives": true
}
```

This is dynamic confirmation for `SETTING-001` and `SETTING-002`, not an
inference from static call chains.
