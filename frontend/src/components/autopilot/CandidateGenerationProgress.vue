<template>
  <section
    class="candidate-progress"
    :class="{ 'is-active': progress.isActive, 'is-error': progress.tone === 'error' }"
    aria-label="候选生成进度"
  >
    <header class="candidate-progress__head">
      <div class="candidate-progress__title-block">
        <span class="candidate-progress__eyebrow">候选生成轨道</span>
        <h2 aria-live="polite">{{ progress.label }}</h2>
        <p>{{ progress.detail }}</p>
      </div>
      <div class="candidate-progress__percent" :class="`is-${progress.tone}`">
        <strong>{{ progress.formalProgress }}%</strong>
        <span>正式书稿</span>
      </div>
    </header>

    <div class="candidate-progress__meta" aria-label="当前生成摘要">
      <span><n-icon :component="DocumentTextOutline" aria-hidden="true" />{{ formalChapterLabel }}</span>
      <span><n-icon :component="CreateOutline" aria-hidden="true" />{{ candidateChapterLabel }}</span>
      <span><n-icon :component="ClipboardOutline" aria-hidden="true" />{{ progress.modeLabel }}</span>
    </div>

    <div
      class="candidate-progress__book-bar"
      role="progressbar"
      aria-label="正式章节完成度"
      :aria-valuemin="0"
      :aria-valuemax="100"
      :aria-valuenow="progress.formalProgress"
    >
      <span :style="{ width: `${progress.formalProgress}%` }" />
    </div>

    <ol class="candidate-progress__steps" aria-label="候选章五阶段流程">
      <li
        v-for="(step, index) in progress.steps"
        :key="step.key"
        class="candidate-progress__step"
        :class="`is-${step.state}`"
        :aria-current="index === progress.activeStep ? 'step' : undefined"
      >
        <span class="candidate-progress__marker" aria-hidden="true">
          <n-icon v-if="step.state === 'complete'" :component="CheckmarkOutline" />
          <n-icon v-else-if="step.state === 'failed'" :component="AlertCircleOutline" />
          <n-icon v-else-if="step.state === 'attention'" :component="PauseCircleOutline" />
          <n-icon v-else :component="stepIcon(step.key)" />
          <span v-if="step.state === 'active'" class="candidate-progress__pulse" />
        </span>
        <span class="candidate-progress__step-copy">
          <strong>{{ step.label }}</strong>
          <small>{{ step.detail }}</small>
        </span>
      </li>
    </ol>
  </section>
</template>

<script setup lang="ts">
import { computed, type Component } from 'vue'
import {
  AlertCircleOutline,
  CheckmarkOutline,
  ClipboardOutline,
  CreateOutline,
  DocumentTextOutline,
  EyeOutline,
  PauseCircleOutline,
  ShieldCheckmarkOutline,
  SyncOutline,
} from '@vicons/ionicons5'
import type { GenerationRun } from '@/api/generation'
import {
  getCandidateGenerationProgress,
  type CandidateGenerationProgressStep,
} from '@/domain/candidateGenerationProgress'

const props = defineProps<{ run: GenerationRun | null }>()

const progress = computed(() => getCandidateGenerationProgress(props.run))
const formalChapterLabel = computed(() => {
  if (!progress.value.targetChapters) return `正式书稿 ${progress.value.formalChapters} 章`
  return `正式书稿 ${progress.value.formalChapters} / ${progress.value.targetChapters} 章`
})
const candidateChapterLabel = computed(() => progress.value.candidateChapter
  ? `当前候选 第 ${progress.value.candidateChapter} 章`
  : '尚未建立候选章')

const stepIcons: Record<CandidateGenerationProgressStep['key'], Component> = {
  draft: CreateOutline,
  audit: ShieldCheckmarkOutline,
  review: EyeOutline,
  commit: DocumentTextOutline,
  sync: SyncOutline,
}

function stepIcon(key: CandidateGenerationProgressStep['key']): Component {
  return stepIcons[key]
}
</script>

<style scoped>
.candidate-progress {
  display: grid;
  gap: 16px;
  width: min(1180px, calc(100% - 32px));
  margin: 0 auto 24px;
  padding: 18px;
  border: 1px solid var(--app-border);
  border-radius: var(--app-radius-md);
  background: var(--app-surface);
  box-shadow: var(--app-shadow-sm);
}

.candidate-progress__head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 18px;
}

.candidate-progress__title-block {
  min-width: 0;
}

.candidate-progress__eyebrow {
  display: block;
  margin-bottom: 4px;
  color: var(--color-brand);
  font-size: 11px;
  font-weight: 700;
  line-height: 1.2;
}

.candidate-progress h2 {
  margin: 0;
  color: var(--app-text-primary);
  font-size: 18px;
  line-height: 1.3;
}

.candidate-progress__title-block p {
  max-width: 720px;
  margin: 5px 0 0;
  color: var(--app-text-secondary);
  font-size: 13px;
  line-height: 1.55;
}

.candidate-progress__percent {
  display: grid;
  flex: 0 0 auto;
  gap: 1px;
  min-width: 76px;
  text-align: right;
}

.candidate-progress__percent strong {
  color: var(--app-text-primary);
  font-size: 22px;
  line-height: 1;
}

