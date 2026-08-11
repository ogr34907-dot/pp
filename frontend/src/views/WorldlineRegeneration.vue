<template>
  <main class="worldline" aria-labelledby="worldline-title">
    <header class="worldline__header">
      <div>
        <p class="worldline__eyebrow">世界线归档 · 只读可恢复 · 新旧记忆严格隔离</p>
        <h1 id="worldline-title">从任意章节重生成</h1>
        <p>保留前缀不变；旧尾部退出当前主线、事实记忆与向量检索，但保留在只读归档中，可随时恢复。</p>
      </div>
      <div class="worldline__header-actions">
        <n-button secondary @click="load" :loading="loading">刷新</n-button>
        <n-button secondary @click="router.push(`/book/${novelId}/review`)">审稿台</n-button>
        <n-button type="primary" @click="router.push(`/book/${novelId}/workbench`)">返回工作台</n-button>
      </div>
    </header>

    <n-alert v-if="error" type="error" :show-icon="true" role="alert" class="worldline__alert">{{ error }}</n-alert>
    <section v-if="presentation" class="worldline__state" :class="`is-${presentation.tone}`" aria-live="polite">
      <div><strong>{{ presentation.label }}</strong><span>{{ presentation.detail }}</span></div>
      <span v-if="run" class="worldline__state-meta">正式至第 {{ run.current_formal_chapter }} 章 · 世代 {{ run.generation_epoch }}</span>
    </section>

    <div class="worldline__grid">
      <section class="worldline-panel worldline-planner" aria-label="重生成计划">
        <div class="worldline-panel__head"><span class="worldline-panel__kicker">计划</span><h2>定义新世界线</h2></div>
        <p class="worldline-planner__intro">当前正式主线共 <strong>{{ generatedChapters }}</strong> 章。输入从哪一章开始重生成，并选择后续写作模式。</p>
        <div class="worldline-inputs">
          <n-form-item label="从第 N 章开始"><n-input-number v-model:value="startChapter" :min="1" :max="Math.max(1, generatedChapters + 1)" /></n-form-item>
          <n-form-item label="新目标 Y 章"><n-input-number v-model:value="targetChapters" :min="Math.max(1, startChapter)" :max="100000" /></n-form-item>
        </div>
        <p class="worldline-rule">{{ rangeRule }}</p>
        <div class="mode-cards" role="radiogroup" aria-label="重生成后的自动驾驶模式">
          <button type="button" class="mode-card" :class="{ 'is-selected': runMode === 'continuous' }" role="radio" :aria-checked="runMode === 'continuous'" @click="runMode = 'continuous'">
            <n-icon :component="FlashOutline" :size="22" aria-hidden="true" /><strong>连续自动驾驶</strong><span>候选生成、机器审校、正式写入与同步连续执行；硬性冲突或同步失败立即暂停。</span>
          </button>
          <button type="button" class="mode-card" :class="{ 'is-selected': runMode === 'chapter_review' }" role="radio" :aria-checked="runMode === 'chapter_review'" @click="runMode = 'chapter_review'">
            <n-icon :component="ClipboardOutline" :size="22" aria-hidden="true" /><strong>逐章人工审核</strong><span>最多一个候选章；完整候选后停在审稿台，绝不预取下一章或额外消耗 Token。</span>
          </button>
        </div>
        <div class="worldline-planner__actions">
          <n-button type="primary" :loading="previewing" @click="createPreview">查看影响预览</n-button>
          <n-button secondary :disabled="!preview" :loading="executing" @click="executePreview">确认归档并开始重建</n-button>
        </div>
      </section>

      <section class="worldline-panel worldline-impact" aria-label="影响预览">
        <div class="worldline-panel__head"><span class="worldline-panel__kicker">预览</span><h2>影响范围</h2></div>
        <div v-if="!preview" class="worldline-impact__empty">先生成预览；执行操作时必须带上这次预览令牌，避免在内容已变化后误截断。</div>
        <template v-else>
          <div class="impact-headline">
            <span>{{ preview.operation === 'continue' ? '普通续写' : `保留至第 ${preview.retained_through} 章` }}</span>
            <strong>{{ preview.operation === 'continue' ? `从第 ${startChapter} 章续写` : `第 ${preview.archive_from}～${preview.archive_to} 章进入归档` }}</strong>
          </div>
          <dl class="impact-counts">
            <div v-for="(count, name) in preview.counts" :key="name"><dt>{{ tableLabel(name) }}</dt><dd>{{ count }}</dd></div>
          </dl>
          <ul class="impact-notes">
            <li>旧尾部的正文、章纲、节拍、审校、规范提交、事件、三元组、状态、记忆与向量均撤出当前主线。</li>
            <li>第 1～{{ preview.retained_through }} 章的编号、修订和内容哈希保持不变。</li>
            <li>跨截断点的伏笔、摘要和世界状态会从保留前缀重新构建；不会用新大纲覆盖已发生事实。</li>
          </ul>
        </template>
      </section>

      <aside class="worldline-panel worldline-rebuild" aria-label="重建与归档">
        <div class="worldline-panel__head"><span class="worldline-panel__kicker">恢复</span><h2>规范事实重建</h2></div>
        <p v-if="rebuildStatus" class="rebuild-copy">{{ rebuildSummary }}</p>
        <p v-else class="rebuild-copy">归档或恢复后，系统会从保留前缀重建规范事实与长期记忆。完成前不会开始下一章。</p>
        <n-space vertical :size="8" class="worldline-rebuild__actions">
          <n-button block :disabled="!needsRebuild" :loading="rebuilding" type="primary" @click="rebuild">重建保留前缀</n-button>
          <n-button block tertiary type="error" :disabled="!needsRebuild" :loading="cancelling" @click="cancelRebuild">取消重建并停止</n-button>
        </n-space>
        <div class="archive-list">
          <h3>只读世界线归档</h3>
          <p v-if="!archives.length" class="archive-list__empty">暂无可恢复的旧尾部。</p>
          <article v-for="archive in archives" :key="archive.id" class="archive-row">
            <div><strong>第 {{ archive.start_chapter }}～{{ archive.end_chapter }} 章</strong><small>{{ archive.status }} · {{ formatDate(archive.created_at) }}</small></div>
            <n-button size="tiny" secondary :loading="restoringId === archive.id" @click="restore(archive.id)">恢复</n-button>
          </article>
        </div>
      </aside>
    </div>
  </main>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useMessage } from 'naive-ui'
