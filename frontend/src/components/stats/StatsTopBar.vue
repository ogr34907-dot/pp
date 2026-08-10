<template>
  <div class="stats-top-bar" :class="{ loading, error: Boolean(error) }">
    <!-- 左侧：AI 工具统一入口（隐藏原始按钮，通过 ref 触发） -->
    <div class="topbar-left">
      <div class="workbench-context">
        <span class="workbench-context__eyebrow">写作工作台</span>
        <span class="workbench-context__line">
          <strong :title="contextTitle">{{ contextTitle }}</strong>
          <span class="workbench-context__chapter" :title="chapterLabel">{{ chapterLabel }}</span>
        </span>
      </div>

      <!-- 隐藏的原始组件，仅用于保留其 drawer/modal 功能 -->
      <div class="ai-hidden-entries" aria-hidden="true">
        <GlobalLLMEntryButton ref="llmRef" appearance="topbar" />
        <PromptPlazaEntryButton ref="plazaRef" appearance="topbar" />
      </div>

      <!-- 可见的统一触发按钮 -->
      <n-dropdown
        trigger="click"
        placement="bottom-start"
        :options="aiToolsOptions"
        @select="handleAiToolSelect"
      >
        <button type="button" class="ai-tools-trigger" aria-label="打开 AI 工具菜单">
          <n-icon size="17"><SparklesOutline /></n-icon>
          <span class="ai-tools-label">AI 工具</span>
          <n-icon size="13"><ChevronDownOutline /></n-icon>
        </button>
      </n-dropdown>
    </div>

    <!-- 中间：统计数据 -->
    <div class="topbar-center">
      <n-spin v-if="loading" size="small" aria-label="加载作品统计" />
      <div v-else-if="error" class="topbar-error" role="status">
        <span title="作品统计暂不可用">统计暂不可用</span>
        <n-button size="tiny" text type="primary" @click="retryLoad">重试</n-button>
      </div>
      <template v-else>
        <div
          v-for="stat in stats"
          :key="stat.key"
          class="stat-item"
          role="group"
          :aria-label="stat.label"
        >
          <n-tooltip :show-arrow="false">
            <template #trigger>
              <div class="stat-content">
                <span class="stat-label">{{ stat.label }}</span>
                <span class="stat-value">{{ stat.value }}</span>
              </div>
            </template>
            <span>{{ stat.tooltip }}</span>
          </n-tooltip>
        </div>
      </template>
    </div>

    <!-- 右侧：操作按钮 -->
    <div class="top-bar-actions">
      <!-- 导出按钮 -->
      <n-dropdown 
        trigger="click" 
        placement="bottom-end"
        :options="exportOptions"
        @select="handleExport"
      >
        <button type="button" class="action-trigger" aria-label="导出作品">
          <n-icon size="18"><DownloadOutline /></n-icon>
        </button>
      </n-dropdown>

      <button
        type="button"
        class="action-trigger"
        :aria-pressed="focusMode"
        :aria-label="focusMode ? '退出聚焦写作' : '进入聚焦写作'"
        @click="$emit('toggle-focus')"
      >
        <n-icon size="18"><component :is="focusMode ? ContractOutline : ExpandOutline" /></n-icon>
      </button>

      <!-- 设置按钮 -->
      <button type="button" class="settings-trigger" @click="$emit('open-settings')" aria-label="打开设置">
        <n-icon size="18"><SettingsOutline /></n-icon>
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, h, onMounted, ref, type Component } from 'vue'
import { NTooltip, NSpin, NDropdown, NButton, NIcon, useMessage } from 'naive-ui'
import { useStatsStore } from '@/stores/statsStore'
import { novelApi } from '@/api/novel'
import GlobalLLMEntryButton from '@/components/global/GlobalLLMEntryButton.vue'
import PromptPlazaEntryButton from '@/components/global/PromptPlazaEntryButton.vue'
import {
  BookOutline,
  ChevronDownOutline,
  CloudOutline,
  ContractOutline,
  DocumentTextOutline,
  DownloadOutline,
  ExpandOutline,
  SettingsOutline,
  SparklesOutline,
} from '@vicons/ionicons5'

const props = defineProps<{
  slug: string
  contextTitle?: string
  chapterLabel?: string
  focusMode?: boolean
}>()

