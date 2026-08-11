<template>
  <div
    class="workbench"
    :class="{
      'is-focus-mode': focusMode,
      'is-compact-shell': compactShell,
      'compact-pane--structure': compactShell && compactPane === 'structure',
      'compact-pane--writing': compactShell && compactPane === 'writing',
      'compact-pane--inspector': compactShell && compactPane === 'inspector',
    }"
  >
    <StatsTopBar
      :slug="slug"
      :context-title="bookTitle || slug"
      :chapter-label="currentChapter ? `第 ${currentChapter.number} 章 · ${currentChapter.title || '未命名章节'}` : '尚未选择章节'"
      :focus-mode="focusMode"
      @toggle-focus="focusMode = !focusMode"
      @open-settings="appSettingsShell.open()"
    />

    <nav v-if="compactShell && !focusMode" class="workbench-compact-nav" aria-label="工作台面板">
      <button
        v-for="pane in compactPaneOptions"
        :key="pane.value"
        type="button"
        class="workbench-compact-nav__button"
        :class="{ 'is-active': compactPane === pane.value }"
        :aria-pressed="compactPane === pane.value"
        @click="selectCompactPane(pane.value)"
      >
        <n-icon :component="pane.icon" :size="18" aria-hidden="true" />
        <span>{{ pane.label }}</span>
      </button>
    </nav>

    <n-spin :show="pageLoading" class="workbench-spin" description="加载工作台…">
      <div class="workbench-inner">
        <n-split
          class="workbench-primary-split"
          direction="horizontal"
          :min="WORKBENCH_SPLIT.sidebarMin"
          :max="WORKBENCH_SPLIT.sidebarMax"
          :default-size="WORKBENCH_SPLIT.sidebarDefault"
        >
          <template #1>
            <ChapterList
              ref="chapterListRef"
              :slug="slug"
              :chapters="chapters"
              :current-chapter-id="currentChapterId"
              :generation-prefs="generationPrefs"
              :writing-chapter-number="writingChapterNumber"
              :writing-pipeline-step="writingPipelineStep"
              @select="onSidebarChapterSelect"
              @back="goHome"
              @refresh="handleChapterUpdated"
              @plan-act="handlePlanAct"
            />
          </template>

          <template #2>
            <div class="wb-main-split" :class="{ 'wb-right-collapsed': rightCollapsed }">
              <n-split
                direction="horizontal"
                :min="WORKBENCH_SPLIT.mainMin"
                :max="WORKBENCH_SPLIT.mainMax"
                :default-size="WORKBENCH_SPLIT.mainDefault"
              >
                <template #1>
                  <WorkArea
                    ref="workAreaRef"
                    :slug="slug"
                    :book-title="bookTitle"
                    :chapters="chapters"
                    :current-chapter-id="currentChapterId"
                    :chapter-content="chapterContent"
                    :chapter-loading="chapterLoading"
                    :generation-prefs="generationPrefs"
                    :target-chapters="bookMeta.target_chapters"
                    @chapter-updated="handleChapterUpdated"
                    @select-chapter="handleChapterSelect"
                  />
                </template>

                <template #2>
                  <button
                    v-if="rightCollapsed"
                    type="button"
                    class="wb-right-strip"
                    aria-label="展开右侧检查器"
                    @click="toggleRight"
                  >
                    <n-icon size="17"><ChevronBackOutline /></n-icon>
                  </button>
                  <SettingsPanel
                    v-else
                    :slug="slug"
                    :current-panel="rightPanel"
                    :current-chapter="currentChapter"
                    :generation-prefs="generationPrefs"
                    @update:current-panel="onSettingsPanelChange"
                    @collapse="toggleRight"
                  />
                </template>
              </n-split>
            </div>
          </template>
        </n-split>
      </div>
    </n-spin>

    <!-- 幕→章 AI 规划弹层 -->
    <ActPlanningModal
      v-model:show="showActPlanning"
      :act-id="actPlanningId"
      :act-title="actPlanningTitle"
      @confirmed="handleChapterUpdated"
    />
  </div>
</template>

