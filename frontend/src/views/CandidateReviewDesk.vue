<template>
  <main class="review-desk" aria-labelledby="review-desk-title">
    <header class="review-desk__header">
      <div>
        <p class="review-desk__eyebrow">候选层 · 未通过内容不会进入正文、事实、记忆或向量</p>
        <h1 id="review-desk-title">独立审稿台</h1>
        <p>审核完整章节，而不是被剧本与节拍逐项打断。逐章模式下，下一章在通过前绝不会预取。</p>
      </div>
      <div class="review-desk__header-actions">
        <n-button secondary @click="refresh" :loading="loading">刷新权威状态</n-button>
        <n-button secondary @click="router.push(`/book/${novelId}/outline`)">大纲工作室</n-button>
        <n-button type="primary" @click="router.push(`/book/${novelId}/workbench`)">返回工作台</n-button>
      </div>
    </header>

    <n-alert v-if="error" type="error" :show-icon="true" role="alert" class="review-desk__alert">{{ error }}</n-alert>
    <section v-if="presentation" class="review-state" :class="`is-${presentation.tone}`" aria-live="polite">
      <n-icon :component="stateIcon" :size="18" aria-hidden="true" />
      <div><strong>{{ presentation.label }}</strong><span>{{ presentation.detail }}</span></div>
      <n-button v-if="run?.canonical_sync_status === 'failed' && candidate" size="small" type="error" secondary :loading="actionLoading" @click="retrySync">
        重试当前章同步
      </n-button>
      <n-button v-if="continuationError" size="small" secondary :loading="actionLoading" @click="retryContinuation">
        重试自动继续
      </n-button>
    </section>

    <nav class="review-mobile-tabs" aria-label="审稿台面板">
      <button v-for="tab in mobileTabs" :key="tab.value" type="button" :class="{ 'is-active': mobileTab === tab.value }" :aria-pressed="mobileTab === tab.value" @click="mobileTab = tab.value">
        <n-icon :component="tab.icon" :size="17" aria-hidden="true" />{{ tab.label }}
      </button>
    </nav>

    <div v-if="candidate" class="review-desk__grid" :class="`mobile-tab--${mobileTab}`">
      <aside class="review-panel review-contract" aria-label="当前五级大纲契约">
        <div class="review-panel__head"><span class="review-panel__kicker">计划契约</span><h2>必须与禁止</h2></div>
        <div class="review-contract__chapter"><span>候选章节</span><strong>第 {{ candidate.chapter_number }} 章 · {{ candidate.title }}</strong></div>
        <section v-for="level in orderedLevels" :key="level" class="contract-level">
          <div class="contract-level__head"><span>{{ levelLabels[level] }}</span><small>r{{ candidate.outline_chain[level]?.revision ?? '—' }}</small></div>
          <p>{{ candidate.outline_chain[level]?.payload?.creative_goal || candidate.outline_chain[level]?.payload?.narrative_text || '暂无说明' }}</p>
          <ul v-if="level === 'chapter' && requiredEvents.length"><li v-for="event in requiredEvents" :key="event">必须：{{ event }}</li></ul>
          <ul v-if="level === 'chapter' && forbiddenEvents.length" class="contract-level__forbidden"><li v-for="event in forbiddenEvents" :key="event">禁止：{{ event }}</li></ul>
        </section>
        <n-button block secondary @click="router.push(`/book/${novelId}/outline`)">
          <template #icon><n-icon :component="GitNetworkOutline" /></template>修订大纲草稿
        </n-button>
      </aside>

      <section class="review-panel review-manuscript" aria-label="候选正文">
        <div class="review-panel__head review-manuscript__head">
          <div><span class="review-panel__kicker">候选正文</span><h2>第 {{ candidate.chapter_number }} 章</h2></div>
          <span class="review-revision">正文 r{{ candidate.content_revision }} · {{ candidate.author_content !== null && candidate.author_content !== undefined ? '作者修订' : 'AI 原稿' }}</span>
        </div>
        <n-alert v-if="!candidate.audit_is_current" type="warning" :show-icon="true" class="review-stale-alert">
          正文已改动，审校和提交清单已过期。重新审校前不能正式提交。
        </n-alert>
        <n-input v-model:value="editorContent" type="textarea" :autosize="false" class="review-manuscript__editor" aria-label="候选正文编辑器" />
        <div class="review-manuscript__footer">
          <span>{{ wordCount }} 字</span>
          <n-space wrap>
            <n-button secondary :disabled="editorContent === candidate.final_content" :loading="actionLoading" @click="saveContent">保存作者修订</n-button>
            <n-button secondary :loading="actionLoading" @click="regenerate">按意见重新生成</n-button>
          </n-space>
        </div>
        <n-input v-model:value="feedback" type="textarea" :rows="2" placeholder="给重新生成的修改意见（可选）。只有你主动点“重新生成”才会消耗这一章的 Token。" aria-label="修改意见" />
        <details class="review-versions">
          <summary>查看候选版本（{{ versions.length }}）</summary>
          <button v-for="version in versions" :key="version.id" type="button" @click="editorContent = version.content">
            r{{ version.content_revision }} · {{ version.source === 'author' ? '作者' : 'AI' }} · {{ version.content.slice(0, 54) || '空内容' }}
          </button>
        </details>
      </section>

      <aside class="review-panel review-audit" aria-label="机器审校与提交清单">
        <div class="review-panel__head"><span class="review-panel__kicker">审校与提交</span><h2>计划 / 实际</h2></div>
        <section class="audit-block">
          <h3>机器审校</h3>
          <p v-if="candidate.audit_is_current">{{ auditSummary }}</p>
          <p v-else>正文改动后，旧审校不会被用于提交。</p>
          <ul v-if="hardBlocks.length" class="audit-block__errors"><li v-for="block in hardBlocks" :key="String(block.message || block)">{{ String(block.message || block) }}</li></ul>
          <ul v-if="softWarnings.length" class="audit-block__warnings"><li v-for="warning in softWarnings" :key="String(warning)">{{ String(warning) }}</li></ul>
          <n-button block secondary :loading="actionLoading" @click="reaudit">重新审校</n-button>
        </section>
        <section class="audit-block">
          <h3>作者确认的提交清单</h3>
          <n-form-item label="章节摘要"><n-input v-model:value="commitPlan.chapter_summary" type="textarea" :rows="3" /></n-form-item>
          <n-form-item label="叙事事件（每行一条）"><n-input v-model:value="commitPlan.eventsText" type="textarea" :rows="3" /></n-form-item>
          <n-form-item label="下一章交接（每行一条）"><n-input v-model:value="commitPlan.handoffText" type="textarea" :rows="3" /></n-form-item>
          <p class="audit-block__note">你修改的是可读字段；系统会保存为本次正式提交的唯一作者确认清单。</p>
          <n-button block secondary :disabled="!candidate.commit_plan_is_current" :loading="actionLoading" @click="saveCommitPlan">保存提交清单</n-button>
        </section>
        <section class="audit-block audit-block--actions">
          <h3>正式决定</h3>
          <n-button block type="primary" :disabled="!canCommit" :loading="actionLoading" @click="approve(true)">
            通过并生成下一章
          </n-button>
          <n-button block :disabled="!canCommit" :loading="actionLoading" @click="approve(false)">通过但暂停</n-button>
          <n-button block tertiary type="error" :loading="actionLoading" @click="reject">放弃候选并停止</n-button>
        </section>
      </aside>
    </div>

    <section v-else class="review-empty">
      <n-icon :component="DocumentTextOutline" :size="42" aria-hidden="true" />
      <h2>{{ presentation?.label || '暂无候选章节' }}</h2>
      <p>{{ presentation?.detail || '先在大纲工作室发布并同步完整五级约束链，然后选择连续自动驾驶或逐章审核。' }}</p>
      <n-space wrap>
        <n-button type="primary" @click="router.push(`/book/${novelId}/outline`)">前往大纲工作室</n-button>
        <n-button @click="router.push(`/book/${novelId}/workbench`)">打开自动驾驶</n-button>
      </n-space>
    </section>
  </main>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useMessage } from 'naive-ui'
