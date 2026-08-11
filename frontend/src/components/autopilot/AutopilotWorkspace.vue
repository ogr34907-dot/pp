<template>
  <div class="ap-workspace">
    <AutopilotShellNav />

    <div class="ap-workspace__body">
      <!-- 候选写作页保持挂载；其它重页面按需挂载，避免隐藏图表/DAG 常驻占用内存。 -->
      <section
        v-show="workspace.activeTab === 'cockpit'"
      class="ap-workspace__pane ap-workspace__pane--cockpit"
      aria-label="候选写作"
    >
      <GenerationModeLauncher
        class="ap-workspace__candidate-launcher"
        :novel-id="novelId"
        :target-chapters="targetChapters"
        @status-change="onCandidateStatusChange"
      />
      <CandidateGenerationProgress :run="candidateRun" />
      </section>

      <section
        v-if="workspace.activeTab === 'governance'"
        class="ap-workspace__pane ap-workspace__pane--governance"
        aria-label="总编辑驾驶舱"
      >
        <NarrativeGovernanceCockpit :novel-id="novelId" />
      </section>

      <section
        v-if="workspace.activeTab === 'dashboard'"
        class="ap-workspace__pane"
        aria-label="仪表盘"
      >
        <AutopilotMetricsDashboard
          ref="metricsRef"
          :novel-id="novelId"
          @desk-refresh="onMetricsDeskRefresh"
        />
      </section>

      <section
        v-if="workspace.activeTab === 'operations'"
        class="ap-workspace__pane ap-workspace__pane--ops"
        aria-label="监控与 DAG"
      >
        <AutopilotOperationsView
          :novel-id="novelId"
          @desk-refresh="onOpsDeskRefresh"
          @chapter-metrics-refresh="onChapterMetricsRefresh"
        />
      </section>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, defineAsyncComponent, ref, toRef, watch, nextTick } from 'vue'
import { useAutopilotWorkspaceStore } from '@/stores/autopilotWorkspaceStore'
import { useDAGSSE } from '@/composables/useDAGSSE'
import AutopilotShellNav from './AutopilotShellNav.vue'
import GenerationModeLauncher from './GenerationModeLauncher.vue'
import CandidateGenerationProgress from './CandidateGenerationProgress.vue'
import type { GenerationRun } from '@/api/generation'

const NarrativeGovernanceCockpit = defineAsyncComponent(() => import('./NarrativeGovernanceCockpit.vue'))
const AutopilotMetricsDashboard = defineAsyncComponent(() => import('./AutopilotMetricsDashboard.vue'))
const AutopilotOperationsView = defineAsyncComponent(() => import('./AutopilotOperationsView.vue'))

const props = defineProps<{
  novelId: string
  targetChapters?: number
}>()

const emit = defineEmits<{
  'desk-refresh': []
  'chapter-metrics-refresh': []
}>()

const workspace = useAutopilotWorkspaceStore()
const metricsRef = ref<{ relayoutTension?: () => void; bumpRefresh?: () => void } | null>(null)
const candidateRun = ref<GenerationRun | null>(null)
const operationsActive = computed(() => workspace.activeTab === 'operations')

/** DAG/日志 SSE 只在监控页打开时连接。 */
useDAGSSE(toRef(props, 'novelId'), operationsActive)

watch(
  () => workspace.activeTab,
  (tab) => {
    if (tab === 'dashboard') {
      void nextTick(() => {
        requestAnimationFrame(() => metricsRef.value?.relayoutTension?.())
      })
    }
  },
)

function onOpsDeskRefresh() {
  emit('desk-refresh')
}

function onMetricsDeskRefresh() {
  emit('desk-refresh')
}

function onChapterMetricsRefresh() {
  metricsRef.value?.bumpRefresh?.()
  emit('chapter-metrics-refresh')
}

function onCandidateStatusChange(run: GenerationRun | null) {
  candidateRun.value = run
}
</script>

<style scoped>
.ap-workspace {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: var(--app-page-bg);
}

.ap-workspace__body {
  flex: 1;
  min-height: 0;
  position: relative;
  overflow: hidden;
  background: var(--app-page-bg);
}

.ap-workspace__pane {
  position: absolute;
  inset: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: var(--app-surface);
}

.ap-workspace__pane--cockpit {
  overflow-y: auto;
  background: var(--app-page-bg);
}

.ap-workspace__pane--governance {
  overflow: hidden;
  background: var(--app-page-bg);
}

.ap-workspace__candidate-launcher {
  flex-shrink: 0;
  width: min(1180px, calc(100% - 32px));
  margin: 16px auto 0;
}

.ap-workspace__pane--ops {
  background: var(--app-surface-subtle);
}

@media (max-width: 720px) {
  .ap-workspace__candidate-launcher {
    width: calc(100% - 16px);
    margin: 8px auto 0;
  }
}
</style>
