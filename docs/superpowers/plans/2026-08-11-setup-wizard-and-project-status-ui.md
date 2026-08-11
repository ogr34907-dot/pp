# Complete Setup Wizard and Project Status UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make all four new-book wizard AI stages show truthful lifecycle progress, and make project cards reflect explicit autopilot terminal states.

**Architecture:** Do not alter execution APIs. A shared pure mapper translates an Invocation lifecycle into stage copy, elapsed time, and a bounded preview of the already-persisted real model stream; one reusable progress surface serves worldbuilding, characters, locations, and plot outline. A second pure mapper projects persisted novel lifecycle plus recovery reason into the Home-card label.

**Tech Stack:** Vue 3, TypeScript, Vitest, Python dataclasses, pytest.

## Global Constraints

- Implement only in `W:\novel\test` until verification and commits finish.
- Do not change Invocation policy, prompts, models, routes, SSE payloads, or autopilot stop semantics.
- Never show invented completion percentages for one-shot model tasks.
- Keep normal stopped lifecycle labels when no explicit recovery reason exists.
- Use red/green tests before each production change.

---

### Task 1: Expose and project terminal autopilot state

**Files:**
- Create: `frontend/src/domain/projectCardStatus.ts`
- Create: `frontend/src/domain/projectCardStatus.spec.ts`
- Modify: `application/core/dtos/novel_dto.py`
- Modify: `tests/unit/application/core/test_novel_preset_persistence.py`
- Modify: `frontend/src/api/novel.ts`
- Modify: `frontend/src/views/Home.vue`

**Interfaces:**
- Consumes `stage`, `autopilot_status`, and `autopilot_recovery_reason`.
- Produces `getProjectCardStatusPresentation(input): { key; label; tagType }`.

- [ ] **Step 1: Write a failing DTO test**

```python
def test_novel_dto_exposes_explicit_autopilot_recovery_reason():
    novel = Novel(
        id=NovelId("novel-1"), title="测试小说", author="作者", target_chapters=10,
        autopilot_recovery_reason="manual_terminate",
    )
    assert NovelDTO.from_domain(novel).autopilot_recovery_reason == "manual_terminate"
```

- [ ] **Step 2: Verify DTO RED**

Run: `W:\novel\test\.venv\Scripts\python.exe -m pytest tests/unit/application/core/test_novel_preset_persistence.py -q`

Expected: the DTO lacks the field.

- [ ] **Step 3: Implement the DTO field**

Add `autopilot_recovery_reason: str = ""` and populate it from the existing domain value; no persistence or route edit.

- [ ] **Step 4: Verify DTO GREEN**

Run: `W:\novel\test\.venv\Scripts\python.exe -m pytest tests/unit/application/core/test_novel_preset_persistence.py -q`

Expected: PASS.

- [ ] **Step 5: Write failing card-projection tests**

```ts
expect(getProjectCardStatusPresentation({
  stage: 'writing', autopilotStatus: 'stopped', recoveryReason: 'manual_terminate',
})).toMatchObject({ key: 'terminated', label: '已终止' })
expect(getProjectCardStatusPresentation({
  stage: 'writing', autopilotStatus: 'stopped', recoveryReason: 'manual_pause',
})).toMatchObject({ key: 'paused', label: '已暂停' })
expect(getProjectCardStatusPresentation({
  stage: 'reviewing', autopilotStatus: 'stopped', recoveryReason: '',
})).toMatchObject({ key: 'reviewing', label: '审稿中' })
```

Also test that `error` overrides a stale writing label.

- [ ] **Step 6: Verify card-projection RED**

Run: `npm run test:unit -- src/domain/projectCardStatus.spec.ts`

Expected: FAIL because the mapper does not exist.

- [ ] **Step 7: Implement the shared card mapper and consume it in both Home grids**

Precedence is `error`, `manual_terminate`, `manual_pause`, then current lifecycle presentation. Extend the frontend DTO, map each fetched novel once, and render `statusKey`, `statusLabel`, and `statusTagType` in both grids. Add warm semantic dot styles for paused, terminated, and error.

- [ ] **Step 8: Verify card GREEN**

Run: `npm run test:unit -- src/domain/projectCardStatus.spec.ts`

Expected: PASS.