import {
  AlertCircleOutline, CheckmarkCircleOutline, DocumentTextOutline, GitNetworkOutline,
  HourglassOutline, PauseCircleOutline, ReceiptOutline,
} from '@vicons/ionicons5'
import {
  generationApi, getGenerationRunOrNull, type CandidateVersion, type ChapterCandidate, type GenerationRun,
} from '@/api/generation'
import { getGenerationPresentation } from '@/domain/generationPresentation'
import { isReviewDraftDirty, type ReviewDraft } from '@/domain/reviewDraft'

type MobileTab = 'manuscript' | 'contract' | 'audit' | 'commit'
const route = useRoute()
const router = useRouter()
const message = useMessage()
const novelId = computed(() => String(route.params.slug || ''))
const loading = ref(false)
const actionLoading = ref(false)
const error = ref('')
const run = ref<GenerationRun | null>(null)
const candidate = computed(() => run.value?.candidate || null)
const versions = ref<CandidateVersion[]>([])
const editorContent = ref('')
const feedback = ref('')
const mobileTab = ref<MobileTab>('manuscript')
const mobileTabs = [
  { value: 'manuscript' as const, label: '正文', icon: DocumentTextOutline },
  { value: 'contract' as const, label: '契约', icon: GitNetworkOutline },
  { value: 'audit' as const, label: '审校', icon: AlertCircleOutline },
  { value: 'commit' as const, label: '提交', icon: ReceiptOutline },
]
const orderedLevels = ['outline', 'part', 'volume', 'act', 'chapter']
const levelLabels: Record<string, string> = { outline: '总纲', part: '部纲', volume: '卷纲', act: '幕纲', chapter: '章纲' }
const presentation = computed(() => getGenerationPresentation(run.value))
const stateIcon = computed(() => {
  if (presentation.value?.tone === 'error') return AlertCircleOutline
  if (presentation.value?.tone === 'warning') return PauseCircleOutline
  if (presentation.value?.tone === 'success') return CheckmarkCircleOutline
  return HourglassOutline
})
const requiredEvents = computed(() => candidate.value?.outline_chain.chapter?.payload?.required_events || [])
const forbiddenEvents = computed(() => candidate.value?.outline_chain.chapter?.payload?.forbidden_events || [])
const hardBlocks = computed<Record<string, unknown>[]>(() => Array.isArray(candidate.value?.audit?.hard_blocks) ? candidate.value!.audit.hard_blocks as Record<string, unknown>[] : [])
const softWarnings = computed<unknown[]>(() => Array.isArray(candidate.value?.audit?.soft_warnings) ? candidate.value!.audit.soft_warnings as unknown[] : [])
const auditSummary = computed(() => candidate.value?.audit?.status === 'blocked' ? '存在硬性阻断；请修改正文或创建大纲修订草稿。' : '已完成无正式副作用的机器审校，等待作者确认。')
const wordCount = computed(() => editorContent.value.replace(/\s/g, '').length)
const canCommit = computed(() => Boolean(candidate.value?.audit_is_current && candidate.value?.commit_plan_is_current && candidate.value?.status === 'awaiting_review'))
const continuationError = ref('')
const commitPlan = reactive({ chapter_summary: '', eventsText: '', handoffText: '' })
const savedDraft = ref<ReviewDraft>({
  content: '', feedback: '', chapterSummary: '', eventsText: '', handoffText: '',
})
const hasUnsavedDraft = computed(() => isReviewDraftDirty(currentDraft(), savedDraft.value))
let pollTimer: number | null = null
let refreshEpoch = 0