<script setup lang="ts">
import { onMounted, onUnmounted, computed, ref, watch, defineAsyncComponent, type ComponentPublicInstance } from 'vue'
import { useRoute } from 'vue-router'
import { useMessage } from 'naive-ui'
import { useMediaQuery } from '@vueuse/core'
import { CreateOutline, ListOutline, OptionsOutline } from '@vicons/ionicons5'
import { useDebouncedTask } from '../composables/useDebouncedTask'
import { useWorkbench } from '../composables/useWorkbench'
import { useStatsStore } from '../stores/statsStore'
import { useWorkbenchRefreshStore } from '../stores/workbenchRefreshStore'
import { useAppSettingsShellStore } from '../stores/appSettingsShellStore'
import StatsTopBar from '../components/stats/StatsTopBar.vue'
import ChapterList from '../components/workbench/ChapterList.vue'
import WorkArea from '../components/workbench/WorkArea.vue'
import SettingsPanel from '../components/workbench/SettingsPanel.vue'
import {
  WORKBENCH_CHAPTER_DESK_CHANGE_EVENT,
  WORKBENCH_OPEN_SETTINGS_PANEL_EVENT,
  WORKBENCH_GENERATION_PREFS_UPDATED_EVENT,
  isWorkbenchSettingsPanelName,
} from '../workbench/deskEvents'
import { WORKBENCH_SPLIT } from '../design/layoutDensity'
import { storageKeys } from '@/config/storageKeys'
import { runtimePerformance } from '@/config/performance'
import { readStorageBoolean, writeStorageBoolean } from '@/utils/storage'
import { ChevronBackOutline } from '@vicons/ionicons5'

const ActPlanningModal = defineAsyncComponent(() => import('../components/workbench/ActPlanningModal.vue'))

const route = useRoute()
const message = useMessage()
const statsStore = useStatsStore()
const workbenchRefresh = useWorkbenchRefreshStore()
const appSettingsShell = useAppSettingsShellStore()

const slug = computed(() => String(route.params.slug ?? ''))

const chapterListRef = ref<ComponentPublicInstance<{ refreshStoryTree: () => void }> | null>(null)
const workAreaRef = ref<ComponentPublicInstance<{
  ensureAssistedMode: () => void
  streamingChapterNumber: import('vue').Ref<number | null>
  writingPipelineStep: import('vue').ComputedRef<number | null>
}> | null>(null)

const writingChapterNumber = computed(() => workAreaRef.value?.streamingChapterNumber?.value ?? null)
const writingPipelineStep = computed(() => workAreaRef.value?.writingPipelineStep?.value ?? null)

async function onSidebarChapterSelect(chapterId: number, title = '') {
  await handleChapterSelect(chapterId, title)
  workAreaRef.value?.ensureAssistedMode?.()
}

async function runChapterDeskReload() {
  await loadDesk()
  void statsStore.loadBookStats(slug.value, true).catch(() => {})
  window.dispatchEvent(new CustomEvent('plotpilot:bible-panel:soft-reload'))
  chapterListRef.value?.refreshStoryTree?.()
  workbenchRefresh.bumpAfterChapterDeskChange()
}

/** 合并短时间内的多次「整桌刷新」：全托管状态抖动 / 多源 emit 时只拉一次 API，减轻闪烁与日志刷屏 */
const chapterDeskReload = useDebouncedTask(
  runChapterDeskReload,
  () => runtimePerformance.workbench.deskReloadDebounceMs,
  {
    onError: () => {
      message.error('刷新工作台失败，请检查网络与后端是否已启动')
    },
  },
)

const handleChapterUpdated = () => {
  chapterDeskReload.schedule()
}

function onDeskChangeSignalFromPanels() {
  handleChapterUpdated()
}

function onOpenSettingsPanelFromChild(e: Event) {
  const panel = (e as CustomEvent<{ panel?: string }>).detail?.panel
  if (typeof panel === 'string' && isWorkbenchSettingsPanelName(panel)) {
    rightPanel.value = panel
  }
}

// 幕→章 规划弹层
const showActPlanning = ref(false)
const actPlanningId = ref('')
const actPlanningTitle = ref('')

const handlePlanAct = (actId: string, actTitle: string) => {
  actPlanningId.value = actId
  actPlanningTitle.value = actTitle
  showActPlanning.value = true
}

const rightCollapsed = ref(readStorageBoolean(storageKeys.workbenchRightPanelCollapsed))
const focusMode = ref(false)
type CompactPane = 'structure' | 'writing' | 'inspector'
const compactShell = useMediaQuery('(max-width: 900px)')
const compactPane = ref<CompactPane>('writing')
const compactPaneOptions = [
  { value: 'structure' as const, label: '结构', icon: ListOutline },
  { value: 'writing' as const, label: '写作', icon: CreateOutline },
  { value: 'inspector' as const, label: '检查器', icon: OptionsOutline },
]

