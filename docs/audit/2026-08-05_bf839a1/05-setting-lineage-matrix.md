# Setting Lineage Matrix

This matrix distinguishes persistence from actual final-provider inclusion.
The audit used six unique trace markers and a recording LLM provider. A check
means the behavior is implemented on the audit baseline; a cross means a
confirmed break.

| Stage | Title | Genre | Theme | World | Style | Taboo |
| --- | --- | --- | --- | --- | --- | --- |
| UI request / API DTO | yes | yes | yes | yes | yes | yes |
| `generation_prefs_json` persistence | yes | yes | yes | yes | yes | yes |
| Correct aggregate source | `Novel.title` | `generation_prefs` | `generation_prefs` | `generation_prefs` | `generation_prefs` | `generation_prefs` |
| Legacy Variable Hub backfill | yes | no | no | no | no | no |
| Bible setup backfill | yes | no | no | no | no | no |
| `ContextBudgetAllocator` narrative promise | no | no | no | no | no | no |
| Built StoryPipeline context | no | no | yes | no | no | no |
| Final provider prompt, first call | no | no | yes | no | no | no |
| Final provider prompt, retry call | no | no | yes | no | no | no |
| Clearing a previously set value | no | no | no | no | no | no |

The apparent theme success is not sufficient: it does not make the other
locked values reliable and it does not prove consistent generation semantics.
The mandatory end-to-end acceptance criterion is the last two Provider Prompt
rows, not merely database persistence or intermediate context construction.

## Required target behavior

1. Every reader takes locked creative values from `Novel.generation_prefs`.
2. Variable Hub writes update both canonical and compatibility alias keys.
3. Empty values overwrite old values; they are not skipped.
4. The context allocator receives the novel repository and can form its
   narrative-promise slot from authoritative preferences.
5. The chapter-prose invocation contract explicitly carries the assembled
   settings block so that CPMS cannot silently omit it.
6. Provider-spy assertions prove inclusion on both initial and retry calls.
