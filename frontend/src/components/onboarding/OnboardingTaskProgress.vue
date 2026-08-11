<template>
  <section
    class="onboarding-task-progress"
    role="status"
    aria-live="polite"
    :aria-busy="isActive ? 'true' : 'false'"
  >
    <div class="onboarding-task-progress__summary">
      <div>
        <p class="onboarding-task-progress__eyebrow">生成状态</p>
        <h4>{{ presentation.message }}</h4>
        <p class="onboarding-task-progress__detail">{{ presentation.detail }}</p>
      </div>
      <span class="onboarding-task-progress__elapsed">{{ elapsedLabel }}</span>
    </div>

    <ol class="onboarding-task-progress__milestones" aria-label="生成生命周期">
      <li
        v-for="milestone in milestones"
        :key="milestone.key"
        :class="`onboarding-task-progress__milestone--${milestone.state}`"
      >
        <span class="onboarding-task-progress__marker" aria-hidden="true" />
        <span>{{ milestone.label }}</span>
      </li>
    </ol>

    <div class="onboarding-task-progress__stream">
      <div class="onboarding-task-progress__stream-header">
        <span>实时生成片段</span>
        <span class="onboarding-task-progress__stream-note">来自服务端已接收输出</span>
      </div>
      <pre v-if="streamPreview" aria-live="off">{{ streamPreview }}<span class="streaming-cursor" aria-hidden="true">▎</span></pre>
      <p v-else class="onboarding-task-progress__stream-waiting">等待首段输出</p>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import {
  clipInvocationStreamPreview,
  formatInvocationElapsedSeconds,
  getOnboardingTaskPresentation,
  type OnboardingInvocationPhase,
  type OnboardingInvocationTask,
} from '../../onboarding/onboardingInvocationProgress'

const props = withDefaults(defineProps<{
  task: OnboardingInvocationTask
  phase: OnboardingInvocationPhase
  elapsedSeconds?: number
  streamContent?: string
}>(), {
  elapsedSeconds: 0,
  streamContent: '',
})

const presentation = computed(() => getOnboardingTaskPresentation(props.task, props.phase))
const elapsedLabel = computed(() => formatInvocationElapsedSeconds(props.elapsedSeconds))
const streamPreview = computed(() => clipInvocationStreamPreview(props.streamContent))
const isActive = computed(() => !presentation.value.isTerminal)

const phaseIndex = computed(() => ({
  creating: 0,
  generating: 1,
  validating: 2,
  committing: 3,
  completed: 4,
  failed: -1,
})[props.phase])

const milestones = computed(() => [
  { key: 'creating', label: '准备任务' },
  { key: 'generating', label: '模型生成' },
  { key: 'validating', label: '校验结果' },
  { key: 'committing', label: '写入设定' },
].map((milestone, index) => ({
  ...milestone,
  state: props.phase === 'failed'
    ? (index === 0 ? 'failed' : 'pending')
    : phaseIndex.value > index || props.phase === 'completed'
      ? 'done'
      : phaseIndex.value === index ? 'active' : 'pending',
})))
</script>

<style scoped>
.onboarding-task-progress {
  display: grid;
  gap: 14px;
  margin: 16px 0;
  padding: 16px;
  color: var(--app-text-primary);
  background: var(--app-surface-subtle);
  border: 1px solid var(--app-border);
  border-inline-start: 3px solid var(--color-brand);
  border-radius: var(--app-radius-md);
}

.onboarding-task-progress__summary {
  display: flex;
  justify-content: space-between;
  gap: 16px;
  align-items: start;
}

.onboarding-task-progress__eyebrow {
  margin: 0 0 4px;
  color: var(--app-text-muted);
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.08em;
}

.onboarding-task-progress h4 {
  margin: 0;
  font-size: 16px;
  line-height: 1.4;
}

.onboarding-task-progress__detail,
.onboarding-task-progress__stream-waiting {
  margin: 4px 0 0;
  color: var(--app-text-secondary);
  font-size: 13px;
  line-height: 1.6;
}

.onboarding-task-progress__elapsed {
  flex: none;
  color: var(--app-text-muted);
  font-size: 12px;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

.onboarding-task-progress__milestones {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 8px;
  padding: 0;
  margin: 0;
  list-style: none;
}

.onboarding-task-progress__milestones li {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
  color: var(--app-text-muted);
  font-size: 12px;
  white-space: nowrap;
}

.onboarding-task-progress__marker {
  width: 8px;
  height: 8px;
  flex: none;
  border: 1px solid var(--app-border-strong);
  border-radius: 50%;
}

.onboarding-task-progress__milestone--done,
.onboarding-task-progress__milestone--active { color: var(--app-text-primary); }
.onboarding-task-progress__milestone--done .onboarding-task-progress__marker { background: var(--color-success); border-color: var(--color-success); }
.onboarding-task-progress__milestone--active .onboarding-task-progress__marker { background: var(--color-brand); border-color: var(--color-brand); }
.onboarding-task-progress__milestone--failed { color: var(--color-danger); }
.onboarding-task-progress__milestone--failed .onboarding-task-progress__marker { background: var(--color-danger); border-color: var(--color-danger); }

.onboarding-task-progress__stream {
  overflow: hidden;
  background: var(--app-surface);
  border: 1px solid var(--app-divider);
  border-radius: var(--app-radius-sm);
}

.onboarding-task-progress__stream-header {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  padding: 8px 10px;
  color: var(--app-text-secondary);
  background: var(--app-surface-subtle);
  border-bottom: 1px solid var(--app-divider);
  font-size: 12px;
  font-weight: 650;
}

.onboarding-task-progress__stream-note {
  color: var(--app-text-muted);
  font-size: 11px;
  font-weight: 400;
}

.onboarding-task-progress pre {
  max-height: 180px;
  padding: 10px;
  margin: 0;
  overflow: auto;
  color: var(--app-text-primary);
  font-family: var(--app-font-mono, ui-monospace, SFMono-Regular, Consolas, monospace);
  font-size: 12px;
  line-height: 1.65;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.onboarding-task-progress__stream-waiting { padding: 10px; margin: 0; }
.streaming-cursor { color: var(--color-brand); }

@media (max-width: 620px) {
  .onboarding-task-progress__summary { display: grid; }
  .onboarding-task-progress__milestones { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .onboarding-task-progress__stream-header { display: grid; gap: 2px; }
}

@media (prefers-reduced-motion: no-preference) {
  .onboarding-task-progress__milestone--active .onboarding-task-progress__marker,
  .streaming-cursor { animation: onboarding-stream-pulse 1.1s ease-in-out infinite; }
}

@keyframes onboarding-stream-pulse {
  50% { opacity: 0.38; }
}
</style>