import { ClipboardOutline, FlashOutline } from '@vicons/ionicons5'
import {
  getGenerationRunOrNull, type GenerationRun, type RunMode, type WorldlineArchive, type WorldlinePreview,
  worldlineRegenerationApi,
} from '@/api/generation'
import { chapterApi } from '@/api/chapter'
import { getGenerationPresentation } from '@/domain/generationPresentation'
import { normalizeWorldlineTarget, resolveGeneratedChapterCount } from '@/domain/worldlineChapterCount'

const route = useRoute()
const router = useRouter()
const message = useMessage()
const novelId = computed(() => String(route.params.slug || ''))
const run = ref<GenerationRun | null>(null)
const preview = ref<WorldlinePreview | null>(null)
const archives = ref<WorldlineArchive[]>([])
const rebuildStatus = ref<Record<string, unknown> | null>(null)
const existingChapterHead = ref(0)
const startChapter = ref(1)
const targetChapters = ref(1)
const runMode = ref<RunMode>('continuous')
const loading = ref(false)
const previewing = ref(false)
const executing = ref(false)
const rebuilding = ref(false)
const cancelling = ref(false)
const restoringId = ref('')
const error = ref('')
const presentation = computed(() => getGenerationPresentation(run.value))
const generatedChapters = computed(() => resolveGeneratedChapterCount(
  run.value?.current_formal_chapter,
  [{ number: existingChapterHead.value }],
))
const needsRebuild = computed(() => ['rebuilding', 'failed'].includes(String(run.value?.canonical_sync_status || '')))
const rangeRule = computed(() => {
  const n = startChapter.value || 1
  const x = generatedChapters.value
  if (n > x) return `N=${n} 大于当前正式章节 ${x}：这是普通续写，不会重置既有主线。`
  if (n === 1) return 'N=1：当前正文世界线会全部归档，然后从第一章重开。'
  if (n === x) return `N=${x}：只归档最后一章，然后从第 ${x} 章重写。`
  return `保留第 1～${n - 1} 章；第 ${n}～${x} 章会退出当前主线并进入只读归档。`
})
const rebuildSummary = computed(() => {
  const jobs = Array.isArray(rebuildStatus.value?.jobs) ? rebuildStatus.value?.jobs as Array<Record<string, unknown>> : []
  if (!jobs.length) return '尚无重建任务。'
  const done = jobs.filter(job => job.status === 'completed').length
  const failed = jobs.filter(job => job.status === 'failed')
  return failed.length ? `重建在 ${failed.map(job => String(job.job_type)).join('、')} 失败；修复后可重试。` : `已完成 ${done}/${jobs.length} 个规范重建任务。`
})
function key(prefix: string) { return `${prefix}-${crypto.randomUUID()}` }
function tableLabel(name: string) { return ({ chapters: '正文', narrative_events: '叙事事件', memory_atoms: '记忆原子', chapter_candidates: '候选稿', story_nodes: '章纲节点' } as Record<string, string>)[name] || name }
function formatDate(value?: string | null) { return value ? new Date(value).toLocaleString() : '—' }