function listText(value: unknown) { return Array.isArray(value) ? value.map(item => String(item)).join('\n') : '' }
function currentDraft(): ReviewDraft {
  return {
    content: editorContent.value,
    feedback: feedback.value,
    chapterSummary: commitPlan.chapter_summary,
    eventsText: commitPlan.eventsText,
    handoffText: commitPlan.handoffText,
  }
}
function draftFromCandidate(value: ChapterCandidate | null): ReviewDraft {
  if (!value) return { content: '', feedback: '', chapterSummary: '', eventsText: '', handoffText: '' }
  const plan = value.commit_plan || {}
  return {
    content: value.final_content,
    feedback: value.feedback || '',
    chapterSummary: String(plan.chapter_summary || ''),
    eventsText: listText(plan.timeline_events),
    handoffText: listText(plan.next_chapter_handoff),
  }
}
function syncEditorFromCandidate(
  value: ChapterCandidate | null,
  { force = false, preserveFeedback = false }: { force?: boolean; preserveFeedback?: boolean } = {},
) {
  if (!force && hasUnsavedDraft.value) return
  const remote = draftFromCandidate(value)
  const localFeedback = feedback.value
  editorContent.value = remote.content
  feedback.value = preserveFeedback ? localFeedback : remote.feedback
  commitPlan.chapter_summary = remote.chapterSummary
  commitPlan.eventsText = remote.eventsText
  commitPlan.handoffText = remote.handoffText
  savedDraft.value = remote
}
function lines(value: string) { return value.split(/\r?\n/).map(item => item.trim()).filter(Boolean) }