.candidate-progress__percent span {
  color: var(--app-text-muted);
  font-size: 11px;
}

.candidate-progress__percent.is-brand strong { color: var(--color-brand); }
.candidate-progress__percent.is-success strong { color: var(--color-success); }
.candidate-progress__percent.is-warning strong { color: var(--color-warning); }
.candidate-progress__percent.is-error strong { color: var(--color-danger); }

.candidate-progress__meta {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 18px;
  color: var(--app-text-secondary);
  font-size: 12px;
}

.candidate-progress__meta span {
  display: inline-flex;
  align-items: center;
  min-width: 0;
  gap: 5px;
}

.candidate-progress__meta :deep(.n-icon) {
  color: var(--app-text-muted);
  font-size: 14px;
}

.candidate-progress__book-bar {
  height: 6px;
  overflow: hidden;
  border-radius: 3px;
  background: var(--app-surface-subtle);
}

.candidate-progress__book-bar > span {
  display: block;
  height: 100%;
  border-radius: inherit;
  background: var(--color-success);
  transition: width 180ms ease;
}

.candidate-progress.is-active .candidate-progress__book-bar > span {
  animation: candidate-book-progress 1800ms ease-in-out infinite;
}

.candidate-progress__steps {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: 0;
  margin: 0;
  padding: 0;
  list-style: none;
}

.candidate-progress__step {
  position: relative;
  display: grid;
  grid-template-columns: 34px minmax(0, 1fr);
  gap: 8px;
  min-width: 0;
  padding: 0 10px;
}

.candidate-progress__step:first-child { padding-inline-start: 0; }
.candidate-progress__step:last-child { padding-inline-end: 0; }

.candidate-progress__step:not(:last-child)::after {
  position: absolute;
  top: 16px;
  left: 34px;
  width: calc(100% - 28px);
  height: 1px;
  content: '';
  background: var(--app-border);
}

.candidate-progress__step.is-complete:not(:last-child)::after {
  background: var(--color-success);
}

.candidate-progress__marker {
  position: relative;
  z-index: 1;
  display: grid;
  width: 34px;
  height: 34px;
  place-items: center;
  border: 1px solid var(--app-border);
  border-radius: 50%;
  color: var(--app-text-muted);
  background: var(--app-surface);
}

.candidate-progress__marker :deep(.n-icon) {
  font-size: 16px;
}

.candidate-progress__step.is-active .candidate-progress__marker {
  border-color: var(--color-brand);
  color: var(--color-brand);
}

.candidate-progress__step.is-complete .candidate-progress__marker {
  border-color: var(--color-success);
  color: var(--color-success);
}

.candidate-progress__step.is-attention .candidate-progress__marker {
  border-color: var(--color-warning);
  color: var(--color-warning);
}

.candidate-progress__step.is-failed .candidate-progress__marker {
  border-color: var(--color-danger);
  color: var(--color-danger);
}

.candidate-progress__pulse {
  position: absolute;
  inset: -5px;
  border: 1px solid var(--color-brand);
  border-radius: 50%;
  animation: candidate-stage-pulse 1600ms ease-out infinite;
}

.candidate-progress__step-copy {
  display: grid;
  gap: 3px;
  min-width: 0;
  padding-top: 2px;
}

.candidate-progress__step-copy strong {
  overflow: hidden;
  color: var(--app-text-primary);
  font-size: 12px;
  line-height: 1.35;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.candidate-progress__step-copy small {
  color: var(--app-text-muted);
  font-size: 11px;
  line-height: 1.45;
}

.candidate-progress__step.is-active .candidate-progress__step-copy strong { color: var(--color-brand); }
.candidate-progress__step.is-attention .candidate-progress__step-copy strong { color: var(--color-warning); }
.candidate-progress__step.is-failed .candidate-progress__step-copy strong { color: var(--color-danger); }
.candidate-progress__step.is-complete .candidate-progress__step-copy strong { color: var(--color-success); }

@keyframes candidate-book-progress {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.72; }
}

@keyframes candidate-stage-pulse {
  0% { opacity: 0.65; transform: scale(0.8); }
  75%, 100% { opacity: 0; transform: scale(1.18); }
}

@media (max-width: 900px) {
  .candidate-progress__steps {
    grid-template-columns: 1fr;
    gap: 12px;
  }

  .candidate-progress__step,
  .candidate-progress__step:first-child,
  .candidate-progress__step:last-child {
    padding-inline: 0;
  }

  .candidate-progress__step:not(:last-child)::after {
    top: 34px;
    left: 16px;
    width: 1px;
    height: calc(100% - 10px);
  }

  .candidate-progress__step-copy small {
    max-width: none;
  }
}

@media (max-width: 560px) {
  .candidate-progress {
    width: calc(100% - 16px);
    margin-bottom: 16px;
    padding: 14px;
  }

  .candidate-progress__head {
    gap: 12px;
  }

  .candidate-progress h2 { font-size: 16px; }
  .candidate-progress__title-block p { font-size: 12px; }
  .candidate-progress__percent strong { font-size: 20px; }
}

@media (prefers-reduced-motion: reduce) {
  .candidate-progress__book-bar > span,
  .candidate-progress.is-active .candidate-progress__book-bar > span,
  .candidate-progress__pulse {
    animation: none;
    transition: none;
  }
}
</style>