defineEmits<{
  'open-settings': []
  'toggle-focus': []
}>()

const message = useMessage()

// AI 工具组件引用（用于以编程方式触发各组件内部按钮）
const llmRef = ref<{ $el: HTMLElement } | null>(null)
const plazaRef = ref<{ $el: HTMLElement } | null>(null)

const renderIcon = (icon: Component) => () => h(NIcon, null, { default: () => h(icon) })

const aiToolsOptions = [
  { label: 'AI 控制台', key: 'llm', icon: renderIcon(SparklesOutline) },
  { label: '提示词广场', key: 'plaza', icon: renderIcon(BookOutline) },
]

function handleAiToolSelect(key: string) {
  if (key === 'llm') {
    llmRef.value?.$el?.querySelector('button')?.click()
  } else if (key === 'plaza') {
    plazaRef.value?.$el?.querySelector('button')?.click()
  }
}

// 导出选项
const exportOptions = [
  { label: 'EPUB（电子书）', key: 'epub', icon: renderIcon(CloudOutline) },
  { label: 'PDF（打印）', key: 'pdf', icon: renderIcon(DocumentTextOutline) },
  { label: 'DOCX（Word）', key: 'docx', icon: renderIcon(DocumentTextOutline) },
  { label: 'Markdown', key: 'markdown', icon: renderIcon(DocumentTextOutline) }
]

async function handleExport(format: string) {
  try {
    message.info(`开始导出为 ${format} 格式...`)
    const blob = await novelApi.exportNovel(props.slug, format)
    
    // 创建下载链接
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `novel-${props.slug}.${format}`
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    URL.revokeObjectURL(url)
    
    message.success(`导出 ${format} 格式成功！`)
  } catch (error) {
    console.error('导出失败:', error)
    message.error('导出失败，请稍后重试')
  }
}

const statsStore = useStatsStore()

// Constants
const DECIMAL_PRECISION = 1
const MS_PER_DAY = 1000 * 60 * 60 * 24
const DAYS_THRESHOLD = 7

// State
const loading = ref(false)
const error = ref<string | null>(null)

// Fix: Remove .value before function call
const bookStats = computed(() => statsStore.getBookStats(props.slug))

const stats = computed(() => {
  if (!bookStats.value) return []

  const s = bookStats.value

  const totalWords = Number(s.total_words ?? 0)
  const rate = Number(s.completion_rate ?? 0)
  const avgWords = Number(s.avg_chapter_words ?? 0)
  const done = Number(s.completed_chapters ?? 0)
  const total = Number(s.total_chapters ?? 0)

  const formattedWords = totalWords.toLocaleString()
  const formattedCompletionRate = rate.toFixed(DECIMAL_PRECISION)
  const formattedAvgWords = avgWords.toLocaleString()

  return [
    {
      key: 'words',
      label: '总字数',
      value: formattedWords,
      tooltip: `当前书籍共 ${formattedWords} 字`
    },
    {
      key: 'chapters',
      label: '完成章节',
      value: `${done}/${total}`,
      tooltip: `已完成 ${done} 章，共 ${total} 章`
    },
    {
      key: 'completion',
      label: '完成率',
      value: `${formattedCompletionRate}%`,
      tooltip: `项目完成度：${formattedCompletionRate}%`
    },
    {
      key: 'avg',
      label: '平均字数',
      value: formattedAvgWords,
      tooltip: `每章平均 ${formattedAvgWords} 字`
    },
    {
      key: 'updated',
      label: '最后更新',
      value: formatDate(s.last_updated),
      tooltip: `最后更新时间：${s.last_updated}`
    }
  ]
})

function formatStatsError(err: unknown): string {
  if (err && typeof err === 'object' && 'response' in err) {
    const data = (err as { response?: { data?: { detail?: unknown } } }).response?.data
    const d = data?.detail
    if (typeof d === 'string') return d
    if (Array.isArray(d)) {
      return d
        .map((x: { msg?: string }) => (typeof x?.msg === 'string' ? x.msg : JSON.stringify(x)))
        .join('; ')
    }
  }
  if (err instanceof Error) return err.message
  return String(err)
}