async function refresh(options: { force?: boolean; preserveFeedback?: boolean } = {}) {
  if (!novelId.value) return
  const requestEpoch = ++refreshEpoch
  loading.value = true
  error.value = ''
  try {
    const refreshedRun = await getGenerationRunOrNull(novelId.value)
    if (requestEpoch !== refreshEpoch) return
    const refreshedCandidate = refreshedRun?.candidate || null
    const refreshedVersions = refreshedCandidate
      ? await generationApi.listCandidateVersions(refreshedCandidate.id)
      : []
    if (requestEpoch !== refreshEpoch) return
    run.value = refreshedRun
    if (refreshedRun?.last_error?.includes('generation_runner_claim_failed')) {
      continuationError.value = refreshedRun.last_error
    }
    versions.value = refreshedVersions
    syncEditorFromCandidate(refreshedCandidate, options)
  } catch (cause) {
    if (requestEpoch === refreshEpoch) {
      versions.value = []
      error.value = cause instanceof Error ? cause.message : '读取候选状态失败'
    }
  } finally {
    if (requestEpoch === refreshEpoch) loading.value = false
  }
}

async function applyAction(
  action: () => Promise<unknown>,
  success: string,
  refreshOptions: { preserveFeedback?: boolean } = {},
) {
  actionLoading.value = true
  error.value = ''
  try { await action(); await refresh({ force: true, ...refreshOptions }); message.success(success) }
  catch (cause) { error.value = cause instanceof Error ? cause.message : '操作失败' }
  finally { actionLoading.value = false }
}
async function saveContent() {
  if (!candidate.value) return
  await applyAction(() => generationApi.saveCandidateContent(candidate.value!.id, editorContent.value, feedback.value), '作者修订已保存；请重新审校。')
}
async function reaudit() {
  if (!candidate.value) return
  await applyAction(() => generationApi.reaudit(candidate.value!.id), '审校和提交清单已刷新。')
}
async function saveCommitPlan() {
  if (!candidate.value) return
  const plan = {
    ...candidate.value.commit_plan,
    chapter_summary: commitPlan.chapter_summary.trim(),
    timeline_events: lines(commitPlan.eventsText),
    next_chapter_handoff: lines(commitPlan.handoffText),
  }
  await applyAction(
    () => generationApi.saveCommitPlan(candidate.value!.id, plan),
    '作者确认的提交清单已保存。',
    { preserveFeedback: true },
  )
}
async function regenerate() {
  if (!candidate.value) return
  await applyAction(() => generationApi.regenerate(candidate.value!.id, feedback.value), '新候选已生成并进入待审核。')
}
async function retrySync() {
  if (!candidate.value) return
  await applyAction(() => generationApi.retrySync(candidate.value!.id), '已重试本章规范同步。')
}
async function approve(continueAfterCommit: boolean) {
  if (!candidate.value) return
  const current = candidate.value
  await applyAction(async () => {
    const result = await generationApi.approveAndCommit(current.id, continueAfterCommit)
    continuationError.value = result.continuation_error || ''
    if (!continueAfterCommit) return
    if (result.continuation_error) {
      await refresh({ force: true })
      return
    }
    // Continuous owns its runner on the server. Review mode still needs one
    // explicit next-candidate request, but a failure cannot undo the commit.
    if (run.value?.run_mode !== 'continuous') {
      try {
        await generationApi.generateNext(novelId.value)
      } catch (cause) {
        continuationError.value = cause instanceof Error ? cause.message : '自动继续失败'
        await refresh({ force: true })
      }
    }
  }, continueAfterCommit ? '已正式提交；正在按所选模式推进。' : '已正式提交并暂停。')
}
async function retryContinuation() {
  if (!continuationError.value) return
  await applyAction(async () => {
    if (run.value?.run_mode === 'continuous') {
      await generationApi.runContinuous(novelId.value)
    } else {
      await generationApi.generateNext(novelId.value)
    }
    continuationError.value = ''
  }, '自动继续已重新启动。')
}
async function reject() {
  if (!candidate.value) return
  await applyAction(() => generationApi.reject(candidate.value!.id), '候选稿已放弃，运行已停止。')
}

