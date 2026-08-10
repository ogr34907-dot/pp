<template>
  <section
    class="async-task-status"
    :class="`async-task-status--${status}`"
    :role="liveRole"
    :aria-live="liveMode"
    :aria-busy="status === 'running' ? 'true' : 'false'"
  >
    <div class="async-task-status__marker" aria-hidden="true">
      <n-icon size="20"><component :is="statusIcon" /></n-icon>
    </div>

    <div class="async-task-status__body">
      <div class="async-task-status__heading">
        <div class="async-task-status__title-block">
          <span class="async-task-status__label">{{ statusLabel }}</span>
          <strong>{{ stage }}</strong>
        </div>
        <span v-if="hasProgress" class="async-task-status__counter">
          {{ normalizedCurrent }} / {{ normalizedTotal }}
        </span>
      </div>

      <p class="async-task-status__message">{{ message }}</p>

      <n-progress
        v-if="hasProgress"
        class="async-task-status__progress"
        type="line"
        :percentage="percentage"
        :status="status === 'failed' ? 'error' : 'default'"
        :show-indicator="false"
        :height="6"
        :border-radius="3"
        :aria-label="`已处理 ${normalizedCurrent}，共 ${normalizedTotal}`"
      />

      <div v-if="$slots.meta" class="async-task-status__meta">
        <slot name="meta" />
      </div>
    </div>

    <div v-if="recoveryLabel || $slots.actions" class="async-task-status__actions">
      <n-button
        v-if="recoveryLabel"
        size="small"
        :type="status === 'failed' || status === 'paused' ? 'warning' : 'primary'"
        :secondary="status !== 'running'"
        :loading="recoveryLoading"
        :disabled="recoveryDisabled"
        @click="emitRecovery"
      >
        {{ recoveryLabel }}
      </n-button>
      <slot name="actions" />
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { NButton, NIcon, NProgress } from 'naive-ui'
import {
  AlertCircleOutline,
  CheckmarkCircleOutline,
  PauseCircleOutline,
  SyncCircleOutline,
  TimeOutline,
} from '@vicons/ionicons5'

export type AsyncTaskStatusKind = 'waiting' | 'running' | 'paused' | 'failed' | 'completed'
export type AsyncTaskRecoveryIntent = 'retry' | 'resume' | 'resync-all'

const props = withDefaults(defineProps<{
  status: AsyncTaskStatusKind
  stage: string
  message: string
  current?: number
  total?: number
  recoveryLabel?: string
  recoveryIntent?: AsyncTaskRecoveryIntent
  recoveryDisabled?: boolean
  recoveryLoading?: boolean
}>(), {
  current: undefined,
  total: undefined,
  recoveryLabel: '',
  recoveryIntent: 'retry',
  recoveryDisabled: false,
  recoveryLoading: false,
})

const emit = defineEmits<{
  retry: []
  resume: []
  'resync-all': []
}>()

const normalizedCurrent = computed(() => Math.max(0, Number(props.current || 0)))
const normalizedTotal = computed(() => Math.max(0, Number(props.total || 0)))
const hasProgress = computed(() => props.total != null && normalizedTotal.value > 0)
const percentage = computed(() => hasProgress.value
  ? Math.min(100, Math.round((normalizedCurrent.value / normalizedTotal.value) * 100))
  : 0)
const liveRole = computed(() => ['paused', 'failed'].includes(props.status) ? 'alert' : 'status')
const liveMode = computed(() => ['paused', 'failed'].includes(props.status) ? 'assertive' : 'polite')
const statusLabel = computed(() => ({
  waiting: '等待',
  running: '进行中',
  paused: '等待确认',
  failed: '需要处理',
  completed: '已完成',
})[props.status])
const statusIcon = computed(() => ({
  waiting: TimeOutline,
  running: SyncCircleOutline,
  paused: PauseCircleOutline,
  failed: AlertCircleOutline,
  completed: CheckmarkCircleOutline,
})[props.status])

function emitRecovery() {
  if (props.recoveryIntent === 'resume') {
    emit('resume')
  } else if (props.recoveryIntent === 'resync-all') {
    emit('resync-all')
  } else {
    emit('retry')
  }
}
</script>

<style scoped>
.async-task-status {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr) auto;
  align-items: start;
  gap: var(--plotpilot-space-3, 12px);
  padding: var(--plotpilot-space-3, 12px) var(--plotpilot-space-4, 16px);
  color: var(--app-text-primary);
  background: var(--app-surface-subtle);
  border: 1px solid var(--app-border);
  border-inline-start: 3px solid var(--app-border-strong);
  border-radius: var(--app-radius-md);
}

.async-task-status--running { border-inline-start-color: var(--color-brand); }
.async-task-status--paused { border-inline-start-color: var(--color-warning); }
.async-task-status--failed { border-inline-start-color: var(--color-error); }
.async-task-status--completed { border-inline-start-color: var(--color-success); }

.async-task-status__marker {
  width: 32px;
  height: 32px;
  display: grid;
  place-items: center;
  color: var(--app-text-secondary);
  background: var(--app-surface);
  border: 1px solid var(--app-border);
  border-radius: 50%;
}

.async-task-status--running .async-task-status__marker { color: var(--color-brand); }
.async-task-status--paused .async-task-status__marker { color: var(--color-warning); }
.async-task-status--failed .async-task-status__marker { color: var(--color-error); }
.async-task-status--completed .async-task-status__marker { color: var(--color-success); }

.async-task-status__body { min-width: 0; }
.async-task-status__heading {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: var(--plotpilot-space-3, 12px);
}
.async-task-status__title-block {
  display: flex;
  align-items: baseline;
  gap: var(--plotpilot-space-2, 8px);
  min-width: 0;
}
.async-task-status__label {
  flex: none;
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.08em;
  color: var(--app-text-muted);
}
.async-task-status__title-block strong {
  overflow-wrap: anywhere;
  font-size: 14px;
  font-weight: 650;
}
.async-task-status__counter {
  flex: none;
  color: var(--app-text-secondary);
  font-variant-numeric: tabular-nums;
  font-size: 12px;
}
.async-task-status__message {
  margin: 4px 0 0;
  color: var(--app-text-secondary);
  font-size: 13px;
  line-height: 1.6;
  overflow-wrap: anywhere;
}
.async-task-status__progress { margin-top: 10px; }
.async-task-status__meta {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 14px;
  margin-top: 8px;
  color: var(--app-text-muted);
  font-size: 11px;
  font-variant-numeric: tabular-nums;
}
.async-task-status__actions {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--plotpilot-space-2, 8px);
}

@media (max-width: 720px) {
  .async-task-status { grid-template-columns: auto minmax(0, 1fr); }
  .async-task-status__actions { grid-column: 1 / -1; padding-inline-start: 44px; }
}

@media (max-width: 480px) {
  .async-task-status { grid-template-columns: 1fr; }
  .async-task-status__marker { display: none; }
  .async-task-status__actions { grid-column: auto; padding-inline-start: 0; }
  .async-task-status__actions :deep(.n-button) { width: 100%; min-height: 40px; }
}

@media (prefers-reduced-motion: no-preference) {
  .async-task-status--running .async-task-status__marker :deep(svg) {
    animation: async-task-rotate 1.4s linear infinite;
  }
}

@keyframes async-task-rotate { to { transform: rotate(360deg); } }
</style>