- [ ] **Step 9: Commit Task 1**

```powershell
git add application/core/dtos/novel_dto.py tests/unit/application/core/test_novel_preset_persistence.py frontend/src/api/novel.ts frontend/src/domain/projectCardStatus.ts frontend/src/domain/projectCardStatus.spec.ts frontend/src/views/Home.vue
git commit -m "fix(ui): reflect explicit autopilot terminal states"
```

### Task 2: Create reusable truthful Invocation progress presentation

**Files:**
- Create: `frontend/src/onboarding/onboardingInvocationProgress.ts`
- Create: `frontend/src/onboarding/onboardingInvocationProgress.spec.ts`
- Create: `frontend/src/components/onboarding/OnboardingTaskProgress.vue`
- Create: `frontend/src/components/onboarding/OnboardingTaskProgress.spec.ts`

**Interfaces:**
- Consumes task kind (`worldbuilding | characters | locations | plot_outline`), Invocation session status, commit status, elapsed seconds, and persisted `attempt.content`.
- Produces phase copy (`creating | generating | validating | committing | completed | failed`) plus task-specific title, detail, and a bounded stream preview.

- [ ] **Step 1: Write failing shared-mapper tests**

```ts
expect(getOnboardingInvocationPresentation({
  task: 'characters', sessionStatus: 'generating',
})).toMatchObject({ phase: 'generating', message: 'AI 正在生成人物' })
expect(getOnboardingInvocationPresentation({
  task: 'locations', sessionStatus: 'awaiting_commit',
})).toMatchObject({ phase: 'committing' })
expect(getOnboardingInvocationPresentation({
  task: 'plot_outline', sessionStatus: 'completed', commitStatus: 'succeeded',
})).toMatchObject({ phase: 'completed', isTerminal: true })
expect(formatInvocationElapsedSeconds(72)).toBe('已等待 1 分 12 秒')
expect(clipInvocationStreamPreview('甲'.repeat(1201), 1200)).toBe(`…${'甲'.repeat(1200)}`)
```

- [ ] **Step 2: Verify shared-mapper RED**

Run: `npm run test:unit -- src/onboarding/onboardingInvocationProgress.spec.ts`

Expected: FAIL because the mapper does not exist.

- [ ] **Step 3: Implement the pure mapper**

Map pre-call to task preparation, `generating` to model output, `awaiting_acceptance` to validation, `awaiting_commit` or `committing` to writing, and completed / failed / cancelled / blocked to terminal states. Generate distinct but truthful labels for the four task kinds, including worldbuilding’s “五个维度会在校验后统一写入”. Add `clipInvocationStreamPreview(content, maxLength)` that returns an empty value before the first real chunk and otherwise returns the bounded trailing content with a leading truncation marker.

- [ ] **Step 4: Verify shared-mapper GREEN**

Run: `npm run test:unit -- src/onboarding/onboardingInvocationProgress.spec.ts`

Expected: PASS.

- [ ] **Step 5: Write failing reusable component SSR test**

```ts
const html = await renderToString(createSSRApp({
  render: () => h(OnboardingTaskProgress, {
    task: 'worldbuilding', phase: 'generating', elapsedSeconds: 72,
  }),
}))
expect(html).toContain('AI 正在生成完整世界观')
expect(html).toContain('已等待 1 分 12 秒')
expect(html).toContain('aria-live="polite"')
expect(html).toContain('实时生成片段')
```

- [ ] **Step 6: Verify component RED**

Run: `npm run test:unit -- src/components/onboarding/OnboardingTaskProgress.spec.ts`

Expected: FAIL because the component does not exist.

- [ ] **Step 7: Implement the reusable progress component**

Render four real lifecycle milestones (创建任务、生成内容、校验结构、写入结果), a `role="status"` / polite live region, and elapsed time. Render a safe `pre`-style “实时生成片段” only when the current `attempt.content` has real text; otherwise say “等待首段输出”. Only the actual current lifecycle milestone is active; no numeric completion percentage is rendered.

- [ ] **Step 8: Verify component GREEN**

Run: `npm run test:unit -- src/onboarding/onboardingInvocationProgress.spec.ts src/components/onboarding/OnboardingTaskProgress.spec.ts`

Expected: PASS.

- [ ] **Step 9: Commit Task 2**