function formatDate(dateStr: string | undefined): string {
  if (!dateStr) return '—'
  try {
    const date = new Date(dateStr)
    const now = new Date()
    const diffMs = now.getTime() - date.getTime()
    const diffDays = Math.floor(diffMs / MS_PER_DAY)

    if (diffDays === 0) {
      return '今天'
    } else if (diffDays === 1) {
      return '昨天'
    } else if (diffDays < DAYS_THRESHOLD) {
      return `${diffDays}天前`
    } else {
      return date.toLocaleDateString('zh-CN', { month: 'short', day: 'numeric' })
    }
  } catch {
    return dateStr
  }
}

async function loadStats() {
  loading.value = true
  error.value = null
  try {
    await statsStore.loadBookStats(props.slug)
  } catch (err) {
    console.error('Failed to load book stats:', err)
    error.value = `加载统计数据失败：${formatStatsError(err)}`
  } finally {
    loading.value = false
  }
}

async function retryLoad() {
  await loadStats()
}

onMounted(loadStats)
</script>

<style scoped>
/* ═══════════════════════════════════════════════════
   StatsTopBar — 与 AI 控制台一体化的顶部导航栏
   使用 CSS 变量，自动适配亮/暗主题
   ═══════════════════════════════════════════════════ */
.stats-top-bar {
  height: var(--plotpilot-topbar-height);
  background: var(--app-surface);
  display: flex;
  flex-direction: row;
  flex-wrap: nowrap;
  align-items: center;
  justify-content: space-between;
  padding: 0 var(--plotpilot-topbar-padding-x);
  color: var(--app-text-primary);
  position: relative;
  gap: var(--plotpilot-topbar-inner-gap);
  min-width: 0;
  /* 横向不允许出现滚动条：内容若溢出则靠中间 stat 区自然收窄 */
  overflow: hidden;
  border-bottom: 1px solid var(--app-border);
  box-shadow: var(--app-shadow-sm);
}

/* 左侧：AI 控制台入口 */
.topbar-left {
  flex-shrink: 0;
  z-index: 2;
  display: flex;
  flex-direction: row;
  flex-wrap: nowrap;
  align-items: center;
  gap: var(--plotpilot-topbar-inner-gap);
}

.workbench-context {
  min-width: 0;
  max-width: min(30vw, 360px);
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding-inline-end: var(--plotpilot-space-3);
  border-inline-end: 1px solid var(--app-divider);
}

.workbench-context__eyebrow {
  color: var(--app-text-muted);
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.08em;
}

.workbench-context__line {
  display: flex;
  align-items: baseline;
  gap: var(--plotpilot-space-2);
  min-width: 0;
}