onMounted(async () => {
  await refresh({ force: true })
  pollTimer = window.setInterval(() => { if (!actionLoading.value) void refresh() }, 2500)
})
onUnmounted(() => { if (pollTimer !== null) window.clearInterval(pollTimer) })
</script>

<style scoped>
.review-desk { min-height: 100vh; padding: 28px; color: var(--app-text-primary); background: var(--app-page-bg); }.review-desk__header { display: flex; align-items: flex-start; justify-content: space-between; gap: 24px; max-width: 1540px; margin: 0 auto 18px; }.review-desk__eyebrow, .review-panel__kicker { margin: 0 0 4px; color: var(--color-brand); font-size: 12px; font-weight: 700; letter-spacing: .06em; }.review-desk h1 { margin: 0; font: 700 clamp(24px, 3vw, 34px)/1.2 var(--app-font-serif, serif); }.review-desk__header p:not(.review-desk__eyebrow) { margin: 8px 0 0; color: var(--app-text-secondary); }.review-desk__header-actions { display: flex; flex-wrap: wrap; gap: 8px; }.review-desk__alert, .review-state { max-width: 1540px; margin: 0 auto 14px; }.review-state { display: flex; align-items: center; gap: 10px; padding: 11px 14px; border: 1px solid var(--app-border); border-radius: var(--app-radius-md); background: var(--app-surface); }.review-state > div { display: grid; flex: 1; gap: 2px; }.review-state span { color: var(--app-text-secondary); font-size: 12px; }.review-state.is-error { border-color: color-mix(in srgb, var(--color-danger) 38%, var(--app-border)); }.review-state.is-warning { border-color: color-mix(in srgb, var(--color-warning) 38%, var(--app-border)); }.review-desk__grid { display: grid; grid-template-columns: minmax(210px, .72fr) minmax(430px, 1.55fr) minmax(285px, .88fr); gap: 16px; max-width: 1540px; margin: 0 auto; align-items: start; }.review-panel { min-width: 0; overflow: hidden; border: 1px solid var(--app-border); border-radius: var(--app-radius-lg); background: var(--app-surface); box-shadow: var(--app-shadow-sm); }.review-panel__head { padding: 16px; border-bottom: 1px solid var(--app-divider); }.review-panel__head h2 { margin: 0; font-size: 16px; }.review-contract { position: sticky; top: 16px; }.review-contract__chapter { display: grid; gap: 4px; padding: 14px 16px; border-bottom: 1px solid var(--app-divider); }.review-contract__chapter span { color: var(--app-text-muted); font-size: 11px; }.contract-level { padding: 13px 16px; border-bottom: 1px solid var(--app-divider); }.contract-level__head { display: flex; justify-content: space-between; gap: 8px; font-weight: 700; font-size: 13px; }.contract-level__head small { color: var(--app-text-muted); font-weight: 500; }.contract-level p, .contract-level ul { margin: 7px 0 0; color: var(--app-text-secondary); font-size: 12px; line-height: 1.55; }.contract-level ul { padding-left: 16px; }.contract-level__forbidden { color: var(--color-danger) !important; }.review-contract :deep(.n-button) { margin: 14px 16px; width: calc(100% - 32px); }.review-manuscript { min-height: calc(100vh - 215px); display: flex; flex-direction: column; }.review-manuscript__head { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }.review-revision { color: var(--app-text-muted); font-size: 11px; }.review-stale-alert { margin: 12px 14px 0; }.review-manuscript__editor { flex: 1; min-height: 460px; padding: 14px; }.review-manuscript__editor :deep(textarea) { min-height: 440px !important; font-family: var(--app-font-serif, serif); font-size: 16px; line-height: 1.9; }.review-manuscript__footer { display: flex; align-items: center; justify-content: space-between; gap: 10px; padding: 0 14px 12px; color: var(--app-text-muted); font-size: 12px; }.review-manuscript > :deep(.n-input:last-of-type) { margin: 0 14px 14px; }.review-versions { padding: 12px 14px; border-top: 1px solid var(--app-divider); }.review-versions summary { cursor: pointer; color: var(--app-text-secondary); font-size: 12px; }.review-versions button { display: block; width: 100%; margin-top: 8px; overflow: hidden; color: var(--app-text-secondary); text-align: left; text-overflow: ellipsis; white-space: nowrap; border: 0; background: transparent; cursor: pointer; }.review-audit { position: sticky; top: 16px; }.audit-block { padding: 14px 16px; border-bottom: 1px solid var(--app-divider); }.audit-block h3 { margin: 0 0 8px; font-size: 13px; }.audit-block p { margin: 0; color: var(--app-text-secondary); font-size: 12px; line-height: 1.55; }.audit-block ul { margin: 8px 0; padding-left: 17px; font-size: 12px; line-height: 1.5; }.audit-block__errors { color: var(--color-danger); }.audit-block__warnings { color: var(--color-warning); }.audit-block__note { margin: 8px 0 !important; color: var(--app-text-muted) !important; }.audit-block :deep(.n-form-item) { margin-bottom: 9px; }.audit-block :deep(.n-button + .n-button) { margin-top: 8px; }.review-empty { display: grid; place-items: center; justify-items: center; max-width: 680px; min-height: 52vh; margin: 0 auto; padding: 26px; text-align: center; border: 1px dashed var(--app-border); border-radius: var(--app-radius-lg); background: var(--app-surface); }.review-empty h2 { margin: 12px 0 6px; }.review-empty p { max-width: 52ch; margin: 0 0 16px; color: var(--app-text-secondary); line-height: 1.6; }.review-mobile-tabs { display: none; }
@media (max-width: 1120px) { .review-desk__grid { grid-template-columns: minmax(0, 1.3fr) minmax(270px, .8fr); }.review-contract { position: static; grid-column: 1 / -1; display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); }.review-contract .review-panel__head { grid-column: 1 / -1; }.review-contract__chapter, .contract-level { border-right: 1px solid var(--app-divider); border-bottom: 0; }.review-contract :deep(.n-button) { grid-column: 1 / -1; }.review-audit { position: static; } }
@media (max-width: 767px) { .review-desk { padding: 16px; }.review-desk__header { flex-direction: column; gap: 14px; }.review-desk__header-actions { width: 100%; }.review-desk__header-actions :deep(.n-button) { flex: 1; }.review-mobile-tabs { display: grid; grid-template-columns: repeat(4, 1fr); gap: 4px; max-width: 1540px; margin: 0 auto 10px; }.review-mobile-tabs button { display: grid; place-items: center; gap: 3px; min-height: 51px; color: var(--app-text-secondary); font-size: 11px; border: 1px solid var(--app-border); border-radius: var(--app-radius-sm); background: var(--app-surface); }.review-mobile-tabs button.is-active { color: var(--color-brand); border-color: var(--color-brand); background: var(--app-surface-subtle); }.review-desk__grid { display: block; }.review-panel { display: none; }.review-desk__grid.mobile-tab--manuscript .review-manuscript, .review-desk__grid.mobile-tab--contract .review-contract, .review-desk__grid.mobile-tab--audit .review-audit, .review-desk__grid.mobile-tab--commit .review-audit { display: block; }.review-desk__grid.mobile-tab--commit .review-audit .audit-block:not(.audit-block--actions), .review-desk__grid.mobile-tab--audit .review-audit .audit-block--actions { display: none; }.review-contract { position: static; }.review-contract__chapter, .contract-level { display: block; border-right: 0; border-bottom: 1px solid var(--app-divider); }.review-manuscript { min-height: 0; }.review-manuscript__editor { min-height: 50vh; }.review-manuscript__editor :deep(textarea) { min-height: 48vh !important; }.review-manuscript__footer { flex-direction: column; align-items: stretch; }.review-audit { position: static; } }
</style>