async function load() {
  if (!novelId.value) return
  loading.value = true
  error.value = ''
  try {
    const [nextRun, nextArchives] = await Promise.all([
      getGenerationRunOrNull(novelId.value),
      worldlineRegenerationApi.listArchives(novelId.value),
    ])
    run.value = nextRun
    archives.value = nextArchives
    const chapters = nextRun ? [] : await chapterApi.listChapters(novelId.value)
    existingChapterHead.value = resolveGeneratedChapterCount(null, chapters)
    const generated = resolveGeneratedChapterCount(nextRun?.current_formal_chapter, chapters)
    if (nextRun) {
      startChapter.value = Math.max(1, generated || 1)
      targetChapters.value = Math.max(nextRun.target_chapters || 1, generated || 1)
      runMode.value = nextRun.run_mode
      if (['rebuilding', 'failed'].includes(nextRun.canonical_sync_status)) {
        rebuildStatus.value = await worldlineRegenerationApi.rebuildStatus(novelId.value)
      }
    } else {
      startChapter.value = Math.max(1, generated || 1)
      targetChapters.value = Math.max(1, generated || 1)
    }
  } catch (cause) { error.value = cause instanceof Error ? cause.message : '读取世界线状态失败' }
  finally { loading.value = false }
}
async function createPreview() {
  previewing.value = true; error.value = ''
  const normalizedTarget = normalizeWorldlineTarget(startChapter.value, targetChapters.value)
  targetChapters.value = normalizedTarget
  try { preview.value = await worldlineRegenerationApi.preview(novelId.value, startChapter.value, normalizedTarget) }
  catch (cause) { error.value = cause instanceof Error ? cause.message : '创建影响预览失败' }
  finally { previewing.value = false }
}
async function executePreview() {
  if (!preview.value) return
  executing.value = true; error.value = ''
  try {
    const result = await worldlineRegenerationApi.execute(novelId.value, preview.value.token, runMode.value, key('worldline'))
    preview.value = null
    await load()
    message.success(result.operation === 'continue' ? '已设置为普通续写。' : '旧尾部已归档；请先完成保留前缀重建。')
  } catch (cause) { error.value = cause instanceof Error ? cause.message : '执行世界线重生成失败' }
  finally { executing.value = false }
}
async function rebuild() {
  rebuilding.value = true; error.value = ''
  try { await worldlineRegenerationApi.rebuild(novelId.value); await load(); message.success('规范事实与长期记忆已按保留前缀重建。') }
  catch (cause) { error.value = cause instanceof Error ? cause.message : '世界线重建失败' }
  finally { rebuilding.value = false }
}
async function cancelRebuild() {
  cancelling.value = true; error.value = ''
  try { await worldlineRegenerationApi.cancelRebuild(novelId.value); await load(); message.info('重建已取消，运行已安全停止。') }
  catch (cause) { error.value = cause instanceof Error ? cause.message : '取消重建失败' }
  finally { cancelling.value = false }
}
async function restore(archiveId: string) {
  restoringId.value = archiveId; error.value = ''
  try { await worldlineRegenerationApi.restore(novelId.value, archiveId, runMode.value, key('worldline-restore')); await load(); message.success('已恢复归档正文；请重建规范事实后再继续。') }
  catch (cause) { error.value = cause instanceof Error ? cause.message : '恢复旧世界线失败' }
  finally { restoringId.value = '' }
}
watch([startChapter, targetChapters], ([nextStart, nextTarget]) => {
  const normalizedTarget = normalizeWorldlineTarget(nextStart, nextTarget)
  if (normalizedTarget !== nextTarget) targetChapters.value = normalizedTarget
  preview.value = null
})
onMounted(load)
</script>