.workbench-context__line strong,
.workbench-context__chapter {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.workbench-context__line strong {
  color: var(--app-text-primary);
  font-family: var(--app-font-editorial);
  font-size: 14px;
}

.workbench-context__chapter {
  color: var(--app-text-secondary);
  font-size: 12px;
}

/* 隐藏的 AI 入口组件（仅保留功能，不参与布局） */
.ai-hidden-entries {
  position: absolute;
  visibility: hidden;
  pointer-events: none;
  width: 0;
  height: 0;
  overflow: hidden;
  top: -9999px;
}

/* 统一 AI 工具触发按钮 */
.ai-tools-trigger {
  display: flex;
  align-items: center;
  gap: var(--plotpilot-space-2);
  padding: var(--plotpilot-ai-trigger-pad-y) var(--plotpilot-ai-trigger-pad-x);
  border-radius: var(--app-radius-md);
  cursor: pointer;
  background: var(--app-surface-subtle);
  border: 1px solid var(--app-border);
  color: var(--app-text-secondary);
  transition: all var(--app-transition);
  white-space: nowrap;
  box-shadow: none;
  user-select: none;
  font: inherit;
}

.ai-tools-trigger:hover {
  background: var(--color-brand-light);
  border-color: var(--color-brand-border);
  color: var(--color-brand);
}

.ai-tools-label {
  font-size: 13px;
  font-weight: 600;
  letter-spacing: 0.01em;
}

/* 中间：统计数据 */
.topbar-center {
  flex: 1;
  display: flex;
  flex-direction: row;
  flex-wrap: nowrap;
  align-items: center;
  justify-content: center;
  gap: 4px;
  min-width: 0;
  z-index: 1;
  overflow: hidden;
}

.topbar-error {
  display: flex;
  align-items: center;
  gap: var(--plotpilot-space-2);
  color: var(--app-text-muted);
  font-size: 12px;
}

.stat-item {
  flex: 0 1 auto;
  text-align: center;
  cursor: help;
  padding: 4px 10px;
  border-radius: var(--app-radius-sm);
  transition: background 0.2s ease;
}

.stat-item:hover {
  background: var(--app-surface-subtle);
}

.stat-content {
  display: flex;
  flex-direction: column;
  gap: 2px;
  align-items: center;
}

.stat-label {
  font-size: 12px;
  opacity: 1;
  font-weight: 600;
  letter-spacing: 0.03em;
  white-space: nowrap;
  color: var(--app-text-secondary);
}

.stat-value {
  font-size: var(--plotpilot-topbar-stat-value-size);
  font-weight: 800;
  letter-spacing: -0.02em;
  line-height: 1.2;
  color: var(--app-text-primary);
  text-shadow: none;
}

.stat-item:hover .stat-value {
  transform: scale(1.04);
  transition: transform 0.2s ease;
}

/* 右侧：操作按钮 */
.top-bar-actions {
  display: flex;
  flex-direction: row;
  flex-wrap: nowrap;
  gap: var(--plotpilot-space-2);
  flex: 0 0 auto;
  align-items: center;
}

.action-trigger {
  width: var(--plotpilot-topbar-hit-lg);
  height: var(--plotpilot-topbar-hit-lg);
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  opacity: 1;
  transition: all 0.18s ease;
  border-radius: var(--app-radius-sm);
  color: var(--app-text-secondary);
  padding: 0;
  border: 0;
  background: transparent;
}

.action-trigger:hover,
.settings-trigger:hover {
  background: var(--color-brand-light);
  color: var(--color-brand);
}

/* 右侧：设置触发器 */
.settings-trigger {
  flex-shrink: 0;
  width: var(--plotpilot-topbar-hit-md);
  height: var(--plotpilot-topbar-hit-md);
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  opacity: 1;
  transition: all 0.18s ease;
  border-radius: var(--app-radius-sm);
  color: var(--app-text-secondary);
  padding: 0;
  border: 0;
  background: transparent;
}

.dropdown-item-icon {
  margin-right: 8px;
  font-size: 16px;
}

/* Accessibility: Focus styles */
.stat-item:focus-within {
  outline: 2px solid var(--color-focus);
  outline-offset: 4px;
  border-radius: 4px;
}

.settings-trigger:focus-visible,
.action-trigger:focus-visible,
.ai-tools-trigger:focus-visible {
  outline: 2px solid var(--color-focus);
  outline-offset: 2px;
}

/* Responsive design — 全程单行横向，窄屏可横向滚动 */
@media (max-width: 900px) {
  .stats-top-bar {
    flex-wrap: nowrap;
    padding: var(--plotpilot-space-3) var(--plotpilot-topbar-padding-x);
    gap: var(--plotpilot-topbar-inner-gap);
  }

  .topbar-left {
    flex-shrink: 0;
  }

  .topbar-left :deep(.global-llm-main.variant-topbar),
  .topbar-left :deep(.plaza-main.variant-topbar) {
    min-height: 42px;
    padding: 6px 10px;
  }

  .topbar-center {
    justify-content: flex-end;
    flex: 1 1 auto;
  }

  .stat-value {
    font-size: clamp(13px, 0.9rem + 0.2vw, 15px);
  }

  .settings-trigger {
    position: static;
    transform: none;
  }

  .workbench-context { max-width: 240px; }
}

@media (max-width: 480px) {
  .workbench-context__chapter,
  .ai-tools-label,
  .topbar-center { display: none; }

  .workbench-context { max-width: 42vw; }

  .stat-item {
    flex: 0 0 33%;
  }

  .stat-value {
    font-size: 14px;
  }

  .stat-label {
    font-size: 12px;
  }
}

@media (prefers-reduced-motion: reduce) {
  .ai-tools-trigger,
  .stat-item,
  .action-trigger,
  .settings-trigger { transition: none; }
}
</style>