function selectCompactPane(pane: CompactPane) {
  compactPane.value = pane
  if (pane === 'inspector' && rightCollapsed.value) rightCollapsed.value = false
}

watch(focusMode, (enabled) => {
  if (enabled) compactPane.value = 'writing'
})

function toggleRight() {
  rightCollapsed.value = !rightCollapsed.value
  writeStorageBoolean(storageKeys.workbenchRightPanelCollapsed, rightCollapsed.value)
}

const {
  bookTitle,
  chapters,
  generationPrefs,
  rightPanel,
  pageLoading,
  bookMeta,
  currentJobId,
  currentChapterId,
  chapterContent,
  chapterLoading,
  setRightPanel,
  loadDesk,
  reloadDeskForSlugChange,
  goHome,
  goToChapter,
  handleChapterSelect,
} = useWorkbench({ slug })

const currentChapter = computed(() => {
  if (!currentChapterId.value) return null
  return chapters.value.find(ch => ch.id === currentChapterId.value) || null
})

function onSettingsPanelChange(panel: string) {
  rightPanel.value = panel
}

function parseChapterQuery(q: unknown): number | null {
  if (q == null || q === '') return null
  const raw = Array.isArray(q) ? q[0] : q
  const n = Number(raw)
  return !Number.isNaN(n) && n >= 1 ? n : null
}

async function syncChapterFromRoute() {
  const n = parseChapterQuery(route.query.chapter)
  if (n != null) {
    await goToChapter(n)
  }
}

function onGenerationPrefsUpdated() {
  void loadDesk()
  chapterListRef.value?.refreshStoryTree?.()
}

onMounted(async () => {
  window.addEventListener(WORKBENCH_CHAPTER_DESK_CHANGE_EVENT, onDeskChangeSignalFromPanels)
  window.addEventListener(WORKBENCH_OPEN_SETTINGS_PANEL_EVENT, onOpenSettingsPanelFromChild)
  window.addEventListener(WORKBENCH_GENERATION_PREFS_UPDATED_EVENT, onGenerationPrefsUpdated)
  try {
    await loadDesk()
    await syncChapterFromRoute()
  } catch {
    message.error('加载失败，请检查网络与后端是否已启动')
    bookTitle.value = slug.value
  } finally {
    pageLoading.value = false
  }
})

onUnmounted(() => {
  window.removeEventListener(WORKBENCH_CHAPTER_DESK_CHANGE_EVENT, onDeskChangeSignalFromPanels)
  window.removeEventListener(WORKBENCH_OPEN_SETTINGS_PANEL_EVENT, onOpenSettingsPanelFromChild)
  window.removeEventListener(WORKBENCH_GENERATION_PREFS_UPDATED_EVENT, onGenerationPrefsUpdated)
  chapterDeskReload.cancel()
})

watch(
  () => route.query.chapter,
  () => {
    void syncChapterFromRoute()
  }
)

watch(
  slug,
  async (next, prev) => {
    if (!next || prev === next) return
    try {
      await reloadDeskForSlugChange()
      await syncChapterFromRoute()
      void statsStore.loadBookStats(next, true).catch(() => {})
      chapterListRef.value?.refreshStoryTree?.()
      workbenchRefresh.bumpAfterChapterDeskChange()
    } catch {
      message.error('切换作品失败，请检查网络与后端是否已启动')
      bookTitle.value = next
    }
  }
)
</script>

<style scoped>
.workbench {
  height: 100vh;
  min-height: 0;
  max-height: 100vh;
  overflow: hidden;
  background: var(--app-page-bg);
  display: flex;
  flex-direction: column;
}

.workbench-spin {
  flex: 1;
  min-height: 0;
  overflow: hidden;
  display: flex;
  flex-direction: column;
}

