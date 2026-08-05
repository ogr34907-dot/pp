# 04. Onboarding Setting Inventory

## Audit scope

The onboarding and creation paths were traced across the Vue guide, API DTOs,
application services, domain persistence and CPMS variables. The inventory
groups fields by their existing ownership rather than inventing a second
configuration model.

| Setting family | Primary UI/API owner | Durable owner | Writing/planning consumers |
| --- | --- | --- | --- |
| identity and premise | home creation form / novel API | Novel and novel repository | macro plan, chapter context |
| genre, world preset and style | setup guide / novel setup values | locked novel/setup values | macro prompt, writer context |
| target length and chapter target | setup guide / GenerationPreferences | novel generation preferences | phase ranges, volume/act capacity |
| worldbuilding and locations | Bible/worldbuilding panels | Bible repository | macro contract, T1 writer slots |
| characters, factions, props and foreshadowing | Bible and workbench panels | unified repositories/Bible | character projection, context and aftermath |
| plot outline and stage plan | setup guide / planning API | setup outline plus StoryNode tree after confirmation | planning and structure editor |
| model and embedding configuration | AI control panel / settings API | existing settings storage | provider/vector runtime only |

## Current audit conclusions

- New-book critical data is not treated as effective merely because it is
  stored. The acceptance chain requires persistence, reread, runtime carrier,
  rendered Prompt or structured planner request, and provider-boundary proof.
- ONBOARD-001 is repaired: an AI-produced plot outline is marked previewed
  until the supported save succeeds, rather than being shown as already
  durable.
- BLUEPRINT-003 is repaired: stage ranges derive from the actual target
  chapter count, including short books, and manual edits cannot exceed that
  target.
- SETTING-003 and SETTING-004 are repaired: worldbuilding/locations now have
  explicit planning and writer-context carriers.

No test run wrote a real user novel or used a real LLM. Dynamic evidence used
isolated test records and deterministic Mock responses only.
