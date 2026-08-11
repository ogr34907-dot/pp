<template>
  <section class="generation-launcher" aria-label="新一代自动驾驶">
    <header class="generation-launcher__head">
      <div>
        <span class="generation-launcher__eyebrow">候选优先自动驾驶</span>
        <h2>以已发布五级大纲为准</h2>
      </div>
      <span v-if="presentation" class="generation-launcher__status" :class="`is-${presentation.tone}`" aria-live="polite">
        {{ presentation.label }}
      </span>
    </header>
    <p class="generation-launcher__description">{{ presentation?.detail || '发布并同步总纲、部纲、卷纲、幕纲和章纲后，再选择运行方式。' }}</p>
    <n-alert v-if="error" type="error" :show-icon="true" role="alert">{{ error }}</n-alert>
    <div class="generation-launcher__modes" role="radiogroup" aria-label="自动驾驶模式">
      <button type="button" class="generation-mode" :class="{ 'is-selected': mode === 'continuous' }" role="radio" :aria-checked="mode === 'continuous'" @click="mode = 'continuous'">
        <n-icon :component="FlashOutline" :size="20" aria-hidden="true" /><strong>连续自动驾驶</strong><span>正式写入与同步后自动推进；硬冲突、记忆失败或技术错误会暂停。</span>
      </button>
      <button type="button" class="generation-mode" :class="{ 'is-selected': mode === 'chapter_review' }" role="radio" :aria-checked="mode === 'chapter_review'" @click="mode = 'chapter_review'">
        <n-icon :component="ClipboardOutline" :size="20" aria-hidden="true" /><strong>逐章人工审核</strong><span>最多一个候选章；等待审核期间后续 LLM 调用严格为零。</span>
      </button>
    </div>
    <div class="generation-launcher__controls">
      <n-input-number v-model:value="targetChapters" :min="1" :max="100000" size="small" aria-label="目标章节数" />
      <n-button type="primary" :loading="starting" @click="start">按此模式开始</n-button>
      <n-button secondary :disabled="!run || run.state === 'stopped'" :loading="stopping" @click="stop">安全停止</n-button>
    </div>
    <div class="generation-launcher__links">
      <RouterLink :to="`/book/${novelId}/outline`">编辑五级大纲</RouterLink>
      <RouterLink :to="`/book/${novelId}/review`">打开审稿台</RouterLink>
      <RouterLink :to="`/book/${novelId}/worldline`">从任意章重生成</RouterLink>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { ClipboardOutline, FlashOutline } from '@vicons/ionicons5'
import { generationApi, getGenerationRunOrNull, type GenerationRun, type RunMode } from '@/api/generation'
import { getGenerationPresentation } from '@/domain/generationPresentation'

const props = defineProps<{ novelId: string; targetChapters?: number }>()
const emit = defineEmits<{ 'status-change': [run: GenerationRun | null] }>()
const run = ref<GenerationRun | null>(null)
const mode = ref<RunMode>('continuous')
const targetChapters = ref(Math.max(1, props.targetChapters || 1))
const starting = ref(false)
const stopping = ref(false)
const error = ref('')
let timer: number | null = null
const presentation = computed(() => getGenerationPresentation(run.value))

async function refresh() {
  error.value = ''
  try {
    run.value = await getGenerationRunOrNull(props.novelId)
    if (run.value) {
      mode.value = run.value.run_mode
      targetChapters.value = Math.max(run.value.target_chapters || 1, 1)
    }
  } catch (cause) {
    run.value = null
    error.value = cause instanceof Error ? cause.message : '读取候选自动驾驶状态失败'
  } finally { emit('status-change', run.value) }
}
async function start() {
  starting.value = true
  error.value = ''
  try {
    run.value = await generationApi.start(props.novelId, mode.value, targetChapters.value)
    // Keep the browser responsive while the server persists authority states.
    const advance = mode.value === 'continuous'
      ? generationApi.runContinuous(props.novelId)
      : generationApi.generateNext(props.novelId)
    void advance.catch(cause => {
      error.value = cause instanceof Error ? cause.message : '启动候选章节失败'
    }).finally(refresh)
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '启动候选自动驾驶失败'
  } finally { starting.value = false; emit('status-change', run.value) }
}
async function stop() {
  stopping.value = true
  error.value = ''
  try { run.value = await generationApi.stop(props.novelId) }
  catch (cause) { error.value = cause instanceof Error ? cause.message : '停止候选自动驾驶失败' }
  finally { stopping.value = false; emit('status-change', run.value) }
}
watch(() => props.targetChapters, value => { if (!run.value && value) targetChapters.value = Math.max(1, value) })
onMounted(() => { void refresh(); timer = window.setInterval(() => void refresh(), 2500) })
onUnmounted(() => { if (timer !== null) window.clearInterval(timer) })
</script>

<style scoped>
.generation-launcher { display: grid; gap: 12px; padding: 14px 16px; border: 1px solid var(--app-border); border-radius: var(--app-radius-md); background: var(--app-surface-subtle); }.generation-launcher__head { display: flex; justify-content: space-between; gap: 12px; align-items: flex-start; }.generation-launcher__eyebrow { display: block; margin-bottom: 2px; color: var(--color-brand); font-size: 10px; font-weight: 700; letter-spacing: .06em; }.generation-launcher h2 { margin: 0; font-size: 15px; }.generation-launcher__status { padding: 4px 7px; border: 1px solid var(--app-border); border-radius: 999px; color: var(--app-text-secondary); font-size: 11px; white-space: nowrap; }.generation-launcher__status.is-brand { color: var(--color-brand); }.generation-launcher__status.is-warning { color: var(--color-warning); }.generation-launcher__status.is-error { color: var(--color-danger); }.generation-launcher__description { margin: 0; color: var(--app-text-secondary); font-size: 12px; line-height: 1.55; }.generation-launcher__modes { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }.generation-mode { display: grid; gap: 5px; padding: 10px; color: var(--app-text-secondary); text-align: left; border: 1px solid var(--app-border); border-radius: var(--app-radius-sm); background: var(--app-surface); cursor: pointer; }.generation-mode strong { color: var(--app-text-primary); font-size: 12px; }.generation-mode span { font-size: 11px; line-height: 1.45; }.generation-mode.is-selected { border-color: var(--color-brand); box-shadow: inset 0 0 0 1px var(--color-brand); }.generation-launcher__controls { display: flex; align-items: center; gap: 8px; }.generation-launcher__controls :deep(.n-input-number) { width: 112px; }.generation-launcher__links { display: flex; flex-wrap: wrap; gap: 12px; }.generation-launcher__links a { color: var(--color-brand); font-size: 12px; text-decoration: none; }.generation-launcher__links a:hover { text-decoration: underline; }
@media (max-width: 560px) { .generation-launcher__modes { grid-template-columns: 1fr; }.generation-launcher__controls { align-items: stretch; flex-wrap: wrap; }.generation-launcher__controls :deep(.n-input-number) { flex: 1; }.generation-launcher__controls :deep(.n-button) { flex: 1; } }
</style>