```powershell
git add frontend/src/onboarding/onboardingInvocationProgress.ts frontend/src/onboarding/onboardingInvocationProgress.spec.ts frontend/src/components/onboarding/OnboardingTaskProgress.vue frontend/src/components/onboarding/OnboardingTaskProgress.spec.ts
git commit -m "feat(ui): add shared onboarding invocation progress"
```

### Task 3: Integrate shared progress across all four wizard stages

**Files:**
- Modify: `frontend/src/components/onboarding/NovelSetupGuide.vue`
- Modify: `frontend/src/components/onboarding/WizardSkeleton.vue`
- Create: `frontend/src/components/onboarding/WizardSkeleton.spec.ts`

**Interfaces:**
- Consumes the Task 2 mapper/component and current `aiInvocationStore.onSessionUpdate` subscriptions.
- Produces a single current-stage Invocation progress state with safe timer and subscription cleanup.

- [ ] **Step 1: Write a failing WizardSkeleton SSR test**

SSR-render `WizardSkeleton` in Invocation mode and assert it says `完整设定生成中`, exposes a polite live region, and does not render the misleading `0 / 5` text.

- [ ] **Step 2: Verify WizardSkeleton RED**

Run: `npm run test:unit -- src/components/onboarding/WizardSkeleton.spec.ts`

Expected: FAIL because the Invocation mode is absent.

- [ ] **Step 3: Implement one active Invocation timer in the wizard**

Start it only when each stage receives `approval_required`; map every `onSessionUpdate` payload through the Task 2 mapper and feed the persisted `attempt.content` to the shared preview. Clear the timer on each stage’s success, failure, retry/restart, close, and unmount. Ensure a completion callback only changes its own stage, not a later stage.

- [ ] **Step 4: Render the common component in all four generation surfaces**

Worldbuilding replaces the `0 / 5` and 0% line in Invocation mode and shows raw real content until JSON fields become parseable. Characters and locations show the same stream preview until actual incremental SSE objects become parseable, then retain their existing cards. Plot outline replaces its isolated status wording with the same milestones, elapsed time, and already-existing live content preview while preserving outline cache behavior.

- [ ] **Step 5: Update WizardSkeleton Invocation mode**

When the worldbuilding call is an Invocation, describe all five dimensions as being generated together and awaiting validation; when genuine dimension SSE is present, retain the existing per-dimension view.

- [ ] **Step 6: Verify focused frontend tests GREEN**

Run: `npm run test:unit -- src/onboarding/onboardingInvocationProgress.spec.ts src/components/onboarding/OnboardingTaskProgress.spec.ts src/components/onboarding/WizardSkeleton.spec.ts`

Expected: PASS.

- [ ] **Step 7: Commit Task 3**

```powershell
git add frontend/src/components/onboarding/NovelSetupGuide.vue frontend/src/components/onboarding/WizardSkeleton.vue frontend/src/components/onboarding/WizardSkeleton.spec.ts
git commit -m "fix(ui): show progress for every onboarding generation stage"
```

### Task 4: Verify, smoke-test, and synchronize

**Files:**
- Modify: no production files unless a new red/green failure requires it.

- [ ] **Step 1: Run automated verification**

```powershell
W:\novel\test\.venv\Scripts\python.exe -m pytest tests/unit/application/core/test_novel_preset_persistence.py tests/unit/interfaces/api/v1/test_autopilot_manual_controls.py -q
npm run test:unit
npm run lint
npm run build
git diff --check
```

Expected: every command exits 0.

- [ ] **Step 2: Run non-mutating browser smoke checks**

Verify that the existing manually terminated `山河未断` shows “已终止” and ordinary planning remains “规划中”. Verify the shared progress component’s four tasks through SSR with a growing real-content fixture and component visual inspection without starting, stopping, saving, syncing, or generating against existing user novels.

- [ ] **Step 3: Sync only after clean evidence**

```powershell
git -C W:\novel\test status --short
git -C W:\novel\PlotPilot status --short
git -C W:\novel\PlotPilot fetch W:\novel\test codex/plotpilot-memory-stability
git -C W:\novel\PlotPilot merge --ff-only FETCH_HEAD
```

Expected: test clone clean; workspace fast-forwards without overwriting user changes.
