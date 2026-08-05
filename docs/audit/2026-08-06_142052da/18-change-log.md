# 18. Change Log

| Issue set | Files | Minimal change |
| --- | --- | --- |
| MEMORY-001 / Mock E2E | infrastructure/ai/providers/mock_provider.py, provider tests | add deterministic contract-specific response dispatch for memory extraction, macro/act/beat/prose/sync flows |
| MEMORY-002 | application/core/services/chapter_rewrite_coordinator.py, coordinator tests | choose last retained nonempty prose chapter as replay head |
| ONBOARD-001 / BLUEPRINT-003 | setup outline continuation, Vue setup guide/model, tests | separate preview from persistence and bound stage ranges to target chapters |
| SETTING-003 | autopilot planning contract and tests | bind worldbuilding content and locations through existing Variable Hub schema |
| SETTING-004 | context allocator/providers and tests | add bounded T1 canonical location/faction catalog |
| PROMPT-001 | AI Invocation routes and route tests | preserve explicit session values across review refresh while retaining Hub/user-edit precedence |
| MANUSCRIPT-001/002 | manuscript repository/routes and integration tests | use unified prop/character sources and synchronous visibility for response-bearing compatibility writes |
| API-001 | manuscript entity route and integration tests | turn whitespace-only holder validation into the established HTTP 422 contract |
| FRONTEND-001 | StoryStructureTree.vue | show existing planning command in a non-autopilot empty structure state |
| Daemon test hygiene | daemon manager/tests | allow explicit isolated orphan-cleanup opt-out and limit cleanup to current interpreter scope |

All changes stay within existing modules and contracts. No schema-wide rewrite,
new persistence service, queue, alternate memory engine, or API redesign was
added.