<style scoped>
.worldline { min-height: 100vh; padding: 28px; color: var(--app-text-primary); background: var(--app-page-bg); }.worldline__header { display: flex; justify-content: space-between; align-items: flex-start; gap: 24px; max-width: 1480px; margin: 0 auto 18px; }.worldline__eyebrow, .worldline-panel__kicker { margin: 0 0 4px; color: var(--color-brand); font-size: 12px; font-weight: 700; letter-spacing: .06em; }.worldline h1 { margin: 0; font: 700 clamp(24px, 3vw, 34px)/1.2 var(--app-font-serif, serif); }.worldline__header p:not(.worldline__eyebrow) { max-width: 70ch; margin: 8px 0 0; color: var(--app-text-secondary); }.worldline__header-actions { display: flex; flex-wrap: wrap; gap: 8px; }.worldline__alert, .worldline__state { max-width: 1480px; margin: 0 auto 14px; }.worldline__state { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 12px 14px; border: 1px solid var(--app-border); border-radius: var(--app-radius-md); background: var(--app-surface); }.worldline__state div { display: grid; gap: 3px; }.worldline__state span { color: var(--app-text-secondary); font-size: 12px; }.worldline__state.is-error { border-color: color-mix(in srgb, var(--color-danger) 42%, var(--app-border)); }.worldline__state.is-warning { border-color: color-mix(in srgb, var(--color-warning) 42%, var(--app-border)); }.worldline__state-meta { white-space: nowrap; }.worldline__grid { display: grid; grid-template-columns: minmax(360px, 1.15fr) minmax(280px, .9fr) minmax(270px, .8fr); gap: 16px; max-width: 1480px; margin: 0 auto; align-items: start; }.worldline-panel { min-width: 0; overflow: hidden; border: 1px solid var(--app-border); border-radius: var(--app-radius-lg); background: var(--app-surface); box-shadow: var(--app-shadow-sm); }.worldline-panel__head { padding: 16px; border-bottom: 1px solid var(--app-divider); }.worldline-panel__head h2 { margin: 0; font-size: 16px; }.worldline-planner { padding-bottom: 16px; }.worldline-planner__intro, .worldline-rule { margin: 16px; color: var(--app-text-secondary); line-height: 1.6; }.worldline-inputs { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; padding: 0 16px; }.worldline-inputs :deep(.n-form-item) { margin: 0; }.worldline-rule { padding: 10px 12px; border-left: 3px solid var(--color-brand); background: var(--app-surface-subtle); font-size: 13px; }.mode-cards { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; padding: 0 16px; }.mode-card { display: grid; gap: 7px; padding: 14px; color: var(--app-text-secondary); text-align: left; border: 1px solid var(--app-border); border-radius: var(--app-radius-md); background: var(--app-surface); cursor: pointer; }.mode-card strong { color: var(--app-text-primary); }.mode-card span { font-size: 12px; line-height: 1.5; }.mode-card.is-selected { border-color: var(--color-brand); box-shadow: inset 0 0 0 1px var(--color-brand); background: var(--app-surface-subtle); }.worldline-planner__actions { display: flex; flex-wrap: wrap; gap: 8px; padding: 16px 16px 0; }.worldline-impact__empty, .rebuild-copy { padding: 18px 16px; color: var(--app-text-secondary); line-height: 1.6; }.impact-headline { display: grid; gap: 4px; padding: 16px; border-bottom: 1px solid var(--app-divider); }.impact-headline span { color: var(--app-text-muted); font-size: 12px; }.impact-counts { display: grid; grid-template-columns: repeat(2, 1fr); gap: 1px; margin: 0; background: var(--app-divider); }.impact-counts div { padding: 12px; background: var(--app-surface); }.impact-counts dt { color: var(--app-text-muted); font-size: 11px; }.impact-counts dd { margin: 4px 0 0; font-size: 20px; font-variant-numeric: tabular-nums; }.impact-notes { margin: 0; padding: 16px 16px 16px 32px; color: var(--app-text-secondary); font-size: 12px; line-height: 1.65; }.worldline-rebuild__actions { padding: 0 16px 16px; }.archive-list { border-top: 1px solid var(--app-divider); padding: 14px 16px; }.archive-list h3 { margin: 0 0 10px; font-size: 13px; }.archive-list__empty { margin: 0; color: var(--app-text-muted); font-size: 12px; }.archive-row { display: flex; justify-content: space-between; gap: 8px; align-items: center; padding: 10px 0; border-top: 1px solid var(--app-divider); }.archive-row div { min-width: 0; display: grid; gap: 3px; }.archive-row strong { font-size: 12px; }.archive-row small { color: var(--app-text-muted); font-size: 10px; }
@media (max-width: 1100px) { .worldline__grid { grid-template-columns: 1fr 1fr; }.worldline-rebuild { grid-column: 1 / -1; }.archive-list { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 0 16px; }.archive-list h3, .archive-list__empty { grid-column: 1 / -1; } }
@media (max-width: 720px) { .worldline { padding: 16px; }.worldline__header { flex-direction: column; gap: 14px; }.worldline__header-actions { width: 100%; }.worldline__header-actions :deep(.n-button) { flex: 1; }.worldline__state { align-items: flex-start; flex-direction: column; }.worldline__grid { display: block; }.worldline-panel + .worldline-panel { margin-top: 12px; }.worldline-inputs, .mode-cards { grid-template-columns: 1fr; }.archive-list { display: block; } }
</style>
