# 05. Setting Lineage Matrix

The matrix records the highest available evidence level for critical new-book
setting families. Dynamic means a test or isolated API path verified the
behavior; it does not expose private prompt text.

| Field family | UI/API input | Durable source | Planning path | Writer context / final request | Status | Evidence |
| --- | --- | --- | --- | --- | --- | --- |
| title, premise, genre, style and special requirements | home/setup form -> novel API | novel and setup values | continuous planning | existing CPMS/Variable Hub bindings | complete for audited path | Mock API acceptance persists and reloads marker values |
| target chapters | setup guide -> preferences | GenerationPreferences / novel | phase normalization and capacity checks | chapter numbering constraints | complete | short-target continuation tests and frontend type/build checks |
| plot outline | setup guide -> save/confirm endpoints | setup outline then StoryNode structure | macro/act/chapter planning | chapter outlines | effective after save/confirm | ONBOARD-001 regression and Mock API chain |
| worldbuilding content | worldbuilding panel / setup data | Bible/worldbuilding repository | worldbuilding.content Variable Hub binding | canonical macro request | complete | macro contract test and isolated prompt evidence |
| locations and factions | Bible locations | Bible repository | locations.list Variable Hub binding | scene hint plus bounded LOCATION_CATALOG T1 slot | complete | context allocator unit/API evidence, 3/3 saved locations observed |
| characters and props | Bible/workbench APIs | unified character/prop repositories | character/context projection | writer context and canonical aftermath | complete for compatibility path | manuscript FastAPI/SQLite integration tests |
| frozen prose context | StoryPipeline prepared explicit input | persisted invocation variable plan | not applicable | review/preview/resume/retry final prompt | complete | resumed Mock LLM request retains explicit marker and rejects stale Hub value |
| embedding configuration | AI control panel -> settings API | existing settings store | not applicable | vector runtime selection | complete for save feedback | browser observed successful save feedback |

The matrix does not claim that every optional setting has an unconditional use
in every chapter. Context policy may omit an irrelevant field; audited defects
were missing carriers and incorrect replacement of core persisted values.