.workbench-spin :deep(.n-spin-content) {
  flex: 1;
  min-height: 0;
  height: auto;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.workbench-inner {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  padding: 6px;
}

.workbench-inner :deep(.n-split) {
  flex: 1;
  min-height: 0;
  height: 100%;
}

.workbench-inner :deep(.n-split-pane-1),
.workbench-inner :deep(.n-split-pane-2) {
  min-height: 0;
  overflow: hidden;
}

/* ── Right sidebar collapse ─────────────────────────── */

.wb-main-split {
  height: 100%;
  width: 100%;
  overflow: hidden;
  background: var(--app-surface);
  border: 1px solid var(--app-border);
  border-radius: var(--app-radius-md);
}

.wb-right-collapsed :deep(.n-split-pane-1) {
  flex: 1 1 0 !important;
  width: 0 !important;
  max-width: none !important;
}

.wb-right-collapsed :deep(.n-split-pane-2) {
  flex: 0 0 32px !important;
  width: 32px !important;
  min-width: 0 !important;
  max-width: 32px !important;
  overflow: hidden;
}

.wb-right-collapsed :deep(> .n-split > .n-split__resize-trigger-wrapper) {
  display: none !important;
  pointer-events: none !important;
}

.wb-right-strip {
  height: 100%;
  width: 32px;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  background: var(--app-surface);
  border-left: 1px solid var(--plotpilot-split-border);
  color: var(--app-text-muted);
  font-size: 12px;
  transition: background 0.15s, color 0.15s;
  user-select: none;
  padding: 0;
}

.wb-right-strip:hover {
  background: var(--plotpilot-panel-muted);
  color: var(--app-text-primary);
}

.workbench-compact-nav {
  display: none;
}

.wb-right-strip:focus-visible {
  outline: 2px solid var(--color-focus);
  outline-offset: -3px;
}

.is-focus-mode :deep(.workbench-primary-split > .n-split-pane-1),
.is-focus-mode :deep(.workbench-primary-split > .n-split__resize-trigger-wrapper),
.is-focus-mode .wb-main-split :deep(> .n-split > .n-split-pane-2),
.is-focus-mode .wb-main-split :deep(> .n-split > .n-split__resize-trigger-wrapper) {
  display: none !important;
  pointer-events: none !important;
}

.is-focus-mode :deep(.workbench-primary-split > .n-split-pane-2),
.is-focus-mode .wb-main-split :deep(> .n-split > .n-split-pane-1) {
  width: 100% !important;
  max-width: none !important;
  flex: 1 1 100% !important;
}

@media (max-width: 900px) {
  .workbench-compact-nav {
    flex: 0 0 auto;
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 4px;
    padding: 6px 8px;
    border-bottom: 1px solid var(--app-border);
    background: var(--app-page-bg);
  }

  .workbench-compact-nav__button {
    min-width: 0;
    min-height: 44px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 6px;
    padding: 6px 10px;
    border: 1px solid transparent;
    border-radius: var(--app-radius-md);
    color: var(--app-text-secondary);
    background: transparent;
    font: inherit;
    font-size: 13px;
    font-weight: 600;
    cursor: pointer;
  }

  .workbench-compact-nav__button:hover {
    color: var(--app-text-primary);
    background: var(--app-surface-subtle);
  }

  .workbench-compact-nav__button.is-active {
    color: var(--color-brand);
    background: var(--color-brand-light);
    border-color: var(--color-brand-border);
  }

  .workbench-compact-nav__button:focus-visible {
    outline: 2px solid var(--color-focus);
    outline-offset: 2px;
  }

  .workbench-inner { padding: 0; }
  .wb-main-split { border-radius: 0; border-block: 0; }

  .is-compact-shell :deep(.workbench-primary-split > .n-split__resize-trigger-wrapper),
  .is-compact-shell .wb-main-split :deep(> .n-split > .n-split__resize-trigger-wrapper) {
    display: none !important;
    pointer-events: none !important;
  }

  .is-compact-shell :deep(.workbench-primary-split > .n-split-pane-1),
  .is-compact-shell :deep(.workbench-primary-split > .n-split-pane-2),
  .is-compact-shell .wb-main-split :deep(> .n-split > .n-split-pane-1),
  .is-compact-shell .wb-main-split :deep(> .n-split > .n-split-pane-2) {
    display: none !important;
    pointer-events: none !important;
    width: 0 !important;
    max-width: 0 !important;
    flex: 0 0 0 !important;
  }

  .is-compact-shell.compact-pane--structure :deep(.workbench-primary-split > .n-split-pane-1),
  .is-compact-shell.compact-pane--writing :deep(.workbench-primary-split > .n-split-pane-2),
  .is-compact-shell.compact-pane--inspector :deep(.workbench-primary-split > .n-split-pane-2),
  .is-compact-shell.compact-pane--writing .wb-main-split :deep(> .n-split > .n-split-pane-1),
  .is-compact-shell.compact-pane--inspector .wb-main-split :deep(> .n-split > .n-split-pane-2) {
    display: block !important;
    pointer-events: auto !important;
    width: 100% !important;
    max-width: none !important;
    flex: 1 1 100% !important;
  }
}

@media (prefers-reduced-motion: reduce) {
  .wb-right-strip { transition: none; }
}
</style>
